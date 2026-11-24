"""[S-1] Every environment variable the pipeline reads is in the settings registry, first wave or not.

The scan reads the code, not a list: it walks the top-level modules and tools/ (Python: os.environ.get / os.getenv /
os.environ[...] and the small env helpers; shell: ${VAR} and $VAR), so a new read that nobody registered fails here.
"""

import ast
import os
import re

from dubtitlerr.settings import settings_registry as reg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
SKIP_DIRS = {
    "tests",
    "web",
    "node_modules",
    "skills",
    "scripts",
    "specs",
    "deploy",
    "docs",
    "Docs",
    "glossaries",
    "data",
    "__pycache__",
}
CALLS = {"get", "getenv", "env_int", "env_float", "_env_num", "_fraction", "_count", "pop", "setdefault"}
# Names the scan sees that are not settings: shell locals, and the per-show inputs the loops set for each child.
NON_SETTINGS = {
    "REPAIR_PROFILE": "an old runtime.env key, read once by settings_migrate.py",
    "REPAIR_REF_CLASSIC": "R2 rollback / A-B kill-switch (legacy merged reference pool); deliberately not a stored setting",
    "REPAIR_GATE_DUB_VOUCHED": "R3 rollback kill-switch (role-aware gates back to classic); not a stored setting",
    "FORCE_REDO": "per-request signal from gen_loop.sh, never a stored setting",
    "DT_CONTAINER_BOOT_ID": "exported by container_run.sh at start, read by common.boot_id()",
    "DT_VERSION": "exported by container_run.sh at start (the boot banner); gen_loop.sh only prints it",
    "PROCESSING_STATE_FILE": "processing-state path override: a file location, not a stored setting (created by the boot path)",
    "ARTIFACT_STORE": "P39 artifact store path override; a file location, not a stored setting",
    "PROCESSING_POLL_INTERVAL": "processing-state hold cadence: operational loop constant, deliberately not a stored setting",
    "PROCESSING_BOOT_DEFAULT": "container_run.sh internal: the word used only when the state file is deleted mid-run",
    "ACQ_FLAGS": "gen_loop.sh local",
    "ANIME": "gen_loop.sh local",
    "APP": "merge_pass.sh and publish tool local",
    "DURATION": "mux.py local constant",
    "GLOSS": "gen_loop.sh local",
    "GLOSS_DIR": "gen_loop.sh local",
    "ORDER": "gen_loop.sh local",
    "PATTERN": "merge_pass.sh local",
    "ROOT": "merge_pass.sh local",
    "REQUEST_SINCE": "gen_loop.sh -> generate.py plumbing: when the request being served started",
    "RUN_REPORT": "gen_loop.sh -> generate.py plumbing: where to write the request's scope report",
    "TMPDIR": "the standard temp-directory variable, read by gen_loop.sh",
    "ROOT_ABS": "merge_pass.sh local: the absolute library root every stage path starts from",
    "PWD": "the shell's own working directory, read once by merge_pass.sh after cd",
    "UTC": "review_requests.py time zone name",
    "SHOW_NAME": "set per show by the loops",
    "GLOSSARY_FILE": "set per show by the loops",
    "GENERATE_ONLY_RE": "set per request by the loops",
}


def _walk():
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and "venv" not in d]
        yield directory, files


def _python_reads(path):
    out = set()
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in CALLS:
                for arg in node.args[:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and NAME.match(arg.value):
                        out.add(arg.value)
                        break
        elif isinstance(node, ast.Subscript):
            target = node.value
            if isinstance(target, ast.Attribute) and target.attr == "environ":
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    out.add(node.slice.value)
    return out


def _shell_reads(path):
    text = open(path, encoding="utf-8").read()
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)[:}+=\-]", text)) | set(re.findall(r"\$([A-Z][A-Z0-9_]{2,})\b", text))


def scan():
    reads = set()
    for directory, files in _walk():
        for name in files:
            path = os.path.join(directory, name)
            if name.endswith(".py"):
                reads |= _python_reads(path)
            elif name.endswith(".sh") and (directory == ROOT or os.path.basename(directory) in ("shell", "tools")):
                reads |= _shell_reads(path)
    return reads


def test_every_env_read_is_in_the_registry():
    missing = sorted(n for n in scan() if n not in reg.REGISTRY and n not in NON_SETTINGS)
    assert not missing, f"read by the pipeline but missing from settings_registry.REGISTRY: {missing}"


def test_the_scan_finds_reads_in_python_and_shell():
    reads = scan()
    assert {"WHISPER_BEAM_SIZE", "MINE_MIN_COUNT", "ACQUIRE_TIMEOUT", "REVIEW_PORT", "MERGE_INTERVAL"} <= reads


def test_non_settings_are_not_also_registry_keys():
    assert not set(NON_SETTINGS) & set(reg.REGISTRY)


# --- scope vs the code that reads it --------------------------------------------------------------------------------------
# `scope` is the process that reads a setting (settings_registry's module docstring), and the exporter hands a process only
# the keys `keys_for_scope` returns. A plain merge-only row whose reader runs in the generate loop is therefore silent: the
# Settings row saves, the generate loop never receives it, and its help promises a change nothing makes.
def _package_modules():
    """{dotted.module: path} for every module inside the package (not tests, tools or web)."""
    found = {}
    package = os.path.join(ROOT, "dubtitlerr")
    for directory, _dirs, files in os.walk(package):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                found[os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")] = path
    return found


def _module_imports(path):
    """The dubtitlerr modules this file loads at import time (top level, plus top-level try/if arms).

    In-function imports are deliberately not followed: they run only on a path the process may never take, and following
    them drags repair.py into the generate closure through a lazy glossary_decision -> decision_client import.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names, pending = set(), list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, (ast.Try, ast.If, ast.With)):
            pending.extend(node.body)
            pending.extend(getattr(node, "orelse", []))
            pending.extend(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                pending.extend(handler.body)
    return names


def _generate_side_modules():
    """Every module a generate-loop child runs, plus everything they load at import time.

    generate.py is not the only generate-side entry: gen_loop.sh also runs the glossary stages and the queue helpers. That is
    why REPAIR_BACKEND and REPAIR_LLAMACPP_URL have generate-side readers (the legacy fallback in glossary_verify.py and
    punctuation.py), not just repair.py.
    """
    modules = _package_modules()
    with open(os.path.join(ROOT, "gen_loop.sh"), encoding="utf-8") as handle:
        shell = handle.read()
    pending = {match.group(1) for match in re.finditer(r"-m\s+(dubtitlerr(?:\.\w+)+)", shell)}
    pending.add("dubtitlerr.pipeline.generate")
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in modules:
            continue
        seen.add(name)
        pending.update(_module_imports(modules[name]))
    return {name: modules[name] for name in seen}


def _generate_side_readers():
    """{registry key: {module, ...}} for every environment read in a generate-side module."""
    readers = {}
    for name, path in _generate_side_modules().items():
        for key in _python_reads(path):
            if key in reg.REGISTRY:
                readers.setdefault(key, set()).add(name)
    return readers


def test_a_plain_merge_only_setting_read_by_the_generate_loop_is_scoped_to_gen():
    """The bug this pins: a plain row whose reader runs in gen but whose scope is merge-only never reaches that reader.

    Only merge-only rows are checked. A container row reaches every child through container_run.sh's boot export, and a
    server row is the review server's choice, so neither is the generate loop losing a value. Connection-managed rows are
    excluded on purpose: a Connection expands only into the scopes its rows allow, and the repair endpoint/key must not
    reach the generate loop (settings_connections' KEY_HOLDER_SCOPES and expand()).
    """
    stranded = {
        key: sorted(names)
        for key, names in _generate_side_readers().items()
        if reg.REGISTRY[key]["scope"] == ["merge"] and reg.REGISTRY[key]["managed_by"] is None
    }
    assert not stranded, "plain merge-only settings read inside the generate loop (scope needs 'gen' too): " + ", ".join(
        f"{key} <- {', '.join(names)}" for key, names in sorted(stranded.items())
    )


def test_the_generate_side_rows_are_pinned_to_the_module_that_reads_them():
    """The rows this scope audit moved, and the reader each one's scope answers for."""
    modules = _generate_side_modules()
    for key, reader in {
        "RESTORE_PUNCTUATION": "dubtitlerr.pipeline.punctuation",
        "RESTORE_MIN_RUN": "dubtitlerr.pipeline.punctuation",
        "RESTORE_MAX_TOKENS": "dubtitlerr.pipeline.punctuation",
        "REPAIR_AUTO_DISMISS_WORDS": "dubtitlerr.core.unresolved",
        "SIDECAR_SUFFIX": "dubtitlerr.pipeline.generate",
    }.items():
        assert reader in modules, f"{key} is pinned to {reader}, which is no longer generate-side"
        assert key in _python_reads(modules[reader]), f"{key} is pinned to {reader}, which no longer reads it"
        entry = reg.REGISTRY[key]
        assert set(entry["scope"]) == {"gen", "merge"}, key
        assert entry["applies"] == "next_show", key
        assert key in reg.keys_for_scope("gen"), key
