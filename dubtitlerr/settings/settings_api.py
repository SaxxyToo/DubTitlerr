"""Settings API: the registry's schema, the effective values with their sources, and
the validated, all-or-nothing writes. Pure functions over an environ mapping and the store; review_server.py only routes.

The role of the caller (user or admin) is enforced before these functions run, by the role table in review_auth.py: the routes
are registered there from ROLE_ENTRIES. The one thing checked here is the secret gate, _secrets_writable()."""

import errno
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from dubtitlerr.core import notify
from dubtitlerr.review import review_auth, review_settings
from dubtitlerr.settings import settings_connections as conns
from dubtitlerr.settings import settings_notifications as notif
from dubtitlerr.settings import settings_registry as reg
from dubtitlerr.settings import settings_store as store

_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@,]*@")

ROLE_ENTRIES = {
    ("GET", "/api/settings"): "user",
    ("GET", "/api/settings/schema"): "user",
    ("PUT", "/api/settings"): "admin",
    ("POST", "/api/settings/reset"): "admin",
    ("POST", "/api/settings/test"): "admin",
    ("POST", "/api/settings/models"): "admin",
    ("POST", "/api/system/restart"): "admin",
    ("GET", "/api/system/processing"): "user",
    ("POST", "/api/system/start"): "admin",
    ("POST", "/api/system/pause"): "admin",
    ("POST", "/api/system/stop"): "admin",
}
SECRETS_NEED_LOGIN = "secrets can be changed only after login is enabled"
RETIRED_ENV = ("REPAIR_PROFILES", "RUNTIME_VERIFY_BACKENDS", "RUNTIME_DECISION_MODELS")


def _env(environ):
    return os.environ if environ is None else environ


def _secrets_writable():
    """Secrets are accepted only with accounts on and the first-run setup finished: before that no admin can be told apart."""
    return review_auth.auth_on() and not review_auth.setup_required()


# --- reading --------------------------------------------------------------------------------------------------------------


def _container_raw(key, environ):
    """What the container holds for key. A boot-applied key reports its pre-store value (DT_BOOT_<KEY>, written by
    `export --boot`), because the process environment already carries the stored value."""
    boot = "DT_BOOT_" + key
    return environ.get(boot) if boot in environ else environ.get(key)


def _reports(environ):
    gen_path = review_settings.get_effective_env_path(environ)
    merge_path = os.path.join(os.path.dirname(gen_path), "merge-effective.env")
    return {
        "gen": (review_settings.read_effective_env(gen_path), _mtime(gen_path)),
        "merge": (review_settings.read_effective_env(merge_path), _mtime(merge_path)),
    }


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _applied(key, entry, environ, reports):
    """What the consuming process reports it runs with: the mapping it reports in (a report file, or the server's own
    environment for a restart-applied key)."""
    scope = set(entry["scope"])
    if "gen" in scope:
        return reports["gen"][0]
    if "merge" in scope:
        return reports["merge"][0]
    return environ


def _resolve(key, state, environ, reports, settings_mtime):
    entry = reg.REGISTRY[key]
    raw = _container_raw(key, environ)
    env_set = bool((raw or "").strip())
    if entry["secret"]:
        stored = key in state.secret_values
        out = {"set": stored or env_set, "source": "ui" if stored else "env" if env_set else "default"}
        if entry["applies"] == "restart":
            pending = stored and environ.get(key) != state.secret_values[key]
        else:
            report_mtime = reports["gen" if "gen" in entry["scope"] else "merge"][1]
            pending = stored and report_mtime < settings_mtime
    else:
        env_value, env_ok = reg.from_env(key, raw)
        if key in state.values:
            value, source, expected = state.values[key], "ui", state.values[key]
        elif env_value is not None:
            value, source, expected = env_value, "env", env_value
        else:
            value, source, expected = entry["default"], "default", None
        reported = _applied(key, entry, environ, reports)
        applied, _ = reg.from_env(key, reported.get(key))
        out = {"value": value, "source": source}
        if source == "env" and not env_ok:
            out["env_invalid"] = True
        # A value nobody stored differs only when the builder reports the key (a reset it has not applied yet): a builder that
        # has not reported at all is not a pending change.
        pending = applied != expected and (source == "ui" or key in reported)
    out["overridden_env"] = (key in state.values or key in state.secret_values) and env_set
    out["pending"] = bool(pending)
    return out


def _redact(key, out):
    """No credential leaves in a value: URLs lose user info and query, an extra body is reported only as set or not."""
    if "value" not in out:
        return out
    if key.endswith("_EXTRA_BODY"):
        out["set"] = bool(str(out.pop("value") or "").strip())
    elif isinstance(out["value"], str):
        v = out["value"]
        if reg.REGISTRY[key]["type"] == "url" or reg.URL_RE.match(v):
            v = store.redact_url(v) if v else v
        out["value"] = _URL_USERINFO_RE.sub(r"\1", v)
    elif isinstance(out["value"], list):
        out["value"] = [_URL_USERINFO_RE.sub(r"\1", item) if isinstance(item, str) else item for item in out["value"]]
    return out


def public_connection(conn, state):
    """A Connection as the page sees it: every field, and whether a key is stored. Never the key."""
    out = {f: conn.get(f) for f in conns.FIELDS}
    out["url"] = store.redact_url(out["url"]) if out.get("url") else ""
    out["extra_body"] = {"set": bool(out.get("extra_body"))}
    out["api_key"] = {"set": conn["name"] in state.conn_keys}
    return out


def public_notification(prov, state):
    """A notification as the page sees it: its visible fields, its triggers, and whether each secret is stored. Never a
    secret. A Discord or webhook URL is credential-bearing, so it is reported as set or not set like the others."""
    out = {
        "name": prov["name"],
        "kind": prov["kind"],
        "enabled": bool(prov.get("enabled", True)),
        "triggers": list(prov.get("triggers", [])),
    }
    for field in notif.VISIBLE_FIELDS.get(prov["kind"], ()):
        out[field] = prov.get(field, "")
    stored = state.notif_secrets.get(prov["name"], {})
    for field in notif.SECRET_FIELDS.get(prov["kind"], ()):
        out[field] = {"set": field in stored}
    return out


def get_settings(environ=None):
    environ = _env(environ)
    from dubtitlerr.core import processing_state

    state = store.load(environ)
    reports = _reports(environ)
    settings_mtime = _mtime(store.settings_path(environ))
    values = {key: _redact(key, _resolve(key, state, environ, reports, settings_mtime)) for key in reg.REGISTRY}
    return 200, {
        "values": values,
        "connections": [public_connection(c, state) for c in state.connections],
        "notifications": [public_notification(n, state) for n in state.notifications],
        "stages": dict(state.stages),
        "processing": processing_state.describe(environ),
        "restart_pending": any(values[k]["pending"] for k, e in reg.REGISTRY.items() if e["applies"] == "restart"),
        "revision": state.rev,
        "store_error": state.error,
        "secrets_error": state.secrets_error,
        "secrets_writable": _secrets_writable() and not state.secrets_error,
        "retired_env": [name for name in RETIRED_ENV if (environ.get(name) or "").strip()],
    }


# --- writing --------------------------------------------------------------------------------------------------------------


def _write_failure(e):
    if e.errno == errno.ENOSPC:
        return 507, {"error": f"the config volume is full ({e.__class__.__name__})"}
    return 503, {"error": f"the config volume is not writable ({e.__class__.__name__})"}


def _validate_values(values):
    """({KEY: ("set"|"reset"|"secret_set"|"secret_clear", canonical)}, {KEY: message}); a secret sent as "" means no change."""
    plan, errors = {}, {}
    for key, value in values.items():
        entry = reg.REGISTRY.get(key) if isinstance(key, str) else None
        if entry is None:
            errors[str(key)] = "unknown setting"
        elif not entry["editable"] and not (entry.get("legacy_override") and value is None):
            errors[key] = "set through a Connection" if entry["managed_by"] == "connections" else "edit in a later release"
        elif entry["secret"]:
            if value == "":
                continue
            if value is None or (isinstance(value, dict) and value == {"clear": True}):
                plan[key] = ("secret_clear", None)
            elif (
                isinstance(value, str)
                and "\n" not in value
                and "\r" not in value
                and "\x00" not in value
                and len(value) <= 512
                and value.strip()
            ):
                plan[key] = ("secret_set", value)
            else:
                errors[key] = 'must be one line of at most 512 characters, or {"clear": true}'
        elif value is None:
            plan[key] = ("reset", None)
        elif entry["type"] == "url" and isinstance(value, str):
            parts = urllib.parse.urlsplit(value.strip())
            if parts.query or parts.fragment:
                errors[key] = "must not contain a query or fragment; tokens belong in a secret field"
            else:
                try:
                    plan[key] = ("set", reg.validate(key, value))
                except reg.Invalid as e:
                    errors[key] = str(e)
        else:
            try:
                plan[key] = ("set", reg.validate(key, value))
            except reg.Invalid as e:
                errors[key] = str(e)
    return plan, errors


def _apply(state, plan):
    """Apply a validated plan to state; the keys that really changed, and the names of the secrets touched."""
    changed, secret_keys = [], []
    for key, (action, value) in plan.items():
        if action == "set" and state.values.get(key, object()) != value:
            state.values[key] = value
            changed.append(key)
        elif action == "reset" and key in state.values:
            del state.values[key]
            changed.append(key)
        elif action == "secret_set" and state.secret_values.get(key) != value:
            state.secret_values[key] = value
            changed.append(key)
            secret_keys.append(key)
        elif action == "secret_clear" and key in state.secret_values:
            del state.secret_values[key]
            changed.append(key)
            secret_keys.append(key)
    return changed, secret_keys


def _commit(environ, who, build, touches_secrets=False, prepare=None):
    """Lock, run prepare() (file moves that must happen under the lock), load fresh, let build(state) -> (changed, secret_keys,
    urls) or an (status, payload) refusal, save, audit. A corrupt settings.json refuses every save; a corrupt secrets.json
    only the ones that touch secrets."""
    try:
        with store.locked(environ) as got:
            if not got:
                return 503, {"error": "settings are busy, try again"}
            if prepare is not None:
                prepare()
            state = store.load(environ)
            if state.error or (state.secrets_error and touches_secrets):
                return 409, {
                    "error": f"{state.error or state.secrets_error}; reset the settings before saving",
                    "reset_required": True,
                }
            outcome = build(state)
            if isinstance(outcome[0], int):
                return outcome
            changed, secret_keys, urls = outcome
            if changed:
                store.save(state, environ)
                store.audit(who, changed, secret_keys, urls)
            return 200, {
                "ok": True,
                "changed": sorted(changed),
                "restart_required": sorted(k for k in changed if k in reg.REGISTRY and reg.REGISTRY[k]["applies"] == "restart"),
                "revision": state.rev,
            }
    except OSError as e:
        return _write_failure(e)


def _validate_connections(items):
    """(clean connections, {name: key action}, errors). A key action is ("set", key) or ("clear", None); absent, "" or null mean
    no change."""
    clean, actions, errors, seen = [], {}, {}, set()
    if not isinstance(items, list):
        return [], {}, {"connections": "must be a list"}
    for index, item in enumerate(items):
        conn, problems = conns.validate_connection(item)
        label = (
            item.get("name") if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name") else str(index)
        )
        for field, message in problems.items():
            errors[f"connections.{label}.{field}"] = message
        if problems:
            continue
        if conn["name"] in seen:
            errors[f"connections.{label}.name"] = "is already used by another connection"
            continue
        seen.add(conn["name"])
        clean.append(conn)
        raw = item.get("api_key")
        if raw == {"clear": True}:
            actions[conn["name"]] = ("clear", None)
        elif isinstance(raw, str) and raw:
            why = conns.key_problem(conn, raw)
            if why:
                errors[f"connections.{label}.api_key"] = why
            else:
                actions[conn["name"]] = ("set", raw)
        elif raw not in (None, ""):
            errors[f"connections.{label}.api_key"] = 'must be text, or {"clear": true}'
    return clean, actions, errors


def _apply_connections(state, clean, actions):
    """Replace the Connection list. A changed URL or kind drops the stored key unless a key arrives in the same save, so a stolen
    session cannot point a saved key at another host. Returns (changed labels, secret labels, url map)."""
    old = {c["name"]: c for c in state.connections}
    changed, secret_keys, urls = [], [], {}

    def touch(name, key_changed=False):
        label = f"connection:{name}"
        if label not in changed:
            changed.append(label)
        if key_changed:
            secret_keys.append(f"{label}.api_key")

    for conn in clean:
        name, prior, action = conn["name"], old.get(conn["name"]), actions.get(conn["name"])
        extra = conn.get("extra_body")
        if isinstance(extra, dict) and list(extra.keys()) == ["set"] and type(extra.get("set")) is bool:
            conn["extra_body"] = dict(prior.get("extra_body", {})) if prior else {}
        if prior != conn:
            touch(name)
        if prior is None or prior["url"] != conn["url"]:
            urls[f"connection {name}"] = conn["url"]
        orphan = prior is None and action is None
        moved = prior is not None and (prior["url"] != conn["url"] or prior["kind"] != conn["kind"])
        if ((moved or orphan) and action is None) or action == ("clear", None):
            if state.conn_keys.pop(name, None) is not None:
                touch(name, True)
        elif action and action[0] == "set" and state.conn_keys.get(name) != action[1]:
            state.conn_keys[name] = action[1]
            touch(name, True)
    for name in sorted(set(old) - {c["name"] for c in clean}):
        touch(name, state.conn_keys.pop(name, None) is not None)
    state.connections = clean
    return changed, secret_keys, urls


def _validate_notifications(items):
    """(clean notifications, {name: {secret field: ("set", value) | ("clear", None)}}, errors). Absent, "" or null mean no
    change."""
    clean, actions, errors, seen = [], {}, {}, set()
    if not isinstance(items, list):
        return [], {}, {"notifications": "must be a list"}
    for index, item in enumerate(items):
        prov, problems = notif.validate_notification(item)
        label = (
            item.get("name")
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name")
            else str(index)
        )
        for field, message in problems.items():
            errors[f"notifications.{label}.{field}"] = message
        if problems:
            continue
        if prov["name"] in seen:
            errors[f"notifications.{label}.name"] = "is already used by another notification"
            continue
        seen.add(prov["name"])
        clean.append(prov)
        fields = {}
        for field in notif.SECRET_FIELDS.get(prov["kind"], ()):
            raw = item.get(field)
            if raw == {"clear": True}:
                fields[field] = ("clear", None)
            elif isinstance(raw, str) and raw:
                why = notif.secret_problem(prov["kind"], field, raw)
                if why:
                    errors[f"notifications.{label}.{field}"] = why
                else:
                    fields[field] = ("set", raw)
            elif raw not in (None, ""):
                errors[f"notifications.{label}.{field}"] = 'must be text, or {"clear": true}'
        if fields:
            actions[prov["name"]] = fields
    return clean, actions, errors


def _apply_notifications(state, clean, actions):
    """Replace the notification list. A changed kind, or a changed ntfy server, drops the stored secret unless a new secret
    arrives in the same save. Returns (changed labels, secret labels, url map). The Discord and webhook URLs are secrets, so
    they never reach the audit log; only an ntfy server (no credentials in it) is passed as a URL."""
    old = {n["name"]: n for n in state.notifications}
    changed, secret_keys, urls = [], [], {}

    def touch(name, secret_fields=()):
        label = f"notification:{name}"
        if label not in changed:
            changed.append(label)
        for field in secret_fields:
            entry = f"{label}.{field}"
            if entry not in secret_keys:
                secret_keys.append(entry)

    for prov in clean:
        name, prior = prov["name"], old.get(prov["name"])
        before = dict(state.notif_secrets.get(name, {}))
        kept = dict(before)
        dropped = []
        if prior is not None and prior["kind"] != prov["kind"]:
            dropped = list(kept)  # a secret belongs to the kind that reads it
            kept = {}
        elif prov["kind"] == "ntfy" and prior is not None and prior.get("server") != prov.get("server"):
            if "token" in kept:
                kept.pop("token")
                dropped.append("token")
        for field, (action, value) in actions.get(name, {}).items():
            if action == "clear":
                if field in kept:
                    kept.pop(field)
                    dropped.append(field)
            elif kept.get(field) != value:
                kept[field] = value
                dropped.append(field)
        if kept != before:
            if kept:
                state.notif_secrets[name] = kept
            else:
                state.notif_secrets.pop(name, None)
        if prior != prov:
            touch(name)
        touch(name, dropped)
        if prov["kind"] == "ntfy" and (prior is None or prior.get("server") != prov.get("server")):
            urls[f"notification {name}"] = prov.get("server", "")
    for name in sorted(set(old) - {p["name"] for p in clean}):
        gone = state.notif_secrets.pop(name, None)
        touch(name, list(gone) if gone else ())
    state.notifications = clean
    return changed, secret_keys, urls


def put_settings(body, environ=None, who="admin"):
    environ = _env(environ)
    if not isinstance(body, dict) or not isinstance(body.get("values", {}), dict):
        return 400, {"errors": {"body": 'send {"values": {KEY: value}}'}}
    extra = sorted(set(body) - {"values", "revision", "connections", "stages", "notifications"})
    if extra:
        return 400, {"errors": {"body": f"unknown field: {extra[0]}"}}
    if type(body.get("revision")) is not int:
        return 400, {"errors": {"revision": "send the revision you loaded, as an integer"}}
    plan, errors = _validate_values(body.get("values", {}))
    clean, actions = None, {}
    if "connections" in body:
        clean, actions, problems = _validate_connections(body["connections"])
        errors.update(problems)
    clean_notifs, notif_actions = None, {}
    if "notifications" in body:
        clean_notifs, notif_actions, problems = _validate_notifications(body["notifications"])
        errors.update(problems)
    stages_in = body.get("stages")
    if stages_in is not None and not (
        isinstance(stages_in, dict)
        and all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in stages_in.items())
    ):
        errors["stages"] = "must map a stage to a connection name, or null"
        stages_in = None
    touches_secrets = (
        any(action.startswith("secret") for action, _ in plan.values())
        or bool(actions)
        or bool(notif_actions)
    )
    if touches_secrets and not _secrets_writable():
        return 403, {"error": SECRETS_NEED_LOGIN}
    if errors:
        return 400, {"errors": errors}

    def build(state):
        if body["revision"] != state.rev:
            status, current = get_settings(environ)
            return 409, {"error": "settings changed since you loaded them", "current": current}
        changed, secret_keys = _apply(state, plan)
        urls = {k: state.values[k] for k in changed if k in state.values and reg.REGISTRY[k]["type"] == "url" and state.values[k]}
        if clean is not None:
            more, more_secrets, more_urls = _apply_connections(state, clean, actions)
            changed, secret_keys, urls = changed + more, secret_keys + more_secrets, {**urls, **more_urls}
        if clean_notifs is not None:
            more, more_secrets, more_urls = _apply_notifications(state, clean_notifs, notif_actions)
            changed, secret_keys, urls = changed + more, secret_keys + more_secrets, {**urls, **more_urls}
        if stages_in is not None:
            before = dict(state.stages)
            for stage, name in stages_in.items():
                if name:
                    state.stages[stage] = name
                else:
                    state.stages.pop(stage, None)
            if state.stages != before:
                changed.append("stages")
        if clean is not None or stages_in is not None:
            problems = {f"stages.{k}": v for k, v in conns.validate_stages(state.stages, state.connections).items()}
            if problems:
                return 400, {"errors": problems}
        return changed, secret_keys, urls

    return _commit(environ, who, build, touches_secrets)


def _move_aside(path):
    os.replace(path, path + ".corrupt")


def reset_settings(body, environ=None, who="admin"):
    environ = _env(environ)
    shape = 'send {"keys": [...]}, {"category": id} or {"all": true}'
    if not isinstance(body, dict):
        return 400, {"errors": {"body": shape}}
    writable = _secrets_writable()
    everything = body.get("all") is True
    if isinstance(body.get("keys"), list):
        if not all(isinstance(k, str) for k in body["keys"]):
            return 400, {"errors": {"keys": "must be a list of strings"}}
        keys = body["keys"]
        bad = {k: "unknown setting" for k in keys if k not in reg.REGISTRY}
        if bad:
            return 400, {"errors": bad}
        if any(reg.REGISTRY[k]["secret"] for k in keys) and not writable:
            return 403, {"error": SECRETS_NEED_LOGIN}
    elif isinstance(body.get("category"), str):
        if body["category"] not in reg.CATEGORY_IDS:
            return 400, {"errors": {"category": "unknown category"}}
        keys = [k for k, e in reg.REGISTRY.items() if e["category"] == body["category"]]
    elif everything:
        keys = list(reg.REGISTRY)
    else:
        return 400, {"errors": {"body": shape}}
    keys = [
        k
        for k in keys
        if (reg.REGISTRY[k]["editable"] or reg.REGISTRY[k].get("legacy_override")) and (writable or not reg.REGISTRY[k]["secret"])
    ]
    clears_connections = everything or body.get("category") == "connections"
    clears_notifications = everything or body.get("category") == "connect"
    touches_secrets = (
        any(reg.REGISTRY[k]["secret"] for k in keys)
        or (clears_connections and writable)
        or (clears_notifications and writable)
    )

    def prepare():
        if not everything:
            return
        for path, unusable in (
            (store.settings_path(environ), bool(store.load(environ).error)),
            (store.secrets_path(environ), bool(store._read_json(store.secrets_path(environ))[1])),
        ):
            if unusable and os.path.exists(path):
                _move_aside(path)

    def build(state):
        plan = {k: ("secret_clear" if reg.REGISTRY[k]["secret"] else "reset", None) for k in keys}
        changed, secret_keys = _apply(state, plan)
        if clears_connections:
            if state.connections:
                state.connections = []
                changed.append("connections")
            if state.stages:
                state.stages = {}
                changed.append("stages")
            if writable and state.conn_keys:
                state.conn_keys = {}
                changed.append("connection keys")
                secret_keys.append("connection keys")
        if clears_notifications:
            if state.notifications:
                state.notifications = []
                changed.append("notifications")
            if writable and state.notif_secrets:
                state.notif_secrets = {}
                changed.append("notification secrets")
                secret_keys.append("notification secrets")
        return changed, secret_keys, {}

    status, payload = _commit(environ, who, build, touches_secrets, prepare if everything else None)
    if status == 200:
        payload["reset"] = payload.pop("changed")
    return status, payload


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, *, checked_ips=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self._checked_ips = checked_ips or []

    def connect(self):
        port = self.port or 80
        errors = []
        for ip in self._checked_ips:
            try:
                self.sock = socket.create_connection((ip, port), self.timeout, self.source_address)
                try:
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    # NODELAY only trims the handshake latency; a socket that refuses it still connects.
                    pass
                return
            except OSError as e:
                errors.append(e)
        if errors:
            raise errors[-1]
        raise OSError(f"no addresses to connect to for {self.host}")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, *, checked_ips=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self._checked_ips = checked_ips or []

    def connect(self):
        port = self.port or 443
        errors = []
        for ip in self._checked_ips:
            try:
                sock = socket.create_connection((ip, port), self.timeout, self.source_address)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    # NODELAY only trims the handshake latency; a socket that refuses it still connects.
                    pass
                server_hostname = self._tunnel_host or self.host
                self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)
                return
            except OSError as e:
                errors.append(e)
        if errors:
            raise errors[-1]
        raise OSError(f"no addresses to connect to for {self.host}")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, checked_ips):
        super().__init__()
        self._checked_ips = checked_ips

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: _PinnedHTTPConnection(host, checked_ips=self._checked_ips, **kw),
            req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, checked_ips, context=None):
        super().__init__(context=context)
        self._checked_ips = checked_ips

    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, checked_ips=self._checked_ips, **kw),
            req,
            context=self._context,
        )


def _pinned_opener(checked_ips, context=None):
    return urllib.request.build_opener(
        _NoRedirectHandler,
        _PinnedHTTPHandler(checked_ips),
        _PinnedHTTPSHandler(checked_ips, context=context),
    ).open


def test_connection(name, environ=None, opener=None, context=None):
    """Probe one saved Connection with its own timeouts and key. Always 200 with {ok, latency_ms, error}; the key is read here
    and travels in the Authorization header only."""
    state = store.load(_env(environ))
    conn = next((c for c in state.connections if c["name"] == name), None) if isinstance(name, str) else None
    if conn is None:
        return 400, {"error": "unknown connection"}
    key = state.conn_keys.get(conn["name"], "")
    if conn["kind"] == "openai":
        if not key:
            return 200, {"ok": False, "latency_ms": 0, "error": "API key not configured"}
        url, headers = conns.models_url(conn["url"]), {"Authorization": f"Bearer {key}"}
    else:
        url, headers = conn["url"], {}
    target_url, checked_ips, problem = conns.check_test_target(url)
    if problem:
        return 200, {"ok": False, "latency_ms": 0, "error": problem}
    if opener is None:
        opener = _pinned_opener(checked_ips, context=context)
    result = review_settings.probe(target_url, headers, key, conn["timeout_connect"], opener)
    return 200, {**result, "error": store.scrub(result["error"], state)}


MODELS_MAX_BYTES = 1_000_000


def _models_request(url, headers, timeout, opener):
    """GET a model-list URL and map it to {"ok", "models", "latency_ms", "error"}. The error text never carries the key
    or a URL: a status, a fixed phrase, or an exception's class name."""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    start = time.monotonic()
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(MODELS_MAX_BYTES + 1)
        latency_ms = int((time.monotonic() - start) * 1000)
    except urllib.error.HTTPError as e:
        return {"ok": False, "models": [], "latency_ms": 0, "error": f"HTTP {e.code}"}
    except TimeoutError:
        return {"ok": False, "models": [], "latency_ms": 0, "error": "timed out"}
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, TimeoutError):
            return {"ok": False, "models": [], "latency_ms": 0, "error": "timed out"}
        if isinstance(reason, ConnectionRefusedError) or (isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED):
            return {"ok": False, "models": [], "latency_ms": 0, "error": "connection refused"}
        return {"ok": False, "models": [], "latency_ms": 0, "error": "connection failed"}
    except OSError as e:
        if isinstance(e, ConnectionRefusedError) or e.errno == errno.ECONNREFUSED:
            return {"ok": False, "models": [], "latency_ms": 0, "error": "connection refused"}
        return {"ok": False, "models": [], "latency_ms": 0, "error": type(e).__name__}
    except Exception as e:  # a fake opener may raise anything; never let text through raw
        return {"ok": False, "models": [], "latency_ms": 0, "error": type(e).__name__}
    if not 200 <= status < 300:
        return {"ok": False, "models": [], "latency_ms": latency_ms, "error": f"HTTP {status}"}
    if len(raw) > MODELS_MAX_BYTES:
        return {"ok": False, "models": [], "latency_ms": latency_ms, "error": "the model list is too large"}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (TypeError, ValueError):
        return {"ok": False, "models": [], "latency_ms": latency_ms, "error": "the server did not answer with JSON"}
    return {"ok": True, "models": [], "latency_ms": latency_ms, "error": "", "_payload": payload}


def _probe_models(url, headers, timeout, opener, kind):
    result = _models_request(url, headers, timeout, opener)
    payload = result.pop("_payload", None)
    if result["ok"]:
        result["models"] = conns.model_ids(kind, payload)
    return result


def _effective(key, state, environ):
    """The value a setting runs with: what the UI stored, else the container's environment, else the default."""
    if key in state.values:
        return state.values[key]
    value, _ = reg.from_env(key, _container_raw(key, environ))
    return reg.REGISTRY[key]["default"] if value is None else value


def _keyless_transport_ok(url):
    """The pipeline only sends a key over https, or to loopback."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" or parts.hostname in ("127.0.0.1", "localhost", "::1")


def fetch_models(target, environ=None, opener=None, context=None):
    """List the models a saved Connection, or the Transcription endpoint, offers. Always 200 with
    {ok, models, latency_ms, error} once the target is known; the stored key is read here and travels in the Authorization
    header only, never in the answer. A server that needs no key (llama-swap on loopback) is probed without one."""
    environ = _env(environ)
    state = store.load(environ)
    if isinstance(target, dict) and target.get("endpoint") == "transcription":
        asr_url = _effective("ASR_URL", state, environ)
        key_name = _effective("ASR_KEY_ENV", state, environ)
        key = (environ.get(key_name) or "") if isinstance(key_name, str) and key_name else ""
        headers = {"Authorization": f"Bearer {key}"} if key and _keyless_transport_ok(asr_url) else {}
        kind, timeout = "openai", 10.0
        url = conns.models_probe_url(kind, asr_url)
    else:
        name = target.get("connection") if isinstance(target, dict) else None
        conn = next((c for c in state.connections if c["name"] == name), None) if isinstance(name, str) else None
        if conn is None:
            return 400, {"error": "unknown connection"}
        kind, timeout = conn["kind"], conn["timeout_connect"]
        url = conns.models_probe_url(kind, conn["url"])
        key = state.conn_keys.get(conn["name"], "")
        headers = {"Authorization": f"Bearer {key}"} if kind == "openai" and key else {}
    target_url, checked_ips, problem = conns.check_test_target(url)
    if problem:
        return 200, {"ok": False, "models": [], "latency_ms": 0, "error": problem}
    if opener is None:
        opener = _pinned_opener(checked_ips, context=context)
    result = _probe_models(target_url, headers, timeout, opener, kind)
    result["error"] = store.scrub(result["error"], state)
    return 200, result


def test_notification(name, environ=None):
    """Send one test event to a saved notification provider. Always 200 with {ok, error}; the secret is read from disk by
    notify.py itself and never travels through this function's return. This is the same code path as
    `python3 -m dubtitlerr.core.notify test --name <n>` (its CLI calls test_provider)."""
    environ = _env(environ)
    state = store.load(environ)
    prov = next((n for n in state.notifications if n["name"] == name), None) if isinstance(name, str) else None
    if prov is None:
        return 400, {"error": "unknown notification"}
    try:
        result = notify.test_provider(name, config_dir=store.config_dir(environ))
    except LookupError:
        return 200, {"ok": False, "error": "the provider is not saved yet"}
    error = store.scrub(str(result.get("detail") or ""), state)
    return 200, {"ok": result.get("ok") is True, "error": error}


def request_processing(action, environ=None, who="admin"):
    """Persist start/pause/stop. Stop also writes the soft-stop flag, so the loops drain and the
    container exits exactly as a `docker stop` would; the restart policy brings the UI back and
    the persisted `stopped` then holds the loops idle until someone starts them."""
    from dubtitlerr.core import processing_state

    return processing_state.apply(action, who=who, environ=environ)


def request_restart(environ=None, who="admin"):
    """Ask the builder for a soft stop: touch the flag every loop checks between units of work. The episode in flight (a mux
    included) finishes, the loops exit 0, and the container's restart policy brings the container back with a new boot id."""
    from dubtitlerr.core.common import boot_id, log

    flag = _env(environ).get("STOP_FLAG") or "/tmp/dubtitlerr.stop"
    try:
        with open(flag, "a"):
            os.utime(flag, None)
    except OSError as e:
        return 500, {"error": f"could not write the stop flag ({e.__class__.__name__})"}
    log(f"review server: restart requested by {who}: soft stop flag written")
    return 202, {"ok": True, "boot_id": boot_id()}


# --- dispatch -------------------------------------------------------------------------------------------------------------


def handle(method, path, body=None, environ=None, who="admin", opener=None, context=None):
    route = (path or "").split("?", 1)[0]
    if method == "GET" and route == "/api/settings/schema":
        return 200, {**reg.schema(), "connections": conns.rules(), "notifications": notif.rules()}
    if method == "GET" and route == "/api/settings":
        return get_settings(environ)
    if method == "PUT" and route == "/api/settings":
        return put_settings(body, environ, who)
    if method == "POST" and route == "/api/settings/reset":
        return reset_settings(body, environ, who)
    if method == "POST" and route == "/api/settings/test":
        if isinstance(body, dict) and "connection" in body:
            return test_connection(body["connection"], environ, opener, context=context)
        if isinstance(body, dict) and "notification" in body:
            return test_notification(body["notification"], environ)
        return review_settings.handle(method, route, body, environ=environ, opener=opener)
    if method == "POST" and route == "/api/settings/models":
        return fetch_models(body if isinstance(body, dict) else {}, environ, opener, context=context)
    if method == "POST" and route == "/api/system/restart":
        return request_restart(environ, who)
    if method == "GET" and route == "/api/system/processing":
        from dubtitlerr.core import processing_state

        return 200, processing_state.describe(_env(environ))
    if method == "POST" and route in ("/api/system/start", "/api/system/pause", "/api/system/stop"):
        return request_processing(route.rsplit("/", 1)[-1], environ, who)
    return 404, {"error": "no such route"}
