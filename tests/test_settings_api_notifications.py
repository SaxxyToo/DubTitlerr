"""[S-7] Notifications through PUT /api/settings and the Test action: write-only secrets, list replacement, per-field 400s,
and a changed kind or ntfy server dropping the stored secret."""

import json
import os
import stat

import pytest

from dubtitlerr.core import notify as notify_mod
from dubtitlerr.settings import settings_api as api
from dubtitlerr.settings import settings_store as store

DISCORD_URL = "https://discord.com/api/webhooks/1/abcdefghijklmnop"  # gitleaks:allow -- test fixture
WEBHOOK_URL = "https://hooks.example.test/notify/abcdefgh"  # gitleaks:allow -- test fixture
TOKEN = "ntfy-token-0123456789"  # gitleaks:allow -- test fixture

DISCORD = {"name": "chat", "kind": "discord", "enabled": True, "triggers": ["sweep.completed"], "url": DISCORD_URL}
NTFY = {
    "name": "phone",
    "kind": "ntfy",
    "enabled": True,
    "triggers": ["pass.failed", "review.pending"],
    "server": "https://ntfy.example.test",
    "topic": "dubtitlerr",
    "token": TOKEN,
}
WEBHOOK = {"name": "hook", "kind": "webhook", "enabled": False, "triggers": [], "url": WEBHOOK_URL}


@pytest.fixture
def env(tmp_path):
    return {"CONFIG_DIR": str(tmp_path), "EFFECTIVE_ENV_FILE": str(tmp_path / "effective.env")}


@pytest.fixture(autouse=True)
def login_on(monkeypatch):
    monkeypatch.setattr(api, "_secrets_writable", lambda: True)


def put(env, **body):
    if "revision" not in body:
        body["revision"] = store.load(env).rev
    return api.handle("PUT", "/api/settings", body, environ=env)


def listed(env):
    return {n["name"]: n for n in api.handle("GET", "/api/settings", environ=env)[1]["notifications"]}


def strip_secrets(item):
    return {k: v for k, v in item.items() if k not in ("url", "token")}


def test_a_notification_is_created_and_its_secret_is_only_a_flag(env, tmp_path):
    status, payload = put(env, notifications=[DISCORD, NTFY, WEBHOOK])
    assert status == 200
    assert payload["changed"] == ["notification:chat", "notification:hook", "notification:phone"]
    got = api.handle("GET", "/api/settings", environ=env)[1]
    assert [n["name"] for n in got["notifications"]] == ["chat", "phone", "hook"]
    assert listed(env)["chat"]["url"] == {"set": True}
    assert listed(env)["phone"]["token"] == {"set": True}
    assert listed(env)["phone"]["server"] == "https://ntfy.example.test"
    assert listed(env)["hook"]["url"] == {"set": True}
    body = json.dumps(got)
    for secret in (DISCORD_URL, WEBHOOK_URL, TOKEN):
        assert secret not in body
    assert stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode) == 0o600
    settings_text = (tmp_path / "settings.json").read_text()
    secrets_text = (tmp_path / "secrets.json").read_text()
    for secret in (DISCORD_URL, WEBHOOK_URL, TOKEN):
        assert secret not in settings_text
        assert secret in secrets_text


def test_the_files_match_the_shape_notify_py_reads(env, tmp_path):
    put(env, notifications=[DISCORD, NTFY])
    settings = json.loads((tmp_path / "settings.json").read_text())
    secrets = json.loads((tmp_path / "secrets.json").read_text())
    assert [n["name"] for n in settings["notifications"]] == ["chat", "phone"]
    assert settings["notifications"][1]["server"] == "https://ntfy.example.test"
    assert "url" not in settings["notifications"][0]
    assert secrets["notifications"] == {"chat": {"url": DISCORD_URL}, "phone": {"token": TOKEN}}
    providers, loaded = notify_mod._load(str(tmp_path))
    assert [p["name"] for p in providers] == ["chat", "phone"]
    assert loaded["phone"]["token"] == TOKEN


def test_a_changed_kind_without_a_new_secret_drops_the_stored_one(env):
    put(env, notifications=[DISCORD])
    switched = {"name": "chat", "kind": "webhook", "enabled": True, "triggers": [], "url": WEBHOOK_URL}
    assert put(env, notifications=[switched])[0] == 200
    assert store.load(env).notif_secrets == {"chat": {"url": WEBHOOK_URL}}
    # A kind change with no secret in the same save leaves nothing stored.
    put(env, notifications=[DISCORD])
    assert put(env, notifications=[{"name": "chat", "kind": "webhook", "enabled": True, "triggers": []}])[0] == 200
    assert store.load(env).notif_secrets == {}


def test_a_changed_ntfy_server_drops_the_token_unless_a_new_token_arrives(env):
    put(env, notifications=[NTFY])
    moved = {**NTFY, "server": "https://ntfy.other.test", "token": None}
    assert put(env, notifications=[moved])[0] == 200
    assert store.load(env).notif_secrets == {}
    assert listed(env)["phone"]["token"] == {"set": False}

    put(env, notifications=[NTFY])
    moved = {**NTFY, "server": "https://ntfy.other.test", "token": "new-token-abcdef"}  # gitleaks:allow
    assert put(env, notifications=[moved])[0] == 200
    assert store.load(env).notif_secrets == {"phone": {"token": "new-token-abcdef"}}


def test_an_unchanged_notification_without_a_secret_field_keeps_its_secret(env):
    put(env, notifications=[NTFY])
    kept = {k: v for k, v in NTFY.items() if k != "token"} | {"triggers": ["merge.completed"]}
    assert put(env, notifications=[kept])[0] == 200
    assert store.load(env).notif_secrets == {"phone": {"token": TOKEN}}


def test_a_secret_can_be_cleared_and_a_deleted_notification_takes_it_along(env):
    put(env, notifications=[NTFY, DISCORD])
    assert put(env, notifications=[{**NTFY, "token": {"clear": True}}, DISCORD])[0] == 200
    assert store.load(env).notif_secrets == {"chat": {"url": DISCORD_URL}}
    put(env, notifications=[NTFY, DISCORD])
    assert put(env, notifications=[DISCORD])[0] == 200
    assert store.load(env).notif_secrets == {"chat": {"url": DISCORD_URL}}
    assert list(listed(env)) == ["chat"]


def test_a_link_local_url_is_refused_with_400_naming_the_field(env, tmp_path):
    status, payload = put(env, notifications=[{**DISCORD, "url": "http://169.254.169.254/latest"}])
    assert status == 400 and "link-local" in payload["errors"]["notifications.chat.url"]
    assert not (tmp_path / "settings.json").exists()


def test_field_validation_names_the_provider_and_the_field(env):
    status, payload = put(env, notifications=[{**NTFY, "name": "9 bad name"}])
    assert status == 400 and "letters" in payload["errors"]["notifications.9 bad name.name"]
    status, payload = put(env, notifications=[{**NTFY, "kind": "telegram"}])
    assert status == 400 and "discord, ntfy or webhook" in payload["errors"]["notifications.phone.kind"]
    status, payload = put(env, notifications=[{**NTFY, "triggers": ["sweep.completed", "nope"]}])
    assert status == 400 and "unknown trigger" in payload["errors"]["notifications.phone.triggers"]
    status, payload = put(env, notifications=[{**NTFY, "triggers": "sweep.completed"}])
    assert status == 400 and "list" in payload["errors"]["notifications.phone.triggers"]
    status, payload = put(env, notifications=[{**NTFY, "topic": "bad topic!"}])
    assert status == 400 and "letters, digits" in payload["errors"]["notifications.phone.topic"]
    status, payload = put(env, notifications=[{**DISCORD, "server": "https://x.test"}])
    assert status == 400 and "not used by a discord" in payload["errors"]["notifications.chat.server"]
    status, payload = put(env, notifications=[{**DISCORD, "surprise": 1}])
    assert status == 400 and "unknown field" in payload["errors"]["notifications.chat.notification"]


def test_a_discord_link_url_is_visible_kept_in_settings_and_validated(env, tmp_path):
    status, _ = put(env, notifications=[{**DISCORD, "link_url": "https://dub.example.test/review"}])
    assert status == 200
    assert listed(env)["chat"]["link_url"] == "https://dub.example.test/review"
    assert "dub.example.test" in (tmp_path / "settings.json").read_text()
    assert "dub.example.test" not in (tmp_path / "secrets.json").read_text()

    status, payload = put(env, notifications=[{**DISCORD, "link_url": "http://169.254.169.254/latest"}])
    assert status == 400 and "link-local" in payload["errors"]["notifications.chat.link_url"]


def test_a_link_url_belongs_to_discord_only(env):
    status, payload = put(env, notifications=[{**NTFY, "link_url": "https://dub.example.test"}])
    assert status == 400 and "not used by a ntfy" in payload["errors"]["notifications.phone.link_url"]


def test_two_notifications_may_not_share_a_name(env):
    status, payload = put(env, notifications=[NTFY, strip_secrets(NTFY)])
    assert status == 400 and "already used" in payload["errors"]["notifications.phone.name"]


def test_a_provider_without_a_secret_is_not_a_secret_write_but_one_with_a_secret_is(env, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_secrets_writable", lambda: False)
    assert put(env, notifications=[strip_secrets(DISCORD)])[0] == 200, "no secret in the payload"
    assert put(env, notifications=[{**NTFY, "token": TOKEN}])[0] == 403
    assert TOKEN not in (tmp_path / "secrets.json").read_text()


def test_the_audit_line_names_providers_and_never_a_secret(env, capsys):
    put(env, notifications=[DISCORD, NTFY])
    out = capsys.readouterr().out
    assert "notification:chat" in out and "notification:phone" in out
    assert "notification:chat.url" in out and "notification:phone.token" in out
    assert "url notification phone=https://ntfy.example.test" in out
    for secret in (DISCORD_URL, TOKEN):
        assert secret not in out
    assert f"url notification chat={DISCORD_URL}" not in out, "a Discord webhook URL is a credential"


def test_the_schema_route_publishes_the_notification_rules(env):
    status, payload = api.handle("GET", "/api/settings/schema", environ=env)
    rules = payload["notifications"]
    assert status == 200
    assert rules["kinds"] == ["discord", "ntfy", "webhook"]
    assert set(rules["triggers"]) == set(notify_mod.EVENTS)
    assert rules["fields"]["ntfy"] == {"visible": ["server", "topic"], "secret": ["token"]}
    assert rules["fields"]["discord"] == {"visible": ["link_url"], "secret": ["url"]}


class Response:
    status = 200

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_test_action_probes_a_saved_provider_and_never_leaks_the_secret(env, monkeypatch):
    put(env, notifications=[NTFY])
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    monkeypatch.setattr(notify_mod, "_urlopen", fake_urlopen)
    status, payload = api.handle("POST", "/api/settings/test", {"notification": "phone"}, environ=env)
    assert status == 200 and payload["ok"] is True and payload["error"] == ""
    assert seen[0][0] == "https://ntfy.example.test/dubtitlerr"
    assert seen[0][1]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(payload)


def test_a_failing_test_reports_ok_false_without_the_secret(env, monkeypatch):
    put(env, notifications=[DISCORD])

    def failing_urlopen(request, timeout=None):
        raise OSError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(notify_mod, "_urlopen", failing_urlopen)
    status, payload = api.handle("POST", "/api/settings/test", {"notification": "chat"}, environ=env)
    assert status == 200 and payload["ok"] is False
    assert DISCORD_URL not in json.dumps(payload)


def test_testing_an_unsaved_provider_is_400(env):
    assert api.handle("POST", "/api/settings/test", {"notification": "ghost"}, environ=env)[0] == 400


def test_the_test_action_is_an_admin_route():
    assert api.ROLE_ENTRIES[("POST", "/api/settings/test")] == "admin"
    assert api.ROLE_ENTRIES[("PUT", "/api/settings")] == "admin"


def test_reset_all_clears_notifications_and_their_secrets(env):
    put(env, notifications=[DISCORD, NTFY])
    status, payload = api.handle("POST", "/api/settings/reset", {"all": True}, environ=env)
    assert status == 200 and payload["ok"] is True
    state = store.load(env)
    assert state.notifications == [] and state.notif_secrets == {}


def test_resetting_the_connect_category_clears_notifications(env):
    put(env, notifications=[DISCORD])
    status, payload = api.handle("POST", "/api/settings/reset", {"category": "connect"}, environ=env)
    assert status == 200 and "notifications" in payload["reset"]
    assert store.load(env).notifications == [] and store.load(env).notif_secrets == {}


def test_an_unusable_stored_provider_is_dropped_on_load_with_a_note(env, tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rev": 1,
                "notifications": [
                    {"name": "bad kind", "kind": "nope", "enabled": True, "triggers": []},
                    {**strip_secrets(DISCORD), "name": "chat"},
                ],
            }
        )
    )
    state = store.load(env)
    assert [n["name"] for n in state.notifications] == ["chat"]
    assert any("notification bad kind ignored" in note for note in state.notes)
