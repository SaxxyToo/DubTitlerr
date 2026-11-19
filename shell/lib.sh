# shellcheck shell=sh  # sourced by merge_pass.sh, which is #!/bin/sh
# Shared shell helpers for the DubTitlerr pipeline stages (merge_pass.sh, post_show.sh).
# The Python-side single source of truth is common.py::load_extras(); this is its shell
# counterpart, both reading data/extras.txt. Meant to be `source`d, not executed.

# extras_grep_pattern [path] — reads data/extras.txt (one dir name per line, `#` comments
# allowed, default path "data/extras.txt") and prints a grep -iE alternation pattern, e.g.
# '(Behind The Scenes|Deleted Scenes|Featurettes|Interviews|Scenes|Shorts|Trailers|Other|Extras)'
# — the same set the pre-consolidation inline regex in merge_pass.sh/post_show.sh matched
# (title-case is cosmetic only: callers use `grep -i`, so matching is case-insensitive
# regardless). Fails (prints nothing, returns 1) if the file is missing/empty/unreadable
# -- callers should fall back to an inline pattern in that case (see B9); a pattern that
# always prints even a hollow "()" would make `grep -ivE` match (and thus exclude) every
# line, since an empty alternation matches the empty string in any input.
# Returns 1 (prints nothing) if the file is missing/empty/unreadable so callers can
# detect the failure via `||` on the command substitution. That source/fallback shape is
# deliberate: a missing extras file must never turn into an empty pattern that matches everything.
extras_grep_pattern() {
	dir="${1:-data/extras.txt}"
	pattern=$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$dir" 2>/dev/null |
		awk '{for (i = 1; i <= NF; i++) $i = toupper(substr($i, 1, 1)) substr($i, 2); print}' |
		paste -sd'|' -)
	[ -z "$pattern" ] && return 1
	printf '(%s)' "$pattern"
}

# stop_requested — true once the soft-stop flag file exists. container_run.sh (PID 1) touches
# $STOP_FLAG on SIGTERM; every loop that STARTS work checks it and winds down instead of
# starting the next unit. With STOP_FLAG unset (manual runs, tests) nothing ever stops.
# Use inside if/&&/|| only: it returns 1 in the normal case, which `set -e` would treat as fatal.
stop_requested() {
	[ -n "${STOP_FLAG:-}" ] && [ -e "$STOP_FLAG" ]
}

# sleep_unless_stopped SECONDS — an interruptible sleep without any signal plumbing: sleeps in
# 5 s steps and returns early once the stop flag exists. Always returns 0 (safe under set -e).
# An argument that is not a plain integer ("1.5", "6h") is passed to plain sleep as is.
sleep_unless_stopped() {
	case "${1:-}" in
	'' | *[!0-9]*) # not whole seconds ("1.5", "6h"): let sleep parse it, uninterruptibly
		sleep "${1:-0}"
		return 0
		;;
	esac
	_sus_left="$1"
	while [ "$_sus_left" -gt 0 ]; do
		if stop_requested; then
			return 0
		fi
		if [ "$_sus_left" -gt 5 ]; then
			sleep 5
			_sus_left=$((_sus_left - 5))
		else
			sleep "$_sus_left"
			_sus_left=0
		fi
	done
	return 0
}

# `PROCESSING_STATE_FILE` is the persisted state file. The shell helper treats a MISSING file
# as running: these stages are also run directly outside the container (tests, a manual repair),
# where there is no app boot to honor. container_run.sh materialises `paused` at boot before any
# loop starts, so the application's default is paused. A corrupt/unknown file is always paused:
# the safe direction is to touch the library as little as possible.
processing_state_file() {
	printf '%s' "${PROCESSING_STATE_FILE:-${CONFIG_DIR:-/config}/processing.json}"
}

# processing_state — running | paused | stopped. A missing file is running for a direct stage
# invocation (see above); reading is a tiny sed over the machine-written JSON, not a subprocess.
processing_state() {
	_psf=$(processing_state_file)
	if [ ! -r "$_psf" ]; then
		printf '%s\n' "${PROCESSING_BOOT_DEFAULT:-running}"
		return 0
	fi
	_pss=$(sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([a-zA-Z]*\)".*/\1/p' "$_psf" 2>/dev/null | head -n 1)
	case "$_pss" in
	running | paused | stopped) printf '%s\n' "$_pss" ;;
	*) printf 'paused\n' ;;
	esac
}

# processing_running — true only for the running word. Use inside if/&&/|| (returns 1 otherwise).
processing_running() {
	[ "$(processing_state)" = "running" ]
}

# hold_while_paused — the pause boundary. Returns 0 when processing may start, and 1 when the
# soft stop arrived while holding (the caller then drains exactly as it does for stop_requested).
# Logs "processing paused — holding" once per hold, then polls every PROCESSING_POLL_INTERVAL
# seconds. The sleep is the existing stop-aware one, so a `docker stop` during a hold is prompt.
hold_while_paused() {
	_hwp_logged=0
	while :; do
		if stop_requested; then
			return 1
		fi
		_hwp_state=$(processing_state)
		if [ "$_hwp_state" = "running" ]; then
			return 0
		fi
		if [ "$_hwp_logged" -eq 0 ]; then
			echo "processing ${_hwp_state} — holding"
			_hwp_logged=1
		fi
		sleep_unless_stopped "${PROCESSING_POLL_INTERVAL:-15}"
	done
}

# library_sweep_enabled — false only when LIBRARY_SWEEP is an explicit off spelling (0, false, no, off, dry-run; any case,
# surrounding whitespace ignored): unset, empty or anything else keeps today's behaviour, the whole-library sweep. The same
# spellings the registry's bool type reads as off.
library_sweep_enabled() {
	case "$(printf '%s' "${LIBRARY_SWEEP:-}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')" in
	0 | false | no | off | dry-run) return 1 ;;
	*) return 0 ;;
	esac
}
