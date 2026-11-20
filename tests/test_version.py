"""One version, in every place a person or a script can read it.

``dubtitlerr.__version__`` is the single source. This file fails on drift: pyproject, the lockfile,
the /api/v1 status route, the container boot banner, the host-side ``--version`` and the image
labels all have to agree with it. A version that exists in one file nobody reads is not a version.
"""

import os
import re
import subprocess
import sys

import pytest
import tomllib  # both files this reads are TOML; stdlib since 3.11 (ruff files it here: target-version is py39)

import dubtitlerr
from dubtitlerr.review import review_api_v1

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _toml(name):
    with open(os.path.join(REPO, name), "rb") as handle:
        return tomllib.load(handle)


def _shell(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as handle:
        return handle.read()


def _version_block():
    """The container's version resolution, exactly the lines the entrypoint runs."""
    body = _shell("container_run.sh")
    return body[body.index("# >>> version") : body.index("# <<< version")]


def _banner_line(name, needle):
    return next(line for line in _shell(name).splitlines() if needle in line)


def test_the_package_version_is_a_semver():
    assert SEMVER.match(dubtitlerr.__version__), dubtitlerr.__version__


def test_pyproject_resolves_the_version_from_the_package():
    project = _toml("pyproject.toml")["project"]
    if "version" in project:  # a static copy is allowed, but only if it agrees
        assert project["version"] == dubtitlerr.__version__
        return
    assert "version" in project.get("dynamic", []), "pyproject must carry or resolve a version"
    dynamic = _toml("pyproject.toml")["tool"]["setuptools"]["dynamic"]
    assert dynamic["version"]["attr"] == "dubtitlerr.__version__"


def test_the_lockfile_does_not_pin_a_second_version():
    """`uv lock --check` compares the lock against pyproject; this compares it against the code.

    uv stores a project whose version is dynamic with NO version field, so the honest assertion is
    that the lock either omits it or agrees -- a leftover literal here is a second source of truth
    that would survive a bump (it is exactly what the 0.3.0 entry was).
    """
    entries = [package for package in _toml("uv.lock")["package"] if package["name"] == "dubtitlerr"]
    assert len(entries) == 1, entries
    assert entries[0].get("version") in (None, dubtitlerr.__version__), entries[0]


def test_the_api_reports_the_package_version():
    assert review_api_v1._app_version() == dubtitlerr.__version__


def test_the_host_can_print_the_version():
    result = subprocess.run(
        [sys.executable, "-m", "dubtitlerr", "--version"], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"dubtitlerr {dubtitlerr.__version__}"


@pytest.fixture
def shell_env():
    env = {k: v for k, v in os.environ.items() if k not in ("DT_VERSION",)}
    env.update({"APP_DIR": REPO, "PYTHONPATH": REPO})
    return env


def _run_banner(shell_env, script):
    return subprocess.run(["sh", "-c", script], env=shell_env, capture_output=True, text=True, timeout=120)


# The banner lines interpolate the entrypoint's own defaults; set them so the test runs the real
# echo line without also running the loop that would consume them.
_BANNER_VARS = "MERGE_INTERVAL=600 RESCAN_INTERVAL=21600 boot_state=paused ORDER=/config/anime_order.txt ANIME=/media\n"


def test_container_boot_banner_prints_the_version(shell_env):
    """Runs the entrypoint's real banner line: a version resolved but never printed is invisible."""
    script = "set -u\n" + _version_block() + "\n" + _BANNER_VARS + _banner_line("container_run.sh", "dubtitle-builder up")
    result = _run_banner(shell_env, script)
    assert result.returncode == 0, result.stderr
    assert f"dubtitlerr {dubtitlerr.__version__}" in result.stdout, result.stdout


def test_gen_loop_boot_line_prints_the_version(shell_env):
    script = "set -u\n" + _version_block() + "\n" + _BANNER_VARS + _banner_line("gen_loop.sh", "gen_loop up")
    result = _run_banner(shell_env, script)
    assert result.returncode == 0, result.stderr
    assert f"dubtitlerr {dubtitlerr.__version__}" in result.stdout, result.stdout


@pytest.mark.parametrize("name", ["Dockerfile.builder", "Dockerfile.lite"])
def test_each_image_labels_the_version_and_revision(name):
    body = _shell(name)
    assert "ARG DUBTITLERR_VERSION" in body
    assert "ARG VCS_REF" in body
    assert 'org.opencontainers.image.version="${DUBTITLERR_VERSION}"' in body
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in body
    # A literal version in an image is a second source of truth: it would survive a bump.
    assert dubtitlerr.__version__ not in body
