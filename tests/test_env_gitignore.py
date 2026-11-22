"""`.env.example` is tracked and names every token a deployment needs. A real `.env` beside it is one
`git add -A` away from publishing live credentials to a repository anybody can read, so the file is
ignored -- and this is what keeps it ignored. An ignore rule is invisible by construction, which is
exactly how a live one gets deleted by a tidy-up and nobody notices until a token leaks.

`.env.example` must stay visible in the other direction: it is the documentation a new install follows.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")


def ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0


@pytest.mark.parametrize("path", [".env", ".env.local", ".env.bak-20261122-1656"])
def test_a_local_env_file_is_ignored(path):
    assert ignored(path), f"{path} must stay gitignored: it holds live tokens and this repo is public"


def test_the_env_example_is_tracked_and_not_ignored():
    """Both halves matter: ignoring it would drop the only description of the variables, and leaving
    it merely present but untracked would keep it out of a fresh clone."""
    assert not ignored(".env.example"), ".env.example must not be ignored"
    assert subprocess.run(["git", "ls-files", "--error-unmatch", ".env.example"], cwd=ROOT, capture_output=True).returncode == 0
