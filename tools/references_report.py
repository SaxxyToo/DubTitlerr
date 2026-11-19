#!/usr/bin/env python3
"""Read-only library audit: which reference axes each episode has and which route fits it.

R2 records the reference role a *card* actually used (``ref_role`` / ``anchor_mode``). This
tool answers the question one level up, before any pipeline work starts: for every episode
in a media root, what does the file itself offer?

    axes   native-dub           a native English track classified as dub captions
           native-translation   only a native translation track
           both                 both kinds of native reference
           none                 no native English dialogue text (PGS-only, no subs, ours only)

    route  sourced    native dub captions exist -> the episode can be sourced, not generated
           generate   no native dub captions -> ASR (+ repair anchored on translation, or
                      unanchored when the show declares it)
           unfillable nothing to transcribe (unreadable video / no audio stream)

Everything here is READ-ONLY. The only file this tool ever writes is its own report, and it
refuses a report path inside a scanned root so an accident cannot land on the library.

Layout/env follows ``watch_queue.py``: ``--root`` defaults to ``ANIME_ROOT`` (``/media/Anime
Library``), repeatable. ``MEDIA_ROOT``/``OUTPUT_ROOT`` are deliberately not used to redirect
reads -- an audit must see what the pipeline reads. ``SIDECAR_SUFFIX`` (default
``.eng.dubtitles``) is honoured for the pipeline's own artifact name, and the house
``.dub.en.*`` name is checked too because the deployed install publishes under it.
``GLOSSARY_DIR`` (default ``/config/glossaries``) only supplies the per-show
``unanchored_repair`` flag that tells a planner whether repair can add anything to an
unanchored episode.

Classification is R1's (``dubtitlerr.core.reftracks``), so subrip/srt tracks count as
references and bitmap codecs (PGS/VobSub) do not. Two depths are available:

* **metadata** (default) — one ffprobe per episode, roles from the title rules in
  ``reftracks.title_role`` plus the provenance rule in ``common.is_our_track``. An explicit
  ``Dub``/``Dubtitle`` title is confirmed; ``SDH``/``CC``/blank stay provisional exactly as
  R1 says. Fast enough to audit the whole library (ffprobe reads headers, not the file).
* **content-verified** (``--verify-content``) — R1's full ``classify()``: every text stream
  is extracted and parsed so ``dialogue_events`` is known and, when the episode's own
  ``.dubtitles.conf.json`` exists, its ASR text confirms or overturns the title prior,
  exactly as ``repair.reference_tracks`` does. This is the measured role, but extracting a
  track requires demuxing the whole file (~40 s on a 1.4 GB episode), so it is meant for a
  show or a smoke run, not 917 episodes.

The JSON holds counts and track metadata only -- never subtitle text or ASR text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import common, reftracks  # noqa: E402

SCHEMA = 1

AXES = ("native-dub", "native-translation", "both", "none")
ROUTES = ("sourced", "generate", "unfillable")

# Copied from generate.SKIP_FILE_RE (that module pulls in whisper at import time, which an
# audit must not require) -- creditless OP/ED and "scene" clips are not episodes.
SKIP_FILE_RE = re.compile(r"\bNCED\b|\bNCOP\b|\bNCBD\b|-\s*scene\b|creditless", re.I)

CONF_SUFFIX = ".dubtitles.conf.json"
DEFAULT_ROOT = os.environ.get("ANIME_ROOT", "/media/Anime Library")
DEFAULT_GLOSSARY_DIR = os.environ.get("GLOSSARY_DIR", "/config/glossaries")


# --- pure classification -> report vocabulary ---------------------------------


def _native(tracks, role, require_events=True):
    return [
        t for t in tracks
        if t.provenance == reftracks.PROVENANCE_NATIVE and t.role == role and (t.events or not require_events)
    ]


def axes_of(tracks, *, require_events=True) -> str:
    """Which native reference axes an episode actually carries.

    With ``require_events`` (content-verified mode) a track with no parsed dialogue events
    does not count: a "Dub"-titled stream that is only signs/karaoke is not dub captions,
    and treating it as such would plan a sourced route with nothing to source. In metadata
    mode events are unknown, so presence is judged on the R1 title/provenance rules alone.
    ``ours`` never counts (it is our own output, not a witness).
    """
    dub = bool(_native(tracks, reftracks.ROLE_DUB, require_events))
    trans = bool(_native(tracks, reftracks.ROLE_TRANSLATION, require_events))
    if dub and trans:
        return "both"
    if dub:
        return "native-dub"
    if trans:
        return "native-translation"
    return "none"


def route_of(tracks, *, audio_ok: bool = True, unanchored_ok: bool = False, require_events=True) -> tuple[str, str]:
    """``(route, reason)`` for one episode. See the module docstring for the definitions.

    ``audio_ok`` False means nothing can be transcribed. ``unanchored_ok`` is the show's
    ``unanchored_repair`` flag and never changes the route -- an unanchored episode still
    generates ASR text -- but it is spelled out in the reason so a planner sees whether the
    repair stage can contribute.
    """
    if not audio_ok:
        return "unfillable", "no readable audio stream to transcribe"
    dub = _native(tracks, reftracks.ROLE_DUB, require_events)
    if dub:
        codecs = sorted({t.codec for t in dub})
        return "sourced", "native dub captions present (" + ",".join(codecs) + ")"
    if _native(tracks, reftracks.ROLE_TRANSLATION, require_events):
        reason = "translation reference only; generate + translation-anchored repair"
        return "generate", reason
    if unanchored_ok:
        return "generate", "no native English reference; show declares unanchored_repair"
    return "generate", "no native English reference; ASR only (repair stays unanchored)"


# --- filesystem inventory (read-only) -----------------------------------------


def sidecar_files(stem: str, suffixes=None) -> list[str]:
    """Existing published/artifact sidecars beside ``stem`` (basenames, order preserved).

    Two names matter: the house convention ``<stem>.dub.en.<ext>`` this install publishes
    under (``OUTPUT_MODE=sidecar``, ``SIDECAR_SUFFIX=.dub.en``), and whatever
    ``SIDECAR_SUFFIX`` says here (``.eng.dubtitles`` by default), which is where the
    pipeline writes its own artifact before publishing.
    """
    if suffixes is None:
        configured = os.environ.get("SIDECAR_SUFFIX", ".eng.dubtitles") or ".eng.dubtitles"
        suffixes = [".dub.en.ass", ".dub.en.srt", configured + ".ass", configured + ".srt"]
    seen, out = set(), []
    for suff in suffixes:
        p = stem + suff
        if p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(os.path.basename(p))
    return out


def stamp_state(stem: str, video: str) -> dict:
    """The episode's ``.dubtitles.done`` stamp, resolved through common's own staleness rule."""
    doc = common.read_stamp(stem + common.STAMP_SUFFIX)
    if doc is None:
        return {"present": False, "current": False, "stale_tiers": ["transcribe", "text"], "mode": None}
    stale = sorted(common.stale_tiers(doc, video))
    legacy = doc.get("version")
    return {
        "present": True,
        "current": not stale,
        "stale_tiers": stale,
        "mode": common.stamp_mode(doc),
        "transcribe_version": doc.get("transcribe_version", legacy),
        "text_version": doc.get("text_version", legacy),
    }


def probe_audio(video: str) -> bool:
    """True when ffprobe reports at least one audio stream. A probe failure reads as no audio."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", video],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=90,
        )
        return bool(json.loads(r.stdout or "{}").get("streams"))
    except Exception:  # noqa: BLE001 - an unreadable file is reported, never raised
        return False


def probe_subtitle_streams(video: str) -> list[dict]:
    """Every subtitle stream ffprobe reports (raw, unfiltered), or [] on a probe failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index,codec_name:stream_tags=language,title",
                "-of", "json", video,
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=90,
        )
        return json.loads(r.stdout or "{}").get("streams", [])
    except Exception:  # noqa: BLE001 - one bad probe must not abort the audit
        return []


def tracks_from_streams(streams) -> list[reftracks.Track]:
    """R1's metadata classification of raw ffprobe subtitle streams.

    Same stream filters as ``reftracks.classify`` (text codecs only, accepted languages
    only) and the same ``title_role`` / ``is_our_track`` predicates, but no extraction, so
    ``events`` is empty and ``role_confirmed`` carries R1's own title-prior meaning
    (explicit Dub/Signs confirmed, SDH/CC/blank provisional).
    """
    tracks = []
    for st in streams:
        codec = st.get("codec_name") or ""
        if codec not in reftracks.TEXT_CODECS:
            continue
        lang = ((st.get("tags") or {}).get("language", "") or "").lower()
        if lang not in common.SUB_LANGS:
            continue
        title = common.stream_title(st)
        role, confirmed = reftracks.title_role(title)
        provenance = reftracks.PROVENANCE_OURS if common.is_our_track(title) else reftracks.PROVENANCE_NATIVE
        tracks.append(
            reftracks.Track(
                index=st["index"],
                codec=codec,
                lang=lang,
                title=title,
                provenance=provenance,
                role=role,
                role_confirmed=confirmed,
                role_overlap=None,
                events=(),
            )
        )
    return tracks


def read_sample(stem: str) -> str | None:
    """The episode's stored ASR card text, or None. Read-only; never fatal."""
    try:
        with open(stem + CONF_SUFFIX, encoding="utf-8") as f:
            cards = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cards, list):
        return None
    return " ".join(str(c.get("text") or "") for c in cards if isinstance(c, dict))


def show_unanchored(show_dir: str, glossary_dir: str) -> bool:
    """The per-show glossary's ``unanchored_repair`` flag (missing/unreadable -> False)."""
    try:
        with open(os.path.join(glossary_dir, os.path.basename(show_dir) + ".json"), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    return bool(isinstance(doc, dict) and doc.get("unanchored_repair"))


def iter_episodes(root: str):
    """Video files under ``root``, pruning Plex extras dirs and creditless/scene clips."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in common.EXTRA_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith(common.VIDEO_EXTS):
                continue
            if SKIP_FILE_RE.search(fn):
                continue
            yield os.path.join(dirpath, fn)


def episode_report(video: str, root: str, glossary_dir: str, sample: bool, verify: bool = False) -> dict:
    """One episode's inventory. Read-only; any classifier failure degrades, never raises."""
    stem, _ = os.path.splitext(video)
    rel_dir = os.path.relpath(os.path.dirname(os.path.abspath(video)), os.path.abspath(root))
    show = rel_dir.split(os.sep)[0] if rel_dir not in (".", "") else os.path.basename(os.path.abspath(root))
    show_dir = os.path.join(root, show)
    streams = probe_subtitle_streams(video)
    if verify:
        try:
            tracks = reftracks.classify(video, sample_text=read_sample(stem) if sample else None)
        except Exception:  # noqa: BLE001 - one bad file must not abort an audit
            tracks = []
    else:
        tracks = tracks_from_streams(streams)
    audio_ok = probe_audio(video)
    unanchored = show_unanchored(show_dir, glossary_dir)
    route, reason = route_of(tracks, audio_ok=audio_ok, unanchored_ok=unanchored, require_events=verify)
    sidecars = sidecar_files(stem)
    return {
        "file": os.path.basename(video),
        "path": video,
        "dir": rel_dir,
        "axes": axes_of(tracks, require_events=verify),
        "route": route,
        "route_reason": reason,
        "audio_ok": audio_ok,
        "unanchored_repair": unanchored,
        "subtitle_streams": [
            {
                "index": st["index"],
                "codec": st.get("codec_name") or "",
                "lang": ((st.get("tags") or {}).get("language", "") or "").lower(),
                "title": common.stream_title(st),
            }
            for st in streams
        ],
        "tracks": [
            {
                "index": t.index,
                "codec": t.codec,
                "lang": t.lang,
                "title": t.title,
                "provenance": t.provenance,
                "role": t.role,
                "role_confirmed": t.role_confirmed,
                "role_overlap": None if t.role_overlap is None else round(t.role_overlap, 3),
                "dialogue_events": len(t.events) if verify else None,
            }
            for t in tracks
        ],
        "ours_embedded": sum(1 for t in tracks if t.provenance == reftracks.PROVENANCE_OURS),
        "sidecar": {"present": bool(sidecars), "files": sidecars},
        "stamp": stamp_state(stem, video),
    }


# --- report assembly ----------------------------------------------------------


def build_report(roots, glossary_dir: str, *, sample=True, jobs=4, show_filter=None, limit=None,
                 verify=False) -> dict:
    """Walk every root and return the report document (no writes)."""
    buckets: dict[tuple, dict] = {}
    order: list[tuple] = []
    work = []
    for root in roots:
        if not os.path.isdir(root):
            print(f"WARNING: root does not exist, skipped: {root}", file=sys.stderr)
            continue
        for video in iter_episodes(root):
            show = os.path.relpath(video, root).split(os.sep)[0]
            if show_filter and show_filter.lower() not in show.lower():
                continue
            key = (root, show)
            if key not in buckets:
                buckets[key] = {"show": show, "root": root, "episodes": []}
                order.append(key)
            buckets[key]["episodes"].append(video)
    episodes_by_show = [buckets[k] for k in order]

    for bucket in episodes_by_show:
        per_episode = []
        for video in bucket["episodes"]:
            if limit is not None and len(per_episode) >= limit:
                break
            per_episode.append((video, bucket["root"]))
        work.extend(per_episode)
        bucket["_todo"] = per_episode

    def run(item):
        video, root = item
        return episode_report(video, root, glossary_dir, sample, verify)

    results = list(ThreadPoolExecutor(max_workers=max(1, jobs)).map(run, work)) if work else []

    reports_by_show, i = {}, 0
    for bucket in episodes_by_show:
        eps = []
        for _ in bucket["_todo"]:
            eps.append(results[i])
            i += 1
        reports_by_show[(bucket["root"], bucket["show"])] = summarize_show(bucket["show"], bucket["root"], eps)

    shows = [reports_by_show[(b["root"], b["show"])] for b in episodes_by_show]
    totals = {
        "shows": len(shows),
        "episodes": sum(s["episodes"] for s in shows),
        "axes": {a: sum(s["axes"][a] for s in shows) for a in AXES},
        "routes": {r: sum(s["routes"][r] for s in shows) for r in ROUTES},
        "sidecar_present": sum(s["sidecar_present"] for s in shows),
        "stamp_current": sum(s["stamp_current"] for s in shows),
        "stamp_stale": sum(s["stamp_stale"] for s in shows),
        "unstamped": sum(s["unstamped"] for s in shows),
    }
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": list(roots),
        "glossary_dir": glossary_dir,
        "mode": "content-verified" if verify else "metadata",
        "sample": bool(sample and verify),
        "totals": totals,
        "shows": shows,
    }


def summarize_show(show: str, root: str, episodes: list[dict]) -> dict:
    """Per-show counts from per-episode reports -- counts only, no subtitle text."""
    axes = {a: sum(1 for e in episodes if e["axes"] == a) for a in AXES}
    routes = {r: sum(1 for e in episodes if e["route"] == r) for r in ROUTES}
    dub_codecs: dict[str, int] = {}
    for e in episodes:
        for t in e["tracks"]:
            if t["provenance"] != reftracks.PROVENANCE_NATIVE or t["role"] != reftracks.ROLE_DUB:
                continue
            # metadata mode has no event count; presence of the track is what is known
            if t["dialogue_events"] is not None and t["dialogue_events"] == 0:
                continue
            dub_codecs[t["codec"]] = dub_codecs.get(t["codec"], 0) + 1
    return {
        "show": show,
        "root": root,
        "episodes": len(episodes),
        "axes": axes,
        "routes": routes,
        "dub_codecs": dict(sorted(dub_codecs.items())),
        "sidecar_present": sum(1 for e in episodes if e["sidecar"]["present"]),
        "embedded_ours": sum(1 for e in episodes if e["ours_embedded"]),
        "stamp_current": sum(1 for e in episodes if e["stamp"]["current"]),
        "stamp_stale": sum(1 for e in episodes if e["stamp"]["present"] and not e["stamp"]["current"]),
        "unstamped": sum(1 for e in episodes if not e["stamp"]["present"]),
        "unfillable": [{"file": e["file"], "reason": e["route_reason"]} for e in episodes if e["route"] == "unfillable"],
        "items": episodes,
    }


def summary_lines(report: dict) -> list[str]:
    """Human summary: one line per show plus totals. Counts only."""
    out = []
    width = max([len(s["show"]) for s in report["shows"]] + [22])
    for s in report["shows"]:
        a, r = s["axes"], s["routes"]
        out.append(
            f"{s['show']:<{width}}  ep={s['episodes']:<4} "
            f"axes[dub={a['native-dub']} trans={a['native-translation']} both={a['both']} none={a['none']}]  "
            f"routes[sourced={r['sourced']} generate={r['generate']} unfillable={r['unfillable']}]  "
            f"sidecar={s['sidecar_present']} stamp[ok={s['stamp_current']} stale={s['stamp_stale']} "
            f"none={s['unstamped']}]"
        )
    t = report["totals"]
    out.append("")
    out.append(
        f"TOTAL shows={t['shows']} episodes={t['episodes']} "
        f"axes[dub={t['axes']['native-dub']} trans={t['axes']['native-translation']} "
        f"both={t['axes']['both']} none={t['axes']['none']}] "
        f"routes[sourced={t['routes']['sourced']} generate={t['routes']['generate']} "
        f"unfillable={t['routes']['unfillable']}]"
    )
    return out


def _inside(path: str, roots) -> bool:
    p = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only reference-axis + route report for a media library.")
    ap.add_argument("--root", action="append", default=None,
                    help=f"media root to scan (repeatable; default $ANIME_ROOT = {DEFAULT_ROOT!r})")
    ap.add_argument("--glossary-dir", default=DEFAULT_GLOSSARY_DIR,
                    help=f"per-show glossary dir, for unanchored_repair (default {DEFAULT_GLOSSARY_DIR!r})")
    ap.add_argument("--json", default=None, help="write the JSON report here ('-' = stdout)")
    ap.add_argument("--verify-content", action="store_true",
                    help="R1 full classify(): extract+parse every text track (slow: demuxes each file)")
    ap.add_argument("--no-sample", action="store_true",
                    help="with --verify-content, skip the conf.json ASR sample (title roles only)")
    ap.add_argument("--jobs", type=int, default=4, help="parallel ffprobe/ffmpeg workers (default 4)")
    ap.add_argument("--show", default=None, help="only shows whose directory name contains this text")
    ap.add_argument("--limit", type=int, default=None, help="max episodes per show (smoke runs)")
    ap.add_argument("--quiet", action="store_true", help="suppress the human summary")
    args = ap.parse_args(argv)

    roots = args.root or [DEFAULT_ROOT]
    if args.json and args.json != "-" and _inside(args.json, roots):
        print(f"refusing to write the report inside a scanned root (read-only audit): {args.json}", file=sys.stderr)
        return 2

    report = build_report(roots, args.glossary_dir, sample=not args.no_sample, jobs=args.jobs,
                          show_filter=args.show, limit=args.limit, verify=args.verify_content)

    if args.json == "-":
        print(json.dumps(report, indent=2))
    elif args.json:
        parent = os.path.dirname(os.path.abspath(args.json))
        os.makedirs(parent, exist_ok=True)
        common.atomic_write(args.json, lambda f: json.dump(report, f, indent=2), mode=0o644)
        print(f"report: {args.json}", file=sys.stderr)
    if not args.quiet:
        print("\n".join(summary_lines(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
