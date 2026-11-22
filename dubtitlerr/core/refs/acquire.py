#!/usr/bin/env python3
"""Fetch one Japanese reference subtitle for an episode whose release ships none.

The gap this fills is the one ``glossary_acquire`` documents: a show whose releases carry no
mineable track cannot get its names from anywhere, so every proper noun is left to the ASR's
guessing. Jimaku has the fansub track. This module goes and gets it, decodes it, and then
refuses it unless it provably belongs to this video.

The refusal is the point. A downloaded track is untrusted input: it may be another cut,
another season, a season pack, or a file for the wrong show that happened to match a name.
So every candidate goes through ``subtitle_fit.check`` against the video's duration before it
is handed on, and a failure is reported with its reason rather than swallowed.

Nothing is written outside ``out`` and nothing is muxed: acquisition produces a sidecar
candidate plus a verdict, and the repair lane decides what to do with it.

Usage (module form, like every other stage):

    JIMAKU_API_KEY=... python3 -m dubtitlerr.core.refs.acquire \\
        --anilist-id 154587 --season 1 --episode 5 --video /media/.../Show.S01E05.mkv \\
        --out /tmp/refs

Exit status is 0 when a reference was admitted, 1 when none was (with the reasons on stdout
as JSON), 2 on a usage or configuration error.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field

from dubtitlerr.core import subtitle_encoding, subtitle_fit
from dubtitlerr.core.refs import match
from dubtitlerr.core.refs.jimaku import Jimaku, JimakuAuthError, JimakuError

MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_CANDIDATES = 6
DEFAULT_LANGUAGE = "ja"


@dataclass
class Candidate:
    """One downloaded file and the verdict on it."""

    name: str
    entry_id: int
    language: str | None
    encoding: str
    cues: int
    verdict: dict
    data: bytes = field(default=b"", repr=False)

    def describe(self):
        return {
            "name": self.name,
            "entry_id": self.entry_id,
            "language": self.language,
            "encoding": self.encoding,
            "cues": self.cues,
            "verdict": self.verdict,
        }


@dataclass
class Result:
    ok: bool
    chosen: str | None
    candidates: list
    entries: list
    reasons: list = field(default_factory=list)

    def describe(self):
        return {
            "ok": self.ok,
            "chosen": self.chosen,
            "entries": list(self.entries),
            "reasons": list(self.reasons),
            "candidates": [candidate.describe() for candidate in self.candidates],
        }


def probe_duration(video):
    """The video's duration in seconds via ffprobe, or ``None`` when it cannot be read."""
    if not video or not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def _safe_member(name):
    """True for a subtitle member of an archive: a plain basename, no traversal, right suffix."""
    if not name or name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
        return False
    return match.is_subtitle(os.path.basename(name))


def extract_subtitles(name, data):
    """``[(name, bytes), ...]`` subtitle members of an archive, or the file itself.

    ZIP is read with the stdlib. RAR needs ``bsdtar`` (the images carry it); without it the
    archive is reported as unextractable rather than silently skipped -- a rar-only entry
    looks like "no subtitles exist" otherwise.
    """
    lower = (name or "").lower()
    if match.is_subtitle(lower):
        return [(lower, data)]
    if lower.endswith(".zip"):
        out = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not _safe_member(info.filename) or info.file_size > MAX_MEMBER_BYTES:
                        continue
                    out.append((os.path.basename(info.filename), archive.read(info)))
        except (zipfile.BadZipFile, OSError) as exc:
            raise JimakuError(f"acquisition: {name} is not a readable zip ({exc})") from exc
        return out
    if lower.endswith(".rar"):
        return _extract_rar(name, data)
    return []


def _extract_rar(name, data):
    if not shutil.which("bsdtar"):
        raise JimakuError(f"acquisition: {name} is a rar and bsdtar is not installed")
    try:
        listing = subprocess.run(["bsdtar", "-tf", "-"], input=data, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JimakuError(f"acquisition: cannot list {name} ({exc})") from exc
    if listing.returncode != 0:
        raise JimakuError(f"acquisition: bsdtar cannot list {name}")
    out = []
    for member in listing.stdout.decode("utf-8", "replace").splitlines():
        if not _safe_member(member):
            continue
        extracted = subprocess.run(
            ["bsdtar", "-xOf", "-", member],
            input=data,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if extracted.returncode == 0 and extracted.stdout and len(extracted.stdout) <= MAX_MEMBER_BYTES:
            out.append((os.path.basename(member), extracted.stdout))
    return out


def _read_candidate(name, payload, entry_id, language, duration, witness):
    decoded = subtitle_encoding.decode(payload, language=language)
    cues = subtitle_fit.parse_cues(decoded.text)
    verdict = subtitle_fit.check(cues, duration=duration, witness=witness).describe()
    return Candidate(
        name=name,
        entry_id=entry_id,
        language=language,
        encoding=decoded.encoding,
        cues=len(cues),
        verdict=verdict,
        data=payload,
    )


def acquire(
    client,
    *,
    titles=(),
    anilist_id=None,
    season=None,
    episode=None,
    absolute_episode=None,
    language=DEFAULT_LANGUAGE,
    duration=None,
    witness=None,
    max_candidates=MAX_CANDIDATES,
    max_entries=5,
):
    """Search, download and judge reference candidates. The first admitted one wins.

    ``witness`` is an optional cue list (``[(start, end), ...]``) the candidate must agree
    with; ``duration`` is the video's length. With neither, only the structural tests run --
    that is the common case, because the whole reason for asking is that the release has no
    track to witness against.
    """
    if not client.configured:
        return Result(False, None, [], [], ["jimaku:no_api_key"])

    number = absolute_episode if absolute_episode is not None else episode
    if number is None:
        return Result(False, None, [], [], ["query:no_episode"])

    entries = []
    if anilist_id is not None:
        entries = client.entries(anilist_id=int(anilist_id))
    if not entries and titles:
        for title in titles[:3]:
            entries = client.entries(query=title)
            if entries:
                break
    if not entries:
        return Result(False, None, [], [], ["jimaku:no_entry"])

    entries = match.select_entries(entries, titles=titles, season=season, absolute=absolute_episode is not None)[:max_entries]
    if not entries:
        return Result(False, None, [], [], ["jimaku:no_matching_entry"])

    candidates = []
    reasons = []
    for entry in entries:
        entry_id = entry["id"]
        try:
            files = client.files(entry_id)
        except JimakuError as exc:
            reasons.append(f"jimaku:files_failed:{exc}")
            continue
        chosen_files = match.select_files(
            files,
            episode=number,
            season=season,
            absolute=absolute_episode is not None,
            language=language,
        )
        if not chosen_files:
            reasons.append(f"entry:{entry_id}:no_file_for_episode")
            continue
        for remote in chosen_files[: max(1, max_candidates - len(candidates))]:
            try:
                payload = client.download(remote["url"])
                members = extract_subtitles(remote.get("name", ""), payload)
            except JimakuError as exc:
                reasons.append(f"download:{remote.get('name')}:{exc}")
                continue
            if not members:
                reasons.append(f"download:{remote.get('name')}:no_subtitle_inside")
                continue
            for member_name, member_bytes in members:
                stated = match.language_from_tags(remote.get("name", "")) or language
                candidate = _read_candidate(member_name, member_bytes, entry_id, stated, duration, witness)
                candidates.append(candidate)
                if candidate.verdict.get("ok"):
                    return Result(True, member_name, candidates, [entry["id"] for entry in entries], reasons)

    reasons.append("admission:no_candidate_passed")
    return Result(False, None, candidates, [entry["id"] for entry in entries], reasons)


def save(result, out_dir):
    """Write the admitted file (or every candidate, for inspection) under ``out_dir``."""
    written = []
    os.makedirs(out_dir, exist_ok=True)
    for candidate in result.candidates:
        if result.chosen and candidate.name != result.chosen:
            continue
        if not candidate.verdict.get("ok") and result.chosen:
            continue
        name = os.path.basename(candidate.name)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(candidate.data)
        written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dubtitlerr.core.refs.acquire", description=__doc__)
    parser.add_argument("--anilist-id", type=int, default=None)
    parser.add_argument("--title", action="append", default=[], help="one or more search titles")
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--episode", type=int, default=None)
    parser.add_argument("--absolute-episode", type=int, default=None)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--video", default=None, help="the video the reference must fit (for its duration)")
    parser.add_argument("--witness", default=None, help="a subtitle file to compare line starts against")
    parser.add_argument("--out", default=None, help="directory to write the admitted file into")
    parser.add_argument("--dry-run", action="store_true", help="list the files Jimaku has, download nothing")
    args = parser.parse_args(argv)

    api_key = os.environ.get("JIMAKU_API_KEY", "")
    if not api_key:
        print(json.dumps({"ok": False, "reasons": ["jimaku:no_api_key (set JIMAKU_API_KEY)"]}))
        return 2
    if args.episode is None and args.absolute_episode is None:
        print(json.dumps({"ok": False, "reasons": ["query:no_episode (--episode or --absolute-episode)"]}))
        return 2

    client = Jimaku(api_key)
    duration = probe_duration(args.video)
    witness = None
    if args.witness:
        witness = subtitle_fit.parse_cues(subtitle_encoding.decode_file(args.witness).text)

    if args.dry_run:
        try:
            entries = client.entries(anilist_id=args.anilist_id) if args.anilist_id else []
            if not entries:
                for title in args.title[:3]:
                    entries = client.entries(query=title)
                    if entries:
                        break
            listing = {entry["id"]: [file["name"] for file in client.files(entry["id"])] for entry in entries}
        except (JimakuAuthError, JimakuError) as exc:
            print(json.dumps({"ok": False, "reasons": [str(exc)]}))
            return 2
        print(json.dumps({"ok": True, "entries": listing}, ensure_ascii=False, indent=2))
        return 0

    try:
        result = acquire(
            client,
            titles=args.title,
            anilist_id=args.anilist_id,
            season=args.season,
            episode=args.episode,
            absolute_episode=args.absolute_episode,
            language=args.language,
            duration=duration,
            witness=witness,
        )
    except (JimakuAuthError, JimakuError) as exc:
        print(json.dumps({"ok": False, "reasons": [str(exc)]}))
        return 2

    payload = result.describe()
    if args.out and result.chosen:
        payload["written"] = save(result, args.out)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
