"""The documentation's relative links have to resolve in the tree that ships.

A renamed wiki page leaves a 404 on GitHub and a dead path in the README, and nothing in the
Python suite notices. `scripts/check_docs.py` is the gate; this runs it locally so
`scripts/check.sh` catches the same breakage CI does.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tracked_markdown_relative_links_resolve():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_docs.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
