"""[S-7] the review server: handlers only, no socket is ever opened.

Security posture, and why it is not the LAN default: container_run.sh runs as root so
generate.py can chown into the media tree; this server adds WRITE routes to that same
process tree -- routes that rewrite subtitles and force re-muxes. A downstream user on host
networking would otherwise expose an unauthenticated root-owned endpoint. So an unset
REVIEW_TOKEN generates one; only an explicitly empty REVIEW_TOKEN disables auth."""

import json
import os
import stat
import time
from typing import Any

import pytest
import store_seed

from dubtitlerr.core import artifact_store, common, decisions, unresolved
from dubtitlerr.review import review_server

# Card texts and queue entries are kept in CORRESPONDENCE on purpose. repair.py only ever
# queues a line it processed, so every entry's original_text is a real card of this episode.
# An earlier version of this fixture queued "a"/"c"/"d" against a single card -- a state the
# pipeline cannot produce, and the same class of trap that cost two earlier sprints. It also
# matters directly now: [S-6]'s orphan filter releases an episode whose queue entries match
# no current conf.json row, so a careless fixture would quietly describe an orphaned queue.
_CARDS = ["I saw spondum", "the ship sailed", "he went thataway", "we run this joint"]


def _episode(tmp_path, name="ep"):
    """An episode with a queue holding one of each reason that matters here."""
    stem = str(tmp_path / name)
    store_seed.conf(
        stem + ".dubtitles.conf.json", [{"start": i * 2.0, "end": i * 2.0 + 2.0, "text": t} for i, t in enumerate(_CARDS)]
    )
    unresolved.record(stem, "repair_applied", "accepted", original_text=_CARDS[0], proposed_text="I saw Spandam")
    unresolved.record(
        stem, "repair", "rejected_guard", original_text=_CARDS[1], proposed_text="the ship has sailed", reference="ref"
    )
    unresolved.record(stem, "repair", "no_reference", original_text=_CARDS[2])
    unresolved.record(stem, "repair", "llm_empty", original_text=_CARDS[3])
    return stem


def test_an_unset_token_is_generated_persisted_0600_and_required(tmp_path, monkeypatch):
    """The unsafe default is the thing being tested away.

    An earlier draft of the spec had unset mean "no auth, which is the LAN default". That
    reasoning was about the MAINTAINER's convenience and did not transfer to a downstream
    install running as root on host networking. Reversed after adversarial review."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    tok = review_server.resolve_token(str(tmp_path))

    assert tok and len(tok) >= 32, "a generated token must not be guessable"
    path = os.path.join(str(tmp_path), "review_token")
    assert os.path.exists(path), "it must survive a restart, or every restart locks the operator out"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "0600: the media tree is group-writable, this must not be"
    assert open(path).read().strip() == tok
    assert review_server.resolve_token(str(tmp_path)) == tok, "a second start reuses it rather than rotating"


def test_an_explicitly_empty_token_still_generates_one(tmp_path, monkeypatch):
    """An earlier version of this server treated REVIEW_TOKEN= as an opt-out of auth. Too
    many installs left it blank by accident -- a stray .env line, or Compose's
    ${REVIEW_TOKEN:-} rendering an unset variable as an empty string -- and got an
    unauthenticated root-owned endpoint without meaning to. An empty value is now treated
    exactly like an unset one: a token is still generated, and auth stays on."""
    monkeypatch.delenv("REVIEW_AUTH", raising=False)
    monkeypatch.setenv("REVIEW_TOKEN", "")
    tok = review_server.resolve_token(str(tmp_path))

    assert tok and len(tok) >= 32, "an empty REVIEW_TOKEN must still generate a real token"
    assert review_server.auth_required(str(tmp_path)) is True, "an empty REVIEW_TOKEN must not disable auth"


def test_review_auth_off_is_the_only_way_to_disable_auth(tmp_path, monkeypatch):
    """REVIEW_TOKEN's value -- unset, empty, or set -- must never disable auth by itself.
    Only REVIEW_AUTH=off does, and it does so regardless of REVIEW_TOKEN."""
    monkeypatch.delenv("REVIEW_AUTH", raising=False)

    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    assert review_server.auth_required(str(tmp_path)) is True, "unset REVIEW_TOKEN must not disable auth"

    monkeypatch.setenv("REVIEW_TOKEN", "")
    assert review_server.auth_required(str(tmp_path)) is True, "empty REVIEW_TOKEN must not disable auth"

    monkeypatch.setenv("REVIEW_TOKEN", "hunter2")
    assert review_server.auth_required(str(tmp_path)) is True, "a real REVIEW_TOKEN must not disable auth"

    monkeypatch.setenv("REVIEW_AUTH", "off")
    assert review_server.auth_required(str(tmp_path)) is False, "REVIEW_AUTH=off is the only disable"


def test_review_auth_off_on_the_wide_bind_gets_a_startup_warning(tmp_path, monkeypatch, capsys):
    """REVIEW_AUTH=off and the default 0.0.0.0 bind are each individually fine and
    documented; together they mean every write route is open to the network. That
    combination lived only in the module docstring -- an operator who finds the opt-out in
    a forum thread should see the risk in their own logs."""
    monkeypatch.setenv("REVIEW_AUTH", "off")
    review_server.announce_token(str(tmp_path), bind="0.0.0.0")
    out = capsys.readouterr().out
    assert "WARNING" in out and "0.0.0.0" in out


def test_no_warning_when_the_bind_is_host_only_or_auth_is_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEW_AUTH", "off")
    review_server.announce_token(str(tmp_path), bind="127.0.0.1")
    assert "WARNING" not in capsys.readouterr().out

    monkeypatch.delenv("REVIEW_AUTH", raising=False)
    monkeypatch.setenv("REVIEW_TOKEN", "")
    review_server.announce_token(str(tmp_path), bind="0.0.0.0")
    assert "WARNING" not in capsys.readouterr().out, "an empty REVIEW_TOKEN alone must not warn -- only REVIEW_AUTH=off does"

    monkeypatch.setenv("REVIEW_TOKEN", "hunter2")
    review_server.announce_token(str(tmp_path), bind="0.0.0.0")
    assert "WARNING" not in capsys.readouterr().out


def test_a_write_route_without_the_token_is_refused_and_a_read_route_is_not(tmp_path, monkeypatch):
    """All routes are gated when auth is enabled: the token is the boundary. WRITES are what
    rewrite subtitles and force re-muxes, but every read exposes what is already on the
    operator's disk too -- so nothing bypasses the token."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    tok = review_server.resolve_token(str(tmp_path))
    assert review_server.route("POST", "/api/decide", {}, None)[0] == 401
    assert review_server.route("POST", "/api/decide", {}, "wrong-token")[0] == 401
    assert review_server.route("POST", "/api/decide", {}, tok)[0] != 401
    assert review_server.route("GET", "/api/episodes", {}, None)[0] == 401, "/api/ reads require token"
    assert review_server.route("GET", "/api/episodes", {}, tok)[0] == 200


def test_an_unknown_stem_is_refused_by_every_route(tmp_path, monkeypatch):
    """A stem is a FILE PATH. Honouring one from a request would let any caller read or
    overwrite anything this root process can reach -- /etc/shadow, another user's library,
    the decision store itself. Every route resolves against the set discovered by walking
    MERGE_ROOTS, and anything else is refused before a single file is touched."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    for probe in ("/etc/passwd", str(tmp_path / "../../etc/shadow"), stem + "/../ep", ""):
        assert review_server.handle_episode(probe).get("error") == "unknown episode", probe
        assert review_server.handle_decide(probe, 0, "reject").get("error") == "unknown episode", probe
        assert review_server.handle_apply(probe).get("error") == "unknown episode", probe


def test_the_default_view_omits_non_primary_reasons(tmp_path, monkeypatch):
    """Asserted on the ABSENCE. unresolved.pending() applies no stage filter of its own, so
    a server that simply returned everything would pass a presence-only check."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    reasons = {e["reason"] for e in review_server.handle_episode(stem)["entries"]}
    assert "no_reference" not in reasons and "llm_empty" not in reasons
    assert {"accepted", "rejected_guard"} <= reasons

    all_reasons = {e["reason"] for e in review_server.handle_episode(stem, all_reasons=True)["entries"]}
    assert {"no_reference", "llm_empty"} <= all_reasons, "the full walk is still reachable"


def test_the_verdicts_offered_depend_on_the_entry(tmp_path, monkeypatch):
    """An `accept` on an entry the gate REFUSED is a `force` with no distinct record, which
    defeats the counting force exists for -- the whole later argument about whether
    accept_repair is too strict rests on knowing how often a human overrode it."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    by_reason = {e["reason"]: e for e in review_server.handle_episode(stem)["entries"]}

    assert "force" not in by_reason["accepted"]["offered"]
    assert "accept" in by_reason["accepted"]["offered"]
    assert "force" in by_reason["rejected_guard"]["offered"]
    assert "accept" not in by_reason["rejected_guard"]["offered"], "an accept here would be an unlabelled force"


def test_forcing_an_unanchored_card_is_labelled_permanent(tmp_path, monkeypatch):
    """A card with no fansub reference cannot be repaired by anything downstream, so a bad
    force there is unrecoverable. Every S31 card is unanchored, which is why this warning
    exists rather than being a nicety."""
    stem = str(tmp_path / "ep_unanch")
    # BOTH originals are real cards of this episode. An entry whose text is not in conf.json
    # is an orphan and is now filtered out of the view, so a fixture that invents originals
    # would silently test an empty list.
    store_seed.conf(
        stem + ".dubtitles.conf.json", [{"start": 0.0, "end": 2.0, "text": "x"}, {"start": 2.0, "end": 4.0, "text": "p"}]
    )
    unresolved.record(stem, "repair", "rejected_guard", original_text="x", proposed_text="y", reference="the fansub line")
    unresolved.record(stem, "repair", "rejected_guard", original_text="p", proposed_text="q")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    entries = review_server.handle_episode(stem)["entries"]
    assert entries[0]["permanent"] is False, "an anchored card can be repaired again later"
    assert entries[1]["permanent"] is True, "an unanchored one cannot"


def test_a_classified_repair_is_labelled_with_the_role_of_its_reference(tmp_path, monkeypatch):
    """R5. r2 records WHICH track a repair's reference came from. Without the label the
    reviewer cannot tell a dub-caption anchor -- near-certain, the dub's own words -- from a
    separate translation, which is a judgement call. Both are "a reference" in the old view."""
    stem = str(tmp_path / "ep_roles")
    cards = ["I saw spondum", "the ship sailed", "he went thataway"]
    store_seed.conf(
        stem + ".dubtitles.conf.json", [{"start": i * 2.0, "end": i * 2.0 + 2.0, "text": t} for i, t in enumerate(cards)]
    )
    unresolved.record(
        stem,
        "repair_applied",
        "accepted",
        original_text=cards[0],
        proposed_text="I saw Spandam",
        reference="I saw Spandam",
        ref_role="dub",
        ref_provenance="native",
        ref_track_title="English Dub",
    )
    unresolved.record(
        stem,
        "repair",
        "rejected_guard",
        original_text=cards[1],
        proposed_text="the ship has sailed",
        reference="the ship has sailed",
        ref_role="translation",
        ref_provenance="native",
        ref_track_title="Full Subtitles",
    )
    # No reference at all: nothing downstream can repair this card again.
    unresolved.record(stem, "repair", "rejected_guard", original_text=cards[2], proposed_text="he went that way")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    by_text = {e["original_text"]: e for e in review_server.handle_episode(stem)["entries"]}

    dub = by_text[cards[0]]
    assert dub["ref_class"] == "dub"
    assert (dub["ref_role"], dub["ref_provenance"], dub["ref_track_title"]) == ("dub", "native", "English Dub")
    assert by_text[cards[1]]["ref_class"] == "translation"
    assert by_text[cards[2]]["ref_class"] == "unanchored"


def test_a_legacy_entry_with_a_reference_is_unclassified_not_unanchored(tmp_path, monkeypatch):
    """The items already open when r2 shipped carry no role fields. They DID have an anchor,
    so reporting them as unanchored would tell the reviewer a repairable card can never be
    repaired again -- the one claim that changes what a force means."""
    stem = str(tmp_path / "ep_legacy")
    cards = ["a legacy anchored line", "a legacy bare line"]
    store_seed.conf(
        stem + ".dubtitles.conf.json", [{"start": i * 2.0, "end": i * 2.0 + 2.0, "text": t} for i, t in enumerate(cards)]
    )
    unresolved.record(
        stem,
        "repair",
        "rejected_guard",
        original_text=cards[0],
        proposed_text="a legacy anchored line.",
        reference="the fansub line",
    )
    unresolved.record(stem, "repair", "rejected_guard", original_text=cards[1], proposed_text="a legacy bare line.")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    by_text = {e["original_text"]: e for e in review_server.handle_episode(stem)["entries"]}

    legacy = by_text[cards[0]]
    assert legacy["ref_class"] == "unclassified"
    assert (legacy["ref_role"], legacy["ref_provenance"], legacy["ref_track_title"]) == (None, None, None)
    assert legacy["permanent"] is False, "it is anchored, its role is just unknown"
    assert by_text[cards[1]]["ref_class"] == "unanchored"


def test_the_ref_block_is_additive_and_present_on_every_entry(tmp_path, monkeypatch):
    """Existing consumers must keep every field they already read, and a legacy entry must
    carry the new keys as nulls rather than being a special shape the page has to detect."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    entries = review_server.handle_episode(stem, all_reasons=True)["entries"]

    assert entries, "the fixture must produce rows or this proves nothing"
    for e in entries:
        assert {"id", "index", "risk", "starts", "context", "offered", "permanent"} <= set(e)
        assert {"ref_role", "ref_provenance", "ref_track_title", "ref_class"} <= set(e)
        assert e["ref_class"] in review_server.REF_CLASSES


def test_a_shared_line_reports_an_anchor_only_when_its_episodes_agree(tmp_path, monkeypatch):
    """A repeated song is one question, but the episodes carrying it can each have been
    anchored on a different track. Reporting whichever episode the walk found first would be
    a claim about all the others that nothing checked."""
    same = ("same anchor line here", "Same anchor line here.")
    mixed = ("mixed anchor line here", "Mixed anchor line here.")
    for name, roles in (("e01", ("dub", "dub")), ("e02", ("dub", "translation"))):
        stem = str(tmp_path / name)
        store_seed.conf(
            stem + ".dubtitles.conf.json",
            [
                {"start": 100.0, "end": 102.0, "text": same[0]},
                {"start": 300.0, "end": 302.0, "text": mixed[0]},
            ],
        )
        for (o, p), role in zip((same, mixed), roles):
            unresolved.record(
                stem,
                "repair_applied",
                "accepted",
                original_text=o,
                proposed_text=p,
                reference=p,
                ref_role=role,
                ref_provenance="native",
            )
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")

    rows = {r["original_text"]: r for r in review_server.handle_shared()["pairs"]}

    assert rows[same[0]]["ref_class"] == "dub"
    assert rows[mixed[0]]["ref_class"] is None, "the episodes disagree, so there is no single answer"


def test_duplicate_queue_entries_from_a_rerun_collapse_to_one_row(tmp_path, monkeypatch):
    """Reported 2026-09-01 on a real review page: the same line, same reason, same
    timestamp, rendered four times in a row. Root cause is real and not itself a bug --
    unresolved.record() is a deliberate O(1) append (see its own docstring: the array
    version was O(n^2) I/O on a path that fires ~86x per episode), so calling repair.py
    again on an episode still holding open queue entries -- exactly what a glossary
    improvement or a model swap asks for -- appends a second identical line rather than
    updating the first. The fix belongs at READ time, not at record() time: undecided()
    already treats a text pair as settled everywhere once ANY one occurrence is decided
    (see its own docstring, "a line with a stored verdict IS settled, whatever its queue
    flag says"), so collapsing duplicate rows for DISPLAY is safe -- the redundant
    siblings need no separate resolution, they are hidden by the same text-keyed check
    the moment the one visible row is decided."""
    stem = str(tmp_path / "ep_dup")
    store_seed.conf(stem + ".dubtitles.conf.json", [{"start": 0.0, "end": 2.0, "text": "I saw spondum"}])
    for _ in range(4):
        unresolved.record(stem, "repair", "rejected_guard", original_text="I saw spondum", proposed_text="I saw Spandam")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    entries = review_server.handle_episode(stem)["entries"]

    assert len(entries) == 1, "four identical reruns of the same event must render as one question, not four"


def test_deciding_the_collapsed_row_clears_every_duplicate_next_load(tmp_path, monkeypatch):
    """The other half of the same fix: collapsing for display only works if answering the
    one visible row genuinely settles the ones hidden behind it, not just re-hides them
    until the cache clears."""
    stem = str(tmp_path / "ep_dup2")
    store_seed.conf(stem + ".dubtitles.conf.json", [{"start": 0.0, "end": 2.0, "text": "I saw spondum"}])
    for _ in range(3):
        unresolved.record(stem, "repair", "rejected_guard", original_text="I saw spondum", proposed_text="I saw Spandam")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    monkeypatch.setattr(decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda p: "Show")
    collapsed_index = review_server.handle_episode(stem)["entries"][0]["index"]

    res = review_server.handle_decide(stem, collapsed_index, "reject", note="regression")
    review_server.forget(stem)

    assert res.get("saved") is True
    assert review_server.handle_episode(stem)["entries"] == [], "all three duplicates must be gone, not just the one decided"


def test_decide_persists_a_verdict_and_resolves_the_queue_entry(tmp_path, monkeypatch):
    """Both writes, or neither is useful: the DECISION is what stops repair.py re-applying
    and re-queueing the line, the RESOLVED flag is what takes it out of the reviewer's
    queue. Sprint 006 found the gate holding forever when only one of the two happened."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    monkeypatch.setattr(decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda p: "Show")

    res = review_server.handle_decide(stem, 0, "reject", note="regression")

    assert res.get("saved") is True
    assert unresolved.items(stem)[0]["resolved"] is True, "the entry leaves the reviewer's queue"
    hit = decisions.lookup(decisions.load("Show", str(tmp_path)), "I saw spondum", "I saw Spandam")
    assert hit is not None and hit["verdict"] == "reject", "and repair.py can see the verdict"


def test_decide_refuses_a_verdict_the_entry_does_not_offer(tmp_path, monkeypatch):
    """The offered set is enforced server-side, not merely rendered. A client is not a
    trust boundary, and `force` on an accepted entry would be an unlabelled bypass."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    monkeypatch.setattr(decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda p: "Show")

    res = review_server.handle_decide(stem, 0, "force")

    assert res.get("error") == "verdict not offered for this entry"
    assert unresolved.items(stem)[0].get("resolved") is False, "and nothing was written"


def test_apply_invokes_the_write_back(tmp_path, monkeypatch):
    """The server holds no durable logic: applying is [S-5]'s job, and this route is a call
    into it. Asserted on the delegation, because a route that reported success while doing
    nothing would look identical from outside."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    seen = {}

    def fake_apply(s, store, apply=False):
        seen.update({"stem": s, "apply": apply})
        return {"stem": s, "changed": 1}

    monkeypatch.setattr(review_server.review_apply, "apply_episode", fake_apply)

    res = review_server.handle_apply(stem)

    assert res["changed"] == 1
    assert seen == {"stem": stem, "apply": True}, "the route must actually write, not dry-run"


def test_the_index_lists_only_episodes_with_something_pending(tmp_path, monkeypatch):
    """An index padded with episodes that need nothing is how a review queue stops being
    read. Counted on the PRIMARY view, matching what the episode page opens with."""
    busy = _episode(tmp_path, "busy")
    quiet = str(tmp_path / "quiet")
    store_seed.conf(quiet + ".dubtitles.conf.json", [{"start": 0.0, "end": 1.0, "text": "q"}])
    unresolved.record(quiet, "repair", "no_reference", original_text="q")
    monkeypatch.setattr(review_server, "known_stems", lambda: [busy, quiet])

    names = {e["name"]: e["pending"] for e in review_server.handle_index()["episodes"]}

    assert names == {"busy": 2}, "no_reference alone is not a reason to open an episode"


def test_known_stems_only_finds_episodes_under_the_configured_roots(tmp_path, monkeypatch):
    """The allow-list is built from MERGE_ROOTS, so an episode outside them is not merely
    unlisted -- it is unreachable by every route."""
    inside = tmp_path / "lib"
    inside.mkdir()
    store_seed.conf(str(inside / ("a" + review_server.CONF_SUFFIX)), [])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    store_seed.conf(str(outside / ("b" + review_server.CONF_SUFFIX)), [])
    monkeypatch.setattr(review_server, "ROOTS", [str(inside)])

    found = review_server.known_stems()

    assert found == [str(inside / "a")]
    assert review_server.handle_episode(str(outside / "b"))["error"] == "unknown episode"


def test_the_router_gates_writes_and_passes_reads(tmp_path, monkeypatch):
    """route() is the whole request surface, tested directly -- no socket is opened."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    tok = review_server.resolve_token(str(tmp_path))

    assert review_server.route("GET", "/api/episodes", {}, tok)[0] == 200, "GET /api/episodes requires token"
    assert review_server.route("GET", "/api/episodes", {}, None)[0] == 401, "GET /api/episodes without token → 401"
    assert review_server.route("POST", "/api/decide", {"stem": stem, "index": 0, "verdict": "reject"}, None)[0] == 401
    assert review_server.route("POST", "/api/decide", {"stem": stem, "index": 0, "verdict": "reject"}, "nope")[0] == 401
    assert review_server.route("POST", "/api/apply", {"stem": stem}, tok)[0] == 200
    assert review_server.route("GET", "/api/nothing-here", {}, tok)[0] == 404, "unknown /api/ route → 404"
    assert review_server.route("GET", "/api/nothing-here", {}, None)[0] == 401, "unknown /api/ route without token → 401"


# --- the transport layer -----------------------------------------------------
# Driven directly, still without a socket: Handler is constructed unbound and given fake
# streams. The previous suite tested route() only, and the body-cap defect below lived
# entirely in the layer that gap left uncovered.


class _Wire:
    """A fake rfile/wfile pair, so do_POST can be driven with no network."""

    def __init__(self, body=b"", declared=None):
        import io

        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body) if declared is None else declared)}
        self.status: int | None = None
        self.sent_headers: dict[str, str] = {}

    def as_handler(self, path="/api/decide"):
        wire = self

        class _H(review_server.Handler):
            """A subclass rather than attribute assignment: BaseHTTPRequestHandler declares
            these, so patching them onto an instance is a type error and, more to the point,
            a subclass is what actually proves the real methods are the ones being driven."""

            def __init__(self):  # no socket, no server, no __init__ chain
                self.rfile, self.wfile, self.path = wire.rfile, wire.wfile, path
                self.headers = wire.headers  # type: ignore[assignment]

            def send_response(self, code, message=None):
                wire.status = code

            def send_header(self, keyword, value):
                wire.sent_headers[keyword] = value

            def end_headers(self):
                pass

        return _H()


def test_a_negative_content_length_cannot_defeat_the_body_cap(tmp_path, monkeypatch):
    """`int("-1")` does not raise, so a declared length of -1 slips past `n > 1<<20`, and
    `rfile.read(-1)` reads to EOF rather than to the cap. The body is read BEFORE
    review_auth.decide() runs, so any unauthenticated caller on the LAN could stream until this root
    process ran out of memory -- while the docstring claimed the read was bounded."""
    monkeypatch.setattr(review_server, "known_stems", lambda: [])
    wire = _Wire(b'{"stem":"x"}' + b" " * 4096, declared=-1)

    wire.as_handler().do_POST()

    assert wire.status == 411, "a length that is absent, negative or unparseable must be refused"
    assert wire.rfile.tell() == 0, "and nothing may be read from the socket first"


def test_an_oversized_body_is_refused_without_being_read(tmp_path, monkeypatch):
    monkeypatch.setattr(review_server, "known_stems", lambda: [])
    wire = _Wire(b"x" * 32, declared=(1 << 20) + 1)

    wire.as_handler().do_POST()

    assert wire.status == 413
    assert wire.rfile.tell() == 0, "the refusal must come before the read, or the cap buys nothing"


def test_a_generated_token_is_stable_when_it_cannot_be_persisted(tmp_path, monkeypatch):
    """If persistence fails, resolve_token() is called fresh on every write request, so a
    new random token would be minted per request -- none matching the one printed at
    startup. Every write becomes 401 forever, including for the operator holding the logged
    token: a total, silent denial of the write API rather than a missing convenience."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    unwritable = str(tmp_path / "nope")
    monkeypatch.setattr(review_server, "TOKEN_DIR", unwritable)
    monkeypatch.setattr(review_server, "_GENERATED", None)

    real_makedirs = os.makedirs

    def boom(path, *a, **k):
        # Only the token directory is unwritable: the P39 artifact store lives elsewhere and
        # must still open, or this measures the store's failure instead of the token's.
        if str(path).startswith(unwritable):
            raise OSError("read-only filesystem")
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(review_server.os, "makedirs", boom)

    first = review_server.resolve_token(unwritable)
    second = review_server.resolve_token(unwritable)

    assert first and first == second, "the token must survive for the life of the process"
    assert review_server.route("GET", "/api/episodes", {}, first)[0] == 200, "the token that was logged must still work"


def test_the_stem_walk_is_cached_so_an_unauthenticated_get_cannot_thrash_the_disk(tmp_path, monkeypatch):
    """GET routes are never gated, and every one of them resolved a stem against the
    episode allow-list. That list used to cost a recursive walk of the whole media root; it
    is now one store query (P39), but it is still per-root work a caller pays for with one
    HTTP GET, so the cache stays.

    Cached with a short TTL rather than forever: an episode appearing mid-session should
    still show up without a restart."""
    root = tmp_path / "lib"
    root.mkdir()
    store_seed.conf(str(root / ("a" + review_server.CONF_SUFFIX)), [])
    monkeypatch.setattr(review_server, "ROOTS", [str(root)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    queries = []
    real_stems = review_server.artifact_store.stems
    monkeypatch.setattr(review_server.artifact_store, "stems", lambda *a, **k: (queries.append(1), real_stems(*a, **k))[1])

    for _ in range(5):
        review_server.known_stems()

    assert len(queries) == 1, "five requests, one walk"


def test_the_stem_cache_expires(tmp_path, monkeypatch):
    """The counterpart: a cache that never expires means a newly generated episode is
    invisible to the reviewer until the container restarts."""
    root = tmp_path / "lib"
    root.mkdir()
    store_seed.conf(str(root / ("a" + review_server.CONF_SUFFIX)), [])
    monkeypatch.setattr(review_server, "ROOTS", [str(root)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    # BOTH, because the TTL is now a floor and the walk's own cost is the other term. On a
    # tmp_path the walk is sub-millisecond, so the derived ceiling is tiny -- but not zero,
    # and this test needs the cache genuinely disabled to say anything.
    monkeypatch.setattr(review_server, "STEMS_TTL", 0.0)
    monkeypatch.setattr(review_server, "STEMS_TTL_FACTOR", 0.0)

    assert len(review_server.known_stems()) == 1
    store_seed.conf(str(root / ("b" + review_server.CONF_SUFFIX)), [])

    assert len(review_server.known_stems()) == 2, "a new episode must appear without a restart"


def test_the_token_never_lands_at_the_filesystem_root(monkeypatch):
    """os.path.dirname("/decisions") is "/", which is truthy, so an `or "/config"` fallback
    never fires and the credential would be written to /review_token."""
    assert review_server.token_dir_for("/config/decisions") == "/config"
    assert review_server.token_dir_for("/decisions") == "/config", "not the filesystem root"
    assert review_server.token_dir_for("decisions") == "/config"
    assert review_server.token_dir_for("/srv/dub/decisions") == "/srv/dub"


def test_a_symlinked_store_key_outside_the_roots_is_not_in_the_allow_list(tmp_path, monkeypatch):
    """The media tree is deliberately group-writable, so anything with local write access
    could plant a path whose stem then entered the allow-list -- and handle_apply would
    follow it out of the roots. Needs a pre-existing local capability, so it is a second
    line of defence rather than the first, but membership in known_stems() is documented as
    the boundary and should actually be one.

    P39: the conf is a store row, so the plant is a symlinked directory that a store key
    resolves through, not a symlinked conf.json file."""
    root = tmp_path / "lib"
    root.mkdir()
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    os.symlink(secret, root / "evil")
    store_seed.conf(str(root / "evil" / ("evil" + review_server.CONF_SUFFIX)), [])
    store_seed.conf(str(root / ("good" + review_server.CONF_SUFFIX)), [])
    monkeypatch.setattr(review_server, "ROOTS", [str(root)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)

    found = review_server.known_stems()

    assert found == [str(root / "good")], "a symlink out of the roots is not an episode of this library"


def test_the_handler_has_a_socket_deadline():
    """Without one, an unauthenticated caller pins a worker thread forever.

    `BaseHTTPRequestHandler.timeout` is None by default, and
    `socketserver.StreamRequestHandler.setup()` only calls `connection.settimeout()` when it
    is not None -- so `rfile.read(n)` blocks with no deadline. The 1MB cap bounds MEMORY per
    request and nothing else: a client declaring a legal length and dripping one byte a
    minute holds a thread, before `review_auth.decide()` is ever reached, and `ThreadingHTTPServer`
    spawns those threads without a cap.

    This asserts the attribute because the attribute IS the mechanism -- setup() reads it
    off the class. It is not a source-text assertion standing in for behaviour."""
    import socketserver

    assert review_server.Handler.timeout is not None, "a None timeout means setup() never calls settimeout"
    assert 0 < review_server.Handler.timeout <= 120, "long enough for a real review POST, short enough to shed a drip"
    assert "settimeout" in __import__("inspect").getsource(socketserver.StreamRequestHandler.setup)


def test_decide_reports_a_failed_resolve_instead_of_claiming_success(tmp_path, monkeypatch):
    """The two writes are independent files and cannot be made atomic across them. What CAN
    be honest is the report: if the verdict is durable but the queue entry did not clear,
    saying `saved: true` tells the reviewer the line is settled when their queue will still
    show it. The gate itself is unaffected -- mux trusts the durable verdict, not the flag
    -- so this is a reporting fix, not a correctness one."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    monkeypatch.setattr(decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda p: "Show")
    monkeypatch.setattr(unresolved, "resolve_many", lambda *a, **k: False)

    res = review_server.handle_decide(stem, 0, "reject")

    assert res.get("saved") is True, "the verdict IS durable and must be reported as such"
    assert res.get("queue_cleared") is False, "but the queue entry did not clear, and that must not be hidden"
    assert "warning" in res


def test_concurrent_requests_are_bounded(monkeypatch):
    """[F-4]. The 30s deadline bounds how LONG one unauthenticated request holds a worker;
    it does not bound HOW MANY. `ThreadingHTTPServer` spawns a daemon thread per connection
    with no cap, so a LAN client can still open many at once -- each now dying after 30s
    instead of never, which turns an indefinite pin into a sustained churn.

    Refused promptly rather than queued: an unbounded queue is the same exhaustion with an
    extra step. Asserted without opening a socket, matching the rest of this file."""
    assert review_server.MAX_CONCURRENT > 0
    assert issubclass(review_server.BoundedHTTPServer, review_server.http.server.ThreadingHTTPServer)

    srv: Any = review_server.BoundedHTTPServer.__new__(review_server.BoundedHTTPServer)
    srv._slots = review_server.threading.Semaphore(1)
    handled, refused = [], []
    monkeypatch.setattr(
        review_server.http.server.ThreadingHTTPServer,
        "process_request",
        lambda self, req, addr: handled.append(addr),
    )
    srv.close_request = lambda req: refused.append(req)

    srv.process_request("r1", ("10.0.0.1", 1))  # takes the only slot, never released here
    srv.process_request("r2", ("10.0.0.2", 2))

    assert handled == [("10.0.0.1", 1)], "the first is served"
    assert refused == ["r2"], "the second is closed immediately, not queued behind it"


def test_the_slot_is_returned_after_a_request(monkeypatch):
    """A semaphore that is never released is a server that stops answering after N requests
    -- a worse outage than the one being prevented."""
    srv: Any = review_server.BoundedHTTPServer.__new__(review_server.BoundedHTTPServer)
    srv._slots = review_server.threading.Semaphore(1)
    srv.close_request = lambda req: None
    monkeypatch.setattr(review_server.http.server.ThreadingHTTPServer, "process_request", lambda self, req, addr: None)
    monkeypatch.setattr(review_server.http.server.ThreadingHTTPServer, "process_request_thread", lambda self, req, addr: None)

    # Through the REAL pair: process_request acquires, process_request_thread releases.
    # Calling only the release half never takes a slot, so the semaphore stays full and the
    # assertion below passes whether or not the release exists -- which is how the first
    # draft of this test let a "never release the slot" mutation through.
    for _ in range(3):
        srv.process_request("req", ("10.0.0.1", 1))
        srv.process_request_thread("req", ("10.0.0.1", 1))

    assert srv._slots.acquire(blocking=False), "the slot must come back, or the server dies after MAX_CONCURRENT requests"


def test_serve_exits_when_require_token_and_auth_off(monkeypatch, tmp_path, capsys):
    """With REQUIRE_TOKEN=1 and REVIEW_AUTH=off both set, serve() exits with status 2
    and logs the exact message before BoundedHTTPServer is constructed."""
    monkeypatch.setenv("REQUIRE_TOKEN", "1")
    monkeypatch.setenv("REVIEW_AUTH", "off")
    # We'll mock BoundedHTTPServer to ensure it's not called
    constructed = {}

    class _Fake:
        def __init__(self, addr, handler):
            constructed["addr"] = addr
            constructed["handler"] = handler

        def serve_forever(self):
            constructed["served"] = True

    monkeypatch.setattr(review_server, "BoundedHTTPServer", _Fake)

    with pytest.raises(SystemExit) as excinfo:
        review_server.serve(port=1)

    assert excinfo.value.code == 2
    # Ensure BoundedHTTPServer was never constructed
    assert not constructed, "BoundedHTTPServer should not be constructed"
    # Check the log message
    out = capsys.readouterr().out
    assert "review server: REQUIRE_TOKEN=1 but auth is disabled (REVIEW_AUTH=off)" in out


def test_serve_uses_the_bounded_server(monkeypatch, tmp_path):
    """serve() constructing a plain ThreadingHTTPServer would leave the cap unreachable in
    production while every unit test above still passed."""
    monkeypatch.setenv("REVIEW_TOKEN", "t")
    built = {}

    class _Fake:
        def __init__(self, addr, handler):
            built["addr"], built["handler"] = addr, handler

        def serve_forever(self):
            built["served"] = True

    monkeypatch.setattr(review_server, "BoundedHTTPServer", _Fake)
    review_server.serve(port=1)

    assert built.get("served") is True, "serve() must construct BoundedHTTPServer, not the unbounded one"
    assert built["handler"] is review_server.Handler


def test_every_start_says_where_the_token_is_even_when_it_is_not_new(tmp_path, monkeypatch, capsys):
    """The value is printed only when GENERATED. On every later start the token is read from
    the file and nothing was logged, so an operator coming back a week later -- after the log
    rotated -- had no way to find it short of knowing the docker exec incantation.

    The value still appears once and only once. What every start gets is the PATH and how to
    read it, which is not a credential."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "_GENERATED", None)
    first = review_server.resolve_token(str(tmp_path))
    out_new = capsys.readouterr().out
    assert first in out_new, "a newly generated token is shown once, or nobody can ever use it"

    monkeypatch.setattr(review_server, "_GENERATED", None)
    review_server.announce_token(str(tmp_path))
    out_again = capsys.readouterr().out

    assert first not in out_again, "the value is not re-printed on a restart"
    assert "review_token" in out_again and "docker exec" in out_again, "but where to find it is"


def test_the_episode_view_hides_entries_orphaned_by_a_re_transcription(tmp_path, monkeypatch):
    """Measured on the live library 2026-08-27: 6,364 One Pace entries describe text no
    episode contains any more, against 0 that still matched. Nothing will re-queue them, so
    nothing will ever resolve them -- they are not questions a human can answer, and showing
    them is how a queue of thousands of dead items happens. The mux gate already ignores
    them; the page was still listing them."""
    stem = str(tmp_path / "ep_orph")
    store_seed.conf(stem + ".dubtitles.conf.json", [{"start": 0.0, "end": 2.0, "text": "a line the episode still has"}])
    unresolved.record(stem, "repair_applied", "accepted", original_text="a line the episode still has", proposed_text="fix")
    unresolved.record(stem, "repair_applied", "accepted", original_text="text from an OLD transcript", proposed_text="fix2")
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    entries = review_server.handle_episode(stem)["entries"]

    assert [e["original_text"] for e in entries] == ["a line the episode still has"]


def test_the_index_separates_admitted_repairs_from_refusals(tmp_path, monkeypatch):
    """One number for both was misleading in exactly the way that matters.

    An ADMITTED repair is a change nothing checked the meaning of -- it shipped, and
    `factory -> needle` passes every gate. A REFUSAL means the ASR text shipped, which is the
    safe outcome; reviewing it asks whether the guard was too strict. The first is the reason
    this loop exists, the second is an audit. A single "pending: 23" told the operator they
    had 23 of the first when they had 0."""
    stem = _episode(tmp_path)  # 1 accepted + 1 rejected_guard, both live against its conf
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    ep = review_server.handle_index()["episodes"][0]

    assert ep["admitted"] == 1, "repairs that shipped unchecked"
    assert ep["refused"] == 1, "repairs the gate turned down"
    assert ep["pending"] == 2, "and the total still adds up"


# --- the index has to survive a real library ---------------------------------
# Rendered against the owner's 294 episodes it was a flat wall of links, every row reading
# "0 admitted", with no way to find a show. Built without ever being looked at at that size.


def _lib(tmp_path, spec):
    """spec: {(show, season): [(name, n_admitted, n_refused)]} -> stems on disk."""
    stems = []
    for (show, season), eps in spec.items():
        d = tmp_path / show / season
        d.mkdir(parents=True, exist_ok=True)
        for name, adm, ref in eps:
            stem = str(d / name)
            cards = [{"start": i * 2.0, "end": i * 2.0 + 2.0, "text": f"card {i}"} for i in range(adm + ref)]
            store_seed.conf(stem + ".dubtitles.conf.json", cards)
            for i in range(adm):
                unresolved.record(stem, "repair_applied", "accepted", original_text=f"card {i}", proposed_text=f"fix {i}")
            for i in range(adm, adm + ref):
                unresolved.record(stem, "repair", "rejected_guard", original_text=f"card {i}", proposed_text=f"no {i}")
            stems.append(stem)
    return stems


def test_the_index_carries_show_and_season_and_sorts_by_admitted(tmp_path, monkeypatch):
    """Ordering is the whole point: with everything at 0 admitted the flat list had no
    meaningful order at all, so the one episode that later gains an accepted repair has to
    surface without the reviewer hunting for it."""
    stems = _lib(
        tmp_path,
        {
            ("One Pace", "Season 31"): [("ep_a", 3, 1)],
            ("Trigun", "Season 01"): [("ep_b", 0, 40)],
            ("JUJUTSU KAISEN", "Season 01"): [("ep_c", 7, 2)],
        },
    )
    monkeypatch.setattr(review_server, "known_stems", lambda: stems)

    eps = review_server.handle_index()["episodes"]

    assert [e["admitted"] for e in eps] == [7, 3, 0], "most admitted first, refusals last"
    assert eps[0]["show"] == "JUJUTSU KAISEN" and eps[0]["season"] == "Season 01"
    assert {e["show"] for e in eps} == {"One Pace", "Trigun", "JUJUTSU KAISEN"}


def _triage_episode(tmp_path, name="triage"):
    """One episode holding one admitted repair of each risk class, queued WORST LAST.

    Queued in the reverse of the wanted order on purpose: the ordering under test has to be
    doing the work, not the append order of the jsonl agreeing with it by luck."""
    cards = [
        "Roger's treasure belongs to me",  # punctuation only
        "We're looking for a factory.",  # one word substituted
        "There's only one who deserves the flame flame fruit.",  # a word deleted
    ]
    props = [
        "Roger's treasure belongs to me.",
        "We're looking for a needle.",
        "There's only one who deserves the flame fruit.",
    ]
    stem = str(tmp_path / name)
    store_seed.conf(
        stem + ".dubtitles.conf.json", [{"start": 60.0 + i * 30, "end": 62.0 + i * 30, "text": t} for i, t in enumerate(cards)]
    )
    for c, p in zip(cards, props):
        unresolved.record(stem, "repair_applied", "accepted", original_text=c, proposed_text=p)
    return stem


def test_risk_class_separates_a_changed_word_from_a_changed_comma():
    """78% of the admitted repairs (529 of 682, measured 2026-08-28) change no word at all.
    Every regression the owner's 45-line read found changed one -- so this is the difference
    between the reviewer's first decision and their five-hundredth."""
    assert review_server.risk_class("That's right, we're shining,", "That's right, we're shining.") == "punctuation"
    assert review_server.risk_class("roger's treasure belongs to me", "Roger's treasure belongs to me.") == "punctuation"
    assert review_server.risk_class("we" + chr(0x2019) + "re shining", "we're shining.") == "punctuation", (
        "U+2019 and U+0027 are one character rendered two ways -- decisions.key folds them "
        "for the same reason, and not folding here would file most contractions as risky"
    )
    assert (
        review_server.risk_class(
            "They don't believe it—that's Cyan Boo from Kano,", "They don't believe it. That's Cyan Boo from Kano."
        )
        == "punctuation"
    ), (
        "an em dash split into a full stop adds a token on one side only -- filed as a word "
        "change it would put pure punctuation at the top of the queue, which is the noise "
        "this ordering exists to remove"
    )
    assert review_server.risk_class("We're looking for a factory.", "We're looking for a needle.") == "substitution"
    assert review_server.risk_class("That come together.", "That comes together.") == "substitution"
    assert review_server.risk_class("deserves the flame flame fruit.", "deserves the flame fruit.") == "words"
    assert review_server.risk_class("and that's not Chin Down behind them.", "That's not Chin Down behind them.") == "words"
    assert review_server.risk_class("CP-0.", "CP 0.") == "words", (
        "the HYPHEN stays word-internal: Flame-Flame, non-stop and CP-0 are one word each"
    )


def test_the_episode_queue_puts_the_word_changing_repairs_first(tmp_path, monkeypatch):
    """A queue in queue order is 78% punctuation, and the reviewer meets the dangerous 22%
    only after wading through it. Ordered by what the change actually did instead."""
    stem = _triage_episode(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))

    entries = review_server.handle_episode(stem)["entries"]

    assert [e["risk"] for e in entries] == ["words", "substitution", "punctuation"], (
        "words added or dropped first -- that is where the flame-flame deletion lives and no "
        "gate in this pipeline can see it; punctuation-only last"
    )
    assert entries[0]["proposed_text"].endswith("the flame fruit.")


def test_a_refusal_sorts_after_every_admitted_repair(tmp_path, monkeypatch):
    """Two different jobs, not one queue ordered by risk. An admitted repair SHIPPED with
    nothing checking its meaning; a refusal means the ASR text shipped, which is the safe
    outcome, and asks the audit question of whether the guard was too strict. Interleaving
    them by risk class would make the reviewer switch jobs line by line."""
    stem = str(tmp_path / "mixed")
    # FIRST in the jsonl, and it substitutes a word -- so both the append order and the risk
    # ordering argue for showing it first. Only the stage rule puts it last.
    unresolved.record(
        stem,
        "repair",
        "rejected_guard",
        original_text="Roger's treasure belongs to me",
        proposed_text="Rogers treasure belongs to Luffy",
        reference="ref",
    )
    _triage_episode(tmp_path, "mixed")
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))

    stages = [e["stage"] for e in review_server.handle_episode(stem)["entries"]]

    assert stages == ["repair_applied"] * 3 + ["repair"], "the refusal sorts last despite substituting a word"


def test_decide_batch_saves_every_verdict_in_one_store_write(tmp_path, monkeypatch):
    """The whole point. Per-verdict posting meant one store load, one store save and one
    queue rewrite EACH, all over the media mount, plus a full page reload between them."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")
    saves = []
    real_save = review_server.decisions.save
    monkeypatch.setattr(review_server.decisions, "save", lambda st, sh, d: (saves.append(1), real_save(st, sh, d))[1])

    res = review_server.handle_decide_batch(
        stem, [{"index": 0, "verdict": "reject"}, {"index": 1, "verdict": "correct", "text": "the ship has sailed."}]
    )

    assert res["saved"] == 2 and not res.get("errors"), res
    assert len(saves) == 1, "one store write for the whole batch, not one per verdict"
    pending = review_server.handle_episode(stem)["entries"]
    assert [e["index"] for e in pending] == [], "and both entries left the queue"
    store = review_server.decisions.load("One Pace", str(tmp_path))
    first = review_server.decisions.lookup(store, _CARDS[0], "I saw Spandam") or {}
    second = review_server.decisions.lookup(store, _CARDS[1], "the ship has sailed") or {}
    assert first.get("verdict") == "reject"
    assert second.get("text") == "the ship has sailed.", "a `correct` carries the human's own text, not the model's"


def test_decide_batch_lands_the_good_verdicts_and_names_the_bad_one(tmp_path, monkeypatch):
    """A reviewer who spent twenty minutes on an episode must not lose nineteen good
    verdicts to one malformed twentieth. The refusal is REPORTED, not swallowed -- silently
    discarding a decision is the failure this whole loop exists to prevent."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")

    res = review_server.handle_decide_batch(
        stem,
        [
            {"index": 0, "verdict": "force"},  # not offered on an ACCEPTED entry
            {"index": 1, "verdict": "force"},  # offered on a guard refusal
            {"index": 99, "verdict": "reject"},  # no such entry
        ],
    )

    assert res["saved"] == 1
    assert len(res["errors"]) == 2
    assert {e["index"] for e in res["errors"]} == {0, 99}
    assert "not offered" in res["errors"][0]["error"]
    left = {e["index"] for e in review_server.handle_episode(stem)["entries"]}
    assert 1 not in left and 0 in left, "the accepted entry is still queued, the forced one is not"


def test_decide_batch_refuses_an_unknown_episode_and_an_unresolvable_show(tmp_path, monkeypatch):
    """Same two refusals the single-verdict path already makes, and for the same reasons:
    the client is not a trust boundary, and a decision with no show has nowhere to go."""
    stem = _episode(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))

    assert review_server.handle_decide_batch(str(tmp_path / "nope"), [{"index": 0, "verdict": "reject"}])["error"]
    monkeypatch.setattr(review_server, "show_for", lambda s: "")
    assert "show" in review_server.handle_decide_batch(stem, [{"index": 0, "verdict": "reject"}])["error"]


def test_the_batch_route_is_gated_by_the_token_like_every_other_write(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_TOKEN", "sekrit")
    status, _ = review_server.route("POST", "/api/decide", {"stem": "x", "decisions": []}, None)
    assert status == 401, "a batch of verdicts is still a write"


def test_a_line_decided_on_one_episode_is_hidden_on_every_other(tmp_path, monkeypatch):
    """The opening song is 24 episodes of the same question. mux has treated a stored
    verdict as settled since [S-6]; the page was the one place that did not, so judging it
    on E01 bought nothing on E02."""
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")
    monkeypatch.setattr(review_server.decisions, "show_for", lambda p, *a, **k: "One Pace")
    stems = [_triage_episode(tmp_path, n) for n in ("e01", "e02")]

    e01 = review_server.handle_episode(stems[0])["entries"]
    # By INDEX, which addresses the jsonl row -- not by display position, which the triage
    # sort reorders. Picking the deletion deliberately: it is the class no gate can see.
    target = next(e for e in e01 if "flame fruit" in e["proposed_text"])
    before = len(review_server.handle_episode(stems[1])["entries"])

    review_server.handle_decide_batch(stems[0], [{"index": target["index"], "verdict": "reject"}])
    after = review_server.handle_episode(stems[1])["entries"]

    assert len(after) == before - 1, "the same line, decided next door, is no longer asked"
    assert all("flame fruit" not in e["proposed_text"] for e in after)
    assert review_server.handle_episode(stems[1])["settled_elsewhere"] == 1, "and the page says so"
    idx = {e["stem"]: e for e in review_server.handle_index()["episodes"]}
    assert idx[stems[1]]["admitted"] == before - 1, "and the index count agrees with the page"


def _shared_library(tmp_path):
    """Three episodes sharing an opening-song line, each with one line of its own."""
    song = ("running forever Let's go along with curiosity", "Running forever. Let's go along with curiosity.")
    stems = []
    for i, name in enumerate(("e01", "e02", "e03")):
        own = (f"line {i} as heard", f"Line {i} as heard.")
        stem = str(tmp_path / name)
        store_seed.conf(
            stem + ".dubtitles.conf.json",
            [{"start": 100.0, "end": 102.0, "text": song[0]}, {"start": 300.0, "end": 302.0, "text": own[0]}],
        )
        for o, p in (song, own):
            unresolved.record(stem, "repair_applied", "accepted", original_text=o, proposed_text=p)
        stems.append(stem)
    return stems


def test_shared_lists_each_repeated_line_once_with_its_episode_count(tmp_path, monkeypatch):
    """One question, asked once. The opening song is the same repair in 24 episodes, and
    reading it 24 times is 23 decisions that carry no information."""
    _shared_library(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")

    rows = review_server.handle_shared()["pairs"]

    assert len(rows) == 1, "only lines in MORE than one episode -- the rest belong to their episode"
    assert rows[0]["episodes"] == 3
    assert rows[0]["proposed_text"].startswith("Running forever.")
    assert rows[0]["risk"] == "punctuation" and rows[0]["offered"] == ["accept", "reject", "correct"]
    assert rows[0]["id"] and rows[0]["id"] == review_server.handle_shared()["pairs"][0]["id"], (
        "a stable handle the client sends back, so raw text never has to be trusted"
    )


def test_deciding_a_shared_line_clears_it_from_every_episode(tmp_path, monkeypatch):
    """The whole point, and it needs no write to any queue file: the verdict is show-wide,
    and unresolved.undecided already hides a settled line wherever it appears."""
    stems = _shared_library(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")
    assert [len(review_server.handle_episode(s)["entries"]) for s in stems] == [2, 2, 2]

    res = review_server.handle_shared_decide([{"id": review_server.handle_shared()["pairs"][0]["id"], "verdict": "accept"}])

    assert res["saved"] == 1 and not res["errors"]
    assert [len(review_server.handle_episode(s)["entries"]) for s in stems] == [1, 1, 1], "gone from all three"
    assert review_server.handle_shared()["pairs"] == [], "and off the shared page too"


def test_a_shared_verdict_is_refused_for_a_pair_that_is_not_on_the_list(tmp_path, monkeypatch):
    """The client sends an INDEX into a list the server recomputes, never the text itself.
    A client is not a trust boundary, and accepting raw text here would let it write a
    decision for any line in the show -- including one nobody was ever shown."""
    _shared_library(tmp_path)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")

    real = review_server.handle_shared()["pairs"][0]["id"]
    res = review_server.handle_shared_decide([{"id": "0" * 16, "verdict": "accept"}, {"id": real, "verdict": "force"}])

    assert res["saved"] == 0
    assert {e["id"] for e in res["errors"]} == {"0" * 16, real}
    assert "not offered" in [e["error"] for e in res["errors"] if e["id"] == real][0], (
        "force on an admitted repair is a gate bypass here exactly as it is on an episode page"
    )


def _two_shared_pairs(tmp_path, monkeypatch):
    """Two episodes that each hold the shared pairs A and B (Sol's F1 fixture)."""
    pairs = [("A harmless line", "A corrected line"), ("B harmless line", "B WRONG line")]
    for name in ("e01", "e02"):
        stem = str(tmp_path / name)
        store_seed.conf(stem + ".dubtitles.conf.json", [{"start": 100.0, "end": 102.0, "text": o} for o, _ in pairs])
        for o, p in pairs:
            unresolved.record(stem, "repair_applied", "accepted", original_text=o, proposed_text=p)
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "S")
    return pairs


def test_a_stale_tab_is_refused_and_never_approves_a_different_pair(tmp_path, monkeypatch):
    """Two tabs load the page with A first. Tab one decides A; tab two, still showing A, decides A too. By index, B had become
    row 0 and the second verdict approved B's WRONG line. By stable id it is refused as stale."""
    pairs = _two_shared_pairs(tmp_path, monkeypatch)
    shown = review_server.handle_shared()["pairs"]
    a = next(r for r in shown if r["original_text"] == pairs[0][0])
    first = review_server.handle_shared_decide([{"id": a["id"], "verdict": "reject"}])
    stale = review_server.handle_shared_decide([{"id": a["id"], "verdict": "accept"}])
    store = review_server.decisions.load("S", str(tmp_path))
    assert first["saved"] == 1 and not first["errors"]
    assert stale["saved"] == 0 and "stale" in stale["errors"][0]["error"]
    assert review_server.decisions.lookup(store, *pairs[1]) is None, "B was never shown to tab two and is not decided"
    assert review_server.decisions.lookup(store, *pairs[0])["verdict"] == "reject"


def test_ids_do_not_move_when_another_pair_is_decided(tmp_path, monkeypatch):
    pairs = _two_shared_pairs(tmp_path, monkeypatch)
    before = {r["original_text"]: r["id"] for r in review_server.handle_shared()["pairs"]}
    review_server.handle_shared_decide([{"id": before[pairs[0][0]], "verdict": "reject"}])
    after = {r["original_text"]: r["id"] for r in review_server.handle_shared()["pairs"]}
    assert after == {pairs[1][0]: before[pairs[1][0]]}


def test_an_index_only_verdict_is_refused(tmp_path, monkeypatch):
    """The old protocol sent a list offset, which stops meaning anything the moment the list changes."""
    _two_shared_pairs(tmp_path, monkeypatch)
    res = review_server.handle_shared_decide([{"pair": 0, "verdict": "accept"}])
    assert res["saved"] == 0 and res["errors"]
    assert review_server.decisions.load("S", str(tmp_path)).get("decisions", []) == []


def test_the_shared_route_is_gated_like_every_other_write(tmp_path, monkeypatch):
    """Reads are open and writes are not, the same split the rest of the surface makes."""
    monkeypatch.setenv("REVIEW_TOKEN", "sekrit")
    assert review_server.route("POST", "/api/shared", {"decisions": []}, None)[0] == 401
    assert review_server.route("GET", "/api/shared", {}, "sekrit")[0] == 200, "GET /api/shared requires token"


def test_the_stem_cache_outlives_the_walk_that_fills_it(tmp_path, monkeypatch):
    """A cache that expires faster than its own refresh is not a cache.

    Measured on the live library 2026-08-28: known_stems() takes 297s over 989 episodes on a
    network mount, against a 30s TTL — so every request re-walked the whole media tree and
    the cache had never once been hit. The TTL cannot be a constant when the cost it is
    hiding is a property of someone else's filesystem. P39 replaced the walk with a store
    query; the cost is still the store's, and the rule is unchanged."""
    walks = []
    clock = {"t": 1000.0}
    real_stems = review_server.artifact_store.stems

    def slow_stems(*args, **kwargs):
        walks.append(args)
        clock["t"] += 300.0  # what the live library actually costs
        return real_stems(*args, **kwargs)

    stem = str(tmp_path / "ep")
    store_seed.conf(stem + ".dubtitles.conf.json", [{"start": 0.0, "end": 2.0, "text": "x"}])
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(review_server.artifact_store, "stems", slow_stems)

    assert review_server.known_stems() == [stem]
    clock["t"] += 60.0  # twice the old TTL later
    assert review_server.known_stems() == [stem]

    assert len(walks) == 1, "a 300s walk must not be redone a minute later"
    assert review_server._stems_ttl(300.0) > 300.0, "the list outlives the cost of rebuilding it"
    assert review_server._stems_ttl(0.01) == review_server.STEMS_TTL, "a fast mount keeps the short TTL"


def test_the_index_reads_each_queue_once_per_walk_not_once_per_request(tmp_path, monkeypatch):
    """Validating the cache must not cost what the cache saves.

    The first version of this stat-ed both files per episode to check freshness. Measured on
    the live library that was 176s -- 989 episodes x 2 stats x ~90ms, almost exactly the
    read it replaced. On this mount ANY per-episode filesystem touch costs the same, so the
    queue cache is tied to the walk that discovered the episodes instead: filled once,
    dropped when that walk is redone."""
    [_triage_episode(tmp_path, n) for n in ("e01", "e02")]
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    # Counts every TOUCH, not just every read: a stat is what the previous version used to
    # check freshness, and on the live mount it cost the same as the read it was avoiding.
    touched = []
    real_stat, real_open = os.stat, open

    def watch(fn):
        def wrapped(path, *a, **k):
            if str(tmp_path) in str(path):
                touched.append(str(path))
            return fn(path, *a, **k)

        return wrapped

    review_server.handle_index()
    monkeypatch.setattr(os, "stat", watch(real_stat))
    monkeypatch.setattr("builtins.open", watch(real_open))

    review_server.handle_index()
    review_server.handle_shared()

    assert touched == [], f"a warm pass must not touch the media tree at all, but hit {touched[:3]}"


def test_a_fresh_walk_drops_the_queue_cache_with_it(tmp_path, monkeypatch):
    """The counterpart. Nothing else expires the queue entries, so if the walk did not clear
    them a re-repaired episode would keep its old queue until the container restarted."""
    _triage_episode(tmp_path, "e01")
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server, "STEMS_TTL", 0.0)
    monkeypatch.setattr(review_server, "STEMS_TTL_FACTOR", 0.0)
    review_server.handle_index()
    assert review_server._QUEUE_CACHE

    review_server.known_stems()

    assert review_server._QUEUE_CACHE == {}, "the two caches are filled together and die together"


def test_deciding_re_reads_that_episode_and_leaves_the_rest_cached(tmp_path, monkeypatch):
    """A write is the one thing that changes a queue file, and the writer knows which one.
    Dropping the whole cache would make every save cost a full library walk -- precisely
    when the reviewer is working."""
    stems = [_triage_episode(tmp_path, n) for n in ("e01", "e02")]
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (float("inf"), stems))
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")
    review_server.handle_index()
    target = next(e for e in review_server.handle_episode(stems[0])["entries"] if "flame fruit" in e["proposed_text"])
    reads = []
    real = unresolved.items
    monkeypatch.setattr(unresolved, "items", lambda s: (reads.append(s), real(s))[1])

    review_server.handle_decide_batch(stems[0], [{"index": target["index"], "verdict": "reject"}])
    review_server.handle_index()

    assert stems[1] not in reads, "the untouched episode stays cached"
    assert stems[0] in reads, "and the one that was written to is read again"


def test_a_verdict_is_visible_immediately_despite_the_queue_cache(tmp_path, monkeypatch):
    """The cache keys on the QUEUE file, and a verdict also writes the decisions store —
    a second file it does not watch. Saving must not leave the reviewer looking at a line
    they just settled, so the store is consulted outside the cache, every time."""
    stems = [_triage_episode(tmp_path, n) for n in ("e01", "e02")]
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", (0.0, []))
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server.decisions, "DECISIONS_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "show_for", lambda s: "One Pace")
    before = len(review_server.handle_episode(stems[1])["entries"])
    target = next(e for e in review_server.handle_episode(stems[0])["entries"] if "flame fruit" in e["proposed_text"])

    review_server.handle_decide_batch(stems[0], [{"index": target["index"], "verdict": "reject"}])

    assert len(review_server.handle_episode(stems[1])["entries"]) == before - 1, (
        "the sibling episode's queue file did not change, but the answer did"
    )


def test_warm_cache_fills_both_caches_so_the_first_request_does_not_pay_for_them(tmp_path, monkeypatch):
    """The walk is unavoidable once; who waits for it is a choice.

    Measured on the live library: a cold index took over 900s and the reviewer was the one
    sitting in front of it, after every deploy. Doing it at startup instead costs the
    container a few minutes it has anyway and hands the page over ready."""
    stems = [_triage_episode(tmp_path, n) for n in ("e01", "e02")]
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})

    review_server.warm_cache()

    assert sorted(review_server._QUEUE_CACHE) == sorted(stems), "every episode's queue, read once"
    touched = []
    real_open = open

    def watch(path, *a, **k):
        if str(tmp_path) in str(path):
            touched.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", watch)
    review_server.handle_index()

    assert touched == [], "so the first real request reads nothing"


def test_warm_cache_never_raises(tmp_path, monkeypatch):
    """It runs on a background thread at startup. A media tree that is not mounted yet must
    delay the first page, never take the server down with it."""
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path / "not-mounted")])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server, "known_stems", lambda: (_ for _ in ()).throw(OSError("mount went away")))

    review_server.warm_cache()  # must not propagate


def test_next_warm_delay_re_warms_before_the_current_cache_expires(monkeypatch):
    """Reported live 2026-09-01: warm_cache() only ever ran once, at server startup.
    Measured on the real 968-episode library: the stems TTL (STEMS_TTL_FACTOR=20 against a
    16.82s walk) expires in ~336s, but nothing re-warmed the cache after that -- so every
    request landing after the FIRST five minutes of the container's life paid the full cold
    cost again (measured live: 115-157s), not just the very first request after a deploy.
    The delay must land BEFORE the tracked expiry, not after it -- the gap between expiry
    and refresh is exactly the window a real request could still land cold in."""
    now = 1_000_000.0
    stems_cache = (now + 336.4, ["a", "b"])  # matches the real measured expiry above

    delay = review_server._next_warm_delay(stems_cache, now)

    assert delay < 336.4, "must fire before the tracked cache actually expires"
    assert now + delay < stems_cache[0], "the re-warm must land inside the still-valid window"


def test_next_warm_delay_falls_back_to_stems_ttl_with_no_cache_yet(monkeypatch):
    """The very first call, or a walk that just failed, leaves no expiry to read -- must
    still produce a sane, bounded delay rather than raising or looping immediately."""
    delay = review_server._next_warm_delay(None, 1_000_000.0)

    assert 0 < delay <= review_server.STEMS_TTL


def test_next_warm_delay_is_floored_against_a_fast_walks_short_ttl(monkeypatch):
    """Found live 2026-09-01: _stems_ttl() scales the cache's OWN lifetime to how long its
    walk took, so a walk that happens to land fast (real NFS latency varies, especially
    while merge_pass concurrently muxes multi-GB files on the same mount) gets assigned a
    short TTL -- which schedules another re-warm soon, which if ALSO fast keeps
    compounding. Observed live as a burst of "cache warm ... in 0s" lines seconds apart. An
    expiry just barely in the future (a fast walk's own short TTL) must not produce a delay
    anywhere near that short -- it must floor at STEMS_TTL, the codebase's own answer to
    "the shortest sane cache lifetime", the same floor _stems_ttl() itself already uses."""
    now = 1_000_000.0
    stems_cache = (now + 2.0, ["a"])  # a fast walk's own tiny self-assigned TTL

    delay = review_server._next_warm_delay(stems_cache, now)

    assert delay >= review_server.STEMS_TTL, "must not compound into a tight loop"


def test_an_entry_carries_the_cards_either_side_of_it(tmp_path, monkeypatch):
    """A card is not a sentence -- reflow splits on duration and line length, so a queued
    line routinely starts or ends mid-clause and the reviewer cannot tell whether a repair
    fits the sentence it belongs to. The page gets the neighbours so they can.

    The break this catches: drop the context off _decorate and every entry is judged on one
    fragment again."""
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    stem = _episode(tmp_path)
    out = review_server.handle_episode(stem)

    by_orig = {e["original_text"]: e for e in out["entries"]}
    mid = by_orig["the ship sailed"]  # _CARDS[1] -- a neighbour on each side
    assert mid["context"] == [{"start": 2.0, "before": ["I saw spondum"], "after": ["he went thataway"]}]

    first = by_orig["I saw spondum"]  # _CARDS[0] -- nothing before it
    assert first["context"] == [{"start": 0.0, "before": [], "after": ["the ship sailed"]}]


def test_healthz_ok_on_a_fresh_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    common.heartbeat(
        last_sweep_start=time.time() - 100,
        last_sweep_end=time.time() - 10,
        roots_readable=True,
        order_file_present=True,
    )

    status, payload = review_server.handle_healthz()

    assert (status, payload) == (200, {"ok": True})


def test_healthz_stale_after_3x_rescan_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("RESCAN_INTERVAL", "100")
    common.heartbeat(
        last_sweep_start=time.time() - 1000,
        last_sweep_end=time.time() - 301,
        roots_readable=True,
        order_file_present=True,
    )

    status, payload = review_server.handle_healthz()

    assert status == 503
    assert payload["ok"] is False
    assert any(r.startswith("stale: last sweep ended") for r in payload["reasons"])
    assert "/" not in __import__("json").dumps(payload), "no filesystem path may leak through /healthz"


def test_healthz_recent_sweep_start_does_not_rescue_a_stale_sweep_end(tmp_path, monkeypatch):
    """Staleness is judged from last_sweep_end alone, by design: an in-progress
    sweep (recent last_sweep_start) cannot be told apart, at this granularity, from
    one wedged mid-episode with no crash -- exactly the failure this probe exists to
    catch. See this task's design-decision note."""
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("RESCAN_INTERVAL", "100")
    common.heartbeat(
        last_sweep_start=time.time() - 5,
        last_sweep_end=time.time() - 301,
        roots_readable=True,
        order_file_present=True,
    )

    status, _ = review_server.handle_healthz()

    assert status == 503


def test_healthz_roots_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    common.heartbeat(
        last_sweep_start=time.time(),
        last_sweep_end=time.time(),
        roots_readable=False,
        order_file_present=True,
    )

    status, payload = review_server.handle_healthz()

    assert status == 503
    assert "roots_readable is false" in payload["reasons"]


def test_healthz_order_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    common.heartbeat(
        last_sweep_start=time.time(),
        last_sweep_end=time.time(),
        roots_readable=True,
        order_file_present=False,
    )

    status, payload = review_server.handle_healthz()

    assert status == 503
    assert "order_file_present is false" in payload["reasons"]


def test_healthz_missing_heartbeat_file(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "does-not-exist.json"))

    status, payload = review_server.handle_healthz()

    assert (status, payload) == (503, {"ok": False, "reasons": ["no heartbeat yet"]})


def test_healthz_never_shipped_no_sweep_completed_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    common.heartbeat(last_sweep_start=time.time())  # no last_sweep_end at all

    status, payload = review_server.handle_healthz()

    assert status == 503
    assert "stale: no sweep has completed yet" in payload["reasons"]


def test_get_healthz_is_open_without_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    common.heartbeat(
        last_sweep_start=time.time(),
        last_sweep_end=time.time(),
        roots_readable=True,
        order_file_present=True,
    )

    wire = _Wire()
    h = wire.as_handler("/healthz")
    h.do_GET()

    assert wire.status == 200


def test_get_healthz_is_never_gated_by_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(common, "HEARTBEAT_PATH", str(tmp_path / "does-not-exist.json"))

    wire = _Wire()
    h = wire.as_handler("/healthz")
    h.do_GET()

    assert wire.status != 401


def test_the_old_page_urls_redirect_to_the_workspace():
    """The server-rendered pages are gone. Their URLs keep working as redirects, so an old
    bookmark lands on the one UI instead of a surface that no longer exists."""
    for path, target in (("/", "/review/"), ("/index.html", "/review/"), ("/shared", "/review/#/shared")):
        wire = _Wire()
        h = wire.as_handler(path)
        h.do_GET()
        assert wire.status == 302, path
        assert wire.sent_headers.get("Location") == target, path


def test_get_api_episodes_without_token_is_401_with_the_literal_body(tmp_path, monkeypatch):
    """review_auth.decide() gates GET the same as POST for every /api/* route -- route()
    is reached only from api paths (verified: do_GET serves /, /index.html and
    /shared directly, before route() is ever called), so this cannot affect those
    three paths or /healthz (Task 16)."""
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(review_server, "known_stems", lambda: [])
    tok = review_server.resolve_token(str(tmp_path))

    wire = _Wire()
    h = wire.as_handler("/api/episodes")
    h.do_GET()
    assert wire.status == 401
    assert json.loads(wire.wfile.getvalue()) == {"error": "login required"}

    wire2 = _Wire()
    wire2.headers[review_server.review_auth.API_KEY_HEADER] = tok
    h2 = wire2.as_handler("/api/episodes")
    h2.do_GET()
    assert wire2.status == 200


def test_head_on_api_paths_is_gated_the_same_as_get():
    """No do_HEAD is defined (verified: Handler defines only do_GET and do_POST), so
    HEAD is exercised against route() directly -- the one function every method goes
    through. A future do_HEAD inherits the same gate for free."""
    assert review_server.route("HEAD", "/api/episodes", {}, None)[0] == 401
    assert review_server.route("HEAD", "/api/episodes", {}, "wrong")[0] == 401


def _strip_ts(stem):
    """Rewrite the queue the way an old build left it: entries with no ts."""
    entries = [{k: v for k, v in e.items() if k != "ts"} for e in unresolved.items(stem)]
    unresolved._rewrite(stem, entries)


def _recent_ts(days_ago):
    """A queue ts `days_ago` old. Fixed calendar dates go stale: P33 expires anything past 14 days."""
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def test_oldest_pending_at_is_the_earliest_entry_ts_among_the_undecided(tmp_path, monkeypatch):
    stem = _episode(tmp_path, "busy")
    rows = unresolved.items(stem)
    rows[0]["ts"] = _recent_ts(3)
    rows[1]["ts"] = _recent_ts(4)  # rejected_guard: pending, the oldest
    rows[2]["ts"] = _recent_ts(2)
    rows[3]["ts"] = _recent_ts(6)  # llm_empty is not a primary reason: not counted
    unresolved._rewrite(stem, rows)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] == rows[1]["ts"]


def test_oldest_pending_at_ignores_a_decided_entry(tmp_path, monkeypatch):
    stem = _episode(tmp_path, "busy")
    rows = unresolved.items(stem)
    for e, days in zip(rows, (2, 5, 1, 3)):
        e["ts"] = _recent_ts(days)
    rows[1]["resolved"] = True  # the oldest is settled
    unresolved._rewrite(stem, rows)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] == rows[0]["ts"]


def test_oldest_pending_at_falls_back_to_the_store_mtime_when_no_entry_has_a_ts(tmp_path, monkeypatch):
    stem = _episode(tmp_path, "busy")
    _strip_ts(stem)
    artifact_store.touch(stem, "unresolved", 1_700_000_000)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])

    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] == "2023-11-14T22:13:20Z"


def test_oldest_pending_at_is_null_when_there_is_no_ts_and_the_store_cannot_be_read(tmp_path, monkeypatch):
    stem = _episode(tmp_path, "busy")
    _strip_ts(stem)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    monkeypatch.setattr(artifact_store, "mtime", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))

    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] is None


def test_forget_drops_the_cached_mtime_with_the_cached_queue(tmp_path, monkeypatch):
    stem = _episode(tmp_path, "busy")
    _strip_ts(stem)
    monkeypatch.setattr(review_server, "known_stems", lambda: [stem])
    artifact_store.touch(stem, "unresolved", 1_700_000_000)
    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] == "2023-11-14T22:13:20Z"

    artifact_store.touch(stem, "unresolved", 1_800_000_000)
    review_server.forget(stem)

    assert review_server.handle_index()["episodes"][0]["oldest_pending_at"] == "2027-01-15T08:00:00Z"


def test_a_fresh_walk_drops_the_cached_mtime_too(tmp_path, monkeypatch):
    _triage_episode(tmp_path, "e01")
    monkeypatch.setattr(review_server, "ROOTS", [str(tmp_path)])
    monkeypatch.setattr(review_server, "_STEMS_CACHE", None)
    monkeypatch.setattr(review_server, "_QUEUE_CACHE", {})
    monkeypatch.setattr(review_server, "_QUEUE_MTIME", {})
    monkeypatch.setattr(review_server, "STEMS_TTL", 0.0)
    monkeypatch.setattr(review_server, "STEMS_TTL_FACTOR", 0.0)
    stem = review_server.known_stems()[0]
    _strip_ts(stem)
    review_server.queue_written_at(stem)
    assert review_server._QUEUE_MTIME

    review_server.known_stems()

    assert review_server._QUEUE_MTIME == {}


def _shows_route(tmp_path, monkeypatch, root):
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("ANIME_ROOT", str(root))
    return review_server.route("GET", "/api/shows", {}, review_server.resolve_token(str(tmp_path)))


def test_api_shows_lists_only_directories_the_run_validator_accepts(tmp_path, monkeypatch):
    from dubtitlerr.review import review_requests

    root = tmp_path / "lib"
    for name in ("Frieren", "Bleach", "a..b"):
        (root / name / "Season 1").mkdir(parents=True)
    (root / "notes.txt").write_text("x")
    (root / "linked").symlink_to(tmp_path)
    (root / "alias").symlink_to(root / "Bleach")  # inside the root, but a link: not followed
    for name in (".hidden", "@eaDir", "lost+found"):
        (root / name).mkdir()

    status, body = _shows_route(tmp_path, monkeypatch, root)

    assert status == 200
    assert body == {"shows": ["Bleach", "Frieren"]}
    for s in body["shows"]:
        review_requests.validate({"show": s}, anime_root=str(root))


def test_api_shows_requires_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.setattr(review_server, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("ANIME_ROOT", str(tmp_path))
    assert review_server.route("GET", "/api/shows", {}, None)[0] == 401


def test_api_shows_is_empty_when_the_root_is_missing(tmp_path, monkeypatch):
    assert _shows_route(tmp_path, monkeypatch, tmp_path / "nope") == (200, {"shows": []})
