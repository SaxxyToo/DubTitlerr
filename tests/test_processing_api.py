"""The processing controls on both API surfaces and the status read.

Classic routes (`/api/system/processing`, `/api/system/start|pause|stop`) are the Svelte UI's
backend; `/api/v1` gets the additive `system.start`, `system.pause`, `system.stop` commands.
Both write the same persisted file through processing_state.write, so who/when is one story.
"""

import json

from dubtitlerr.review import review_api_v1
from dubtitlerr.review import review_server as rs
from dubtitlerr.settings import settings_api


def _env(tmp_path, **extra):
    return {"CONFIG_DIR": str(tmp_path), **extra}


def test_classic_read_defaults_to_paused_without_a_file(tmp_path):
    status, payload = settings_api.handle("GET", "/api/system/processing", environ=_env(tmp_path))
    assert status == 200 and payload["state"] == "paused" and payload["default"] is True


def test_classic_start_pause_stop_write_the_persisted_state(tmp_path):
    environ = _env(tmp_path)
    for route, state in (("/api/system/start", "running"), ("/api/system/pause", "paused"), ("/api/system/stop", "stopped")):
        status, payload = settings_api.handle("POST", route, {}, environ=environ, who="user:alice")
        assert status == 202 and payload["state"] == state and payload["by"] == "user:alice"
        assert json.loads((tmp_path / "processing.json").read_text())["state"] == state


def test_classic_controls_are_admin_routes_and_the_read_is_a_user_route(tmp_path):
    assert settings_api.ROLE_ENTRIES[("GET", "/api/system/processing")] == "user"
    for route in ("/api/system/start", "/api/system/pause", "/api/system/stop"):
        assert settings_api.ROLE_ENTRIES[("POST", route)] == "admin"


def test_classic_stop_also_writes_the_soft_stop_flag(tmp_path):
    flag = tmp_path / "stop"
    status, _ = settings_api.handle("POST", "/api/system/stop", {}, environ=_env(tmp_path, STOP_FLAG=str(flag)))
    assert status == 202 and flag.exists()


def test_settings_read_carries_the_processing_state_for_the_ui(tmp_path):
    settings_api.handle("POST", "/api/system/pause", {}, environ=_env(tmp_path), who="user:alice")
    status, payload = settings_api.get_settings(_env(tmp_path))
    assert status == 200 and payload["processing"]["state"] == "paused"
    assert payload["processing"]["by"] == "user:alice"


def test_the_v1_commands_are_exposed_and_additive():
    assert {"system.start", "system.pause", "system.stop"} <= set(review_api_v1.COMMANDS)
    assert "system.restart" in review_api_v1.COMMANDS


def test_v1_start_pause_stop_reach_the_same_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    for name, state in (("system.start", "running"), ("system.pause", "paused"), ("system.stop", "stopped")):
        status, payload = review_api_v1.handle_command({"name": name, "params": {}}, who="admin")
        assert status == 202 and payload["ok"] is True and payload["result"]["state"] == state


def test_v1_system_status_reports_the_processing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    review_api_v1.handle_command({"name": "system.pause", "params": {}}, who="admin")
    status, payload = review_api_v1.handle_system_status(snapshot=None)
    assert status == 200 and payload["processing"]["state"] == "paused"


def test_review_server_routes_the_classic_processing_paths(tmp_path, monkeypatch):
    """The routes must be reachable through the real dispatcher, not just settings_api.handle."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("REVIEW_AUTH", "off")
    status, payload = rs.route("GET", "/api/system/processing", {}, None)
    assert status == 200 and payload["state"] == "paused", payload
    status, payload = rs.route("POST", "/api/system/pause", {}, None, headers={"X-DubTitlerr-Request": "1"})
    assert status == 202 and payload["state"] == "paused", payload
