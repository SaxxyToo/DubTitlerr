"""API v1: the frozen contract for external consumers (agents, scripts).

The classic ``/api/*`` surface is the web UI's backend and moves with it. Everything here is
additive and versioned: this module is dispatched from ``review_server._route()`` for any path
under ``/api/v1``, and no existing route is renamed, moved or changed.

Auth is the existing RBAC table (``review_auth.ROUTE_ROLES``): reads are ``user``, writes are
``admin``, and ``X-Api-Key`` is an admin credential throughout. Every handler is reached only
after ``review_auth.decide()`` has run, so a v1 path this module does not serve still fails
closed at the role table (an unlisted route needs admin) rather than falling open.

No handler here returns a stored secret. ``GET /api/v1/settings`` reuses
``settings_api.get_settings``'s masked read shape; a notification reports its secrets as
``{"set": true}``; ``GET /api/v1/apikey/info`` reports rotation state and never the key value.

``openapi.json`` is a static file beside this module (``dubtitlerr/review/openapi.json``, shipped
by the same ``COPY dubtitlerr/`` as the package) and is served at ``/api/v1/openapi.json``.
``tests/test_review_api_v1_openapi.py`` asserts the file and the role table cannot drift apart:
a route added or removed without the spec goes red.
"""

import datetime
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from dubtitlerr import __version__
from dubtitlerr.core import common, processing_state
from dubtitlerr.review import review_auth, review_requests
from dubtitlerr.settings import settings_api, settings_notifications, settings_store

API_PREFIX = "/api/v1"
OPENAPI_PATH = Path(__file__).with_name("openapi.json")
STARTED_AT = time.time()

DEFAULT_HISTORY_LIMIT = 25
MAX_HISTORY_LIMIT = 200
# The history index reads two small sidecars per episode over the media mount, so it is built
# once per library-walk lifetime and never on every poll. The cap bounds the one build; a
# library larger than this reports truncated=true rather than silently narrowing the answer.
MAX_HISTORY_EPISODES = 5000

COMMANDS = ("run.request", "system.restart", "system.start", "system.pause", "system.stop", "notification.test")

ROLE_ENTRIES = {
    ("GET", API_PREFIX + "/system/status"): "user",
    ("GET", API_PREFIX + "/health"): "user",
    ("GET", API_PREFIX + "/queue"): "user",
    ("GET", API_PREFIX + "/history"): "user",
    ("GET", API_PREFIX + "/library"): "user",
    ("POST", API_PREFIX + "/command"): "admin",
    ("GET", API_PREFIX + "/notifications"): "user",
    ("POST", API_PREFIX + "/notifications"): "admin",
    ("GET", API_PREFIX + "/notification/*"): "user",
    ("PUT", API_PREFIX + "/notification/*"): "admin",
    ("DELETE", API_PREFIX + "/notification/*"): "admin",
    ("GET", API_PREFIX + "/settings"): "user",
    ("GET", API_PREFIX + "/apikey/info"): "user",
    ("GET", API_PREFIX + "/openapi.json"): "user",
}
review_auth.register_routes(ROLE_ENTRIES)


# --- helpers -----------------------------------------------------------------------------------------------------------


def _env(environ):
    return os.environ if environ is None else environ


def _num(value):
    """A finite float or None (a NaN/inf sidecar value must not reach a response)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _iso(at):
    """Epoch seconds as ``YYYY-MM-DDTHH:MM:SSZ``; None for anything unreadable."""
    at = _num(at)
    if at is None or at <= 0:
        return None
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(stem):
    """(show, season, episode) from a stem path, without touching the filesystem."""
    episode = os.path.basename(stem)
    season = os.path.basename(os.path.dirname(stem))
    show = os.path.basename(os.path.dirname(os.path.dirname(stem)))
    return show, season, episode


def _app_version():
    """This fork's release, from the package constant.

    Not the installed distribution's metadata: the container runs the source tree on
    PYTHONPATH and installs nothing, so every metadata lookup there returned null -- a
    status route whose version field is always null cannot tell two builds apart.
    """
    return __version__


def _int_param(raw, default, low, high, name):
    """(value, problem): an explicit integer within [low, high]. Out-of-range is a 400, not a clamp,
    so an agent that asked for 10000 rows learns the contract instead of silently getting 200."""
    if raw is None or raw == "":
        return default, ""
    if isinstance(raw, bool):
        return 0, f"{name} must be an integer"
    try:
        value = raw if isinstance(raw, int) else int(str(raw).strip())
    except (TypeError, ValueError):
        return 0, f"{name} must be an integer"
    if value < low:
        return 0, f"{name} must be at least {low}"
    if high is not None and value > high:
        return 0, f"{name} must be at most {high}"
    return value, ""


def _stems_of(snapshot):
    """(stems, filled_at) out of review_server._STEMS_CACHE, which is None until warm."""
    if snapshot is None:
        return None, None
    if isinstance(snapshot, tuple) and len(snapshot) >= 3 and isinstance(snapshot[1], list):
        return list(snapshot[1]), snapshot[2]
    if isinstance(snapshot, list):
        return list(snapshot), None
    return [], None


# --- reads -------------------------------------------------------------------------------------------------------------


def handle_health(health=None):
    """Same semantics as /healthz: 200 {"ok": true}, or 503 {"ok": false, "reasons": [...]}.

    The check itself is review_server.handle_healthz, passed in rather than copied: one
    definition of "healthy" is the point of this route."""
    if health is None:
        return 503, {"ok": False, "reasons": ["heartbeat check unavailable"]}
    status, payload = health()
    out = {"ok": payload.get("ok") is True, "status": status}
    if payload.get("reasons"):
        out["reasons"] = list(payload["reasons"])
    return status, out


def _requests_view():
    """The queued/running run requests, newest first, shaped for an agent."""
    docs = review_requests.list_requests()
    items = []
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("status") not in ("pending", "running"):
            continue
        items.append(
            {
                "id": doc.get("id"),
                "status": doc.get("status"),
                "show": doc.get("show"),
                "season": doc.get("season"),
                "episode_re": doc.get("episode_re"),
                "force": bool(doc.get("force")),
                "require_eng": doc.get("require_eng"),
                "created_at": doc.get("created_at"),
                "finished_at": doc.get("finished_at"),
                "error": doc.get("error"),
                # The inbound Sonarr/Radarr payload is not echoed: only whether one was attached.
                "webhook": bool(doc.get("webhook")),
            }
        )
    return items


def _queue_counts():
    items = _requests_view()
    pending = sum(1 for item in items if item["status"] == "pending")
    running = sum(1 for item in items if item["status"] == "running")
    return items, {"pending": pending, "running": running, "depth": len(items)}


def handle_queue():
    items, counts = _queue_counts()
    return 200, {"queue": items, "count": len(items), "counts": counts}


def handle_library(anime_root=None):
    return 200, {"shows": review_requests.list_shows(anime_root)}


def _loop_state(heartbeat, now):
    """unknown | stopping | running | stalled | idle, from the heartbeat the builders write.

    Staleness uses the same STALE_SWEEP_MULTIPLIER x RESCAN_INTERVAL threshold /healthz uses, so
    the two routes can never disagree about a sweep that stopped reporting (one constant, in
    dubtitlerr.core.heartbeat)."""
    rescan = int(os.environ.get("RESCAN_INTERVAL", "21600"))
    if heartbeat is None:
        return "unknown"
    if common.stop_requested():
        return "stopping"
    start, end = _num(heartbeat.get("last_sweep_start")), _num(heartbeat.get("last_sweep_end"))
    if start is not None and (end is None or start > end):
        return "running"
    if end is None:
        return "stalled"
    return "stalled" if (now - end) > common.STALE_SWEEP_MULTIPLIER * rescan else "idle"


def handle_system_status(snapshot=None, health=None, now=None):
    now = time.time() if now is None else now
    heartbeat = common.read_heartbeat()
    if not isinstance(heartbeat, dict):
        heartbeat = None
    stems, filled_at = _stems_of(snapshot)
    _, counts = _queue_counts()
    health_status, health_payload = handle_health(health)
    cache_age = None
    if filled_at is not None and stems is not None:
        filled = _num(filled_at)
        if filled is not None:
            cache_age = round(max(0.0, time.monotonic() - filled), 1)
    return 200, {
        "version": {
            "api": "v1",
            "app": _app_version(),
            "transcribe_version": common.TRANSCRIBE_VERSION,
            "text_version": common.TEXT_VERSION,
        },
        "boot_id": common.boot_id(),
        "uptime_s": round(max(0.0, now - STARTED_AT), 1),
        "loop": {
            "state": _loop_state(heartbeat, now),
            "current_show": (heartbeat or {}).get("current_show"),
            "last_sweep_start": _num((heartbeat or {}).get("last_sweep_start")),
            "last_sweep_end": _num((heartbeat or {}).get("last_sweep_end")),
            "last_sweep_end_at": _iso((heartbeat or {}).get("last_sweep_end")),
            "stop_requested": common.stop_requested(),
            "roots_readable": (heartbeat or {}).get("roots_readable"),
            "order_file_present": (heartbeat or {}).get("order_file_present"),
        },
        "queue": counts,
        "processing": processing_state.describe(),
        "library": {
            "episodes_cached": None if stems is None else len(stems),
            "cache_age_s": cache_age,
        },
        "health": {"ok": health_payload.get("ok") is True, "status": health_status},
    }


_HISTORY = {"key": None, "events": []}
_HISTORY_LOCK = threading.Lock()


def _episode_events(stem):
    """Stage records and the delivery stamp for one episode, as history events."""
    show, season, episode = _split(stem)
    out = []
    stages = common.read_stages(stem)
    for stage, record in stages.items() if isinstance(stages, dict) else ():
        if not isinstance(stage, str) or not isinstance(record, dict):
            continue
        at = _num(record.get("at"))
        out.append(
            {
                "kind": "stage",
                "at": _iso(at),
                "at_ts": at,
                "stem": stem,
                "show": show,
                "season": season,
                "episode": episode,
                "stage": stage,
                "outcome": str(record.get("outcome") or ""),
                "detail": str(record.get("detail") or ""),
            }
        )
    stamp = common.read_stamp(stem + common.STAMP_SUFFIX)
    if isinstance(stamp, dict):
        # The STAMP FILE's mtime is when delivery was recorded. The video's own mtime (what the
        # stamp carries) is unchanged by a sidecar delivery, so it would date half the library wrong.
        try:
            delivered_at = os.path.getmtime(stem + common.STAMP_SUFFIX)
        except OSError:
            delivered_at = _num(stamp.get("mtime"))
        at = _num(delivered_at)
        out.append(
            {
                "kind": "delivery",
                "at": _iso(at),
                "at_ts": at,
                "stem": stem,
                "show": show,
                "season": season,
                "episode": episode,
                "mode": stamp.get("mode") if stamp.get("mode") in ("mux", "sidecar") else "mux",
                "transcribe_version": common._tier_version(stamp, "transcribe_version"),
                "text_version": common._tier_version(stamp, "text_version"),
                "muxed": bool(stamp.get("muxed")),
            }
        )
    return out


def _history_events(stems, key):
    """The event list, built once per library walk and held until the walk is redone."""
    if key is not None and _HISTORY["key"] == key:
        return _HISTORY["events"]
    with _HISTORY_LOCK:
        if key is not None and _HISTORY["key"] == key:
            return _HISTORY["events"]
        events = []
        for stem in stems[:MAX_HISTORY_EPISODES]:
            try:
                events.extend(_episode_events(stem))
            except Exception:  # noqa: BLE001 -- one unreadable episode must not empty the history
                continue
        events.sort(key=lambda e: (-(e["at_ts"] or 0.0), e["kind"], e["stem"], e.get("stage") or ""))
        for event in events:
            event.pop("at_ts", None)
        if key is not None:
            _HISTORY["key"], _HISTORY["events"] = key, events
    return events


def handle_history(query, snapshot=None):
    limit, problem = _int_param(query.get("limit"), DEFAULT_HISTORY_LIMIT, 1, MAX_HISTORY_LIMIT, "limit")
    if problem:
        return 400, {"error": problem}
    offset, problem = _int_param(query.get("offset"), 0, 0, None, "offset")
    if problem:
        return 400, {"error": problem}
    stems, filled_at = _stems_of(snapshot)
    if stems is None:
        return 503, {"error": "cache warming", "boot_id": common.boot_id()}
    events = _history_events(stems, filled_at)
    page = events[offset : offset + limit]
    return 200, {
        "events": page,
        "total": len(events),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(events),
        "scanned": min(len(stems), MAX_HISTORY_EPISODES),
        "truncated": len(stems) > MAX_HISTORY_EPISODES,
    }


def handle_settings(environ=None):
    """The read-only effective settings, exactly the masked shape Settings already serves."""
    return settings_api.get_settings(environ)


def handle_apikey_info():
    return 200, review_auth.api_key_info()


def handle_openapi():
    try:
        spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 500, {"error": "openapi.json is unavailable"}
    return 200, spec


# --- commands ---------------------------------------------------------------------------------------------------------


def _command_refusal(name, status, error, **extra):
    return status, {"name": name, "ok": False, "error": error, **extra}


def handle_command(body, who="admin", environ=None, anime_root=None):
    """arr-style ``{"name": ..., "params": {...}}``. Only the commands in COMMANDS exist."""
    if not isinstance(body, dict):
        return 400, {"error": 'send {"name": ..., "params": {...}}', "commands": list(COMMANDS)}
    extra = sorted(set(body) - {"name", "params"})
    if extra:
        return 400, {"error": f"unknown field: {extra[0]}", "commands": list(COMMANDS)}
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return 400, {"error": "name is required", "commands": list(COMMANDS)}
    params = body.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _command_refusal(name, 400, "params must be an object")
    if name not in COMMANDS:
        return _command_refusal(name, 400, f"unknown command: {name}", commands=list(COMMANDS))

    if name == "run.request":
        try:
            status, result = review_requests.create(params, anime_root=anime_root)
        except review_requests.Refused as refused:
            return _command_refusal(name, refused.status, refused.error)
        common.log(f"review server: api v1 command run.request by {who}: {status}")
        if status >= 400:
            return status, {"name": name, "ok": False, "error": str(result.get("error") or ""), "result": result}
        return status, {"name": name, "ok": True, "result": result}

    if name == "system.restart":
        status, result = settings_api.request_restart(environ, who)
        if status >= 400:
            return status, {"name": name, "ok": False, "error": str(result.get("error") or ""), "result": result}
        return status, {"name": name, "ok": True, "result": result}

    if name in ("system.start", "system.pause", "system.stop"):
        status, result = settings_api.request_processing(name.rsplit(".", 1)[-1], environ, who)
        common.log(f"review server: api v1 command {name} by {who}: {status}")
        if status >= 400:
            return status, {"name": name, "ok": False, "error": str(result.get("error") or ""), "result": result}
        return status, {"name": name, "ok": True, "result": result}

    target = params.get("name")
    if not isinstance(target, str) or not target:
        return _command_refusal(name, 400, "params.name is required")
    status, result = settings_api.test_notification(target, environ)
    common.log(f"review server: api v1 command notification.test by {who}: provider {target} -> {status}")
    if status >= 400:
        return status, {"name": name, "ok": False, "error": str(result.get("error") or ""), "result": result}
    return status, {"name": name, "ok": result.get("ok") is True, "result": result}


# --- notifications ----------------------------------------------------------------------------------------------------


def _masked_notifications(environ):
    state = settings_store.load(environ)
    return [settings_api.public_notification(provider, state) for provider in state.notifications]


def _masked_one(environ, name):
    return next((n for n in _masked_notifications(environ) if n["name"] == name), None)


def _notification_create(body, who, environ):
    if not isinstance(body, dict):
        return 400, {"errors": {"notification": "must be an object"}}
    state = settings_store.load(environ)
    name = body.get("name")
    if isinstance(name, str) and any(provider["name"] == name for provider in state.notifications):
        return 409, {"error": "a notification with that name already exists"}
    status, payload = settings_api.put_settings(
        {"revision": state.rev, "notifications": [*state.notifications, body]}, environ, who
    )
    if status != 200:
        return status, payload
    common.log(f"review server: api v1 notification created by {who}: {name}")
    return 201, {"notification": _masked_one(environ, name), "revision": payload["revision"]}


def _notification_replace(name, body, who, environ):
    problem = None if settings_notifications.NAME_RE.fullmatch(name or "") else "invalid notification name"
    if problem:
        return 404, {"error": "no such notification"}
    if not isinstance(body, dict):
        return 400, {"errors": {"notification": "must be an object"}}
    if "name" in body and body["name"] != name:
        return 400, {"errors": {"name": "cannot be changed; delete and recreate the notification"}}
    state = settings_store.load(environ)
    existing = next((provider for provider in state.notifications if provider["name"] == name), None)
    if existing is None:
        return 404, {"error": "no such notification"}
    merged = {**existing, **body, "name": name}
    wanted = [merged if provider["name"] == name else provider for provider in state.notifications]
    status, payload = settings_api.put_settings({"revision": state.rev, "notifications": wanted}, environ, who)
    if status != 200:
        return status, payload
    common.log(f"review server: api v1 notification updated by {who}: {name}")
    return 200, {"notification": _masked_one(environ, name), "revision": payload["revision"]}


def _notification_delete(name, who, environ):
    state = settings_store.load(environ)
    if not any(provider["name"] == name for provider in state.notifications):
        return 404, {"error": "no such notification"}
    kept = [provider for provider in state.notifications if provider["name"] != name]
    status, payload = settings_api.put_settings({"revision": state.rev, "notifications": kept}, environ, who)
    if status != 200:
        return status, payload
    common.log(f"review server: api v1 notification deleted by {who}: {name}")
    return 200, {"ok": True, "name": name, "revision": payload["revision"]}


def handle_notifications(method, name, body, who="admin", environ=None):
    """GET/POST on the collection, GET/PUT/DELETE on one provider. Every write goes through
    settings_api.put_settings, so the same validation and the same audit line apply as in Settings."""
    if name is None:
        if method == "GET":
            return 200, {"notifications": _masked_notifications(environ)}
        if method == "POST":
            return _notification_create(body, who, environ)
        return 405, {"error": "method not allowed"}
    name = unquote(name)
    if method == "GET":
        found = _masked_one(environ, name)
        return (200, {"notification": found}) if found else (404, {"error": "no such notification"})
    if method == "PUT":
        return _notification_replace(name, body, who, environ)
    if method == "DELETE":
        return _notification_delete(name, who, environ)
    return 405, {"error": "method not allowed"}


# --- dispatch ----------------------------------------------------------------------------------------------------------

_GET_ROUTES = {
    "/system/status": "status",
    "/health": "health",
    "/queue": "queue",
    "/history": "history",
    "/library": "library",
    "/settings": "settings",
    "/apikey/info": "apikey",
    "/openapi.json": "openapi",
}


def handle(method, path, payload=None, *, snapshot=None, anime_root=None, health=None, who="admin", environ=None):
    """(status, payload) for one /api/v1 request. ``payload`` is the query mapping for GET and the
    JSON body for a write; ``snapshot`` is review_server._STEMS_CACHE taken once by the caller."""
    sub = path[len(API_PREFIX) :]
    payload = payload if isinstance(payload, dict) else {}

    if sub in _GET_ROUTES:
        if method != "GET":
            return 405, {"error": "method not allowed"}
        kind = _GET_ROUTES[sub]
        if kind == "status":
            return handle_system_status(snapshot=snapshot, health=health)
        if kind == "health":
            return handle_health(health)
        if kind == "queue":
            return handle_queue()
        if kind == "history":
            return handle_history(payload, snapshot)
        if kind == "library":
            return handle_library(anime_root)
        if kind == "settings":
            return handle_settings(environ)
        if kind == "apikey":
            return handle_apikey_info()
        return handle_openapi()

    if sub == "/command":
        return handle_command(payload, who, environ, anime_root) if method == "POST" else (405, {"error": "method not allowed"})
    if sub == "/notifications":
        return handle_notifications(method, None, payload, who, environ)
    if sub.startswith("/notification/") and len(sub) > len("/notification/"):
        return handle_notifications(method, sub[len("/notification/") :], payload, who, environ)
    return 404, {"error": "no such route"}
