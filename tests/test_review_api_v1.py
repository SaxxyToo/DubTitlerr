"""GET/POST /api/v1/*: the versioned, agent-facing surface.

Auth matrix (read = user, write = admin, fail-closed for unlisted paths), response shapes, the
arr-style command endpoint, notification provider management through the settings store, and the
explicit history paging bounds. The OpenAPI file and its consistency test live in
test_review_api_v1_openapi.py.
"""

import http.client
import json
import os
import threading
import time

import pytest

from dubtitlerr.core import common
from dubtitlerr.review import review_api_v1, review_auth, review_requests
from dubtitlerr.review import review_server as rs
from dubtitlerr.settings import settings_api, settings_store

pytestmark = pytest.mark.real_accounts
KEY = "v1-test-key"
CSRF = {"X-DubTitlerr-Request": "1"}
SECRET = "ntfy-token-3f8a91c2d8e54b60"  # gitleaks:allow -- test fixture, not a real credential


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("REVIEW_TOKEN", KEY)
    monkeypatch.delenv("REVIEW_AUTH", raising=False)
    monkeypatch.setenv("REQUESTS_DIR", str(tmp_path / "requests"))
    monkeypatch.setenv("ANIME_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("STOP_FLAG", str(tmp_path / "stop.flag"))
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setattr(review_auth, "SCRYPT_N", 2**4)
    monkeypatch.setattr(rs, "ROOTS", [str(tmp_path / "media")])
    monkeypatch.setattr(rs, "_STEMS_CACHE", None)
    monkeypatch.setattr(rs, "_QUEUE_CACHE", {})
    review_api_v1._HISTORY.update(key=None, events=[])
    (tmp_path / "media" / "Show A").mkdir(parents=True)
    (tmp_path / "media" / "Show B").mkdir(parents=True)
    review_auth.create_user("root", "admin", "admin password")
    review_auth.create_user("alice", "user", "user password!")
    return tmp_path


def session(name):
    return {"Cookie": f"dubtitlerr_session={review_auth.sessions().create(name, False)}", **CSRF}


def read(env, path, query=None, headers=None):
    return rs.route("GET", path, query or {}, None, headers=headers)


def write(env, method, path, body, headers=None):
    return rs.route(method, path, body, None, headers=headers)


def episode(env, show, season, name, stages=None, stamp=None):
    stem = env / "media" / show / season / name
    stem.parent.mkdir(parents=True, exist_ok=True)
    if stages is not None:
        (stem.parent / (name + common.STAGES_SUFFIX)).write_text(json.dumps(stages))
    if stamp is not None:
        (stem.parent / (name + common.STAMP_SUFFIX)).write_text(json.dumps(stamp))
    return str(stem)


def warm(env, stems):
    rs._STEMS_CACHE = (time.monotonic() + 600, stems, time.monotonic())
    return stems


def request_doc(env, name, status="pending", show="Show A"):
    path = env / "requests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": name.split(".")[0],
                "status": status,
                "show": show,
                "season": 1,
                "episode_re": "E01",
                "force": False,
                "require_eng": None,
                "created_at": "2026-11-19T10:00:00+00:00",
                "finished_at": None,
                "error": None,
                "webhook": {"eventType": "Download"},
            }
        )
    )
    return path


def save_provider(env, **fields):
    """Create a provider through the API itself, so the store and audit path are exercised."""
    body = {
        "name": "ntfy-main",
        "kind": "ntfy",
        "server": "https://ntfy.example.test",
        "topic": "dub",
        "triggers": ["run.requested"],
    }
    if fields.get("kind", "ntfy") != "ntfy":
        body.pop("server"), body.pop("topic")
    body.update(fields)
    status, payload = write(env, "POST", "/api/v1/notifications", body, headers=session("root"))
    assert status == 201, payload
    return payload["notification"]


def command(env, name, params=None):
    """POST /api/v1/command as the admin session."""
    return write(env, "POST", "/api/v1/command", {"name": name, "params": {} if params is None else params}, session("root"))


# --- auth matrix -------------------------------------------------------------------------------------------------------


READS = (
    "/api/v1/system/status",
    "/api/v1/health",
    "/api/v1/queue",
    "/api/v1/history",
    "/api/v1/library",
    "/api/v1/notifications",
    "/api/v1/settings",
    "/api/v1/apikey/info",
    "/api/v1/openapi.json",
)


@pytest.mark.parametrize("path", READS)
def test_a_read_route_needs_a_credential(env, path):
    warm(env, [])
    assert read(env, path) == (401, {"error": "login required"})


@pytest.mark.parametrize("path", READS)
def test_the_api_key_reaches_every_read_route(env, path):
    warm(env, [])
    expected = (200, 503) if path == "/api/v1/health" else (200,)
    assert read(env, path, headers={"X-Api-Key": KEY})[0] in expected


@pytest.mark.parametrize("path", READS)
def test_a_user_session_reaches_every_read_route(env, path):
    warm(env, [])
    expected = (200, 503) if path == "/api/v1/health" else (200,)
    assert read(env, path, headers=session("alice"))[0] in expected


def test_writes_are_admin_only(env):
    assert write(env, "POST", "/api/v1/command", {"name": "run.request", "params": {"show": "Show A"}}, session("alice"))[:2] == (
        403,
        {"error": "admin only"},
    )
    assert write(env, "POST", "/api/v1/notifications", {"name": "x", "kind": "ntfy"}, session("alice"))[0] == 403
    assert write(env, "PUT", "/api/v1/notification/x", {"enabled": False}, session("alice"))[0] == 403
    assert write(env, "DELETE", "/api/v1/notification/x", {}, session("alice"))[0] == 403


def test_a_session_write_needs_the_csrf_header(env):
    status, payload = write(env, "POST", "/api/v1/command", {"name": "system.restart"}, {"Cookie": session("root")["Cookie"]})
    assert (status, payload) == (403, {"error": "cross-site request refused"})


def test_an_unlisted_v1_path_fails_closed_to_admin(env):
    assert read(env, "/api/v1/nope", headers=session("alice"))[:2] == (403, {"error": "admin only"})
    assert read(env, "/api/v1/nope", headers=session("root"))[:2] == (404, {"error": "no such route"})
    assert read(env, "/api/v1/nope", headers={"X-Api-Key": KEY})[:2] == (404, {"error": "no such route"})


def test_every_v1_role_entry_is_registered(env):
    for route, role in review_api_v1.ROLE_ENTRIES.items():
        assert review_auth.ROUTE_ROLES[route] == role


def test_the_classic_routes_are_untouched(env):
    assert read(env, "/api/shows", headers={"X-Api-Key": KEY})[0] == 200
    assert read(env, "/api/v1/nope", headers={"X-Api-Key": KEY})[0] == 404


# --- health / status ---------------------------------------------------------------------------------------------------


def test_health_mirrors_healthz(env):
    assert read(env, "/healthz")[:2] == (503, {"ok": False, "reasons": ["no heartbeat yet"]})
    assert read(env, "/api/v1/health", headers={"X-Api-Key": KEY})[:2] == (
        503,
        {"ok": False, "status": 503, "reasons": ["no heartbeat yet"]},
    )
    common.heartbeat(last_sweep_end=time.time(), roots_readable=True, order_file_present=True)
    assert read(env, "/healthz")[:2] == (200, {"ok": True})
    assert read(env, "/api/v1/health", headers={"X-Api-Key": KEY})[:2] == (200, {"ok": True, "status": 200})


def test_system_status_reports_version_loop_state_and_queue_depth(env):
    request_doc(env, "pending-1.json", "pending")
    request_doc(env, "README.running", "running")
    stems = warm(env, [episode(env, "Show A", "Season 1", "Show A - S01E01")])
    status, payload = read(env, "/api/v1/system/status", headers={"X-Api-Key": KEY})
    assert status == 200
    assert payload["version"]["api"] == "v1"
    assert payload["version"]["transcribe_version"] == common.TRANSCRIBE_VERSION
    assert payload["version"]["text_version"] == common.TEXT_VERSION
    assert payload["boot_id"] == common.boot_id()
    assert payload["uptime_s"] >= 0
    assert payload["loop"]["state"] == "unknown" and payload["loop"]["stop_requested"] is False
    assert payload["queue"] == {"pending": 1, "running": 1, "depth": 2}
    assert payload["library"] == {"episodes_cached": len(stems), "cache_age_s": payload["library"]["cache_age_s"]}
    assert payload["health"] == {"ok": False, "status": 503}


def test_loop_state_running_then_stalled(env, monkeypatch):
    monkeypatch.setenv("RESCAN_INTERVAL", "60")
    common.heartbeat(last_sweep_start=time.time(), last_sweep_end=time.time() - 600, roots_readable=True)
    status = read(env, "/api/v1/system/status", headers={"X-Api-Key": KEY})[1]
    assert status["loop"]["state"] == "running"
    common.heartbeat(last_sweep_start=time.time() - 600, last_sweep_end=time.time() - 600)
    status = read(env, "/api/v1/system/status", headers={"X-Api-Key": KEY})[1]
    assert status["loop"]["state"] == "stalled"
    open(os.environ["STOP_FLAG"], "w").close()
    status = read(env, "/api/v1/system/status", headers={"X-Api-Key": KEY})[1]
    assert status["loop"]["state"] == "stopping"


# --- queue / library ---------------------------------------------------------------------------------------------------


def test_queue_lists_only_pending_and_running(env):
    request_doc(env, "a.json", "pending")
    request_doc(env, "b.running", "running")
    request_doc(env, "done/c.json", "done")
    status, payload = read(env, "/api/v1/queue", headers={"X-Api-Key": KEY})
    assert status == 200
    assert sorted(item["status"] for item in payload["queue"]) == ["pending", "running"]
    assert payload["count"] == 2 and payload["counts"] == {"pending": 1, "running": 1, "depth": 2}
    assert payload["queue"][0]["show"] == "Show A" and payload["queue"][0]["webhook"] is True
    assert "webhook" in payload["queue"][0] and isinstance(payload["queue"][0]["webhook"], bool)


def test_library_lists_shows_like_api_shows(env):
    assert read(env, "/api/v1/library", headers={"X-Api-Key": KEY})[1] == {"shows": ["Show A", "Show B"]}


# --- history -----------------------------------------------------------------------------------------------------------


def _history_fixture(env):
    first = episode(
        env,
        "Show A",
        "Season 1",
        "Show A - S01E01",
        stages={"repair": {"outcome": "ok", "at": 100.0}, "mux": {"outcome": "ok", "at": 300.0}},
        stamp={"size": 1, "mtime": 300.0, "muxed": True, "version": 11, "transcribe_version": 4, "text_version": 11},
    )
    second = episode(
        env,
        "Show B",
        "Season 2",
        "Show B - S02E03",
        stages={"repair": {"outcome": "backend-unreachable", "at": 200.0, "detail": "503"}},
    )
    return warm(env, [first, second])


def test_history_returns_deliveries_and_stage_outcomes_newest_first(env):
    _history_fixture(env)
    status, payload = read(env, "/api/v1/history", headers={"X-Api-Key": KEY})
    assert status == 200
    kinds = [event["kind"] for event in payload["events"]]
    assert kinds.count("delivery") == 1 and kinds.count("stage") == 3
    assert payload["total"] == 4 and payload["scanned"] == 2 and payload["truncated"] is False
    stamps = [event["at"] for event in payload["events"]]
    assert stamps == sorted(stamps, reverse=True), "newest first"
    delivery = next(e for e in payload["events"] if e["kind"] == "delivery")
    assert delivery["mode"] == "mux" and delivery["muxed"] is True and delivery["text_version"] == 11
    stage = next(e for e in payload["events"] if e.get("outcome") == "backend-unreachable")
    assert (stage["stage"], stage["show"], stage["detail"]) == ("repair", "Show B", "503")
    assert all(set(e) >= {"kind", "at", "stem", "show", "season", "episode"} for e in payload["events"])
    assert "at_ts" not in json.dumps(payload), "the sort helper stays internal"


def test_history_pages_with_limit_and_offset(env):
    _history_fixture(env)
    _, whole = read(env, "/api/v1/history", headers={"X-Api-Key": KEY})
    _, first = read(env, "/api/v1/history", {"limit": "2"}, headers={"X-Api-Key": KEY})
    _, second = read(env, "/api/v1/history", {"limit": "2", "offset": "2"}, headers={"X-Api-Key": KEY})
    assert (first["limit"], first["offset"], first["has_more"]) == (2, 0, True)
    assert (second["limit"], second["offset"], second["has_more"]) == (2, 2, False)
    assert first["events"] + second["events"] == whole["events"]
    _, tail = read(env, "/api/v1/history", {"offset": "99"}, headers={"X-Api-Key": KEY})
    assert tail["events"] == [] and tail["has_more"] is False and tail["total"] == 4


def test_history_defaults_are_bounded(env):
    _history_fixture(env)
    _, payload = read(env, "/api/v1/history", headers={"X-Api-Key": KEY})
    assert (payload["limit"], payload["offset"]) == (review_api_v1.DEFAULT_HISTORY_LIMIT, 0)


@pytest.mark.parametrize(
    "query,message",
    [
        ({"limit": "0"}, "limit must be at least 1"),
        ({"limit": "201"}, "limit must be at most 200"),
        ({"limit": "many"}, "limit must be an integer"),
        ({"limit": "1.5"}, "limit must be an integer"),
        ({"offset": "-1"}, "offset must be at least 0"),
        ({"offset": "later"}, "offset must be an integer"),
    ],
)
def test_history_rejects_out_of_bounds_paging(env, query, message):
    _history_fixture(env)
    assert read(env, "/api/v1/history", query, headers={"X-Api-Key": KEY}) == (400, {"error": message})


def test_history_reports_a_cold_cache(env, monkeypatch):
    monkeypatch.setattr(rs, "_STEMS_CACHE", None)
    status, payload = read(env, "/api/v1/history", headers={"X-Api-Key": KEY})
    assert (status, payload["error"]) == (503, "cache warming")


# --- command -----------------------------------------------------------------------------------------------------------


def test_run_request_command_mirrors_post_api_requests(env):
    request = {"show": "Show A", "season": 1, "episode_re": "E01"}
    status, payload = command(env, "run.request", request)
    assert status == 201
    assert payload["name"] == "run.request" and payload["ok"] is True
    assert payload["result"]["status"] == "pending"
    queued = list((env / "requests").glob("*.json"))
    assert len(queued) == 1 and json.loads(queued[0].read_text())["show"] == "Show A"
    duplicate = command(env, "run.request", request)
    assert duplicate[0] == 409 and duplicate[1]["ok"] is False and duplicate[1]["result"]["existing_id"]


@pytest.mark.parametrize(
    "params,status",
    [
        ({"show": "Nope"}, 400),
        ({"show": "Show A", "season": 0}, 400),
        ({"show": "Show A", "nonsense": 1}, 400),
        ({}, 400),
    ],
)
def test_run_request_command_keeps_the_request_validation(env, params, status):
    body = write(env, "POST", "/api/v1/command", {"name": "run.request", "params": params}, session("root"))
    assert body[0] == status and body[1]["ok"] is False and body[1]["error"]


def test_system_restart_command_writes_the_soft_stop_flag(env):
    status, payload = write(env, "POST", "/api/v1/command", {"name": "system.restart", "params": {}}, session("root"))
    assert status == 202 and payload["ok"] is True and payload["result"]["boot_id"] == common.boot_id()
    assert os.path.exists(env / "stop.flag")


def test_notification_test_command_uses_the_settings_path(env, monkeypatch):
    save_provider(env, token=SECRET)
    seen = []

    def fake_test(name, config_dir=None):
        seen.append(name)
        return {"name": name, "ok": True, "detail": ""}

    monkeypatch.setattr(settings_api.notify, "test_provider", fake_test)
    status, payload = command(env, "notification.test", {"name": "ntfy-main"})
    assert (status, payload["ok"]) == (200, True)
    assert seen == ["ntfy-main"] and SECRET not in json.dumps(payload)
    unknown = command(env, "notification.test", {"name": "nobody"})
    assert unknown[0] == 400 and unknown[1]["ok"] is False


def test_unknown_command_and_bad_shapes_are_refused(env):
    refused = command(env, "library.rescan")
    expected = {
        "name": "library.rescan",
        "ok": False,
        "error": "unknown command: library.rescan",
        "commands": list(review_api_v1.COMMANDS),
    }
    assert refused == (400, expected)
    assert command(env, "run.request", [])[0] == 400
    assert write(env, "POST", "/api/v1/command", {"params": {}}, session("root"))[0] == 400
    assert write(env, "POST", "/api/v1/command", {"name": "run.request", "extra": 1}, session("root"))[0] == 400


# --- notifications -----------------------------------------------------------------------------------------------------


def test_notification_crud_masks_secrets_and_keeps_them_until_cleared(env):
    created = save_provider(env, token=SECRET)
    assert created["name"] == "ntfy-main" and created["token"] == {"set": True}
    assert SECRET not in json.dumps(created)

    listed = read(env, "/api/v1/notifications", headers={"X-Api-Key": KEY})[1]
    assert [n["name"] for n in listed["notifications"]] == ["ntfy-main"]
    assert SECRET not in json.dumps(listed)
    assert read(env, "/api/v1/notification/ntfy-main", headers={"X-Api-Key": KEY})[1]["notification"]["topic"] == "dub"

    updated = write(env, "PUT", "/api/v1/notification/ntfy-main", {"enabled": False}, session("root"))
    assert updated[0] == 200 and updated[1]["notification"]["enabled"] is False
    assert updated[1]["notification"]["token"] == {"set": True}, "an absent secret keeps the stored one"
    assert SECRET not in json.dumps(updated)

    cleared = write(env, "PUT", "/api/v1/notification/ntfy-main", {"token": {"clear": True}}, session("root"))
    assert cleared[1]["notification"]["token"] == {"set": False}
    assert settings_store.load(os.environ).notif_secrets.get("ntfy-main", {}).get("token") is None

    deleted = write(env, "DELETE", "/api/v1/notification/ntfy-main", {}, session("root"))
    assert deleted[0] == 200 and deleted[1]["ok"] is True and deleted[1]["name"] == "ntfy-main"
    assert read(env, "/api/v1/notifications", headers={"X-Api-Key": KEY})[1] == {"notifications": []}
    assert write(env, "DELETE", "/api/v1/notification/ntfy-main", {}, session("root"))[0] == 404
    assert read(env, "/api/v1/notification/ntfy-main", headers={"X-Api-Key": KEY})[0] == 404


def test_notification_create_is_validated_and_audited(env, capsys):
    invalid = {"name": "bad name!", "kind": "pager", "triggers": ["nope"]}
    status, payload = write(env, "POST", "/api/v1/notifications", invalid, session("root"))
    assert status == 400
    assert set(payload["errors"]) == {
        "notifications.bad name!.name",
        "notifications.bad name!.kind",
        "notifications.bad name!.triggers",
    }
    save_provider(env)
    again = {"name": "ntfy-main", "kind": "ntfy", "server": "https://x.test", "topic": "t"}
    duplicate = write(env, "POST", "/api/v1/notifications", again, session("root"))
    assert duplicate[:2] == (409, {"error": "a notification with that name already exists"})
    saved = capsys.readouterr().out
    assert "settings saved by" in saved and "notification:ntfy-main" in saved
    assert SECRET not in saved


def test_notification_put_cannot_rename(env):
    save_provider(env)
    assert write(env, "PUT", "/api/v1/notification/ntfy-main", {"name": "other"}, session("root"))[0] == 400
    assert write(env, "PUT", "/api/v1/notification/absent", {"enabled": True}, session("root"))[0] == 404


def test_a_discord_provider_stores_its_url_as_a_secret(env):
    created = save_provider(env, name="disc", kind="discord", url="https://discord.example.test/api/webhooks/1/abc")
    assert created["url"] == {"set": True} and "server" not in created
    assert "discord.example.test" not in json.dumps(created), "the webhook URL is credential-bearing"
    rotated = write(
        env, "PUT", "/api/v1/notification/disc", {"kind": "webhook", "url": "https://hook.example.test/x"}, session("root")
    )
    assert rotated[1]["notification"]["kind"] == "webhook"
    assert rotated[1]["notification"]["url"] == {"set": True}


# --- settings / apikey ---------------------------------------------------------------------------------------------------


def test_settings_is_the_masked_read_shape(env):
    state = settings_store.load(os.environ)
    state.secret_values["PLEX_TOKEN"] = SECRET
    settings_store.save(state, os.environ)
    status, payload = read(env, "/api/v1/settings", headers={"X-Api-Key": KEY})
    assert status == 200
    assert payload["values"]["PLEX_TOKEN"]["set"] is True
    assert payload["values"]["PLEX_TOKEN"]["source"] == "ui"
    assert "revision" in payload and "connections" in payload and "notifications" in payload
    assert SECRET not in json.dumps(payload)


def test_apikey_info_reports_rotation_state_never_the_key(env):
    status, payload = read(env, "/api/v1/apikey/info", headers={"X-Api-Key": KEY})
    assert status == 200
    assert payload["configured"] is True and payload["header"] == "X-Api-Key"
    assert payload["storage"] == "review-token" and payload["legacy_fallback"] is True
    assert set(payload) == {"configured", "storage", "legacy_fallback", "retired", "rotated_at", "header"}
    assert KEY not in json.dumps(payload)
    review_auth.init_api_key()
    payload = read(env, "/api/v1/apikey/info", headers={"X-Api-Key": KEY})[1]
    assert payload["storage"] == "file" and payload["rotated_at"]
    assert review_auth.api_key() not in json.dumps(payload)


def test_openapi_json_is_served(env):
    status, payload = read(env, "/api/v1/openapi.json", headers={"X-Api-Key": KEY})
    assert status == 200 and payload["openapi"].startswith("3.")
    assert "/api/v1/command" in payload["paths"]


def test_the_api_key_header_is_accepted_on_a_mutation(env):
    assert write(env, "POST", "/api/v1/command", {"name": "system.restart"}, {"X-Api-Key": KEY})[0] == 202


def test_requests_module_still_owns_request_validation(env):
    """The command handler must not grow a second copy of validate(): bad params fail through it."""
    with pytest.raises(review_requests.Refused):
        review_requests.validate({"show": "Show A", "season": -1}, anime_root=os.environ["ANIME_ROOT"])


# --- the HTTP layer ----------------------------------------------------------------------------------------------------


@pytest.fixture
def server(env):
    srv = rs.BoundedHTTPServer(("127.0.0.1", 0), rs.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*srv.server_address, timeout=5)
    raw = None if body is None else json.dumps(body)
    hdrs = dict(headers or {})
    if raw is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=raw, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data or b"{}")


def test_the_http_layer_passes_query_strings_and_json_bodies_to_v1(server, env):
    _history_fixture(env)
    assert call(server, "GET", "/api/v1/history?limit=1&offset=1", headers={"X-Api-Key": KEY})[:2] == (
        200,
        {**read(env, "/api/v1/history", {"limit": "1", "offset": "1"}, headers={"X-Api-Key": KEY})[1]},
    )
    assert call(server, "GET", "/api/v1/system/status") == (401, {"error": "login required"})
    assert call(server, "POST", "/api/v1/command", {"name": "system.restart"}, {"X-Api-Key": KEY})[0] == 202
    missing = call(server, "DELETE", "/api/v1/notification/absent", None, {"X-Api-Key": KEY})
    assert missing == (404, {"error": "no such notification"})
