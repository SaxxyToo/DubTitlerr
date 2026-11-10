"""#39: merge_pass.sh must fail as a CONFIGURATION error when it cannot find its scripts.

A manual run without APP_DIR used to try /scripts/repair.py, record
{"repair": {"outcome": "crashed"}} against the first episode in the loop, and leave that
stem blocked for every later pass until someone cleared the stage with common.clear_stage.
"""

import os
import shutil
import subprocess

MERGE_PASS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "merge_pass.sh")

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def test_merge_pass_defaults_app_to_its_own_directory_and_fails_as_config(tmp_path):
    """Without APP_DIR, a copy of the script names the missing app directory and exits
    before the per-stem loop -- it must not record a 'crashed' repair against the first
    episode it touches (which then blocks later passes)."""
    script = tmp_path / "only-the-script" / "merge_pass.sh"
    script.parent.mkdir()
    shutil.copy(MERGE_PASS, script)
    root = tmp_path / "library"
    root.mkdir()
    (root / "ep01.eng.dubtitles.srt").write_text(SRT)
    env = dict(os.environ)
    env.pop("APP_DIR", None)
    env["MERGE_ROOTS"] = str(root)

    r = subprocess.run(["/bin/sh", str(script)], env=env, cwd=str(root), capture_output=True, text=True, timeout=60)

    assert r.returncode != 0
    assert "repair.py" in (r.stdout + r.stderr)
    assert "APP_DIR" in (r.stdout + r.stderr)
    assert not os.path.exists(str(root / "ep01.dubtitles.stages.json"))
