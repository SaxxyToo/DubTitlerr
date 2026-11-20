#!/bin/sh
# ONE merge+mux pass over the whole Anime Library (runs in the container, as root).
# For every episode with a dubtitle sidecar that isn't muxed yet:
#   1. assemble: repair low-confidence lines + merge signs/songs into a .ass (mkv);
#      mp4 episodes have no embedded signs so they stay a dialogue-only .srt,
#   2. delivery: mux (default) embeds the .ass (mkv) / .srt (mp4) into the video as a
#      default "Dubtitles" track WITH the embedded fonts (mp4 is remuxed to mkv) -> signs
#      render correctly, a .dubtitles.done stamp is written, sidecars removed. With
#      OUTPUT_MODE=sidecar the assembled subtitle is published as
#      <episode><SIDECAR_SUFFIX>.<ass|srt> instead and the video is never rewritten.
# Idempotent: a delivered episode has the stamp (and, in mux mode, the track), so it's
# skipped next pass.
# Per-episode availability; refreshes Plex when this pass muxed anything new.
# Env: MERGE_ROOTS, OLLAMA_URL, REPAIR_MODEL, GLOSSARY_DIR, PLEX_URL, PLEX_TOKEN, PLEX_SECTION,
#      MIN_FREE_GB, KEEP_LANGS.
ROOT="${MERGE_ROOTS:-/media/Anime Library}"
APP="${APP_DIR:-$(cd "$(dirname "$0")" 2>/dev/null && pwd)}"

# Notifications are REPORTING ONLY, exactly as in gen_loop.sh: every call is failure-swallowed, so a
# missing module, a broken config or an unreachable destination can never change this pass's exit
# code, its stage records, or whether an episode is muxed. Emissions sit at pass boundaries.
notify_send() {
	python3 -m dubtitlerr.core.notify send "$@" >/dev/null 2>&1 || true
}
# The heartbeat is REPORTING ONLY, exactly as in gen_loop.sh: /healthz, /api/v1/health, loop.state and
# the Dashboard read the same document (dubtitlerr/core/heartbeat.py). A pass writes its own merge_*
# fields and never the sweep fields the readers judge. Failure-swallowed + stdlib-only, so a missing
# module (misbuilt image) or an unwritable config volume cannot change this pass's exit code.
heartbeat_note() {
	HEARTBEAT_PATH="${HEARTBEAT_PATH:-${CONFIG_DIR:-/config}/heartbeat.json}" \
		python3 -m dubtitlerr.core.heartbeat "$@" >/dev/null 2>&1 || true
}
# Fail as a CONFIGURATION error before the per-stem loop, not as a "crashed" repair on the
# first episode it touches: a missing $APP/dubtitlerr/pipeline/repair.py used to record {"repair": "crashed"}
# against that stem, which then blocked every later pass until someone cleared the stage.
_preflight="dubtitlerr/pipeline/repair.py dubtitlerr/pipeline/dub_signs_merge.py dubtitlerr/pipeline/mux.py"
# publish_gate.py too, when the gate will run. The old check lived INSIDE the per-stem loop,
# which is a pipe into `while` (a subshell): a missing file made that subshell exit 1 while
# the pass carried on and printed "MERGE PASS COMPLETE" without delivering a single episode.
if [ "${PUBLISH_GATE:-1}" != "0" ]; then
	_preflight="$_preflight dubtitlerr/pipeline/publish_gate.py"
fi
for _f in $_preflight; do
	if [ ! -f "$APP/$_f" ]; then
		echo "FATAL: $_f not found under APP=$APP — set APP_DIR to the directory holding the pipeline scripts"
		notify_send --event pass.failed --summary "merge pass cannot start: $_f is missing under APP" \
			--loop merge --detail '{"stage":"preflight","reason":"module-missing"}'
		exit 1
	fi
done
export PYTHONPATH="$APP${PYTHONPATH:+:$PYTHONPATH}" # so the `python3 -m dubtitlerr...` stages and the inline snippets resolve

# One settings read per pass decides whether the per-episode delivery event is worth a subprocess:
# episode.delivered is off by default, and with no subscriber the per-stem loop pays nothing at all.
_notify_episodes=0
python3 -m dubtitlerr.core.notify subscribed --event episode.delivered >/dev/null 2>&1 && _notify_episodes=1
# Stored settings (settings.json / secrets.json, written by the web UI): applied at the start of EVERY pass, so a change takes
# effect at the next pass boundary and never mid-episode. NOT `source`d: review_settings_write.py parses and re-validates the
# files and prints only registered, shell-quoted `export` lines. The guard is the exporter, not a settings file: once the old
# runtime.env is migrated away there is no file to test for. If the exporter is missing or fails, the pass runs on the container
# environment. (The exporter's stderr is kept: it names anything it ignored, which would otherwise be invisible.)
if [ -f "$APP/dubtitlerr/review/review_settings_write.py" ]; then
	eval "$(python3 -m dubtitlerr.review.review_settings_write export --scope merge)" || true
fi
# Report what THIS pass runs with, to its own file: the generate loop writes effective.env from an environment that never
# applies the repair profile, so sharing one file would make the Settings page's "pending" flip as the two take turns.
# Written on every pass (also after a reset), but only where a config directory exists.
if [ -d "${CONFIG_DIR:-/config}" ] && [ -f "$APP/dubtitlerr/review/review_requests.py" ]; then
	python3 -m dubtitlerr.review.review_requests write-effective-env "${CONFIG_DIR:-/config}/merge-effective.env" || true
fi
command -v ffmpeg >/dev/null 2>&1 || {
	echo "FATAL: ffmpeg not found — image is misbuilt"
	notify_send --event pass.failed --summary "merge pass cannot start: ffmpeg is missing (image is misbuilt)" \
		--loop merge --detail '{"stage":"preflight","reason":"ffmpeg-missing"}'
	exit 1
}
command -v mkvmerge >/dev/null 2>&1 || {
	echo "FATAL: mkvmerge not found — image is misbuilt"
	notify_send --event pass.failed --summary "merge pass cannot start: mkvmerge is missing (image is misbuilt)" \
		--loop merge --detail '{"stage":"preflight","reason":"mkvmerge-missing"}'
	exit 1
}
python3 -c "import pysubs2" >/dev/null 2>&1 || {
	echo "FATAL: pysubs2 not found — image is misbuilt"
	notify_send --event pass.failed --summary "merge pass cannot start: pysubs2 is missing (image is misbuilt)" \
		--loop merge --detail '{"stage":"preflight","reason":"pysubs2-missing"}'
	exit 1
}
cd "$ROOT" || {
	echo "merge_pass: missing $ROOT"
	notify_send --event pass.failed --summary "merge pass cannot start: library root $ROOT is missing" \
		--loop merge --detail '{"stage":"preflight","reason":"root-missing"}'
	exit 1
}
# mux's OUTPUT_MODE decides what "delivered" means; the per-episode event names it (muxed / sidecar).
# Read AFTER the stored settings above, so a mode set in the UI is the one the event reports.
case "$(printf '%s' "${OUTPUT_MODE:-mux}" | tr '[:upper:]' '[:lower:]')" in
sidecar*) _delivery_mode=sidecar ;;
*) _delivery_mode=mux ;;
esac
# Every path this pass hands a stage starts from here, never from ".". A relative path is only
# as good as the working directory it was made against, and that directory can be replaced
# under a running pass (a rename, a restore, an NFS remount): measured 2026-11-17, the stages'
# write_stage then died in os.replace and publish_gate in os.getcwd(), and the census below
# found nothing and printed COMPLETE. $PWD is the logical path cd just resolved; it still
# names the library after the directory behind it has been swapped.
ROOT_ABS=$PWD

# EXTRA_DIRS single source of truth (B7/B9): data/extras.txt via shell/lib.sh, with an
# inline fallback (the pre-consolidation regex) if the lib or data file isn't present
# (e.g. run under the deprecated $APP=/scripts flow, which doesn't ship these files).
#
# The file test is load-bearing, NOT belt-and-braces: `.` is a POSIX special builtin, so
# sourcing a missing file is a fatal error that terminates a non-interactive shell BEFORE
# the `|| true` is ever considered. Written as `. file || true`, this whole script exited
# silently -- no output, status 2, nothing assembled -- whenever APP_DIR was not /app,
# which is exactly the fallback case the comment above claims to support.
[ -f "$APP/shell/lib.sh" ] && . "$APP/shell/lib.sh"
# Soft stop: stop_requested comes from lib.sh; without it nothing ever stops.
command -v stop_requested >/dev/null 2>&1 || stop_requested() { return 1; }
command -v library_sweep_enabled >/dev/null 2>&1 || library_sweep_enabled() { return 0; }
command -v hold_while_paused >/dev/null 2>&1 || hold_while_paused() { return 0; }

# SCAN_DIRS ("$@" below): where the pass looks. The whole library by default. With LIBRARY_SWEEP off, only the show folders that
# have a run request (pending, running, or done within the 7-day prune window), so nothing spends repair tokens unqueued.
# A show name from a request file is a plain folder name (review_requests.py refuses path parts), and a folder that does
# not exist is dropped. No qualifying show: the pass does nothing.
set -- "$ROOT_ABS"
if ! library_sweep_enabled; then
	set --
	_req_shows=$(python3 -m dubtitlerr.review.review_requests shows "${REQUESTS_DIR:-${CONFIG_DIR:-/config}/requests}" 7 </dev/null 2>/dev/null) || _req_shows=""
	while IFS= read -r _show; do
		[ -n "$_show" ] && [ -d "$ROOT_ABS/$_show" ] && set -- "$@" "$ROOT_ABS/$_show"
	done <<EOF
$_req_shows
EOF
	if [ "$#" -eq 0 ]; then
		echo "merge_pass: LIBRARY_SWEEP is off and no show has a run request — nothing to merge"
		exit 0
	fi
fi
PATTERN=$(extras_grep_pattern "$APP/data/extras.txt" 2>/dev/null || echo '(Behind The Scenes|Deleted Scenes|Featurettes|Interviews|Scenes|Shorts|Trailers|Other|Extras)')
# The pass is real work from here until merge-end below (everything that could decline to run has
# already exited). /api/v1/system/status does not read merge_* (its loop object describes the
# generate loop), so these fields are additive: what the merge loop last did, without touching the
# sweep timestamps the health readers judge.
heartbeat_note merge-start

# Prints the outcome of a stage that failed, nothing if it passed. Fails CLOSED: if the
# check itself cannot run, report "gate-error" so mux is skipped rather than run blind.
stage_failure() {
	python3 -c "import sys; from dubtitlerr.core import common; s=sys.argv[1]; f=common.failed_stage(s, only=(sys.argv[2],)); print(common.read_stages(s)[f].get('outcome') if f else '')" "$1" "$2" </dev/null 2>/dev/null || echo "gate-error"
}

# True if the stage left ANY record. Fails closed: a check that cannot run counts as "no".
has_record() {
	python3 -c "import sys; from dubtitlerr.core import common; sys.exit(0 if sys.argv[2] in common.read_stages(sys.argv[1]) else 1)" "$1" "$2" </dev/null >/dev/null 2>&1
}

before=$(find "$@" -type f -name "*.dubtitles.done" | wc -l)
# Optional bridge (ADOPT_DUB_EN=1): a dubsub-delivered .dub.en.srt/.ass is renamed to this
# pipeline's own artifact name so repair/signs can upgrade it and sidecar mode republishes
# it under the house name. OFF by default: on a library that already carries hundreds of
# dubsub sidecars, adopting them all would re-assemble every one in a single pass.
if [ "${ADOPT_DUB_EN:-0}" = "1" ]; then
	find "$@" -type f \( -name "*.dub.en.srt" -o -name "*.dub.en.ass" \) | while IFS= read -r f; do
		case "$f" in
		*.dub.en.srt) stem="${f%.dub.en.srt}"; ext="srt" ;;
		*) stem="${f%.dub.en.ass}"; ext="ass" ;;
		esac
		# A stamped episode is already delivered; an existing artifact is already queued.
		[ -f "$stem.dubtitles.done" ] && continue
		[ -f "$stem.eng.dubtitles.srt" ] && continue
		[ -f "$stem.eng.dubtitles.ass" ] && continue
		echo "### adopt $(basename "$f")"
		mv -n "$f" "$stem.eng.dubtitles.$ext" || true
	done
fi
# episodes with a sidecar (srt or ass) -> dedup to the stem
find "$@" -type f \( -name "*.eng.dubtitles.srt" -o -name "*.eng.dubtitles.ass" \) |
	# Extras are matched BELOW the root only: the paths are absolute, and a library mounted
	# under a folder such as ".../Other/" must not lose every episode to the "Other" extra.
	R="$ROOT_ABS" P="/$PATTERN/" awk 'BEGIN { r = ENVIRON["R"]; p = tolower(ENVIRON["P"]) } tolower(substr($0, length(r) + 1)) !~ p' |
	sed -E 's/\.eng\.dubtitles\.(srt|ass)$//' | sort -u | while IFS= read -r stem; do
	# window closed (container_run.sh got SIGTERM): start no new stem. This loop is a pipe
	# subshell, so `break`, not `exit`: the census and the COMPLETE/DONE lines below still run.
	if stop_requested; then
		echo "merge_pass: soft stop, not starting $stem"
		break
	fi
	# Pause boundary between stems: the stem in flight finished, no new one starts until Start.
	# A soft stop that arrives while holding breaks this pipe subshell exactly like the check above.
	if ! hold_while_paused; then
		echo "merge_pass: soft stop while paused, not starting $stem"
		break
	fi
	[ -f "$stem.dubtitles.fail" ] && continue # generate crashed on it -> skip
	# Already delivered: a stamp that is current for the video AND records the running
	# OUTPUT_MODE. The default SIDECAR_SUFFIX puts the published file at the artifact's own
	# name, so the finder keeps seeing it, and without this every pass re-ran repair -> signs
	# -> mux over an episode it had already delivered (LLM calls and a rewrite of a published
	# subtitle, every MERGE_INTERVAL). mux.already_delivered() is the predicate mux.py itself
	# uses and --delivered exposes it, so there is no second definition. A dropped or
	# invalidated stamp (review verdicts, tools/reapply_glossary.py, tools/reopen_for_signs.py)
	# makes this false again, which is exactly how those reopen an episode.
	if python3 -m dubtitlerr.pipeline.mux --delivered "$stem" </dev/null >/dev/null 2>&1; then
		echo "skip: already delivered $stem"
		continue
	fi
	if [ ! -f "$stem.eng.dubtitles.ass" ] && [ -f "$stem.eng.dubtitles.srt" ]; then
		echo "### assemble $stem"
		# Drop records from older runs so the crash fallbacks and the gates below only ever see
		# THIS run's outcome (a stale "ok" would otherwise hide a repair that died silently).
		python3 -c "import sys; from dubtitlerr.core import common; common.clear_stage(sys.argv[1], 'repair', 'signs')" "$stem" </dev/null >/dev/null 2>&1 || true
		python3 -m dubtitlerr.pipeline.repair "$stem.dubtitles.conf.json" </dev/null
		rc=$?
		if [ $rc -ne 0 ]; then
			python3 -c "import sys; from dubtitlerr.core import common; s=sys.argv[1]; rc=sys.argv[2]; sys.exit(0 if 'repair' in common.read_stages(s) else common.write_stage(s, 'repair', 'crashed', 'rc='+rc) or 1)" "$stem" "$rc" </dev/null >/dev/null 2>&1 || true
			# the fallback's own write can fail (unwritable sidecar): no record at all is a failure
			if ! has_record "$stem" repair; then
				echo "skip mux: repair crashed (no record)"
				continue
			fi
		fi
		bad=$(stage_failure "$stem" repair)
		if [ -n "$bad" ]; then
			echo "skip mux: repair failed ($bad)" # the .srt stays, no .ass: the next pass retries
			continue
		fi
		python3 -m dubtitlerr.pipeline.dub_signs_merge "$stem.eng.dubtitles.srt" </dev/null
		rc=$?
		if [ $rc -ne 0 ]; then
			python3 -c "import sys; from dubtitlerr.core import common; s=sys.argv[1]; rc=sys.argv[2]; sys.exit(0 if 'signs' in common.read_stages(s) else common.write_stage(s, 'signs', 'crashed', 'rc='+rc) or 1)" "$stem" "$rc" </dev/null >/dev/null 2>&1 || true
			# the fallback's own write can fail (unwritable sidecar): no record at all is a failure
			if ! has_record "$stem" signs; then
				echo "skip mux: signs crashed (no record)"
				continue
			fi
		fi
		bad=$(stage_failure "$stem" signs)
		if [ -n "$bad" ]; then
			echo "skip mux: signs failed ($bad)"
			continue
		fi
	fi
	if stop_requested; then
		echo "skip mux: soft stop (the assembled sidecar waits for the next pass)"
		continue
	fi
	# Publish gate (sidecar only): refuse to publish a subtitle that is not one -- no
	# punctuation, cards that run off the screen, a known mishear name, or a file that stops
	# halfway through the episode. Inspects the subtitle text and the container duration;
	# never touches the video. PUBLISH_GATE=0 is the escape hatch.
	if [ "${PUBLISH_GATE:-1}" != "0" ]; then
		if ! python3 -m dubtitlerr.pipeline.publish_gate "$stem" </dev/null; then
			# Record the refusal on the mux stage: without it the census counts this stem as
			# neither done nor failed and prints COMPLETE while an episode was refused.
			python3 -c "import sys; from dubtitlerr.core import common; common.write_stage(sys.argv[1],'mux','refused','publish-gate')" "$stem" </dev/null >/dev/null 2>&1 || true
			echo "skip mux: publish gate failed"
			continue
		fi
	fi
	for ext in mkv mp4 m4v; do # mux the video (root); embeds + stamps
		[ -f "$stem.$ext" ] && {
			python3 -c "import sys; from dubtitlerr.core import common; common.clear_stage(sys.argv[1], 'mux')" "$stem" </dev/null >/dev/null 2>&1 || true
			python3 -m dubtitlerr.pipeline.mux --apply "$stem.$ext" </dev/null
			rc=$?
			if [ $rc -ne 0 ]; then
				python3 -c "import sys; from dubtitlerr.core import common; s=sys.argv[1]; rc=sys.argv[2]; sys.exit(0 if 'mux' in common.read_stages(s) else common.write_stage(s, 'mux', 'crashed', 'rc='+rc) or 1)" "$stem" "$rc" </dev/null >/dev/null 2>&1 || true
			fi
			# Per-episode delivery event, ONLY when a provider subscribed to it: the whole pass already
			# asked once, so an unsubscribed install pays nothing here. mux --delivered is the pass's own
			# definition of delivered (it is what the skip above uses), so the event never claims an
			# episode the stamp does not.
			if [ "$_notify_episodes" -eq 1 ] &&
				python3 -m dubtitlerr.pipeline.mux --delivered "$stem" </dev/null >/dev/null 2>&1; then
				notify_send --event episode.delivered --summary "delivered $(basename "$stem")" \
					--loop merge --detail "{\"mode\":\"$_delivery_mode\"}"
			fi
			break
		}
	done
done
after=$(find "$@" -type f -name "*.dubtitles.done" | wc -l)

if [ "$after" -gt "$before" ] && [ -n "${PLEX_TOKEN:-}" ]; then
	echo "muxed $((after - before)) new episode(s) -> refreshing Plex"
	python3 -m dubtitlerr.pipeline.plex_refresh "watch" </dev/null
fi

# The per-stem loop above runs in a subshell (piped into `while`), so no
# counter incremented inside it survives past `done` -- the COMPLETE/
# INCOMPLETE decision is computed here, after the loop exits, by a fresh scan
# of every stage sidecar under ROOT rather than from any loop-local state.
scan_out=$(find "$@" -type f -name "*.dubtitles.stages.json" -print0 |
	xargs -0 -r python3 -c "
import sys
from dubtitlerr.core import common
n = 0
for p in sys.argv[1:]:
    stem = p[: -len(common.STAGES_SUFFIX)]
    if common.failed_stage(stem) is not None:
        n += 1
print(n)
" 2>/dev/null)
scan_rc=$?
# xargs may split a big library into several python runs, one count line each: sum them.
failed_count=$(printf '%s\n' "$scan_out" | awk '{ n += $1 } END { print n + 0 }')
if [ "$scan_rc" -eq 0 ] && [ "${failed_count:-0}" -eq 0 ] && stop_requested; then
	# stems skipped by a soft stop have no failure record, so COMPLETE would be misleading.
	# Real failures win over this line: a count is more useful than "stopped" (see below).
	echo "MERGE PASS STOPPED (soft stop): some stems were not processed"
elif [ "$scan_rc" -ne 0 ]; then
	# never claim COMPLETE when the scan could not run (a broken `import common` used to
	# fall through to a count of 0)
	echo "MERGE PASS INCOMPLETE: failed-stage scan error"
elif [ "${failed_count:-0}" -eq 0 ]; then
	echo "MERGE PASS COMPLETE"
else
	echo "MERGE PASS INCOMPLETE: $failed_count episodes with a failed stage"
fi
echo "MERGE_PASS_DONE new=$((after - before)) total_done=$after $(date)"
# Pass boundary report. Counts are the ones this pass already printed above (new stamps = delivered,
# the failed-stage census, and the library-wide delivered total); reporting only, after the work.
merge_new=$((after - before))
merge_failed=${failed_count:-0}
heartbeat_note merge-end --delivered "$merge_new" --failed "$merge_failed"
notify_send --event merge.completed \
	--summary "merge pass: $merge_new delivered, $merge_failed failed, $after total done" \
	--loop merge --counts "{\"delivered\": $merge_new, \"failed\": $merge_failed, \"total_done\": $after}"
