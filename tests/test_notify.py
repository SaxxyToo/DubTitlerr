"""Tests for dubtitlerr.core.notify: config, provider rendering, URL guards, retries and the CLI.

No test reaches the network. ``notify._urlopen`` is the module's single transport seam and every
test replaces it with a queued fake; the subprocess test only imports the module (under ``-S``,
which removes site-packages, so pysubs2 cannot exist there).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from dubtitlerr.core import notify

REPO = Path(__file__).resolve().parent.parent

# Placeholders only: nothing here is a live credential.
DISCORD_SECRET = "https://discord.example/api/webhooks/1234/placeholder-DISCORD-TOKEN"
NTFY_TOKEN = "placeholder-NTFY-TOKEN"
WEBHOOK_SECRET = "https://hooks.example/notify/placeholder-WEBHOOK-TOKEN"


class _Response:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    """Callable standing in for notify._urlopen: one queued outcome per attempt."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)


def http_error(code, url="https://example.invalid/"):
    return urllib.error.HTTPError(url, code, f"status {code}", None, None)


def write_config(config_dir, providers, secrets=None):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({"notifications": providers}))
    if secrets is not None:
        (config_dir / "secrets.json").write_text(json.dumps({"notifications": secrets}))


def provider(kind="discord", name="disc", **fields):
    item = {"name": name, "kind": kind, "enabled": True, "triggers": ["sweep.completed"]}
    item.update(fields)
    return item


def install(monkeypatch, *outcomes):
    transport = _Transport(*outcomes)
    monkeypatch.setattr(notify, "_urlopen", transport)
    return transport


# --- configuration -------------------------------------------------------------------------------------------------------


def test_missing_config_is_a_silent_noop(tmp_path, monkeypatch, capsys):
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path / "absent") == []
    assert transport.calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_empty_notifications_list_is_a_noop(tmp_path, monkeypatch):
    write_config(tmp_path, [])
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path) == []
    assert transport.calls == []


def test_disabled_provider_is_skipped(tmp_path, monkeypatch):
    write_config(tmp_path, [provider(enabled=False)], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path) == []
    assert transport.calls == []


def test_unknown_kind_is_skipped_and_logged(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider(kind="slack")], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path) == []
    assert transport.calls == []
    err = capsys.readouterr().err
    assert "disc" in err
    assert "unknown kind" in err


def test_trigger_must_include_the_event(tmp_path, monkeypatch):
    write_config(tmp_path, [provider(triggers=["merge.completed"])], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path) == []
    assert transport.calls == []


def test_emit_rejects_an_unknown_event_without_raising(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    assert notify.emit("not.an.event", "nope", config_dir=tmp_path) == []
    assert transport.calls == []
    assert "unknown event" in capsys.readouterr().err


def test_malformed_settings_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    (tmp_path / "settings.json").write_text("{not json")
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path) == []
    assert transport.calls == []
    assert "settings.json" in capsys.readouterr().err


# --- provider rendering --------------------------------------------------------------------------------------------------


def test_discord_posts_content_and_suppresses_mentions(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    results = notify.emit("sweep.completed", "3 produced, 0 failed", config_dir=tmp_path)
    assert results == [{"name": "disc", "ok": True, "detail": ""}]
    req = transport.calls[0]
    assert req.full_url == DISCORD_SECRET
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"content": "3 produced, 0 failed", "allowed_mentions": {"parse": []}}
    assert req.headers["Content-type"] == "application/json"


def test_ntfy_posts_summary_with_event_headers_and_priority(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        [
            provider(
                kind="ntfy",
                name="phone",
                triggers=["pass.failed"],
                server="http://ntfy.lan:2586/",
                topic="dubtitlerr",
                tags=["rotating_light", "floppy_disk"],
            )
        ],
        {"phone": {"token": NTFY_TOKEN}},
    )
    transport = install(monkeypatch, 200)
    results = notify.emit("pass.failed", "generate stalled", config_dir=tmp_path)
    assert results == [{"name": "phone", "ok": True, "detail": ""}]
    req = transport.calls[0]
    assert req.full_url == "http://ntfy.lan:2586/dubtitlerr"
    assert req.data == b"generate stalled"
    assert req.headers["Title"] == "pass.failed"
    assert req.headers["Priority"] == "5"
    assert req.headers["Tags"] == "rotating_light,floppy_disk"
    assert req.headers["Authorization"] == f"Bearer {NTFY_TOKEN}"


def test_ntfy_default_priority_and_no_auth_without_token(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        [provider(kind="ntfy", name="phone", server="https://ntfy.sh", topic="dubtitlerr")],
    )
    transport = install(monkeypatch, 200)
    notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    req = transport.calls[0]
    assert req.headers["Priority"] == "default"
    assert "Authorization" not in req.headers


def test_webhook_posts_the_envelope_and_no_version(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        [provider(kind="webhook", name="hook", triggers=["merge.completed"])],
        {"hook": {"url": WEBHOOK_SECRET}},
    )
    transport = install(monkeypatch, 200)
    notify.emit(
        "merge.completed",
        "12 delivered, 3 skipped",
        counts={"delivered": 12, "skipped": 3},
        loop="merge",
        host="omarchy",
        detail={"plex": True},
        config_dir=tmp_path,
    )
    envelope = json.loads(transport.calls[0].data)
    assert envelope["event"] == "merge.completed"
    assert isinstance(envelope["at"], float)
    assert envelope["host"] == "omarchy"
    assert envelope["loop"] == "merge"
    assert envelope["summary"] == "12 delivered, 3 skipped"
    assert envelope["counts"] == {"delivered": 12, "skipped": 3}
    assert envelope["detail"] == {"plex": True}
    assert "version" not in envelope  # the repo exposes no runtime version to report


def test_missing_secret_fails_without_raising_or_calling_transport(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider()])
    transport = install(monkeypatch, 200)
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    assert results == [{"name": "disc", "ok": False, "detail": "missing url"}]
    assert transport.calls == []
    assert "missing url" in capsys.readouterr().err


def test_unserialisable_detail_fails_without_raising(tmp_path, monkeypatch):
    write_config(tmp_path, [provider(kind="webhook", name="hook")], {"hook": {"url": WEBHOOK_SECRET}})
    transport = install(monkeypatch, 200)
    results = notify.emit("sweep.completed", "3 produced", detail={"bad": object()}, config_dir=tmp_path)
    assert results[0]["ok"] is False
    assert transport.calls == []


# --- URL guards -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_url",
    [
        "file:///etc/passwd",
        "ftp://hooks.example/notify",
        "https://user:password@hooks.example/notify",
        "https://hooks.example@evil.example/notify",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/notify",
        "http://100.100.100.200/latest",
        "http://[fd00:ec2::254]/latest",
    ],
)
def test_refused_urls_never_reach_the_transport(tmp_path, monkeypatch, capsys, secret_url):
    write_config(tmp_path, [provider(kind="webhook", name="hook")], {"hook": {"url": secret_url}})
    transport = install(monkeypatch, 200)
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    assert results[0]["ok"] is False
    assert results[0]["detail"].startswith("refused:")
    assert transport.calls == []
    assert secret_url not in capsys.readouterr().err


def test_loopback_and_lan_urls_are_allowed(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        [provider(kind="webhook", name="hook")],
        {"hook": {"url": "http://192.168.1.10:8842/notify"}},
    )
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)[0]["ok"] is True
    assert transport.calls[0].full_url == "http://192.168.1.10:8842/notify"

    write_config(tmp_path, [provider()], {"disc": {"url": "http://127.0.0.1:8842/hook"}})
    transport = install(monkeypatch, 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)[0]["ok"] is True


def test_redirect_handler_refuses_a_metadata_redirect():
    handler = notify._GuardedRedirect()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest")


# --- transport failures ---------------------------------------------------------------------------------------------------


def test_retries_once_on_5xx_then_succeeds(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, http_error(500), 200)
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    assert results == [{"name": "disc", "ok": True, "detail": ""}]
    assert len(transport.calls) == 2
    err = capsys.readouterr().err
    assert err.count("notify: disc sweep.completed") == 2
    assert "HTTP 500" in err


def test_retries_once_on_timeout_then_succeeds(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, TimeoutError("timed out"), 200)
    assert notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)[0]["ok"] is True
    assert len(transport.calls) == 2


def test_retries_once_on_url_error_then_gives_up(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, urllib.error.URLError("down"), urllib.error.URLError("down"))
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)  # must not raise
    assert results == [{"name": "disc", "ok": False, "detail": "network error"}]
    assert len(transport.calls) == 2
    assert "network error" in capsys.readouterr().err


def test_4xx_is_not_retried(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, http_error(403))
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    assert results == [{"name": "disc", "ok": False, "detail": "HTTP 403"}]
    assert len(transport.calls) == 1


def test_unexpected_exception_is_swallowed(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})

    def boom(req, timeout=None):
        raise RuntimeError("placeholder secret in message: " + DISCORD_SECRET)

    monkeypatch.setattr(notify, "_urlopen", boom)
    results = notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    assert results == [{"name": "disc", "ok": False, "detail": "RuntimeError"}]


def test_secrets_never_reach_logs(tmp_path, monkeypatch, capsys):
    write_config(
        tmp_path,
        [provider(), provider(kind="ntfy", name="phone", server="https://ntfy.sh", topic="dubtitlerr")],
        {"disc": {"url": DISCORD_SECRET}, "phone": {"token": NTFY_TOKEN}},
    )
    transport = install(monkeypatch, http_error(500), http_error(500), http_error(500), http_error(500))
    notify.emit("sweep.completed", "3 produced", config_dir=tmp_path)
    captured = capsys.readouterr()
    assert DISCORD_SECRET not in captured.out + captured.err
    assert "DISCORD-TOKEN" not in captured.out + captured.err
    assert NTFY_TOKEN not in captured.out + captured.err
    assert len(transport.calls) == 4


# --- CLI ------------------------------------------------------------------------------------------------------------------


def test_cli_send_uses_the_config_dir_and_posts(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    transport = install(monkeypatch, 200)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    code = notify.main(
        [
            "send",
            "--event",
            "sweep.completed",
            "--summary",
            "3 produced",
            "--loop",
            "gen",
            "--counts",
            '{"produced": 3}',
        ]
    )
    assert code == 0
    assert json.loads(transport.calls[0].data) == {
        "content": "3 produced",
        "allowed_mentions": {"parse": []},
    }
    assert "disc: ok" in capsys.readouterr().out


def test_cli_send_with_no_configuration_is_a_noop(tmp_path, monkeypatch):
    transport = install(monkeypatch, 200)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "missing"))
    assert notify.main(["send", "--event", "sweep.completed", "--summary", "3 produced"]) == 0
    assert transport.calls == []


def test_cli_send_exits_zero_when_delivery_fails(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    install(monkeypatch, http_error(500), http_error(500))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert notify.main(["send", "--event", "sweep.completed", "--summary", "3 produced"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "--event", "not.an.event", "--summary", "x"],
        ["send", "--event", "sweep.completed", "--summary", "x", "--counts", "not json"],
        ["send", "--event", "sweep.completed", "--summary", "x", "--detail", "[1, 2]"],
    ],
)
def test_cli_usage_errors_exit_two(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        notify.main(argv)
    assert excinfo.value.code == 2


def test_cli_test_sends_to_a_disabled_provider_without_triggers(tmp_path, monkeypatch, capsys):
    write_config(
        tmp_path,
        [provider(enabled=False, triggers=[])],
        {"disc": {"url": DISCORD_SECRET}},
    )
    transport = install(monkeypatch, 200)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert notify.main(["test", "--name", "disc"]) == 0
    payload = json.loads(transport.calls[0].data)
    assert "test" in payload["content"].lower()
    assert "disc: ok" in capsys.readouterr().out


def test_cli_test_unknown_provider_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        notify.main(["test", "--name", "nope"])
    assert excinfo.value.code == 2


def test_cli_test_delivery_failure_still_exits_zero(tmp_path, monkeypatch):
    write_config(tmp_path, [provider()], {"disc": {"url": DISCORD_SECRET}})
    install(monkeypatch, http_error(500), http_error(500))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert notify.main(["test", "--name", "disc"]) == 0


# --- the minimal app-dir rule ---------------------------------------------------------------------------------------------


def test_module_imports_and_runs_without_pysubs2(tmp_path):
    app = tmp_path / "app"
    (app / "dubtitlerr" / "core").mkdir(parents=True)
    shutil.copy(REPO / "dubtitlerr" / "__init__.py", app / "dubtitlerr" / "__init__.py")
    shutil.copy(REPO / "dubtitlerr" / "core" / "__init__.py", app / "dubtitlerr" / "core" / "__init__.py")
    shutil.copy(REPO / "dubtitlerr" / "core" / "notify.py", app / "dubtitlerr" / "core" / "notify.py")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(app),
        "CONFIG_DIR": str(tmp_path / "missing"),
    }
    # -S removes site-packages: pysubs2 cannot be imported even if the host has it installed.
    imported = subprocess.run(
        [sys.executable, "-S", "-c", "import dubtitlerr.core.notify as n; print(','.join(n.EVENTS))"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    assert "sweep.completed" in imported.stdout

    ran = subprocess.run(
        [sys.executable, "-S", "-m", "dubtitlerr.core.notify", "send", "--event", "sweep.completed", "--summary", "no config"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
