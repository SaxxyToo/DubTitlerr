import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import uuid
import zoneinfo
from typing import Any

from dubtitlerr.core import artifact_store
from dubtitlerr.core.atomic_file import atomic_write, atomic_write_bytes

REQUESTS_DIR = os.environ.get("REQUESTS_DIR", "/config/requests")
ANIME_ROOT = os.environ.get("ANIME_ROOT", "/media/Anime Library")

# generate.py's poison marker: written before transcription starts, left behind when the
# process dies hard, and read on every later pass as "never transcribe this episode".
CRASH_MARKER_SUFFIX = ".dubtitles.fail"
CRASH_RECORD_SUFFIX = ".dubtitles.crash.json"

# Why generate's scope report files a poison-marked episode, restated rather than imported:
# this module has to stay importable in a minimal app directory, and generate.py pulls in the
# model stack. tests/test_request_accounting.py pins the two strings together.
CRASH_REASON = "prior crash (.dubtitles.fail)"

# The review queue lives in the central artifact store since P39 (see
# dubtitlerr/core/unresolved.py). artifact_store is stdlib-only and unresolved.py is not
# (it imports common/pysubs2), so the kind name is restated here rather than imported --
# the same rule atomic_file.py and notify.py follow.
UNRESOLVED_KIND = "unresolved"

ALLOWED_BODY_KEYS = frozenset(("show", "season", "episode_re", "require_eng", "webhook", "force"))


def unresolved_pending_count(root: str) -> int:
    """Unresolved review items under ``root``, from the review queue's store rows.

    The loops' digest for ``review.pending``: called once per show the sweep already walked, so it
    never adds a second pass over the library. The predicate is unresolved.pending()'s -- an entry
    without ``resolved`` is still open -- and records exactly as that writer emits them, tolerant of
    a corrupt line. An unreadable store or entry counts as nothing rather than raising.
    """
    total = 0
    try:
        stems = artifact_store.stems(root=root, kind=UNRESOLVED_KIND)
    except Exception:
        return 0
    for stem in stems:
        try:
            entries = artifact_store.read_jsonl(stem, UNRESOLVED_KIND)
        except Exception:
            continue
        total += sum(1 for entry in entries if isinstance(entry, dict) and not entry.get("resolved"))
    return total


class Refused(Exception):
    """Raised when a request fails validation. Carries HTTP status code and error message."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


def _has_control_or_format_chars(s: str) -> bool:
    """True if string contains any Unicode Cc (control) or Cf (format) characters."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in s)


def normalize_episode_re(ep: str | None) -> str | None:
    """Normalize episode_re by stripping whitespace."""
    if ep is None:
        return None
    s = ep.strip()
    return s if s else None


def _validate_episode_re_grammar(pattern: str) -> None:
    r"""Validate episode_re against a strict safe grammar to prevent ReDoS and hangs.

    Allowed tokens:
    - ASCII letters and digits ([A-Za-z0-9])
    - Space, dot (literal or escaped), underscore, hyphen ([ ._-], r'\.')
    - Non-capturing parentheses (?: ... ) capped at nesting depth 2
    - Alternation bar (|) capped at 50 branches total
    - Character classes [Ee] and [Ss]
    - Zero repetition 0* (only after [Ee], [Ss], or digit-free literal start of branch;
      at most once in entire pattern; depth <= 1)
    - Escaped digit \d
    - Negative lookahead (?!\d)
    - Digit classes [0-9] and digit ranges [0-9]-[0-9] (e.g. [1-3])
    - Bounded repetition {1,3} on a single repeatable token (groups take NO quantifier)
    No empty or quantifier-only branches like (?:0*).
    No unbounded quantifiers (*, +, ?), no backreferences, no lookbehind,
    no .* or \w* or \s*. Max 200 chars.
    """
    if len(pattern) > 200:
        raise Refused(400, "episode_re exceeds max length of 200 characters")
    if _has_control_or_format_chars(pattern):
        raise Refused(400, "control or format characters not allowed in episode_re")
    if pattern.count("|") > 49:
        raise Refused(400, "number of alternation branches exceeds maximum of 50")

    try:
        re.compile(pattern)
    except re.error as e:
        raise Refused(400, f"invalid episode_re regex: {e}") from None

    if "(?<=" in pattern or "(?<!" in pattern.replace(r"(?!\d)", ""):
        raise Refused(400, "lookbehind regex is not permitted")
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise Refused(400, "backreferences are not permitted")

    def _new_branch():
        return {
            "branch_has_digits": False,
            "branch_has_only_literals": True,
            "last_token_was_es": False,
            "last_token_was_es_type": None,
            "has_substantive_tokens": False,
        }

    branch_stack = [_new_branch()]
    open_groups = 0
    last_token_repeatable = False
    total_zero_star_count = 0
    zero_star_after_s = 0
    zero_star_after_e = 0
    i = 0
    n = len(pattern)

    while i < n:
        if pattern[i:].startswith(r"(?!\d)"):
            cur = branch_stack[-1]
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["branch_has_only_literals"] = False
            i += 6
            last_token_repeatable = False
            continue

        if pattern[i:].startswith(r"(?:"):
            open_groups += 1
            if open_groups > 2:
                raise Refused(400, "group nesting depth exceeds maximum of 2")
            branch_stack.append(_new_branch())
            i += 3
            last_token_repeatable = False
            continue

        if pattern[i] == ")":
            if open_groups <= 0:
                raise Refused(400, "unbalanced parentheses in episode_re")
            open_groups -= 1
            closing_branch = branch_stack.pop()
            if not closing_branch["has_substantive_tokens"]:
                raise Refused(400, "empty or quantifier-only branch in episode_re")
            cur = branch_stack[-1]
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["branch_has_only_literals"] = False
            cur["has_substantive_tokens"] = True
            i += 1
            # Groups take NO quantifier: after ')' the last token is not repeatable
            last_token_repeatable = False
            continue

        if pattern[i] == "|":
            if not branch_stack[-1]["has_substantive_tokens"]:
                raise Refused(400, "empty or quantifier-only branch in episode_re")
            branch_stack[-1] = _new_branch()
            i += 1
            last_token_repeatable = False
            continue

        if pattern[i:].startswith("0*"):
            if open_groups > 1:
                raise Refused(400, "0* is not allowed inside nested groups")
            cur = branch_stack[-1]
            is_after_es = cur["last_token_was_es"]
            is_literal_start = cur["branch_has_only_literals"] and not cur["branch_has_digits"]
            if not (is_after_es or is_literal_start):
                raise Refused(400, "0* is only allowed directly after [Ee]/[Ss] or a digit-free literal branch prefix")

            if cur["last_token_was_es_type"] == "S":
                zero_star_after_s += 1
            elif cur["last_token_was_es_type"] == "E":
                zero_star_after_e += 1

            total_zero_star_count += 1
            # At most one 0* in the whole pattern, unless it matches the specials anchor ([Ss]0*...[Ee]0*)
            specials_two = (
                total_zero_star_count == 2
                and zero_star_after_s == 1
                and zero_star_after_e == 1
            )
            if total_zero_star_count > 2 or (total_zero_star_count == 2 and not specials_two):
                raise Refused(400, "0* is allowed at most once in episode_re")

            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["branch_has_only_literals"] = False
            i += 2
            last_token_repeatable = False
            continue

        if pattern[i:].startswith(r"\d"):
            cur = branch_stack[-1]
            cur["branch_has_digits"] = True
            cur["branch_has_only_literals"] = False
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["has_substantive_tokens"] = True
            i += 2
            last_token_repeatable = True
            continue

        if pattern[i:].startswith(r"\."):
            cur = branch_stack[-1]
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["has_substantive_tokens"] = True
            i += 2
            last_token_repeatable = True
            continue

        if pattern[i:].startswith(("[Ee]", "[eE]", "[Ss]", "[sS]")):
            cur = branch_stack[-1]
            cur["last_token_was_es"] = True
            cur["last_token_was_es_type"] = "S" if pattern[i + 1] in "Ss" else "E"
            cur["branch_has_only_literals"] = False
            cur["has_substantive_tokens"] = True
            i += 4
            last_token_repeatable = True
            continue

        m_range = re.match(r"^\[([0-9])-([0-9])\]", pattern[i:])
        if m_range:
            start_d = int(m_range.group(1))
            end_d = int(m_range.group(2))
            if start_d > end_d:
                raise Refused(400, "invalid character class range in episode_re")
            cur = branch_stack[-1]
            cur["branch_has_digits"] = True
            cur["branch_has_only_literals"] = False
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["has_substantive_tokens"] = True
            i += len(m_range.group(0))
            last_token_repeatable = True
            continue

        m_bound = re.match(r"^\{([1-3])(?:,([1-3]))?\}", pattern[i:])
        if m_bound:
            if not last_token_repeatable:
                raise Refused(400, "quantifier must follow a repeatable token")
            min_r = int(m_bound.group(1))
            max_r = int(m_bound.group(2)) if m_bound.group(2) else min_r
            if min_r > max_r:
                raise Refused(400, "invalid repetition bounds in episode_re")
            cur = branch_stack[-1]
            cur["last_token_was_es"] = False
            cur["last_token_was_es_type"] = None
            cur["branch_has_only_literals"] = False
            i += len(m_bound.group(0))
            last_token_repeatable = False
            continue

        c = pattern[i]
        if c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-":
            cur = branch_stack[-1]
            if c != " ":
                cur["has_substantive_tokens"] = True
            if c in "0123456789":
                cur["branch_has_digits"] = True
            if c in "Ee":
                cur["last_token_was_es"] = True
                cur["last_token_was_es_type"] = "E"
            elif c in "Ss":
                cur["last_token_was_es"] = True
                cur["last_token_was_es_type"] = "S"
            else:
                cur["last_token_was_es"] = False
                cur["last_token_was_es_type"] = None
            i += 1
            last_token_repeatable = True
            continue

        raise Refused(400, f"unsupported token or construct in episode_re: '{c}'")

    if open_groups != 0:
        raise Refused(400, "unclosed non-capturing group in episode_re")

    if not branch_stack[-1]["has_substantive_tokens"]:
        raise Refused(400, "empty or quantifier-only branch in episode_re")



def validate(body: dict, anime_root: str | None = None) -> dict:
    """Validate request body.

    Enforces containment under ANIME_ROOT, rejects path traversal and control/format chars,
    and protects against ReDoS by rejecting lookbehind, backreferences, nested quantifiers,
    alternation in quantified groups, and ungrammatical or backtracking episode_re patterns.
    Returns cleaned dictionary or raises Refused(status, error).
    """
    if not isinstance(body, dict):
        raise Refused(400, "request body must be a JSON object")

    unknown = set(body.keys()) - ALLOWED_BODY_KEYS
    if unknown:
        raise Refused(400, f"unknown keys: {', '.join(sorted(unknown))}")

    for k, v in body.items():
        if isinstance(v, str):
            if any(c in v for c in "\r\n=\0") or _has_control_or_format_chars(v):
                raise Refused(400, f"invalid character in {k}: control characters not permitted")

    if "show" not in body or not isinstance(body["show"], str):
        raise Refused(400, "show is required and must be a string")

    show = body["show"].strip()
    if not show:
        raise Refused(400, "show must not be empty")

    if "/" in show or "\\" in show or ".." in show or "\0" in show:
        raise Refused(400, "invalid show name: path traversal characters not permitted")

    if _has_control_or_format_chars(show):
        raise Refused(400, "control or format characters not allowed in show name")

    if anime_root is not None:
        real_anime = os.path.realpath(anime_root)
        show_dir = os.path.join(anime_root, show)
        real_show = os.path.realpath(show_dir)
        if not (real_show.startswith(real_anime + os.sep) and os.path.isdir(real_show)):
            if not os.path.isdir(show_dir):
                raise Refused(400, f"show directory not found: {show}")
            raise Refused(400, "show path escapes anime root")

    season = body.get("season")
    if season is not None:
        if isinstance(season, bool) or not isinstance(season, int) or season <= 0:
            raise Refused(400, "season must be a positive integer or null")

    episode_re = body.get("episode_re")
    if episode_re is not None:
        if not isinstance(episode_re, str):
            raise Refused(400, "episode_re must be a string or null")
        norm_ep = normalize_episode_re(episode_re)
        if norm_ep is None:
            raise Refused(400, "episode_re must not be empty")
        _validate_episode_re_grammar(norm_ep)
        episode_re = norm_ep

    require_eng = body.get("require_eng")
    if require_eng is not None:
        if isinstance(require_eng, bool) or require_eng not in (0, 1):
            raise Refused(400, "require_eng must be 0, 1, or null")

    webhook = body.get("webhook")
    if webhook is not None and not isinstance(webhook, dict):
        raise Refused(400, "webhook must be an object or null")

    force = body.get("force", False)
    if not isinstance(force, bool):
        raise Refused(400, "force must be a boolean")

    return {
        "show": show,
        "season": season,
        "episode_re": episode_re,
        "require_eng": require_eng,
        "webhook": webhook,
        "force": force,
    }


_SKIP_SHOW_DIRS = ("lost+found",)


def list_shows(anime_root: str | None = None) -> list[str]:
    """Show directory names directly under the library root that validate() accepts, sorted.

    One directory level only; symlinks, dot and @ directories (NAS metadata) and lost+found are skipped.
    Each name goes through validate() itself, so every listed show is
    one a run request can name; anything it refuses (traversal characters, a symlink out of the
    root, control characters) is left out."""
    aroot = anime_root or os.environ.get("ANIME_ROOT", ANIME_ROOT)
    try:
        names = [
            e.name
            for e in os.scandir(aroot)
            if e.is_dir(follow_symlinks=False) and e.name not in _SKIP_SHOW_DIRS and e.name[0] not in ".@"
        ]
    except OSError:
        return []
    out = []
    for name in names:
        try:
            if validate({"show": name}, anime_root=aroot)["show"] == name:
                out.append(name)
        except Refused:
            # A name the validator refuses is not a show; the loop keeps the names that pass.
            pass
    return sorted(out)


def derive_generate_only_re(show: str, season: int | None = None, episode_re: str | None = None) -> str:
    r"""Derive GENERATE_ONLY_RE regex for filename matching with season/episode branches."""
    ep = normalize_episode_re(episode_re)
    if season is None and not ep:
        return ""

    if season is not None and ep:
        if ep.endswith(r"(?!\d)"):
            ep = ep[:-len(r"(?!\d)")]
        branch = f"(?:^|[\\W_])[Ss]0*{season}(?:[\\W_Ee]|(?=[Ee])).*(?:{ep})(?!\\d)"
    elif season is not None:
        # Season-only branch: do NOT append (?!\d) because [Ss]0*1(?=[Ee]|[\W_]|$) already bounds the number
        branch = f"(?:^|[\\W_])[Ss]0*{season}(?=[Ee]|[\\W_]|$)"
    else:
        if ep and ep.endswith(r"(?!\d)"):
            ep = ep[:-len(r"(?!\d)")]
        branch = f"(?:{ep})(?!\\d)"

    if not (branch.startswith("(?:") and branch.endswith(")")):
        branch = f"(?:{branch})"

    return branch



def _emit_run_requested(doc: dict) -> None:
    """Tell the notification providers a run request was queued.

    Lazy import on purpose: the review server runs from an app directory that may not ship notify
    (and a provider config may be broken), and neither may cost a request. Anything that goes wrong
    here is logged to stderr and swallowed -- creation has already succeeded on disk.
    """
    try:
        from dubtitlerr.core import notify

        summary = f"run requested: {doc['show']}"
        if doc.get("season") is not None:
            summary += f" S{int(doc['season']):02d}"
        if doc.get("episode_re"):
            summary += f" {doc['episode_re']}"
        notify.emit(
            "run.requested",
            summary,
            counts={"requests": 1},
            loop="review",
            detail={"request_id": doc.get("id"), "show": doc.get("show")},
        )
    except Exception as exc:  # noqa: BLE001 -- a notification must never fail a request
        print(f"review_requests: run.requested notification failed ({type(exc).__name__})", file=sys.stderr)


def create(
    body: dict,
    anime_root: str | None = None,
    requests_dir: str | None = None,
    now: datetime.datetime | float | None = None,
) -> tuple[int, dict]:
    """Create a new run request in the queue.

    Atomic write with 0664 mode. Serialized with fcntl.flock to prevent races.
    """
    rdir = requests_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
    aroot = anime_root or os.environ.get("ANIME_ROOT", ANIME_ROOT)

    clean = validate(body, anime_root=aroot)
    os.makedirs(rdir, exist_ok=True)

    lock_file = os.path.join(rdir, ".requests.lock")
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o664)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Check for identical pending request
        try:
            for entry in os.scandir(rdir):
                if entry.is_file() and entry.name.endswith(".json") and not entry.name.startswith("."):
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            existing = json.load(f)
                        if existing.get("status") == "pending":
                            if (
                                existing.get("show") == clean["show"]
                                and existing.get("season") == clean["season"]
                                and existing.get("episode_re") == clean["episode_re"]
                            ):
                                return 409, {
                                    "error": "identical request already pending",
                                    "existing_id": existing.get("id", entry.name[:-5]),
                                }
                    except (OSError, ValueError):
                        continue
        except OSError:
            # Request dir unreadable: fall through with whatever was collected.
            pass

        # Timestamp & ID with microsecond precision
        if now is None:
            dt = datetime.datetime.now(datetime.timezone.utc)
        elif isinstance(now, (int, float)):
            dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        elif isinstance(now, datetime.datetime):
            dt = now if now.tzinfo is not None else now.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = datetime.datetime.now(datetime.timezone.utc)

        ts_str = dt.strftime("%Y%m%dT%H%M%S.%fZ")
        hex_id = uuid.uuid4().hex[:8]
        req_id = f"{ts_str}-{hex_id}"

        gen_re = derive_generate_only_re(clean["show"], clean["season"], clean["episode_re"])

        doc = {
            "id": req_id,
            "webhook": clean.get("webhook"),
            "created_at": dt.isoformat(),
            "show": clean["show"],
            "season": clean["season"],
            "episode_re": clean["episode_re"],
            "generate_only_re": gen_re,
            "require_eng": clean["require_eng"],
            "force": clean["force"],
            "status": "pending",
            "error": None,
            "finished_at": None,
        }

        dest_path = os.path.join(rdir, f"{req_id}.json")
        atomic_write_bytes(dest_path, json.dumps(doc, indent=2) + "\n", fsync=True)

        created = {"id": req_id, "status": "pending"}

    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # Outside the lock and after the file is durable: delivery can spend a timeout or two and the
    # queue must never wait for it. _emit_run_requested never raises, so this cannot fail a 201.
    _emit_run_requested(doc)
    return 201, created


def list_requests(requests_dir: str | None = None, now: Any = None) -> list[dict]:
    """List requests across requests_dir, done/, and failed/.

    Uses os.scandir only (no os.walk). Names sorted newest first, at most 200 files opened/parsed.
    """
    rdir = requests_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
    dirs_to_scan = [rdir, os.path.join(rdir, "done"), os.path.join(rdir, "failed")]

    entries = []
    for d in dirs_to_scan:
        if not os.path.isdir(d):
            continue
        try:
            for entry in os.scandir(d):
                if not entry.is_file():
                    continue
                name = entry.name
                if name.startswith("."):
                    continue
                if not (name.endswith(".json") or name.endswith(".running")):
                    continue
                entries.append((entry.name, entry.path))
        except OSError:
            continue

    entries.sort(key=lambda x: x[0], reverse=True)

    items = []
    for _name, path in entries:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and ("id" in data or "status" in data):
                items.append(data)
                if len(items) >= 200:
                    break
        except (OSError, ValueError):
            continue

    return items


def prune(
    done_dir: str,
    failed_dir: str,
    days: int = 7,
    now: datetime.datetime | float | None = None,
) -> int:
    """Prune files in done_dir and failed_dir older than `days`."""
    days = max(1, int(days))
    if now is None:
        now_ts = time.time()
    elif isinstance(now, (int, float)):
        now_ts = float(now)
    elif isinstance(now, datetime.datetime):
        now_ts = now.timestamp()
    else:
        now_ts = time.time()

    cutoff = now_ts - days * 86400
    pruned = 0

    for d in (done_dir, failed_dir):
        if not os.path.isdir(d):
            continue
        try:
            for entry in os.scandir(d):
                if entry.is_file() and entry.name.endswith(".json") and not entry.name.startswith("."):
                    try:
                        st = entry.stat()
                        if st.st_mtime < cutoff:
                            os.unlink(entry.path)
                            pruned += 1
                    except OSError:
                        continue
        except OSError:
            continue

    return pruned


def request_shows(requests_dir: str | None = None, days: int = 7) -> list[str]:
    """Sorted show names that have a run request: pending, running, or finished OK within `days`.

    Read-only. Failed requests do not count (nothing was built for them); unsafe or unreadable entries are skipped.
    """
    rdir = requests_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
    cutoff = time.time() - max(1, int(days)) * 86400
    shows = set()
    for d in (rdir, os.path.join(rdir, "done")):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.name.startswith(".") or not entry.name.endswith((".json", ".running")):
                continue
            try:
                if d != rdir and entry.stat().st_mtime < cutoff:
                    continue
                with open(entry.path, encoding="utf-8") as f:
                    show = json.load(f).get("show")
            except (OSError, ValueError, AttributeError):
                continue
            if (
                isinstance(show, str)
                and show
                and show != "."
                and not any(bad in show for bad in ("/", "\\", "..", "\0"))
                and not _has_control_or_format_chars(show)
            ):
                shows.add(show)
    return sorted(shows)


def next_pending(dir: str | None = None) -> str | None:
    """Find the oldest pending request path (FIFO by filename timestamp)."""
    rdir = dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
    if not os.path.isdir(rdir):
        return None

    candidates = []
    try:
        for entry in os.scandir(rdir):
            if entry.is_file() and entry.name.endswith(".json") and not entry.name.startswith("."):
                candidates.append((entry.name, entry.path))
    except OSError:
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    for _name, path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") == "pending":
                return path
        except (OSError, ValueError):
            continue

    return None


def _check_requests_dir_containment(path: str, requests_dir: str | None = None) -> None:
    """Ensure path is directly inside REQUESTS_DIR."""
    rdir = requests_dir or os.environ.get("REQUESTS_DIR")
    if rdir is None:
        if os.path.isdir(REQUESTS_DIR):
            rdir = REQUESTS_DIR
        else:
            return
    rdir_real = os.path.realpath(rdir)
    path_real = os.path.realpath(path)
    if os.path.dirname(path_real) != rdir_real:
        raise ValueError(f"path {path} is not directly inside REQUESTS_DIR {rdir_real}")


def mark_running(path: str, requests_dir: str | None = None) -> str:
    """Mark a pending request as running, renaming it to .running on disk."""
    _check_requests_dir_containment(path, requests_dir)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    data["status"] = "running"
    if path.endswith(".json"):
        running_path = path[:-5] + ".running"
    else:
        running_path = path + ".running"

    def render(f):
        json.dump(data, f, indent=2)
        f.write("\n")

    atomic_write(running_path, render)
    if os.path.exists(path) and os.path.abspath(path) != os.path.abspath(running_path):
        with contextlib.suppress(OSError):
            os.unlink(path)

    return running_path


def requeue(path: str, requests_dir: str | None = None) -> str:
    """Move a running request back to pending status atomically."""
    actual_path = path
    if not os.path.exists(actual_path):
        if actual_path.endswith(".json") and os.path.exists(actual_path[:-5] + ".running"):
            actual_path = actual_path[:-5] + ".running"
        elif actual_path.endswith(".running") and os.path.exists(actual_path[:-8] + ".json"):
            actual_path = actual_path[:-8] + ".json"
    _check_requests_dir_containment(actual_path, requests_dir)
    with open(actual_path, encoding="utf-8") as f:
        data = json.load(f)

    data["status"] = "pending"
    if actual_path.endswith(".running"):
        pending_path = actual_path[:-8] + ".json"
    elif actual_path.endswith(".json"):
        pending_path = actual_path
    else:
        pending_path = actual_path + ".json"

    def render(f):
        json.dump(data, f, indent=2)
        f.write("\n")

    atomic_write(pending_path, render)
    if os.path.exists(actual_path) and os.path.abspath(actual_path) != os.path.abspath(pending_path):
        with contextlib.suppress(OSError):
            os.unlink(actual_path)

    return pending_path


def verdict(report: dict | None, rc: int) -> tuple[bool, str | None]:
    """(ok, reason) for a served request, from generate's scope report (generate.scope_report).

    Fails closed. A zero exit code says the process ran, not that anything was built:
    measured 2026-11-18, a forced season request finished "done" in nine minutes having
    produced nothing. So the request is done only when every in-scope episode is rebuilt by
    this request, delivered (unforced) or waiting for the merge pass -- and a forced request
    must have rebuilt something."""
    if rc != 0:
        return False, f"generate failed (rc={rc})" + (f"; {_report_line(report)}" if report else "")
    if not isinstance(report, dict):
        return False, "generate wrote no run report, so nothing shows the request was served"
    scope = int(report.get("scope") or 0)
    if scope == 0:
        return False, "no episode matched this request"
    not_done = report.get("not_done") or {}
    if report.get("forced") and not report.get("rebuilt"):
        return False, f"forced run rebuilt nothing ({_report_line(report)})"
    missing = sum(not_done.values())
    if missing:
        reason = f"{missing} of {scope} episodes not done ({_report_line(report)})"
        remedy = _crash_remedy(report, not_done)
        if remedy:
            reason += f"; {remedy}"
        return False, reason
    return True, None


# Plain words for the skip reasons a request report carries. The raw token stays in the
# line (scripts and tests match it); the gloss is what makes a sub-only release legible to
# an operator who has never seen `no-eng-dub` before.
_REASON_GLOSS = {
    "no-eng-dub": "sub-only release; no English audio track",
    "extract-failed": "could not read the audio track",
    "no-speech": "the ASR endpoint found no speech",
}


def _report_line(report: dict) -> str:
    parts = [f"{report.get(k, 0)} {k.replace('_', ' ')}" for k in ("rebuilt", "delivered", "awaiting_merge") if report.get(k)]
    for why, n in sorted((report.get("not_done") or {}).items(), key=lambda kv: -kv[1]):
        gloss = f" ({_REASON_GLOSS[why]})" if why in _REASON_GLOSS else ""
        parts.append(f"{n} {why}{gloss}")
    return f"{report.get('scope', 0)} in scope: " + (", ".join(parts) or "nothing")


def finish(
    path: str,
    ok: bool,
    error: str | None = None,
    now: datetime.datetime | float | None = None,
    requests_dir: str | None = None,
    report: dict | None = None,
) -> str:
    """Move a running/pending request to done/ or failed/ with finished_at and error filled,
    and the scope report attached when there is one (the evidence behind the status)."""
    actual_path = path
    if not os.path.exists(actual_path):
        if actual_path.endswith(".json") and os.path.exists(actual_path[:-5] + ".running"):
            actual_path = actual_path[:-5] + ".running"
        elif actual_path.endswith(".running") and os.path.exists(actual_path[:-8] + ".json"):
            actual_path = actual_path[:-8] + ".json"
    _check_requests_dir_containment(actual_path, requests_dir)
    with open(actual_path, encoding="utf-8") as f:
        data = json.load(f)

    if now is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now, (int, float)):
        dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    elif isinstance(now, datetime.datetime):
        dt = now if now.tzinfo is not None else now.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = datetime.datetime.now(datetime.timezone.utc)

    req_id = data.get("id") or os.path.basename(actual_path).split(".")[0]
    data["status"] = "done" if ok else "failed"
    data["error"] = None if ok else (error or "failed")
    data["finished_at"] = dt.isoformat()
    if report is not None:
        data["report"] = report

    parent_dir = os.path.dirname(os.path.abspath(actual_path))
    target_dir = os.path.join(parent_dir, "done" if ok else "failed")
    os.makedirs(target_dir, exist_ok=True)

    dest_path = os.path.join(target_dir, f"{req_id}.json")
    atomic_write_bytes(dest_path, json.dumps(data, indent=2) + "\n", fsync=True)

    if os.path.abspath(actual_path) != os.path.abspath(dest_path) and os.path.exists(actual_path):
        with contextlib.suppress(OSError):
            os.unlink(actual_path)

    return dest_path


def recover_running(requests_dir: str | None = None, now: Any = None) -> list[str]:
    """Recover leftover .running files by moving them to failed/ with 'interrupted by restart'."""
    rdir = requests_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
    if not os.path.isdir(rdir):
        return []

    recovered = []
    try:
        for entry in os.scandir(rdir):
            if entry.is_file() and entry.name.endswith(".running") and not entry.name.startswith("."):
                dest = finish(entry.path, ok=False, error="interrupted by restart", now=now)
                recovered.append(dest)
    except OSError:
        # Dir unreadable: nothing to recover, and recovery must never block startup.
        pass
    return recovered


def _crash_record(stem: str) -> dict:
    """generate's `.dubtitles.crash.json` for this episode, or {} when there is none readable.

    Best effort on purpose: the file is diagnostic (exception type, message, time) and a
    missing or torn one must not hide the marker that actually blocks the episode.
    """
    try:
        with open(stem + CRASH_RECORD_SUFFIX, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def crash_marked(anime_root: str | None = None) -> list[dict]:
    """Every episode whose `.dubtitles.fail` poison marker is still on disk, sorted by stem.

    generate.py refuses a marked episode on every pass (``skip-prior-crash``), and a request
    that scopes it in fails as a whole, so a restart or deploy that killed an episode in
    flight leaves a blocker nobody can see from the app. This is that listing: one entry per
    marker, with the crash record's detail when generate left one.

    Only directories are walked; symlinked directories are not followed and dot/@
    directories (NAS metadata) are skipped, matching list_shows().
    """
    root = anime_root or os.environ.get("ANIME_ROOT", ANIME_ROOT)
    out = []
    try:
        walk = os.walk(root)
        for dirpath, dirnames, filenames in walk:
            dirnames[:] = sorted(d for d in dirnames if d[:1] not in (".", "@"))
            for name in filenames:
                if not name.endswith(CRASH_MARKER_SUFFIX):
                    continue
                marker = os.path.join(dirpath, name)
                stem = marker[: -len(CRASH_MARKER_SUFFIX)]
                try:
                    marked_at = datetime.datetime.fromtimestamp(
                        os.path.getmtime(marker), tz=datetime.timezone.utc
                    ).isoformat(timespec="seconds")
                except OSError:
                    marked_at = None
                rec = _crash_record(stem)
                crash = {k: rec[k] for k in ("exc_type", "msg", "time") if k in rec} or None
                out.append(
                    {
                        "stem": stem,
                        "marker": marker,
                        "show": os.path.relpath(stem, root).split(os.sep)[0],
                        "marked_at": marked_at,
                        "crash": crash,
                    }
                )
    except OSError:
        # An unreadable tree lists nothing rather than failing the command: the caller is an
        # operator looking for blockers, and a traceback would be a second blocker.
        return []
    out.sort(key=lambda d: d["stem"])
    return out


def clear_crash(path: str, anime_root: str | None = None) -> str:
    """Remove ONE episode's poison marker so the next request re-processes it.

    ``path`` is the episode's stem, its video file, or the marker itself -- whatever the
    listing printed can be pasted back. Confined to ANIME_ROOT by realpath, and it will only
    ever unlink a regular ``.dubtitles.fail`` file, so a typo cannot delete media.

    The crash record (`.dubtitles.crash.json`) is deliberately kept: it is the history of why
    the episode was marked, and clearing the blocker does not make that untrue.
    """
    root = os.path.realpath(anime_root or os.environ.get("ANIME_ROOT", ANIME_ROOT))
    p = os.path.abspath(os.path.expanduser(path))
    if not p.endswith(CRASH_MARKER_SUFFIX):
        p = os.path.splitext(p)[0] + CRASH_MARKER_SUFFIX
    real = os.path.realpath(p)
    if not real.startswith(root + os.sep):
        raise ValueError(f"refusing a path outside the library root: {path}")
    if not os.path.isfile(real):
        raise FileNotFoundError(f"no crash marker for {path}")
    os.unlink(real)
    return real


def _crash_remedy(report: dict, not_done: dict) -> str | None:
    """The actionable half of a failed request that is blocked only by poison markers.

    A marker is not self-explanatory and the old message ("1 of 24 episodes not done") named a
    state nobody could fix from the app. This names the episodes and the two commands: the
    listing, and the clear that lets the next request re-process them.
    """
    n = int(not_done.get(CRASH_REASON) or 0)
    if not n:
        return None
    stems = [os.path.splitext(os.path.basename(p))[0] for p in (report.get("crash_marked") or [])]
    if stems:
        shown = ", ".join(stems[:3])
        if len(stems) > 3:
            shown += f" and {len(stems) - 3} more"
        where = f"blocked by a .dubtitles.fail poison marker: {shown}"
    else:
        where = f"{n} episode(s) blocked by a .dubtitles.fail poison marker"
    return (
        f"{where}; list them with `python3 -m dubtitlerr.review.review_requests crash-marked` "
        "and clear one with `python3 -m dubtitlerr.review.review_requests clear-crash <episode>` "
        "so the next request re-processes it"
    )


def is_window_open(schedule_file: str | None = None, now: datetime.datetime | None = None) -> bool:
    """Determine if current time is within schedule window."""
    sfile = schedule_file or os.environ.get("SCHEDULE_FILE", "/config/schedule.json")
    if not os.path.isfile(sfile):
        return True

    try:
        with open(sfile, encoding="utf-8") as f:
            sched = json.load(f)
    except Exception:
        return True

    if not isinstance(sched, dict) or not sched.get("enabled", False):
        return True

    tz_str = sched.get("tz", "UTC")
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
    except Exception as e:
        print(f"review_requests: warning: unknown or unresolvable timezone {tz_str}: {e}; failing open", file=sys.stderr)
        return True

    cur_dt = now.astimezone(tz) if now is not None else datetime.datetime.now(tz)

    open_str = sched.get("open", "00:00")
    close_str = sched.get("close", "00:00")

    try:
        oh, om = map(int, open_str.split(":"))
        ch, cm = map(int, close_str.split(":"))
    except Exception:
        return True

    cur_min = cur_dt.hour * 60 + cur_dt.minute
    open_min = oh * 60 + om
    close_min = ch * 60 + cm

    if open_min == close_min:
        return True
    elif open_min < close_min:
        return open_min <= cur_min < close_min
    else:
        return cur_min >= open_min or cur_min < close_min


EFFECTIVE_ENV_ALLOW_PREFIXES = ("REPAIR_", "VERIFY_")
EFFECTIVE_ENV_ALLOW_EXACT = frozenset((
    "REQUIRE_ENG",
    "GLOSSARY_ARC_TAGS",
    "RESCAN_INTERVAL",
    "MERGE_WINDOW",
    "ANIME_ORDER",
    "ACQUIRE_BACKEND",
    "WHISPER_MODEL",
    "COMPUTE_TYPE",
    "MERGE_ROOTS",
    "ACQUIRE_APPLY",
))
EFFECTIVE_ENV_SKIP_SUBSTRINGS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
_SECRET_VALUE_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}|\bBearer\b")
_MIN_SECRET_LEN = 8  # shorter values are too common to match against other lines


def _strip_url_userinfo(val: str) -> str:
    """Strip user info by rebuilding netloc from urlsplit().hostname and port.

    A raw @ in the password must not leak a fragment.
    """
    if "://" not in val:
        return val
    try:
        parts = urllib.parse.urlsplit(val)
    except ValueError:
        return val
    if not parts.scheme or not parts.netloc or not parts.hostname:
        return val
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _reportable(key: str) -> bool:
    """Every registry key a builder process reads is reported, so the page can tell a pending change from an applied one."""
    try:
        from dubtitlerr.settings import settings_registry
    except ImportError:  # a minimal app directory without the registry keeps the fixed allow-list above
        return False
    entry = settings_registry.REGISTRY.get(key)
    return entry is not None and not entry["secret"] and bool({"gen", "merge"} & set(entry["scope"]))


def _known_secret_values(source_env: dict) -> list:
    """The values of every secret the registry knows (and the per-stage key holders), so no other line can repeat one."""
    try:
        from dubtitlerr.settings import settings_registry
    except ImportError:
        registry = {}
    else:
        registry = settings_registry.REGISTRY
    out = []
    for k, v in source_env.items():
        secret = (k in registry and registry[k]["secret"]) or k.startswith("DT_CONN_KEY_")
        if secret and len(str(v)) >= _MIN_SECRET_LEN:
            out.append(str(v))
    return out


def write_effective_env(out_file: str | None = None, env: dict | None = None) -> str:
    """Write the effective-env report atomically (mode 0640): allowed variables only, no secret, no repeated secret value, and
    the user info of every URL stripped (the report is for display, so a URL keeps its host, port and path)."""
    target = out_file or os.environ.get(
        "EFFECTIVE_ENV_FILE",
        os.path.join(os.environ.get("CONFIG_DIR", "/config"), "effective.env"),
    )
    source_env = env if env is not None else os.environ
    secret_values = _known_secret_values(source_env)

    lines = []
    for k in sorted(source_env.keys()):
        if k.endswith("_EXTRA_BODY"):
            continue
        allowed = any(k.startswith(p) for p in EFFECTIVE_ENV_ALLOW_PREFIXES) or (k in EFFECTIVE_ENV_ALLOW_EXACT)
        allowed = allowed or _reportable(k)
        if not allowed:
            continue
        if any(s in k for s in EFFECTIVE_ENV_SKIP_SUBSTRINGS):
            continue
        v = _strip_url_userinfo(str(source_env[k]))
        if _SECRET_VALUE_RE.search(v) or any(secret in v for secret in secret_values):
            continue
        val = v.splitlines()[0] if v else ""
        lines.append(f"{k}={val}\n")

    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    # 0o640, umask-proof: the host user reads the report, other users do not.
    atomic_write(target, lambda f: f.writelines(lines), mode=0o640)
    return target


SCHEDULE_FILE = os.environ.get("SCHEDULE_FILE", "/config/schedule.json")
SCHEDULE_SCHEMA_KEYS = frozenset(("enabled", "open", "close", "tz"))
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SCHEDULE = {
    "enabled": False,
    "open": "00:00",
    "close": "00:00",
    "tz": "UTC",
}


def get_schedule(schedule_file: str | None = None) -> dict:
    """Read schedule from schedule.json, returning defaults if missing or invalid."""
    sfile = schedule_file or os.environ.get("SCHEDULE_FILE", SCHEDULE_FILE)
    if not os.path.isfile(sfile):
        return dict(DEFAULT_SCHEDULE)

    try:
        with open(sfile, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_SCHEDULE)
        return {
            "enabled": bool(data.get("enabled", False)),
            "open": str(data.get("open", "00:00")),
            "close": str(data.get("close", "00:00")),
            "tz": str(data.get("tz", "UTC")),
        }
    except Exception:
        return dict(DEFAULT_SCHEDULE)


def validate_schedule(body: dict) -> dict:
    """Validate schedule payload."""
    if not isinstance(body, dict):
        raise Refused(400, "schedule body must be a JSON object")

    missing = [k for k in ("enabled", "open", "close", "tz") if k not in body]
    if missing:
        raise Refused(400, f"missing required schedule keys: {', '.join(missing)}")

    unknown = set(body.keys()) - SCHEDULE_SCHEMA_KEYS
    if unknown:
        raise Refused(400, f"unknown keys: {', '.join(sorted(unknown))}")

    if not isinstance(body["enabled"], bool):
        raise Refused(400, "enabled is required and must be a boolean")
    enabled = body["enabled"]

    open_time = body["open"]
    if not isinstance(open_time, str) or not TIME_RE.match(open_time):
        raise Refused(400, "open must be in HH:MM format (00:00 to 23:59)")

    close_time = body["close"]
    if not isinstance(close_time, str) or not TIME_RE.match(close_time):
        raise Refused(400, "close must be in HH:MM format (00:00 to 23:59)")

    tz = body["tz"]
    if not isinstance(tz, str) or not tz.strip():
        raise Refused(400, "tz is required and must be an IANA timezone name")
    tz = tz.strip()

    try:
        zoneinfo.ZoneInfo(tz)
    except Exception:
        raise Refused(400, f"invalid timezone: {tz}") from None

    return {
        "enabled": enabled,
        "open": open_time,
        "close": close_time,
        "tz": tz,
    }


def put_schedule(body: dict, schedule_file: str | None = None) -> tuple[int, dict]:
    """Atomically write schedule with 0o664 permissions, serialized with file lock."""
    clean = validate_schedule(body)
    sfile = schedule_file or os.environ.get("SCHEDULE_FILE", SCHEDULE_FILE)
    parent = os.path.dirname(os.path.abspath(sfile))
    os.makedirs(parent, exist_ok=True)

    lock_file = os.path.join(parent, ".schedule.lock")
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o664)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        atomic_write_bytes(sfile, json.dumps(clean, indent=2) + "\n", fsync=True)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    return 200, clean


def handle(
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    environ: dict | None = None,
    requests_dir: str | None = None,
    anime_root: str | None = None,
    schedule_file: str | None = None,
    now: datetime.datetime | float | None = None,
) -> tuple[int, Any]:
    """Handle HTTP requests for /api/requests and /api/schedule."""
    clean_path = path.split("?")[0].rstrip("/")
    if clean_path == "/api/requests":
        if method == "GET":
            items = list_requests(requests_dir=requests_dir, now=now)
            return 200, items

        if method == "POST":
            try:
                return create(body or {}, anime_root=anime_root, requests_dir=requests_dir, now=now)
            except Refused as r:
                return r.status, {"error": r.error}

        return 405, {"error": "method not allowed"}

    if clean_path == "/api/schedule":
        if method == "GET":
            return 200, get_schedule(schedule_file=schedule_file)

        if method == "PUT":
            try:
                return put_schedule(body or {}, schedule_file=schedule_file)
            except Refused as r:
                return r.status, {"error": r.error}

        return 405, {"error": "method not allowed"}

    return 404, {"error": "not found"}


def main() -> None:
    """CLI for shell loops (next | unpack | start | finish | finish-report | recover | crash-marked | clear-crash | prune |
    shows | window-open | write-effective-env | unresolved-count)."""
    args = sys.argv[1:]
    if not args:
        print(
            "Usage: review_requests.py next [DIR]|unpack JSON|start PATH|requeue PATH|"
            "finish PATH ok|fail [reason]|finish-report PATH REPORT RC|recover [DIR]|crash-marked [DIR]|"
            "clear-crash [--root DIR] PATH [PATH ...]|prune [DIR] [DAYS]|"
            "shows [DIR] [DAYS]|window-open [FILE]|write-effective-env [FILE]|unresolved-count DIR",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = args[0]
    if cmd == "next":
        target_dir = args[1] if len(args) > 1 else None
        rdir = target_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
        while True:
            p = next_pending(rdir)
            if not p:
                sys.exit(1)
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("request file is not a JSON object")

                show = data.get("show")
                gen_re = data.get("generate_only_re")
                req_eng = data.get("require_eng")

                if not isinstance(show, str) or not show:
                    raise ValueError("invalid show name")
                if "/" in show or "\\" in show or ".." in show or "\0" in show:
                    raise ValueError("path traversal in show name")
                if _has_control_or_format_chars(show):
                    raise ValueError("control chars in show name")

                if gen_re is not None:
                    if not isinstance(gen_re, str) or _has_control_or_format_chars(gen_re):
                        raise ValueError("control chars in generate_only_re")

                if req_eng is not None and (isinstance(req_eng, bool) or req_eng not in (0, 1, "0", "1")):
                    raise ValueError("invalid require_eng")

                # force is the request's "start over" switch: unpack exports it as FORCE_REDO
                # and generate rebuilds every in-scope episode from the cached ASR words. It
                # has to be carried here or it never leaves the request file -- measured
                # 2026-11-22: force=true requests ran, printed `todo=0`, and no-op'd.
                force = data.get("force", False)
                if not isinstance(force, bool):
                    raise ValueError("invalid force")

                out_doc = {
                    "path": p,
                    "show": show,
                    "generate_only_re": gen_re or "",
                    "require_eng": "" if req_eng is None else str(req_eng),
                    "force": force,
                }
                print(json.dumps(out_doc))
                break
            except Exception as e:
                print(f"skipping invalid request file {p}: {e}", file=sys.stderr)
                finish(p, ok=False, error="invalid request file")

    elif cmd == "unpack":
        if len(args) < 2:
            sys.exit(1)
        data = json.loads(args[1])
        print(f"PATH={data.get('path', '')}")
        print(f"SHOW={data.get('show', '')}")
        print(f"GENERATE_ONLY_RE={data.get('generate_only_re', '')}")
        print(f"REQUIRE_ENG={data.get('require_eng', '')}")
        print(f"FORCE_REDO={'1' if data.get('force') is True else ''}")

    elif cmd == "start":
        if len(args) < 2:
            print("Usage: review_requests.py start PATH", file=sys.stderr)
            sys.exit(1)
        try:
            mark_running(args[1])
        except Exception as e:
            print(f"error marking running: {e}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "requeue":
        if len(args) < 2:
            print("Usage: review_requests.py requeue PATH", file=sys.stderr)
            sys.exit(1)
        try:
            requeue(args[1])
        except Exception as e:
            print(f"error requeuing request: {e}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "finish":
        if len(args) < 3:
            print("Usage: review_requests.py finish PATH ok|fail [reason]", file=sys.stderr)
            sys.exit(1)
        path = args[1]
        outcome = args[2]
        ok = outcome == "ok"
        reason = args[3] if len(args) > 3 else None
        try:
            finish(path, ok=ok, error=reason)
        except Exception as e:
            print(f"error finishing request: {e}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "finish-report":
        # gen_loop: decide done/failed from generate's scope report and the exit code.
        if len(args) < 4:
            print("Usage: review_requests.py finish-report PATH REPORT RC", file=sys.stderr)
            sys.exit(1)
        path, report_path = args[1], args[2]
        try:
            rc = int(args[3])
        except ValueError:
            rc = 1
        try:
            with open(report_path, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            report = None
        ok, reason = verdict(report, rc)
        try:
            finish(path, ok=ok, error=reason, report=report)
        except Exception as e:
            print(f"error finishing request: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"request {'done' if ok else 'failed'}" + (f": {reason}" if reason else ""))

    elif cmd == "unresolved-count":
        # gen_loop: one show's open review items, for the review.pending digest at sweep end.
        if len(args) < 2:
            print("Usage: review_requests.py unresolved-count DIR", file=sys.stderr)
            sys.exit(1)
        print(unresolved_pending_count(args[1]))

    elif cmd == "recover":
        target_dir = args[1] if len(args) > 1 else None
        recover_running(target_dir)

    elif cmd == "crash-marked":
        # The library's poison markers, one stem per line (tab + crash detail when recorded).
        # Read-only: listing is the discovery half, clear-crash is the explicit action.
        root = args[1] if len(args) > 1 else None
        for item in crash_marked(root):
            crash = item.get("crash") or {}
            detail = "\t".join(str(crash[k]) for k in ("exc_type", "msg") if crash.get(k))
            print(item["stem"] + (f"\t{detail}" if detail else ""))

    elif cmd == "clear-crash":
        targets = args[1:]
        root = None
        if targets[:1] == ["--root"]:
            if len(targets) < 3:
                print("Usage: review_requests.py clear-crash [--root DIR] PATH [PATH ...]", file=sys.stderr)
                sys.exit(1)
            root, targets = targets[1], targets[2:]
        elif not targets:
            print("Usage: review_requests.py clear-crash [--root DIR] PATH [PATH ...]", file=sys.stderr)
            sys.exit(1)
        rc = 0
        for target in targets:
            try:
                print(f"cleared {clear_crash(target, root)}")
            except (OSError, ValueError) as e:
                print(f"error clearing {target}: {e}", file=sys.stderr)
                rc = 1
        if rc:
            sys.exit(rc)

    elif cmd == "prune":
        target_dir = args[1] if len(args) > 1 else None
        rdir = target_dir or os.environ.get("REQUESTS_DIR", REQUESTS_DIR)
        days = int(args[2]) if len(args) > 2 else 7
        pruned = prune(os.path.join(rdir, "done"), os.path.join(rdir, "failed"), days=days)
        print(f"pruned {pruned} files")

    elif cmd == "shows":
        days = int(args[2]) if len(args) > 2 else 7
        for show in request_shows(args[1] if len(args) > 1 else None, days=days):
            print(show)

    elif cmd == "window-open":
        sfile = args[1] if len(args) > 1 else None
        print("open" if is_window_open(sfile) else "closed")

    elif cmd == "write-effective-env":
        out = args[1] if len(args) > 1 else None
        write_effective_env(out)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
