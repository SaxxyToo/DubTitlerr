#!/bin/sh
# The checks a change must pass before it is pushed -- the same ones CI runs.
#
#   scripts/check.sh          ruff lint + format, then the full Python suite
#   scripts/check.sh --fast   ruff only (what the pre-commit hook runs)
#   scripts/check.sh --dash   also the shell-loop tests under dash, the image's /bin/sh, in a
#                             throwaway Debian container (needs docker; CI's Ubuntu runner is
#                             already dash, a bash workstation is not)
#
# Uses uv when it is installed (no virtualenv to manage), else the current python3. The tool
# versions are the ones .github/workflows/ci.yml pins; keep the two in step.
set -eu
cd "$(dirname "$0")/.."

RUFF_VERSION=0.17.5
PYTEST_VERSION=9.2.0
PYSUBS2_VERSION=1.8.1 # 1.9 needs Python 3.12; CI's 3.11 leg and the images use 1.8
JELLYFISH_VERSION=1.3.0
PY=${CHECK_PYTHON:-3.13}

run_ruff() {
	if command -v uv >/dev/null 2>&1; then
		uv run -q --with "ruff==$RUFF_VERSION" --no-project ruff check .
		uv run -q --with "ruff==$RUFF_VERSION" --no-project ruff format --check .
	else
		python3 -m ruff check .
		python3 -m ruff format --check .
	fi
}

run_pytest() {
	# addopts="" so the summary line ("N passed") is printed: pyproject sets -q, and -q twice hides it.
	if command -v uv >/dev/null 2>&1; then
		uv run -q --python "$PY" --with "pytest==$PYTEST_VERSION" --with "pysubs2==$PYSUBS2_VERSION" --with "jellyfish==$JELLYFISH_VERSION" --no-project \
			pytest -o addopts="" -q -p no:cacheprovider "$@"
	else
		python3 -m pytest -o addopts="" -q -p no:cacheprovider "$@"
	fi
}

echo "== ruff"
run_ruff
[ "${1:-}" = "--fast" ] && exit 0

echo "== pytest (python $PY)"
run_pytest

if [ "${1:-}" = "--dash" ]; then
	echo "== shell loops under dash"
	docker run --rm -v "$PWD":/src:ro python:3.13-slim sh -c '
		cp -r /src /w && cd /w &&
		pip install -q --root-user-action=ignore "pytest=='"$PYTEST_VERSION"'" "pysubs2=='"$PYSUBS2_VERSION"'" "jellyfish=='"$JELLYFISH_VERSION"'" >/dev/null 2>&1 &&
		python -m pytest -o addopts="" -q -p no:cacheprovider tests/test_merge_pass.py tests/test_merge_pass_app_dir.py \
			tests/test_soft_stop.py tests/test_gen_loop_requests.py tests/test_gen_loop_runtime_env.py \
			tests/test_heartbeat_shell.py tests/test_heartbeat_http.py'
fi
echo "== all checks passed"
