"""The container imports from /app, the test suite imports from the repo root, and
nothing in CI builds the image -- so a new top-level module can pass 987 tests and
ImportError on container start. qc.py did exactly that across 33 commits and four
review rounds. This closes the gap between the tested artifact and the shipped one."""

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# modules gen_loop.sh / container_run.sh actually run inside the container (as `python3 -m ...`)
ENTRYPOINTS = [
    "dubtitlerr.pipeline.generate",
    "dubtitlerr.pipeline.repair",
    "dubtitlerr.pipeline.mux",
    "dubtitlerr.glossary.mine_glossary",
    "dubtitlerr.glossary.glossary_verify",
    "dubtitlerr.glossary.glossary_acquire",
    "dubtitlerr.pipeline.recreate_srt",
    "dubtitlerr.pipeline.dub_signs_merge",
    "dubtitlerr.pipeline.plex_refresh",
    "dubtitlerr.pipeline.watch_queue",
    # [S-8/later] merge_pass.sh runs the publish gate before mux (PUBLISH_GATE defaults on), so
    # the gate is an entrypoint the image must carry -- the shell checks it exists, but this
    # list is what proves its imports arrived too (the qc.py failure shape).
    "dubtitlerr.pipeline.publish_gate",
    # [S-8]: the review server is a third container loop, so everything it imports has to
    # be in the image. qc.py passed 987 tests and ImportError'd on container start for
    # exactly this reason.
    "dubtitlerr.review.review_server",
    "dubtitlerr.review.review_users",
    # The shell loops and the container entrypoint run these directly: the settings exporter (imports the registry and the
    # store) and the request helper (writes the effective-env reports).
    "dubtitlerr.review.review_settings_write",
    "dubtitlerr.review.review_requests",
]


def _entry_path(module):
    """'dubtitlerr.pipeline.generate' -> repo-relative 'dubtitlerr/pipeline/generate.py'."""
    return os.path.join(*module.split(".")) + ".py"


def _copied():
    """Repo-relative paths of every .py the builder image's COPY lines bring in.

    Dockerfile.builder copies the whole dubtitlerr/ tree; expand that from the working
    tree so a new module under the package is covered without editing the Dockerfile."""
    with open(os.path.join(ROOT, "Dockerfile.builder")) as f:
        body = f.read().replace("\\\n", " ")
    names = set()
    for line in body.splitlines():
        if not line.startswith("COPY "):
            continue
        names.update(re.findall(r"[\w./-]+\.py", line))
        if re.search(r"COPY\s+dubtitlerr/\s", line):
            for base, _dirs, files in os.walk(os.path.join(ROOT, "dubtitlerr")):
                for fn in files:
                    if fn.endswith(".py"):
                        names.add(os.path.relpath(os.path.join(base, fn), ROOT))
    return {n.lstrip("./") for n in names}


def _local_imports(path):
    """Repo-relative paths of every dubtitlerr module this file imports (any depth)."""
    tree = ast.parse(open(path).read())
    out = set()
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module]
            if node.module.startswith("dubtitlerr"):
                mods += [node.module + "." + a.name for a in node.names]
        for m in mods:
            if not m.startswith("dubtitlerr"):
                continue
            rel = os.path.join(*m.split(".")) + ".py"
            if os.path.exists(os.path.join(ROOT, rel)):
                out.add(rel)
    return out


def test_every_module_an_entrypoint_imports_is_copied_into_the_image():
    copied = _copied()
    missing = {}
    for ep in ENTRYPOINTS:
        p = os.path.join(ROOT, _entry_path(ep))
        if not os.path.exists(p):
            continue
        for dep in _local_imports(p):
            if dep not in copied:
                missing.setdefault(dep, []).append(ep)
    assert not missing, f"not COPY'd into the image but imported at runtime: {missing}"


def test_every_entrypoint_is_itself_copied_into_the_image():
    """The gap the import check leaves. It walks what each entrypoint IMPORTS and never asks
    whether the entrypoint itself is in the COPY line -- so a new entrypoint could be added
    to ENTRYPOINTS, have all its dependencies satisfied, pass, and not exist in the image.
    Found by a mutation: removing review_server.py from the COPY line broke nothing.

    That is the qc.py failure exactly -- 987 tests green, ImportError on container start."""
    copied = _copied()
    absent = [ep for ep in ENTRYPOINTS if os.path.exists(os.path.join(ROOT, _entry_path(ep))) and _entry_path(ep) not in copied]

    assert not absent, f"entrypoints missing from the image: {absent}"


def _dockerfile_arg_default(name):
    """The ARG default Dockerfile.builder bakes for ``name``."""
    with open(os.path.join(ROOT, "Dockerfile.builder")) as f:
        body = f.read().replace("\\\n", " ")
    m = re.search(rf"^ARG\s+{re.escape(name)}=(\S+)\s*$", body, re.M)
    return m.group(1) if m else None


def _module_env_default(module, var, env_key):
    """The fallback ``module`` passes to os.environ.get(env_key) when assigning ``var``.

    Parsed, never imported: generate.py loads faster_whisper at module level."""
    tree = ast.parse(open(os.path.join(ROOT, module)).read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == var for t in node.targets):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and len(call.args) == 2):
            continue
        key = call.args[0]
        if isinstance(key, ast.Constant) and key.value == env_key:
            fallback = call.args[1]
            if isinstance(fallback, ast.Constant):
                return fallback.value
    return None


def test_the_baked_model_and_generates_fallback_cannot_drift():
    """Dockerfile.builder bakes the model its ARG names and exports the same value as the
    container ENV; generate.py's fallback decides what a bare checkout loads. When the two
    disagree, a plain `docker build` produces an image whose baked model is not the one
    generate.py asks for -- and faster-whisper does not error on that, it silently
    re-downloads the missing model into /models on every container start (Dockerfile.builder
    says so in its own comment). Nothing else in the suite compares these two files."""
    baked = _dockerfile_arg_default("WHISPER_MODEL")
    fallback = _module_env_default(_entry_path("dubtitlerr.pipeline.generate"), "MODEL", "WHISPER_MODEL")
    assert baked, "Dockerfile.builder has no ARG WHISPER_MODEL=<default>"
    assert fallback, "generate.py does not default MODEL from WHISPER_MODEL"
    assert baked == fallback, f"baked model {baked!r} != generate.py fallback {fallback!r}"


def _container_run():
    return open(os.path.join(ROOT, "container_run.sh")).read()


def test_container_run_starts_the_review_server_as_a_background_loop():
    """[S-8]. The generate loop is the container's payload and container_run.sh supervises it
    (soft stop: no `exec`, so PID 1 keeps its SIGTERM trap). The review server must never
    be what the supervisor waits on: a sweep killed mid-episode leaves a .dubtitles.fail
    poison marker that must be removed by hand before the episode can be retried."""
    body = _container_run()
    # Executable lines only. Matching the whole file would be satisfied by the word
    # appearing in a COMMENT -- the guard would pass while nothing started the server.
    code = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    # The server is started through run_review_server, which exports the stored server secrets and then execs the server.
    # The function body runs in a backgrounded subshell, so its exec replaces that subshell, never PID 1.
    launches = [i for i, ln in enumerate(code) if ln == "run_review_server &"]
    gen = [i for i, ln in enumerate(code) if "gen_loop.sh" in ln]

    assert launches, "the server has to actually be started, not just mentioned"
    assert "exec python3 -m dubtitlerr.review.review_server" in code, "the function ends by becoming the server"
    assert gen and code[gen[0]].endswith("&"), "the generate loop is started in the background and supervised"
    assert [ln for ln in code if ln.startswith("exec ")] == ["exec python3 -m dubtitlerr.review.review_server"], (
        "exec would drop the SIGTERM trap (soft stop) anywhere but in the backgrounded server function"
    )
    assert 'wait "$gen_pid"' in code, "the supervisor waits on the generate loop, not the review server"
    assert launches[0] < gen[0], "started BEFORE the supervisor's wait, or it never starts at all"


def test_the_review_server_loop_cannot_end_the_container():
    """A crashed or unstartable server -- port in use, unwritable token dir -- is a logged
    annoyance, not an outage. It is wrapped in a restart loop inside a background subshell,
    so a non-zero exit is retried rather than propagating."""
    body = _container_run()
    code = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    at = next(i for i, ln in enumerate(code) if ln == "run_review_server &")
    before, after = code[:at], code[at:]
    loop = max(i for i, ln in enumerate(before) if ln.startswith("while"))

    assert before[loop - 1] == "(", "the restart loop is inside a subshell"
    assert ") &" in after, "which is backgrounded, so a hang cannot block the supervisor"
    assert any(ln.startswith("sleep") for ln in after[:8]), "with a delay, or a crash-loop spins the CPU"
    assert any("||" in ln and ln.startswith("wait") for ln in after[:6]), "a non-zero exit is logged and retried"


def test_container_run_is_valid_posix_shell():
    """A syntax error here does not fail a test, it fails the container at boot. `sh -n`
    parses without executing, which is the whole check: nothing in this file may run in the
    suite -- it launches GPU sweeps."""
    import subprocess

    r = subprocess.run(["sh", "-n", os.path.join(ROOT, "container_run.sh")], capture_output=True, text=True)

    assert r.returncode == 0, r.stderr


def test_healthcheck_targets_healthz():
    """A HEALTHCHECK must exist and hit /healthz -- Task 16's own liveness endpoint,
    not some other port or path that would silently stop meaning anything the moment
    either side changed alone."""
    with open(os.path.join(ROOT, "Dockerfile.builder")) as f:
        body = f.read()
    assert re.search(r"^HEALTHCHECK\b.*CMD\b", body, re.M), "no HEALTHCHECK instruction found"
    assert "/healthz" in body, "the HEALTHCHECK must target /healthz"


def test_every_module_reachable_from_an_entrypoint_is_copied_into_the_image():
    """One hop deeper than the check above: a helper that a helper imports is just as absent from the image."""
    copied = _copied()
    seen, missing = set(), {}
    queue = [rel for rel in (_entry_path(ep) for ep in ENTRYPOINTS) if os.path.exists(os.path.join(ROOT, rel))]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for imported in _local_imports(os.path.join(ROOT, rel)):
            if imported not in copied:
                missing.setdefault(imported, []).append(rel)
            queue.append(imported)
    assert not missing, f"not COPY'd into the image but reachable from an entrypoint: {missing}"


# --- the lite image carries what the pipeline needs at runtime -------------------------------
#
# Dockerfile.lite was built from python:3.11-slim by listing what seemed needed; it shipped without
# mkvtoolnix (found on the first real merge pass: "FATAL: mkvmerge not found") and still lacks
# what Dockerfile.builder installs for accuracy: jellyfish (glossary.py's phonetic name tier
# switches itself off when the import fails), wamerican (the common-word guard falls back to a
# small bundled list), tzdata (MERGE_WINDOW and the schedule read local time), the review UI and
# the health check. Each was invisible until real work ran. These compare the two Dockerfiles, so a
# runtime dependency added to one and not the other fails here instead.

# Installed by Dockerfile.builder, deliberately absent from the lite image.
LITE_EXEMPT_APT = {"python3-pip", "git"}  # the slim base ships pip; nothing at runtime runs git
LITE_EXEMPT_PIP = {"webrtcvad"}  # tools/ analytics only, never imported by the pipeline


def _dockerfile(name):
    with open(os.path.join(ROOT, name)) as f:
        return f.read().replace("\\\n", " ")


def _apt_packages(body):
    out = set()
    for m in re.finditer(r"apt-get install\s+(.*?)(?:&&|$)", body, re.M):
        out.update(t for t in m.group(1).split() if not t.startswith("-"))
    return out


def _pip_packages(body):
    out = set()
    for m in re.finditer(r"pip install\s+(.*?)(?:&&|\|\||\)|$)", body, re.M):
        out.update(t for t in m.group(1).split() if not t.startswith("-"))
    return out


def test_the_lite_image_installs_every_runtime_package_the_full_image_does():
    full, lite = _dockerfile("Dockerfile.builder"), _dockerfile("Dockerfile.lite")
    missing_apt = _apt_packages(full) - _apt_packages(lite) - LITE_EXEMPT_APT
    missing_pip = _pip_packages(full) - _pip_packages(lite) - LITE_EXEMPT_PIP
    assert not missing_apt and not missing_pip, f"lite image lacks apt {sorted(missing_apt)} pip {sorted(missing_pip)}"


def _healthcheck(body):
    m = re.search(r"^HEALTHCHECK .*$", body, re.M)
    return m.group(0) if m else None


def test_the_lite_image_has_the_same_health_check_and_review_ui():
    full, lite = _dockerfile("Dockerfile.builder"), _dockerfile("Dockerfile.lite")
    assert _healthcheck(lite), "Dockerfile.lite has no HEALTHCHECK: a node reports healthy the moment it starts"
    assert _healthcheck(lite) == _healthcheck(full), "the two images check health differently"
    assert "/app/web/dist" in lite, "Dockerfile.lite does not ship the built review UI"


def test_the_lite_image_ships_the_data_files():
    # .dockerignore once excluded data/ for the lite image, which broke the full image's COPY.
    ignored = open(os.path.join(ROOT, ".dockerignore")).read().split()
    assert "data/" not in ignored and "data" not in ignored
