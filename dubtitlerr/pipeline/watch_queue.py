#!/usr/bin/env python3
"""Derive the generate queue from what is actually being watched.

`gen_loop.sh` reads its show order from a flat file and re-reads it every sweep, so this
module only has to regenerate that file. It answers exactly one question -- **is this show
live?** -- and never narrows within a show: once queued, the whole series regenerates,
because the value of a subtitle track is in the episodes ahead of the viewer, not behind.

THREE SOURCES, NONE OF THEM A SUPERSET. Measured 2026-08-21; Jellyfin added 2026-11-19:

  WatchState only   One Pace after 2026-07-11. Playback is on Jellyfin; WatchState syncs
                    the watched flag into Plex WITHOUT bumping Plex's show-level
                    `lastViewedAt`, which sat exactly 40.0 days stale.
  Plex only         SPY x FAMILY, Serial Experiments Lain, Mashle, SK8 the Infinity.
                    WatchState is configured for ONE Plex user (account 1) plus the
                    household Jellyfin; other Plex users are invisible to it.
  Jellyfin direct   The server those two are downstream of. WatchState imports only the
                    slice it syncs and Plex only the accounts it was pointed at; a Jellyfin
                    row is the play itself, so the queue stops depending on what the sync
                    layer managed to copy.

Each source alone returns a confident, correctly-sorted, incomplete list. No one is a
superset, so the queue is their union.

TODO(deploy): the Jellyfin entry above is the reason it was added, not a measurement. On the
live library the deploy phase must compare Jellyfin against the WatchState∪Plex union on a
spread of shows and record what it adds; `Filters=IsPlayed,IsResumable` and the `/Users`
permission of the API key need the same live confirmation.

TRI-STATE, after tools/vad.py: reachable-with-data / reachable-empty / unreachable. An empty
answer and an unreachable source are different facts, and only one of them is evidence. If a
CONFIGURED source cannot be read, the order file is left ALONE -- a stale queue is safe; a
queue silently narrowed by an outage is not.

  One deliberate asymmetry: WatchState and Plex predate Jellyfin, and an unset one still
  REFUSES (their callers were written when the queue had no other way to exist). Jellyfin is
  additive: with neither JELLYFIN_URL nor JELLYFIN_API_KEY set it is simply not a source, so
  every existing deployment keeps working with no new configuration. A HALF-configured
  Jellyfin (one of the two set) is a mistake, not an absent source, and refuses.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dubtitlerr.core.atomic_file import atomic_write

WATCHSTATE_URL = os.environ.get("WATCHSTATE_URL", "")
WATCHSTATE_API_KEY = os.environ.get("WATCHSTATE_API_KEY", "")
PLEX_URL = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_SECTION = os.environ.get("PLEX_SECTION", "7")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "")
ANIME_ROOT = os.environ.get("ANIME_ROOT", "/media/Anime Library")
TIMEOUT = int(os.environ.get("WATCH_QUEUE_TIMEOUT", "30"))
PER_PAGE = 1000
MAX_PAGES = 100  # backstop; 20k items is 21 pages today


class Unreachable(Exception):
    """A source could not be read. NOT the same as a source with nothing to say."""


def _get(url: str, headers: dict | None = None) -> bytes:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        raise Unreachable(f"{url.split('?')[0]}: {e}") from e


def clean_title(title: str) -> str:
    """Strip the `(YYYY)` and `{tvdb-NNNN}` suffixes a library directory carries.

    Same normalisation as glossary_verify._clean_title; duplicated rather than imported
    because that module pulls in the wiki/LLM stack this one has no use for."""
    return re.sub(r"\s*\(\d{4}\)|\s*\{[^}]*\}", "", title).strip()


def from_watchstate(since: int) -> dict:
    """{show title -> last watched unix ts} from WatchState. Raises Unreachable.

    `watched=1` is not optional: over the 30 days to 2026-08-21 the state table held 804
    episode rows with watched=0 whose `updated` fell inside the window -- rows that were
    touched by a sync, not watched by a person. Counting them inflates the queue with shows
    nobody has seen."""
    if not (WATCHSTATE_URL and WATCHSTATE_API_KEY):
        raise Unreachable("WATCHSTATE_URL/WATCHSTATE_API_KEY not set")
    out: dict[str, int] = {}
    base = WATCHSTATE_URL.rstrip("/")
    page, pages = 1, 1
    # EVERY page. The rows are NOT ordered by recency -- page 1 of 21 opens on a 2009
    # timestamp -- so stopping early silently reads a near-random slice of the library and
    # calls it "recently watched".
    while page <= pages and page <= MAX_PAGES:
        body = _get(f"{base}/v1/api/history?perpage={PER_PAGE}&page={page}", {"X-apikey": WATCHSTATE_API_KEY})
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise Unreachable(f"watchstate: bad JSON on page {page}: {e}") from e
        # the payload key is `history`; `items` would read empty and look like "nothing
        # watched", which this module is specifically built not to confuse with an outage
        rows = doc.get("history")
        if rows is None:
            raise Unreachable("watchstate: no `history` key in response")
        pages = int((doc.get("paging") or {}).get("last_page") or 1)
        for it in rows:
            if it.get("type") != "episode" or not it.get("watched"):
                continue  # watched=0 rows carry an air date in `updated`, not a play time
            ts = int(it.get("updated") or 0)
            title = (it.get("title") or "").strip()
            if title and ts >= since:
                # the SHOW's id; `guids` on the row is the episode's
                title = tag_tvdb(title, (it.get("parent") or {}).get("guid_tvdb"))
                out[title] = max(out.get(title, 0), ts)
        page += 1
    return out


def from_plex(since: int) -> dict:
    """{show title -> last viewed unix ts} across ALL Plex accounts. Raises Unreachable.

    Filtered CLIENT-SIDE. Plex's `viewedAt>>=` query parameter is silently ignored: a
    60-day request on 2026-08-21 returned rows back to 2025-08-29, well-formed, with no
    indication the filter had not applied."""
    if not (PLEX_URL and PLEX_TOKEN):
        raise Unreachable("PLEX_URL/PLEX_TOKEN not set")
    url = f"{PLEX_URL.rstrip('/')}/status/sessions/history/all?librarySectionID={PLEX_SECTION}&X-Plex-Token={PLEX_TOKEN}"
    xml = _get(url).decode("utf-8", "replace")
    out: dict[str, int] = {}
    show_tvdb: dict[str, str | None] = {}
    for row in re.findall(r"<Video\b[^>]*>", xml):
        g = re.search(r'grandparentTitle="([^"]*)"', row)
        v = re.search(r'viewedAt="(\d+)"', row)
        if not (g and v):
            continue
        ts = int(v.group(1))
        if ts >= since:
            t = html.unescape(g.group(1))
            key = re.search(r'grandparentKey="(/library/metadata/\d+)"', row)
            if key:
                t = tag_tvdb(t, _plex_show_tvdb(key.group(1), show_tvdb))
            out[t] = max(out.get(t, 0), ts)
    return out


def _plex_show_tvdb(key: str, cache: dict) -> str | None:
    """The show's tvdb id from its Plex metadata, fetched once per show. History rows carry
    only the title, and two series can share one ("JoJo's Bizarre Adventure", 1993 and
    2012). A failed lookup is None, never Unreachable: the id only sharpens a match the
    title can still make, so it must not turn a readable source into an unreadable one."""
    if key not in cache:
        try:
            xml = _get(f"{PLEX_URL.rstrip('/')}{key}?X-Plex-Token={PLEX_TOKEN}").decode("utf-8", "replace")
            m = re.search(r'<Guid id="tvdb://(\d+)"', xml)
            cache[key] = m.group(1) if m else None
        except Unreachable:
            cache[key] = None
    return cache[key]


_JELLYFIN_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
_ISO_FRAC_RE = re.compile(r"\.(\d+)")


def _jellyfin_ts(value) -> int:
    """Epoch seconds from Jellyfin's ISO-8601 `LastPlayedDate`, or 0 when it is absent or
    unusable. .NET writes seven fractional digits; `fromisoformat` reads at most six, so the
    fraction is truncated first. A naive value is treated as UTC, which is what the server
    emits when it omits the offset."""
    if not value:
        return 0
    text = str(value).strip().replace("Z", "+00:00")
    text = _ISO_FRAC_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _jellyfin_users(base: str) -> list:
    """The server's users, for aggregation or name resolution. Raises Unreachable."""
    body = _get(f"{base}/Users", {"X-Emby-Token": JELLYFIN_API_KEY})
    try:
        users = json.loads(body)
    except ValueError as e:
        raise Unreachable(f"jellyfin: bad JSON from /Users: {e}") from e
    if not isinstance(users, list):
        raise Unreachable("jellyfin: /Users did not return a list")
    return users


def _jellyfin_user_ids(users: list, wanted: str) -> list:
    """The user ids to read: one when JELLYFIN_USER names one, every user when it is empty.

    `JELLYFIN_USER` accepts a user id (32 hex characters) or the account name. A raw id skips
    /Users, so an API key that cannot enumerate users still works for a deployment that knows
    the id. An empty user list, an unknown name, or a user without an id is a configuration
    failure, never "nobody watched anything"."""
    if wanted:
        if _JELLYFIN_ID_RE.fullmatch(wanted):
            return [wanted]
        for u in users:
            if (u.get("Name") or "").strip().casefold() == wanted.casefold():
                uid = str(u.get("Id") or "")
                if uid:
                    return [uid]
        raise Unreachable(f"jellyfin: user {wanted!r} not found")
    ids = [str(u["Id"]) for u in users if u.get("Id")]
    if not ids:
        raise Unreachable("jellyfin: /Users returned no users")
    return ids


def _jellyfin_scan_user(base: str, user_id: str, since: int, out: dict) -> None:
    """Merge one user's series rows into `out`, every page. Raises Unreachable."""
    start, pages = 0, 0
    while True:
        url = (
            f"{base}/Users/{user_id}/Items?Recursive=true&IncludeItemTypes=Series"
            f"&Fields=UserData,ProviderIds&Filters=IsPlayed,IsResumable"
            f"&EnableTotalRecordCount=false&StartIndex={start}&Limit={PER_PAGE}"
        )
        body = _get(url, {"X-Emby-Token": JELLYFIN_API_KEY})
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise Unreachable(f"jellyfin: bad JSON for user {user_id} at StartIndex {start}: {e}") from e
        items = doc.get("Items")
        if items is None:
            raise Unreachable("jellyfin: no `Items` key in response")
        for it in items:
            ud = it.get("UserData") or {}
            ts = _jellyfin_ts(ud.get("LastPlayedDate"))
            title = (it.get("Name") or "").strip()
            if title and ts >= since:
                key = tag_tvdb(title, (it.get("ProviderIds") or {}).get("Tvdb"))
                out[key] = max(out.get(key, 0), ts)
        # EVERY page. The selection is not ordered by recency, so the window filter above
        # cannot stop the walk early; StartIndex is the only way to see the rest.
        pages += 1
        start += len(items)
        total = doc.get("TotalRecordCount")
        if not items:
            if isinstance(total, int) and total > start:
                raise Unreachable(f"jellyfin: empty page at StartIndex {start} below TotalRecordCount {total}")
            return
        if isinstance(total, int) and total > 0 and start >= total:
            return
        if pages >= MAX_PAGES:
            # Silently returning here would be the exact failure this module exists to
            # prevent: a complete-looking queue built from the pages that happened to fit.
            raise Unreachable(f"jellyfin: more than {MAX_PAGES} pages for user {user_id}")


def from_jellyfin(since: int) -> dict:
    """{show title -> last played unix ts} from Jellyfin directly. Raises Unreachable.

    `Filters=IsPlayed,IsResumable` narrows the walk to series somebody watched or left
    part-way; `Fields=UserData,ProviderIds` carries both the timestamp and the tvdb id, so
    no per-show metadata request is needed (unlike Plex). Dates are filtered CLIENT-SIDE:
    `LastPlayedDate` has no dependable server-side lower bound and the filter combination is
    not identical on every server build, so a row counts only when its parsed timestamp is
    at or after `since` -- the same reason Plex ignores `viewedAt>>=` below.

    With JELLYFIN_USER empty every user's rows are merged and the newest play wins, because
    the queue asks "is ANYONE watching this". A configured-but-unreadable server raises;
    build() is what decides that an entirely unconfigured Jellyfin is not a source."""
    if not (JELLYFIN_URL and JELLYFIN_API_KEY):
        missing = "/".join(n for n, v in (("JELLYFIN_URL", JELLYFIN_URL), ("JELLYFIN_API_KEY", JELLYFIN_API_KEY)) if not v)
        raise Unreachable(f"{missing} not set")
    base = JELLYFIN_URL.rstrip("/")
    wanted = (JELLYFIN_USER or "").strip()
    users = [] if (wanted and _JELLYFIN_ID_RE.fullmatch(wanted)) else _jellyfin_users(base)
    out: dict[str, int] = {}
    for user_id in _jellyfin_user_ids(users, wanted):
        _jellyfin_scan_user(base, user_id, since, out)
    return out


def library_dirs(root: str) -> list:
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError as e:
        raise Unreachable(f"anime root {root}: {e}") from e


def fold(title: str) -> str:
    """Aggressive match key: case-folded, stripped of everything but letters and digits.

    Media-server titles and directory names disagree in ways that are invisible at a glance
    and each of these is a REAL case from this library on 2026-08-21:

        Plex "Trigun Stampede"                  dir "TRIGUN STAMPEDE (2023) {tvdb-421378}"
        Plex "I'm in Love with the Villainess"  dir "I'm in Love With the Villainess (2023)…"
        Plex "Marriage Toxin"                   dir "MARRIAGETOXIN (2026) {tvdb-468734}"

    Case, one capitalised preposition, and a missing space. All three would have been
    silently dropped from the queue by exact matching."""
    return re.sub(r"[^a-z0-9]+", "", clean_title(title).casefold())


_TVDB_RE = re.compile(r"\{tvdb-(\d+)\}")


def tag_tvdb(title: str, tvdb) -> str:
    """`title {tvdb-N}` -- the library's own directory convention -- so a source can hand
    the show's id to resolve_dir() through the plain {title: ts} shape. No id, no tag."""
    return f"{title} {{tvdb-{tvdb}}}" if tvdb else title


def _untag(title: str) -> str:
    """Strip only the `{...}` suffix, keeping `(YYYY)`: the year is what tells same-named
    series apart when no id is available."""
    return re.sub(r"\s*\{[^}]*\}", "", title).strip()


def _unique(pairs) -> dict:
    """key -> directory, with a key that two directories share mapped to None (ambiguous)."""
    out: dict = {}
    for k, d in pairs:
        out[k] = None if k in out else d
    return out


def _dir_index(dirs: list) -> dict:
    """The lookup tiers over the library directory names, built once. EVERY name-derived
    tier is ambiguity-aware: the clean tier used setdefault() and silently handed the 2012
    "JoJo's Bizarre Adventure" to the alphabetically-first 1993 directory (2026-09-25)."""
    return {
        "tvdb": _unique((m.group(1), d) for d in dirs for m in [_TVDB_RE.search(d)] if m),
        "exact": {d: d for d in dirs},
        "untag": _unique((_untag(d), d) for d in dirs),
        "clean": _unique((clean_title(d), d) for d in dirs),
        "fold": _unique((fold(d), d) for d in dirs),
    }


def resolve_dir(title: str, index: dict) -> str | None:
    """The library directory this title names, or None when nothing (or more than one
    thing) matches. The show's tvdb id first (a `{tvdb-N}` tag), then exact, then the name
    with its year kept, then the `(YYYY)`/`{tvdb-}` normalisation, then `fold()`."""
    m = _TVDB_RE.search(title)
    if m and index["tvdb"].get(m.group(1)):
        return index["tvdb"][m.group(1)]
    name = _untag(title)
    return (
        index["exact"].get(title)
        or index["exact"].get(name)
        or index["untag"].get(name)
        or index["clean"].get(clean_title(name))
        or index["fold"].get(fold(name))
    )


def match_dirs(titles: dict, dirs: list) -> tuple[list, list]:
    """(ordered directory names, unmatched titles). Unmatched titles are RETURNED, never
    dropped quietly -- a library rename would otherwise shrink the queue invisibly."""
    index = _dir_index(dirs)
    hits, misses = {}, []
    for t, ts in titles.items():
        d = resolve_dir(t, index)
        if d:
            hits[d] = max(hits.get(d, 0), ts)
        else:
            misses.append(t)  # includes anything ambiguous -- report, never guess
    order = [d for d, _ in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))]
    return order, sorted(misses)


def build(since: int, root: str, pins: list | None = None) -> tuple[list, dict]:
    """Union every configured source. Raises Unreachable if a configured one cannot be read.

    WatchState and Plex are required, as they always were. Jellyfin is skipped when neither
    JELLYFIN_URL nor JELLYFIN_API_KEY is set; a half-configured pair still goes through
    from_jellyfin() and refuses there instead of silently disappearing."""
    ws = from_watchstate(since)
    px = from_plex(since)
    jf = from_jellyfin(since) if (JELLYFIN_URL or JELLYFIN_API_KEY) else {}
    if not ws and not px and not jf:
        raise Unreachable("all sources reachable but empty -- cannot tell, refusing to write")
    merged: dict[str, int] = dict(ws)
    for source in (px, jf):
        for t, ts in source.items():
            merged[t] = max(merged.get(t, 0), ts)
    dirs = library_dirs(root)
    order, misses = match_dirs(merged, dirs)
    # A pin is resolved through the SAME three tiers as a watched title, and only a pin
    # that names a real directory is inserted. Inserting the raw string instead let any
    # typo or stale rename manufacture a non-empty `order` out of zero real matches, which
    # defeated main()'s zero-match refusal outright: the file was written, and gen_loop.sh
    # then skipped the nonexistent path on every pass -- a silently idle GPU, which is the
    # exact failure that refusal was added to prevent.
    index = _dir_index(dirs)
    bad_pins = []
    for p in reversed(pins or []):  # pinned shows lead, in the order given
        d = resolve_dir(p, index)
        if not d:
            bad_pins.append(p)  # reported, never inserted -- see main()
            continue
        if d in order:
            order.remove(d)
        order.insert(0, d)
    return order, {
        "watchstate": len(ws),
        "plex": len(px),
        "jellyfin": len(jf),
        "union": len(merged),
        "matched": len(order),
        "unmatched": misses,
        "bad_pins": sorted(set(bad_pins)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-days", type=int, default=90)
    ap.add_argument("--out", default=os.environ.get("ANIME_ORDER", "/config/anime_order.txt"))
    ap.add_argument("--root", default=ANIME_ROOT)
    ap.add_argument("--pin", action="append", default=[], help="always queue this show, however long since it was watched")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    import time

    since = int(time.time()) - a.window_days * 86400
    try:
        order, rep = build(since, a.root, a.pin)
    except Unreachable as e:
        print(f"watch_queue: REFUSING TO WRITE -- {e}", file=sys.stderr)
        return 2
    print(
        f"watch_queue: watchstate={rep['watchstate']} plex={rep['plex']} jellyfin={rep['jellyfin']} "
        f"union={rep['union']} matched={rep['matched']} window={a.window_days}d"
    )
    if rep["bad_pins"]:
        # Loud, but NOT fatal on its own: a mistyped pin must not stop a queue whose other
        # shows resolved perfectly. When it was the ONLY pin and nothing else matched,
        # `order` is now empty and the refusal below fires -- which is the point.
        print(f"  PIN NOT IN LIBRARY, not queued: {', '.join(rep['bad_pins'])}", file=sys.stderr)
    if rep["unmatched"]:
        print(
            f"  no library directory for {len(rep['unmatched'])}: "
            f"{', '.join(rep['unmatched'][:10])}" + (" …" if len(rep["unmatched"]) > 10 else "")
        )
    for i, d in enumerate(order, 1):
        print(f"  {i:>2} {d}")
    if a.dry_run:
        print("  (dry run -- nothing written)")
        return 0
    if not order:
        # `build()` only reaches here when at least one source had real entries (both-empty
        # raises Unreachable above), so a zero-hit match is a rename or config problem, never
        # a legitimately empty library. Refusing leaves the PREVIOUS order file in place --
        # gen_loop.sh keeps sweeping what it already had rather than going silent for
        # RESCAN_INTERVAL on an order file that parses to zero shows.
        print("watch_queue: REFUSING TO WRITE -- 0 shows matched a library directory (see unmatched above)", file=sys.stderr)
        return 2
    try:
        # 0600, unchanged: gen_loop.sh reads this as root inside the container.
        atomic_write(a.out, lambda f: f.write("\n".join(order) + "\n"), mode=0o600)
    except OSError as e:
        print(f"watch_queue: REFUSING TO WRITE -- {e}", file=sys.stderr)
        return 2
    print(f"  wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
