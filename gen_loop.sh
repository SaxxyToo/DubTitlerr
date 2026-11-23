#!/bin/sh
# GPU generate loop (in-container, no nested docker). Sweeps the show order transcribing
# each show's English dub; after a full sweep, idles RESCAN_INTERVAL then sweeps again so
# newly added anime get dubtitled automatically. generate.py skips the model load entirely
# when a sweep finds nothing new, so idle rescans are cheap.
set -u
set -e # fail loud (exit, taking down the entrypoint) on anything NOT explicitly tolerated
# below; mine/verify/generate keep their `||` fallthroughs so crash-resume +
# stall-detection (the $after -le $before comparison) still runs even when one fails.
ORDER="${ANIME_ORDER:-/config/anime_order.txt}"
ANIME="${ANIME_ROOT:-/media/Anime Library}"
GLOSS_DIR="${GLOSSARY_DIR:-/config/glossaries}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
REQUESTS_DIR="${REQUESTS_DIR:-$CONFIG_DIR/requests}"
SCHEDULE_FILE="${SCHEDULE_FILE:-$CONFIG_DIR/schedule.json}"
EFFECTIVE_ENV_FILE="${EFFECTIVE_ENV_FILE:-$CONFIG_DIR/effective.env}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$CONFIG_DIR/runtime.env}"
export CONFIG_DIR RUNTIME_ENV_FILE EFFECTIVE_ENV_FILE

# Soft stop (container_run.sh touches $STOP_FLAG on SIGTERM): stop STARTING work, never kill
# work in flight -- a killed generate.py leaves a permanent .dubtitles.fail poison marker. So
# this script has no `trap` and no `wait`; it only looks at the flag between units.
APP_DIR="${APP_DIR:-/app}"
# python3 -m needs the app dir (which holds the dubtitlerr/ package) on the import path.
PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

# Notifications are REPORTING ONLY: every call below is failure-swallowed, so a missing module, an
# unreachable destination or a broken config can never change this loop's exit status, write a stage
# outcome or stop a sweep. Emissions happen at show/sweep boundaries, never mid-episode.
notify_send() {
	python3 -m dubtitlerr.core.notify send "$@" >/dev/null 2>&1 || true
}

# The heartbeat is REPORTING ONLY, exactly like notify_send: /healthz, /api/v1/health, loop.state in
# /api/v1/system/status and the Dashboard all read this one document (dubtitlerr/core/heartbeat.py
# documents which field each event writes). Every call is failure-swallowed, and the module is
# stdlib-only, so a missing module (misbuilt image), a read-only config volume or a bad
# HEARTBEAT_PATH can never change this loop's exit status, write a stage outcome or stop a sweep.
# Path: the stored/container HEARTBEAT_PATH when set, else the same /config default common.py uses.
heartbeat_note() {
	HEARTBEAT_PATH="${HEARTBEAT_PATH:-${CONFIG_DIR:-/config}/heartbeat.json}" \
		python3 -m dubtitlerr.core.heartbeat "$@" >/dev/null 2>&1 || true
}

# What this loop saw at start (the container environment), generated from the settings registry: the _INIT_ values and a
# restore_runtime_env() that puts every key of the scope back. apply_runtime_env calls it first, so a reset reaches this
# long-running loop without a restart and a crashing exporter cannot leave the previous show's stored value in force.
# If the exporter is missing or fails here, nothing is restored and nothing is applied: the loop keeps the container environment.
if [ -f "$APP_DIR/dubtitlerr/review/review_settings_write.py" ]; then
	eval "$(python3 -m dubtitlerr.review.review_settings_write snapshot --scope gen 2>/dev/null)" || true
fi

# Stored settings (settings.json / secrets.json) for the generate loop, applied at every boundary where a value may change:
# each sweep, each show, each request -- never mid-episode. The exporter is parsed and re-validated, never sourced; its stderr is
# logged once per sweep.
apply_runtime_env() {
	if [ -f "$APP_DIR/dubtitlerr/review/review_settings_write.py" ]; then
		if command -v restore_runtime_env >/dev/null 2>&1; then restore_runtime_env; fi
		_exp_err_file="${CONFIG_DIR:-/config}/.exporter.err"
		_exp_out=$(python3 -m dubtitlerr.review.review_settings_write export --scope gen --reset-to-init 2>"$_exp_err_file") || true
		if [ -s "$_exp_err_file" ] && [ "${_EXPORTER_LOGGED_THIS_SWEEP:-0}" -eq 0 ]; then
			cat "$_exp_err_file" >&2
			_EXPORTER_LOGGED_THIS_SWEEP=1
		fi
		rm -f "$_exp_err_file"
		eval "$_exp_out" || true
		python3 -m dubtitlerr.review.review_requests write-effective-env "$EFFECTIVE_ENV_FILE" </dev/null 2>&1 || echo "  effective.env write failed (continuing)"
	fi
}
if [ -f "$APP_DIR/shell/lib.sh" ]; then
	# shellcheck disable=SC1091
	. "$APP_DIR/shell/lib.sh"
else # lib missing (misbuilt image): never stop, plain sleeps
	stop_requested() { return 1; }
	sleep_unless_stopped() { sleep "$1"; }
	library_sweep_enabled() { return 0; }
	processing_state() { printf 'running\n'; }
	processing_running() { return 0; }
	hold_while_paused() { return 0; }
fi

# Which release this loop belongs to. container_run.sh exports DT_VERSION before starting this
# script; a standalone run (debugging on the host) resolves it here so the boot line is never a lie.
if [ -z "${DT_VERSION:-}" ]; then
	DT_VERSION=$(python3 -c 'import dubtitlerr; print(dubtitlerr.__version__)' 2>/dev/null) || DT_VERSION=unknown
	export DT_VERSION
fi
echo "#### gen_loop up dubtitlerr ${DT_VERSION} $(date) — order=$ORDER root=$ANIME"

check_window_open() {
	_win_err_file="${CONFIG_DIR:-/config}/.window.err"
	is_open=$(python3 -m dubtitlerr.review.review_requests window-open "$SCHEDULE_FILE" 2>"$_win_err_file") && rc=0 || rc=$?
	if [ -s "$_win_err_file" ] && [ "${_WINDOW_WARN_LOGGED:-0}" -eq 0 ]; then
		cat "$_win_err_file" >&2
		_WINDOW_WARN_LOGGED=1
	fi
	rm -f "$_win_err_file"
	[ "$rc" -eq 0 ] || is_open="open"
}

wait_for_window() {
	while :; do
		if stop_requested; then return 0; fi
		check_window_open
		[ "$is_open" = "open" ] && return 0
		echo "gen_loop: schedule window closed — sleeping ${SCHEDULE_CHECK_INTERVAL:-60}s"
		sleep_unless_stopped "${SCHEDULE_CHECK_INTERVAL:-60}"
	done
}

# The per-show prep both the sweep and a drained run request run before GENERATE, so the two cannot drift:
# MINE, ACQUIRE, ARC TAGS, VERIFY. (Mux is not here: gen_loop never muxes, merge_pass.sh does that.)
# Results come back in globals (POSIX sh has no `local`, and a $(...) call would lose them): _prep_gloss is the glossary
# path, or "" when the show has none; _prep_stopped is 1 when a soft stop arrived before a step began. The function never
# exits itself: the sweep exits, a request requeues. Its own variables are _ps_-prefixed so they cannot clobber the
# caller's show, rc or GLOSS (a request is drained inside the sweep's read loop).
# Progress counter for one show: delivered dialogue sidecars plus the poison markers still
# live in the artifact store (a crashed episode counts as progress, exactly as its
# `.dubtitles.fail` file used to). A store that cannot be read contributes only the file
# count: a broken counter must never make a working pass look stalled.
progress_count() {
	_pc_files=$(find "$1" -name "*.eng.dubtitles.srt" 2>/dev/null | wc -l)
	_pc_fail=$(python3 -m dubtitlerr.core.artifact_store stems --kind fail --root "$1" </dev/null 2>/dev/null | wc -l)
	echo $((_pc_files + _pc_fail))
}

prep_show() {
	_ps_show="$1"
	_prep_stopped=0
	_prep_gloss=""
	# ADDITIVE dictionary: load the show's existing glossary + mine its NEW episodes'
	# embedded subs for new proper nouns, appending them (never rebuilds). Runs before
	# generate so the grown dictionary applies to the episodes about to be transcribed.
	# Each prep step can run for many minutes (ACQUIRE up to 30, VERIFY up to 20): a stop
	# that arrives during one show's prep must not wait for the rest of it.
	if stop_requested; then
		echo "gen_loop: soft stop, not starting MINE on $_ps_show"
		_prep_stopped=1
		return 0
	fi
	echo "#### MINE $_ps_show $(date)"
	# $2: a request's episode filter, so MINE reads only the episodes the run is about (empty in
	# the library sweep, which mines every new episode).
	GLOSSARY_DIR="$GLOSS_DIR" GENERATE_ONLY_RE="${2:-}" python3 -m dubtitlerr.glossary.mine_glossary "$ANIME/$_ps_show" </dev/null 2>&1 || echo "  mine failed (continuing)"
	# Acquire names the miner cannot reach: releases with no embedded fansub track leave the
	# glossary empty for that stretch of the show. Wiki-owned canonicals only. Two separate
	# gates: ACQUIRE enables the step at all (default on);
	# ACQUIRE_APPLY decides whether it writes what it finds (default: apply; an explicit
	# 0/false/no/off/dry-run makes it log proposals only). Failure-swallowed
	# so it can never stall a sweep either way.
	if [ "${ACQUIRE:-1}" != "0" ] && [ -f "$GLOSS_DIR/$_ps_show.json" ]; then
		if stop_requested; then
			echo "gen_loop: soft stop, not starting ACQUIRE on $_ps_show"
			_prep_stopped=1
			return 0
		fi
		echo "#### ACQUIRE $_ps_show $(date)"
		ACQ_FLAGS=""
		# 0, false, no, off, dry-run (any case, surrounding whitespace ignored) = dry run; unset, empty or anything else = apply
		case "$(printf '%s' "${ACQUIRE_APPLY:-}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')" in
			0 | false | no | off | dry-run) ;;
			*) ACQ_FLAGS="--apply" ;;
		esac
		# acquire_cache only memoises escalate()'s LLM adjudication now, never a
		# proposal's final verdict, so a dry run and a later --apply run always
		# propose the same terms -- no ACQUIRE_NO_CACHE workaround needed. The bug
		# this replaced (a dry run's cached verdict used to fold into `settled` and skip
		# the term on every later run, suppressing 22 apply, 1,654 known and 10,708 flag
		# verdicts -- that last group being review-queue entries that would never have
		# surfaced for a human).
		# 600s was also an incremental-era number; a first full acquisition pass over 463
		# episodes exceeded it and was killed mid-harvest on 2026-08-21.
		timeout "${ACQUIRE_TIMEOUT:-1800}" python3 -m dubtitlerr.glossary.glossary_acquire \
			"$GLOSS_DIR/$_ps_show.json" "$ANIME/$_ps_show" \
			$ACQ_FLAGS </dev/null 2>&1 || echo "  acquire skipped (continuing)"
	fi
	_ps_gloss="$GLOSS_DIR/$_ps_show.json"
	[ -f "$_ps_gloss" ] || _ps_gloss=""
	# Arc tags + arc titles (opt-in, default off): one wiki fetch per distinct season.nfo arc, cached on disk, tags the
	# glossary's names (repair weights its prompt by them) and stores the arc titles VERIFY_ARC_GATE checks writes against.
	# Additive, atomic, failure-swallowed like every prep step; a title that is not an arc changes nothing.
	if [ "${GLOSSARY_ARC_TAGS:-0}" = "1" ] && [ -n "$_ps_gloss" ]; then
		if stop_requested; then
			echo "gen_loop: soft stop, not starting ARC TAGS on $_ps_show"
			_prep_stopped=1
			return 0
		fi
		echo "#### ARC TAGS $_ps_show $(date)"
		timeout "${ARC_TAGS_TIMEOUT:-600}" python3 -m dubtitlerr.glossary.glossary_verify "$_ps_gloss" --tag-arcs "$ANIME/$_ps_show" </dev/null 2>&1 ||
			echo "  arc tagging skipped (continuing)"
	fi
	# wiki-verify the (mined/updated) glossary: canonical, dub-preferred spellings. Incremental +
	# cached, and timeout-bounded + failure-swallowed so a slow/down wiki never stalls the sweep.
	if [ -n "$_ps_gloss" ]; then
		if stop_requested; then
			echo "gen_loop: soft stop, not starting VERIFY on $_ps_show"
			_prep_stopped=1
			return 0
		fi
		echo "#### VERIFY $_ps_show $(date)"
		# 300s was sized for INCREMENTAL verification -- pending_terms() skips anything
		# already in `verified`, so a steady-state run adjudicates a handful of terms. A
		# full re-adjudication (after `verified` is cleared, as on 2026-08-21) is 121 terms
		# for One Pace and hit the wall at exactly 300s. `timeout` returns 124; anything
		# else is a real failure, and collapsing the two into one "skipped" line is how a
		# stage that never once completed still looked like a normal sweep.
		# `&& rc=0 || rc=$?` (the generate.py pattern; rc is reassigned before every other use, so sharing the name is safe) so a `timeout`-killed
		# verify -- exit 124 -- does not trip `set -e` and exit the container before the
		# rc-classified message below ever runs. Verified on vm102 2026-09-05: every restart
		# over the prior 14h exited with code 124, journal-confirmed, with VERIFY_TIMEOUT
		# (1200s) firing on One Pace within ~2s of the restart in every measured instance.
		timeout "${VERIFY_TIMEOUT:-1200}" python3 -m dubtitlerr.glossary.glossary_verify "$_ps_gloss" </dev/null 2>&1 && rc=0 || rc=$?
		[ "$rc" -eq 0 ] || { [ "$rc" -eq 124 ] &&
			echo "  verify TIMED OUT after ${VERIFY_TIMEOUT:-1200}s (continuing; terms stay unverified)" ||
			echo "  verify failed rc=$rc (continuing)"; }
	fi
	# Names-hygiene act (P24): cross-references names x flagged x the wiki cache just refreshed by VERIFY above and
	# REMOVES names the audit deterministically classifies as junk (guard-classified, or a capitalised ordinary word
	# with no exact wiki page) -- the exact-title check protects real cast, and with no cached wiki nothing is removed.
	# The removal is logged to <glossary>.cleanup.json. Failure-swallowed like every prep step; exit 1 means junk was
	# reported that could NOT be removed (no wiki to decide on), not that the stage broke.
	if [ -n "$_ps_gloss" ]; then
		if stop_requested; then
			echo "gen_loop: soft stop, not starting GLOSSARY AUDIT on $_ps_show"
			_prep_stopped=1
			return 0
		fi
		echo "#### GLOSSARY AUDIT $_ps_show $(date)"
		audit_rc=0
		timeout "${AUDIT_TIMEOUT:-300}" python3 -m dubtitlerr.glossary.glossary_audit "$_ps_gloss" --fix </dev/null 2>&1 || audit_rc=$?
		if [ "$audit_rc" -eq 0 ]; then
			echo "  names hygiene: clean (junk removed above, logged to .cleanup.json)"
		elif [ "$audit_rc" -eq 1 ]; then
			echo "  names hygiene: junk reported above, not removed (no cached wiki titles to protect real names)"
		elif [ "$audit_rc" -eq 124 ]; then
			echo "  audit TIMED OUT after ${AUDIT_TIMEOUT:-300}s (continuing)"
		else
			echo "  audit failed rc=$audit_rc (continuing)"
		fi
	fi
	_prep_gloss="$_ps_gloss"
	return 0
}

drain_requests() {
	if [ "${pruned_this_sweep:-0}" -eq 0 ]; then
		python3 -m dubtitlerr.review.review_requests prune "$REQUESTS_DIR" 7 </dev/null 2>&1 || true
		pruned_this_sweep=1
	fi
	last_req_path=""
	while :; do
		if stop_requested; then return 0; fi
		# Pause boundary between requests: the request in flight finishes, then the drain holds.
		hold_while_paused || return 0
		check_window_open
		[ "$is_open" = "open" ] || break

		req_json=$(python3 -m dubtitlerr.review.review_requests next "$REQUESTS_DIR" 2>/dev/null) && req_rc=0 || req_rc=$?
		[ "$req_rc" -eq 0 ] && [ -n "$req_json" ] || break

		req_out=$(python3 -m dubtitlerr.review.review_requests unpack "$req_json" 2>/dev/null) || break

		req_path="" req_show="" req_gen_re="" req_req_eng="" req_force=""
		while IFS='=' read -r k v; do
			case "$k" in
				PATH) [ -z "$req_path" ] && req_path="$v" ;;
				SHOW) [ -z "$req_show" ] && req_show="$v" ;;
				GENERATE_ONLY_RE) [ -z "$req_gen_re" ] && req_gen_re="$v" ;;
				REQUIRE_ENG) [ -z "$req_req_eng" ] && req_req_eng="$v" ;;
				FORCE_REDO) [ -z "$req_force" ] && req_force="$v" ;;
			esac
		done <<EOF
$req_out
EOF
		[ -n "$req_path" ] && [ -n "$req_show" ] || break

		if [ "$req_path" = "$last_req_path" ]; then
			echo "gen_loop: request $req_path seen twice without progress — stopping drain"
			break
		fi
		last_req_path="$req_path"

		if [ ! -d "$ANIME/$req_show" ]; then
			echo "request show directory not found: $ANIME/$req_show"
			if ! python3 -m dubtitlerr.review.review_requests finish "$req_path" fail "show directory not found" </dev/null 2>&1; then
				echo "gen_loop: failed to mark request $req_path as failed — stopping drain"
				break
			fi
			continue
		fi

		echo "#### RUN REQUEST for $req_show ($req_path) $(date)"
		if ! python3 -m dubtitlerr.review.review_requests start "$req_path" </dev/null 2>&1; then
			echo "gen_loop: failed to claim request $req_path — stopping drain"
			break
		fi

		apply_runtime_env

		req_eng="${req_req_eng:-${REQUIRE_ENG:-1}}"
		# Same prep as the sweep gives a show. A soft stop during it leaves the stop flag set, so the generate loop below
		# does not start and the request is requeued by the check after it.
		prep_show "$req_show" "$req_gen_re"
		GLOSS="$_prep_gloss"
		heartbeat_note show --name "$req_show"

		attempt=0
		rc=""
		# One start time for the whole request, shared by every resume pass: generate counts an
		# episode as rebuilt by THIS request when its transcript record is at least this new, and
		# a forced request rebuilds each episode once, not once per pass. The scope report it
		# writes is what decides done/failed below -- the exit code alone does not.
		req_since=$(date +%s)
		req_report="${TMPDIR:-/tmp}/dubtitlerr-request-report.$$.json"
		while :; do
			if stop_requested; then
				echo "gen_loop: soft stop, not starting a generate pass on request $req_path"
				break
			fi
			attempt=$((attempt + 1))
			rm -f "$req_report" # a report left by an earlier pass must not stand in for this one
			before=$(progress_count "$ANIME/$req_show")
			SHOW_NAME="$req_show" GLOSSARY_FILE="$GLOSS" REQUIRE_ENG="$req_eng" COMPUTE_TYPE="${COMPUTE_TYPE:-int8}" \
				GENERATE_ONLY_RE="$req_gen_re" FORCE_REDO="$req_force" REQUEST_SINCE="$req_since" RUN_REPORT="$req_report" \
				python3 -m dubtitlerr.pipeline.generate --root "$ANIME/$req_show" </dev/null && rc=0 || rc=$?
			after=$(progress_count "$ANIME/$req_show")
			[ "$rc" = "0" ] && break
			if [ "$after" -le "$before" ]; then
				echo "GENERATE stalled on request $req_path (rc=$rc) — moving on"
				break
			fi
			[ "$attempt" -ge 40 ] && {
				echo "GENERATE max passes on request $req_path"
				break
			}
			echo "GENERATE crashed on request $req_path (rc=$rc) — resume pass $((attempt + 1)) $(date)"
		done

		if stop_requested; then
			echo "gen_loop: soft stop around request $req_path — requeuing"
			python3 -m dubtitlerr.review.review_requests requeue "$req_path" </dev/null 2>&1 || echo "gen_loop: failed to requeue request $req_path"
			break
		fi

		# done/failed from the scope report (review_requests.verdict), with the report attached
		# to the request as its evidence. A missing report fails the request.
		if ! python3 -m dubtitlerr.review.review_requests finish-report "$req_path" "$req_report" "${rc:-1}" </dev/null 2>&1; then
			echo "gen_loop: failed to file request $req_path — stopping drain"
			rm -f "$req_report"
			break
		fi
		rm -f "$req_report"
	done
}

while :; do
	pruned_this_sweep=0
	_EXPORTER_LOGGED_THIS_SWEEP=0
	_WINDOW_WARN_LOGGED=0
	# What this sweep reports at its end. All three are counted where the loop already knows the
	# answer -- the show read loop is a redirect, not a pipe, so these survive to the summary.
	sweep_shows=0
	sweep_failed=0
	sweep_review_pending=0
	if stop_requested; then
		echo "gen_loop: soft stop, not starting the next sweep"
		exit 0
	fi
	# Pause boundary before a sweep: a paused/stopped app starts no sweep at all. The show in
	# flight (if any) already finished; resume continues with this sweep.
	hold_while_paused || exit 0
	wait_for_window
	if stop_requested; then
		echo "gen_loop: soft stop, not starting the next sweep"
		exit 0
	fi
	# Stored settings first: the show order file and the watch queue below read ANIME_ORDER and WATCH_QUEUE_* from them.
	apply_runtime_env
	ORDER="${ANIME_ORDER:-/config/anime_order.txt}"
	# Watch-gated queue: rewrite $ORDER from what is actually being watched, unioning
	# WatchState (household/Jellyfin) with Plex history across ALL accounts. Neither source
	# is a superset of the other. watch_queue.py exits non-zero and leaves $ORDER untouched
	# if either is unreachable, so a `||` fallthrough is correct here -- last sweep's queue is
	# a safe answer, an empty one is not. Disabled by leaving WATCH_QUEUE_WINDOW_DAYS unset.
	# LIBRARY_SWEEP off: nothing below walks the library; only queued requests run (drain_requests), then the idle wait.
	sweep=1
	library_sweep_enabled || sweep=0
	if [ "$sweep" -eq 1 ] && [ -n "${WATCH_QUEUE_WINDOW_DAYS:-}" ]; then
		echo "#### WATCH QUEUE $(date)"
		ANIME_ROOT="$ANIME" python3 -m dubtitlerr.pipeline.watch_queue \
			--window-days "$WATCH_QUEUE_WINDOW_DAYS" --out "$ORDER" \
			${WATCH_QUEUE_PIN:+--pin "$WATCH_QUEUE_PIN"} </dev/null ||
			echo "  watch_queue declined to write (keeping the existing order file)"
	fi
	# Fresh install with no media server: the watch queue had nothing to say, so build a
	# queue from the library tree itself (newest show first). It never overwrites an
	# existing order file, and a failed scan falls through to the idle below.
	if [ "$sweep" -eq 1 ] && [ ! -f "$ORDER" ]; then
		echo "#### LIBRARY QUEUE $(date)"
		ANIME_ROOT="$ANIME" ANIME_ORDER="$ORDER" python3 -m dubtitlerr.pipeline.library_queue \
			--root "$ANIME" --out "$ORDER" </dev/null ||
			echo "  library_queue could not build $ORDER"
	fi
	if [ "$sweep" -eq 1 ] && [ ! -f "$ORDER" ]; then
		echo "gen_loop: no order file $ORDER — idle 300s"
		# Liveness only: no sweep has completed, so /healthz stays "no sweep has completed yet"
		# by design, but the loop itself must not look dead (the file mtime is the Dashboard's age).
		heartbeat_note ping
		sleep_unless_stopped 300
		continue
	fi
	echo "==== GENERATE SWEEP $(date) ===="
	if [ "$sweep" -eq 0 ]; then
		echo "gen_loop: LIBRARY_SWEEP is off, running queued requests only"
		ORDER=/dev/null
	fi
	# The sweep is in progress from here until sweep-end below. loop.state reads "running" from
	# last_sweep_start > last_sweep_end, so a working loop never reports "unknown" again.
	heartbeat_note sweep-start --root "$ANIME"
	python3 -m dubtitlerr.review.review_requests recover "$REQUESTS_DIR" </dev/null 2>&1 || echo "  recover failed (continuing)"
	[ -f "$APP_DIR/dubtitlerr/review/review_settings_write.py" ] || python3 -m dubtitlerr.review.review_requests write-effective-env "$EFFECTIVE_ENV_FILE" </dev/null 2>&1 || echo "  effective.env write failed (continuing)"
	drain_requests
	while IFS= read -r show; do
		if stop_requested; then
			echo "gen_loop: soft stop, not starting ${show:-the next show}"
			exit 0
		fi
		wait_for_window
		if stop_requested; then
			echo "gen_loop: soft stop, not starting ${show:-the next show}"
			exit 0
		fi
		# Pause boundary between shows: the show in flight finished, no new one starts.
		hold_while_paused || exit 0
		drain_requests
		case "$show" in '' | \#*) continue ;; esac
		[ -d "$ANIME/$show" ] || {
			echo "skip-missing: $show"
			continue
		}
		apply_runtime_env
		prep_show "$show"
		[ "$_prep_stopped" -eq 0 ] || exit 0
		GLOSS="$_prep_gloss"
		heartbeat_note show --name "$show"
		echo "#### GENERATE $show $(date)"
		# crash-resume: re-run until clean exit or no progress (poison files get a .fail marker)
		attempt=0
		rc=1 # until a generate pass below exits 0, this show counts as failed in the sweep summary
		while :; do
			if stop_requested; then
				echo "gen_loop: soft stop, not starting a generate pass on $show"
				break
			fi
			attempt=$((attempt + 1))
			before=$(progress_count "$ANIME/$show")
			# `&& rc=0 || rc=$?` (not a bare command + `rc=$?` on the next line) so a nonzero
			# exit from generate.py doesn't trip `set -e` and kill the container before the
			# crash-resume logic below ever sees it — while still capturing the real exit code.
			SHOW_NAME="$show" GLOSSARY_FILE="$GLOSS" REQUIRE_ENG="${REQUIRE_ENG:-1}" COMPUTE_TYPE="${COMPUTE_TYPE:-int8}" \
				python3 -m dubtitlerr.pipeline.generate --root "$ANIME/$show" </dev/null && rc=0 || rc=$?
			after=$(progress_count "$ANIME/$show")
			[ "$rc" = "0" ] && break
			if [ "$after" -le "$before" ]; then
				echo "GENERATE stalled on $show (rc=$rc) — moving on"
				# A stall is a pass failure the loop already detects; report it, then behave exactly as before.
				notify_send --event pass.failed --summary "generate stalled on $show (rc=$rc): a pass produced no episode" \
					--loop generate --detail '{"stage":"generate","reason":"stalled"}'
				break
			fi
			[ "$attempt" -ge 40 ] && {
				echo "GENERATE max passes on $show"
				break
			}
			echo "GENERATE crashed on $show (rc=$rc) — resume pass $((attempt + 1)) $(date)"
		done
		sweep_shows=$((sweep_shows + 1))
		[ "$rc" -eq 0 ] || sweep_failed=$((sweep_failed + 1))
		# Review items this show left open -- read from the queue files the review side already writes
		# for this show alone, so the digest never adds a second walk of the library.
		_show_pending=$(python3 -m dubtitlerr.review.review_requests unresolved-count "$ANIME/$show" 2>/dev/null) || _show_pending=0
		case "$_show_pending" in '' | *[!0-9]*) _show_pending=0 ;; esac
		sweep_review_pending=$((sweep_review_pending + _show_pending))
	done <"$ORDER"
	# The sweep is over (it always completes the show loop -- a soft stop exits above). Sweep end
	# is the timestamp /healthz and loop.state judge freshness from; with LIBRARY_SWEEP off no
	# order file gated the sweep, so none is recorded as missing.
	if [ "$sweep" -eq 1 ]; then
		heartbeat_note sweep-end --root "$ANIME" --order "$ORDER" --shows "$sweep_shows" --failed "$sweep_failed"
	else
		heartbeat_note sweep-end --root "$ANIME" --shows "$sweep_shows" --failed "$sweep_failed"
	fi
	echo "==== SWEEP COMPLETE — idle ${RESCAN_INTERVAL:-21600}s $(date) ===="
	notify_send --event sweep.completed --loop generate \
		--summary "sweep complete: $sweep_shows show(s), $sweep_failed failed, $sweep_review_pending review item(s) pending" \
		--counts "{\"shows\": $sweep_shows, \"failed\": $sweep_failed, \"review_pending\": $sweep_review_pending}"
	# P33: the app owns its backlog. One hygiene sweep stamps, expires and caps every queue,
	# then sends ONE batched digest. review.pending fires only for the exception classes that
	# historically needed a person -- no per-item nagging, no second walk of the library.
	python3 -m dubtitlerr.core.queue_hygiene --notify 2>/dev/null || true
	idle_target="${RESCAN_INTERVAL:-21600}"
	elapsed=0
	last_check=0
	poll_interval="${REQUEST_POLL_INTERVAL:-${IDLE_CHECK_INTERVAL:-60}}"
	while [ "$elapsed" -lt "$idle_target" ]; do
		if stop_requested; then
			echo "gen_loop: soft stop, leaving the idle wait"
			exit 0
		fi
		# A pause during the long idle wait takes effect now, not after RESCAN_INTERVAL:
		# hold at the boundary, then restart the idle countdown on resume.
		if ! processing_running; then
			hold_while_paused || exit 0
			elapsed=0
			last_check=0
		fi
		if [ $((elapsed - last_check)) -ge "$poll_interval" ] || [ "$elapsed" -eq 0 ]; then
			last_check="$elapsed"
			check_window_open
			if [ "$is_open" = "open" ]; then
				req_pending=$(python3 -m dubtitlerr.review.review_requests next "$REQUESTS_DIR" 2>/dev/null) && req_rc=0 || req_rc=$?
				if [ "$req_rc" -eq 0 ] && [ -n "$req_pending" ]; then
					echo "gen_loop: pending request detected during idle wait"
					drain_requests
				fi
			fi
			# Keep the document fresh while idle: /healthz judges staleness from last_sweep_end (3x
			# RESCAN_INTERVAL), so this changes no verdict, but the Dashboard's heartbeat age comes
			# from the file mtime, which would otherwise grow for the whole RESCAN_INTERVAL idle.
			heartbeat_note ping
		fi
		sleep_unless_stopped 1
		elapsed=$((elapsed + 1))
	done
	if stop_requested; then
		echo "gen_loop: soft stop, leaving the idle wait"
		exit 0
	fi
done
