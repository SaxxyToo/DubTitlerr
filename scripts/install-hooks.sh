#!/bin/sh
# Install a pre-commit hook that runs ruff (scripts/check.sh --fast, a few seconds) so a lint
# error is caught before the commit instead of after it. The full suite takes minutes and stays a
# pre-push step: scripts/check.sh. Remove with: rm .git/hooks/pre-commit
set -eu
cd "$(dirname "$0")/.."
hook=.git/hooks/pre-commit
if [ -e "$hook" ] && ! grep -q "scripts/check.sh --fast" "$hook"; then
	echo "refusing to overwrite an existing $hook" >&2
	exit 1
fi
printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/scripts/check.sh" --fast\n' >"$hook"
chmod +x "$hook"
echo "installed $hook"
