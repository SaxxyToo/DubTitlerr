"""[S-5] container_run.sh exports the stored container- and server-scope settings at container start, before its own defaults."""

import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(REPO, "container_run.sh")).read()


def block():
    start = SCRIPT.index("# >>> stored settings")
    end = SCRIPT.index("# <<< stored settings")
    return SCRIPT[start:end]


def run_block(tmp_path, values, env=None, tail=""):
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "values": values}))
    script = (
        "set -u\n"
        f"{block()}\n"
        ': "${MERGE_INTERVAL:=600}"\n'
        'echo "interval=$MERGE_INTERVAL port=${REVIEW_PORT-unset}"'
        ' "boot_port=${DT_BOOT_REVIEW_PORT-unset} window=${MERGE_WINDOW-unset}"\n'
        f"{tail}\n"
    )
    full = {
        k: v for k, v in os.environ.items() if k not in ("MERGE_INTERVAL", "REVIEW_PORT", "MERGE_WINDOW", "DT_BOOT_REVIEW_PORT")
    }
    full.update({"APP_DIR": REPO, "CONFIG_DIR": str(tmp_path), **(env or {})})
    return subprocess.run(["sh", "-c", script], env=full, capture_output=True, text=True, timeout=30)


def test_stored_container_and_server_values_are_exported_before_the_defaults(tmp_path):
    res = run_block(
        tmp_path, {"MERGE_INTERVAL": 120, "REVIEW_PORT": 9000, "MERGE_WINDOW": "22:00-06:00"}, {"REVIEW_PORT": "8000"}
    )
    assert res.stdout.strip() == "interval=120 port=9000 boot_port=8000 window=22:00-06:00", res.stderr


def test_without_stored_values_the_defaults_and_the_container_env_stand(tmp_path):
    res = run_block(tmp_path, {}, {"REVIEW_PORT": "8000"})
    assert res.stdout.strip() == "interval=600 port=8000 boot_port=unset window=unset"


def test_gen_scope_values_are_not_exported_at_container_start(tmp_path):
    res = run_block(tmp_path, {"WHISPER_BEAM_SIZE": 5}, tail='echo "beam=${WHISPER_BEAM_SIZE-unset}"')
    assert res.stdout.splitlines()[-1] == "beam=unset"


def test_the_library_paths_and_media_ids_reach_every_child_from_the_boot_block(tmp_path):
    """S3: these stopped being bootstrap because nothing at boot reads them before settings.json is loaded.

    The boot block is exactly what a stored value needs to reach a reader that caches it at import (review_server.ROOTS,
    common.MEDIA_UID, merge_pass.sh's ROOT), so it has to land before any loop starts — the tail below stands in for one.
    """
    values = {
        "ANIME_ROOT": "/media/Stored Library",
        "MERGE_ROOTS": "/media/Stored Library",
        "MUX_ROOTS": "/media/Stored Library",
        "MEDIA_UID": 1024,
        "MEDIA_GID": 101,
    }
    res = run_block(tmp_path, values, tail='echo "roots=$ANIME_ROOT|$MERGE_ROOTS|$MUX_ROOTS ids=$MEDIA_UID:$MEDIA_GID"')
    assert res.stdout.splitlines()[-1] == "roots=/media/Stored Library|/media/Stored Library|/media/Stored Library ids=1024:101"
    assert "stored settings" in SCRIPT and SCRIPT.index("# <<< stored settings") < SCRIPT.index('sh "$APP_DIR/gen_loop.sh"')


def test_every_former_bootstrap_key_is_exported_by_that_block_and_not_before_it():
    """The claim the reclassification rests on: the export is the step that carries the value, and it runs after the store
    read (which needs CONFIG_DIR alone) and before the first default and the first child."""
    start, end = SCRIPT.index("# >>> stored settings"), SCRIPT.index("# <<< stored settings")
    assert "export --boot --scope container" in SCRIPT[start:end]
    for key in ("ANIME_ROOT", "MERGE_ROOTS", "MUX_ROOTS", "MEDIA_UID", "MEDIA_GID"):
        assert key not in SCRIPT[:start], f"{key} would be read before settings.json can be read: that keeps it bootstrap"


def test_a_missing_exporter_changes_nothing(tmp_path):
    res = run_block(tmp_path, {"MERGE_INTERVAL": 120}, {"APP_DIR": str(tmp_path / "no-app")})
    assert res.stdout.startswith("interval=600") and res.returncode == 0


def test_the_block_sits_before_the_defaults_it_must_override():
    assert SCRIPT.index("# <<< stored settings") < SCRIPT.index(': "${MERGE_INTERVAL:=600}"')


# --- the review server's own secrets: exported inside its subshell only ------------------------------------------------------


def server_block():
    start = SCRIPT.index("# >>> stored server secrets")
    end = SCRIPT.index("# <<< stored server secrets")
    return SCRIPT[start:end]


def run_server_function(tmp_path, secrets, env=None):
    """Run the real run_review_server against a stub review_server.py that records its environment, then report what the parent
    shell and a sibling child (standing in for the merge and generate loops) can see."""
    import shutil

    app = tmp_path / "app"
    app.mkdir()
    for pkg, name in (
        ("review", "review_settings_write.py"),
        ("settings", "settings_registry.py"),
        ("settings", "settings_store.py"),
        ("settings", "settings_connections.py"),
        ("settings", "settings_notifications.py"),
        ("glossary", "glossary_lock.py"),
        ("core", "common.py"),
        ("core", "atomic_file.py"),
        # common re-exports VIDEO_EXTS from this stdlib-only leaf, so importing common in a
        # minimal app dir needs it alongside.
        ("core", "media_types.py"),
        # container_run.sh reports a dying generate loop through the notify CLI; notify must
        # import from this app copy like the image's does (stdlib only, no common/pysubs2).
        ("core", "notify.py"),
    ):
        (app / "dubtitlerr" / pkg).mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(REPO, "dubtitlerr", pkg, name), app / "dubtitlerr" / pkg / name)
    for pkg in ("", "core", "glossary", "review", "settings"):
        shutil.copy(os.path.join(REPO, "dubtitlerr", pkg, "__init__.py"), app / "dubtitlerr" / pkg / "__init__.py")
    dump = tmp_path / "server-env.json"
    stub = "import json, os\nenv = {k: v for k, v in os.environ.items() if 'WEBHOOK_TOKEN' in k}\n"
    (app / "dubtitlerr" / "review" / "review_server.py").write_text(stub + f"open({str(dump)!r}, 'w').write(json.dumps(env))\n")
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "values": {"REVIEW_PORT": 9000}}))
    (tmp_path / "secrets.json").write_text(json.dumps({"version": 1, "values": secrets, "connections": {}}))
    os.chmod(tmp_path / "secrets.json", 0o600)
    script = (
        "set -u\n"
        f"{server_block()}\n"
        "run_review_server &\nwait $!\n"
        'echo "parent=${WEBHOOK_TOKEN-unset}:${DT_BOOT_WEBHOOK_TOKEN-unset}"\n'
        "sh -c 'echo \"child=${WEBHOOK_TOKEN-unset}\"'\n"
    )
    full = {k: v for k, v in os.environ.items() if "WEBHOOK_TOKEN" not in k}
    full.update({"APP_DIR": str(app), "CONFIG_DIR": str(tmp_path), **(env or {})})
    res = subprocess.run(["sh", "-c", script], env=full, cwd=str(app), capture_output=True, text=True, timeout=30)
    return res, json.loads(dump.read_text()) if dump.exists() else None


def test_a_stored_server_secret_reaches_the_review_server_process_only(tmp_path):
    res, seen = run_server_function(tmp_path, {"WEBHOOK_TOKEN": "hook-stored-1"})
    assert res.returncode == 0, res.stderr
    assert seen == {"WEBHOOK_TOKEN": "hook-stored-1", "DT_BOOT_WEBHOOK_TOKEN": ""}
    assert res.stdout.splitlines() == ["parent=unset:unset", "child=unset"]


def test_the_container_secret_is_never_copied_into_a_boot_variable(tmp_path):
    res, seen = run_server_function(tmp_path, {"WEBHOOK_TOKEN": "hook-stored-1"}, {"WEBHOOK_TOKEN": "container-hook-9"})
    assert seen["WEBHOOK_TOKEN"] == "hook-stored-1" and seen["DT_BOOT_WEBHOOK_TOKEN"] == "1"
    assert "container-hook-9" not in json.dumps(seen)
    assert "child=container-hook-9" in res.stdout, "the container's own value is untouched outside the server"


def test_without_a_stored_secret_the_server_keeps_the_container_value(tmp_path):
    res, seen = run_server_function(tmp_path, {}, {"WEBHOOK_TOKEN": "container-hook-9"})
    assert seen == {"WEBHOOK_TOKEN": "container-hook-9"}


def test_a_missing_exporter_still_starts_the_server(tmp_path):
    res, seen = run_server_function(tmp_path, {"WEBHOOK_TOKEN": "hook-stored-1"}, {"WEBHOOK_TOKEN": "container-hook-9"})
    assert seen["WEBHOOK_TOKEN"] == "hook-stored-1"  # sanity: the stub app has the exporter
    app = tmp_path / "app"
    (app / "dubtitlerr" / "review" / "review_settings_write.py").unlink()
    (tmp_path / "server-env.json").unlink()
    script = f"set -u\n{server_block()}\nrun_review_server &\nwait $!\n"
    full = {**os.environ, "APP_DIR": str(app), "CONFIG_DIR": str(tmp_path), "WEBHOOK_TOKEN": "container-hook-9"}
    subprocess.run(["sh", "-c", script], env=full, cwd=str(app), capture_output=True, text=True, timeout=30)
    assert json.loads((tmp_path / "server-env.json").read_text()) == {"WEBHOOK_TOKEN": "container-hook-9"}


def test_the_review_server_is_started_through_that_function_and_the_boot_block_never_names_a_secret_flag():
    assert "run_review_server &" in SCRIPT and 'python3 "$APP_DIR/review_server.py" &' not in SCRIPT
    assert "--secrets-only" not in block() or "--boot" not in block().split("--secrets-only")[0].splitlines()[-1]


def test_the_one_time_migration_runs_before_the_exports_and_its_values_are_exported(tmp_path):
    (tmp_path / "runtime.env").write_text("REQUIRE_ENG=0\n")
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "values": {"MERGE_INTERVAL": 120}}))
    res = run_block(tmp_path, {"MERGE_INTERVAL": 120}, tail='echo "migrated=$(ls "$CONFIG_DIR" | grep -c runtime.env.migrated)"')
    assert res.stdout.splitlines()[-1] == "migrated=1" and "interval=120 port=unset" in res.stdout, res.stderr
    assert SCRIPT.index("review_settings_write migrate") < SCRIPT.index("review_settings_write export")
