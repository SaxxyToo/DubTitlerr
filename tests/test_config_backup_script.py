"""deploy/dubtitlerr-config-backup.sh: the one job that must contain artifacts.db.

Since P39 the config volume holds the ONLY copy of every internal artifact, so this script is
the difference between "restore" and "re-transcribe the library". The dangerous failure modes
pinned here are the quiet ones:

  * a snapshot written inside the tree it snapshots (rsync copies itself);
  * the store copied as a file instead of via ``sqlite3 .backup`` (a live WAL DB tears);
  * ``COPY_TO`` pointing at a local path on the same filesystem -- which succeeds, reads as an
    off-host mirror, and then loses both copies with the disk.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "deploy", "dubtitlerr-config-backup.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("bash") is None,
    reason="the backup script needs rsync and a POSIX shell",
)


def _run(config_dir, backup_dir, copy_to=None):
    env = dict(os.environ)
    env.update({"CONFIG_DIR": str(config_dir), "BACKUP_DIR": str(backup_dir), "RETAIN_DAYS": "14"})
    if copy_to is None:
        env.pop("COPY_TO", None)
    else:
        env["COPY_TO"] = str(copy_to)
    return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)


def _snapshots(backup_dir):
    return sorted(p for p in backup_dir.iterdir() if p.is_dir())


def test_a_snapshot_carries_the_rest_of_the_config_volume(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text('{"OUTPUT_MODE": "sidecar"}\n')
    backup = tmp_path / "backups"

    r = _run(config, backup)
    assert r.returncode == 0, r.stderr
    snaps = _snapshots(backup)
    assert len(snaps) == 1
    assert (snaps[0] / "settings.json").read_text() == '{"OUTPUT_MODE": "sidecar"}\n'
    assert "warning: no" in r.stderr  # the store is absent, and that is said out loud


def test_the_store_goes_through_sqlite_backup_not_a_plain_copy(tmp_path):
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI is what the script uses to snapshot the store")
    config = tmp_path / "config"
    config.mkdir()
    db = config / "artifacts.db"
    subprocess.run(
        [
            "sqlite3",
            str(db),
            "CREATE TABLE artifacts (stem TEXT, kind TEXT, payload BLOB, meta TEXT, updated_at REAL);"
            "INSERT INTO artifacts VALUES ('/media/x/y', 'done', x'00', '{}', 0);",
        ],
        check=True,
    )
    (config / "artifacts.db-wal").write_bytes(b"live wal, must not be mixed into the snapshot")
    backup = tmp_path / "backups"

    r = _run(config, backup)
    assert r.returncode == 0, r.stderr
    snap = _snapshots(backup)[0]
    assert "artifacts.db: 1 row(s)" in r.stdout
    assert (snap / "artifacts.db").exists()
    assert not (snap / "artifacts.db-wal").exists()
    assert not (snap / "artifacts.db-shm").exists()
    rows = subprocess.run(
        ["sqlite3", str(snap / "artifacts.db"), "SELECT COUNT(*) FROM artifacts"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert rows.stdout.strip() == "1"


def test_a_local_copy_to_on_the_same_filesystem_is_called_out(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text("{}\n")
    backup = tmp_path / "backups"
    mirror = tmp_path / "mirror"

    r = _run(config, backup, copy_to=mirror)
    assert r.returncode == 0, r.stderr
    assert mirror.exists()  # the base dir is created, so rsync cannot fail on it
    assert "NOT off-host" in r.stderr
    mirrored = sorted(p for p in mirror.iterdir() if p.is_dir())
    assert len(mirrored) == 1
    assert (mirrored[0] / "settings.json").exists()


def test_no_copy_to_means_no_mirror_warning(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text("{}\n")
    r = _run(config, tmp_path / "backups")
    assert r.returncode == 0, r.stderr
    assert "NOT off-host" not in r.stderr


def test_a_backup_dir_inside_the_config_dir_is_refused(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text("{}\n")
    r = _run(config, config / "backups")
    assert r.returncode == 1
    assert "FATAL" in r.stderr
    assert not (config / "backups").exists()
