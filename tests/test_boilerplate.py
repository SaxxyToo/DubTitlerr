"""Arm #5: boilerplate/credit cue filter (dubtitlerr/pipeline/boilerplate.py) + the
offline sweep tool (tools/artifact_sweep.py).

The fixtures in tests/fixtures/artifacts/ are REAL BYTES lifted from the delivered
library on 2026-11-20 -- the Afro Samurai S01E01 pre-fix backup, the Demon Slayer tail,
the Vinland Saga mid-file Amara credit, the FMA:B dialogue guard and the JJUUTSU KAISEN
.ass header. A hand-written fixture would test the filter against sentences the corpus
does not contain, which is how a template list drifts from the material it exists for.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dubtitlerr.pipeline import boilerplate, qc

ARTIFACTS = Path(__file__).parent / "fixtures" / "artifacts"
REPO = Path(__file__).parent.parent


def srt_cues(name):
    from tools.artifact_sweep import parse_srt

    return parse_srt((ARTIFACTS / name).read_text(encoding="utf-8"))


def cards(texts):
    return [{"start": float(i * 2), "end": float(i * 2 + 1), "text": t} for i, t in enumerate(texts)]


# --- the template list --------------------------------------------------------------


# Lines lifted from the real artifacts. Every one of these must match a template, and
# every template that names one of these shapes must match it.
REAL_SAMPLES = [
    # Afro Samurai S01E01 pre-fix backup (one cue, two display lines)
    "This video is a derivative\nwork of the Touhou Project.",
    "I'm sorry for the poor quality.",
    "I'm sorry for the lack of subtitles.",
    "I hope you enjoy the video.",
    # Demon Slayer S04E03, last cue
    "Thanks for watching!",
    # Vinland Saga S01E13, cue 4 of 400
    "Subtitles by the Amara.org community",
    # JJUUTSU KAISEN S03E01 .ass header
    "Original Script: Kitsune-HentaiBot [http://www.crunchyroll.com/user/HentaiBot]",
]

# Shapes the 2026-11-20 inventory named but did not preserve a file for, plus the
# blocklist's own credit vocabulary. These pin the templates the corpus does not exercise;
# they are NOT evidence that the phrase occurs -- the real samples above are.
CONSTRUCTED_SAMPLES = [
    "Thank you for watching!",
    "Enjoy the episode.",
    "Please subscribe.",
    "Don't forget to subscribe!",
    "Subscribe to our channel.",
    "Captions by Acme.",
    "Transcribed by Acme.",
    "Translated by Acme.",
    "Amara.org",
    "www.example.com/fansubs",
]


def test_every_template_matches_a_sample():
    """Each pattern earns its place: it matches at least one corpus line or one of the
    named shapes. Iterating TEMPLATES means a template added without a sample fails here."""
    samples = REAL_SAMPLES + CONSTRUCTED_SAMPLES
    for template in boilerplate.TEMPLATES:
        pat = re.compile(template, re.I)
        assert any(pat.fullmatch(boilerplate.normalize(s)) for s in samples), template


def test_every_real_sample_is_caught():
    for sample in REAL_SAMPLES:
        assert boilerplate.is_boilerplate(sample), sample


@pytest.mark.parametrize(
    "text",
    [
        # Afro Samurai S01E01: one cue, two display lines. The filter reads the CUE.
        "This video is a derivative\nwork of the Touhou Project.",
        "I'm sorry for the poor quality.",
        "I'm sorry for the lack of subtitles.",
        "I hope you enjoy the video.",
        # Demon Slayer S04E03, last cue of the file.
        "Thanks for watching!",
        # Vinland Saga S01E13, cue 4 of 400.
        "Subtitles by the Amara.org community",
        # JJUUTSU KAISEN S03E01 .ass header.
        "Original Script: Kitsune-HentaiBot [http://www.crunchyroll.com/user/HentaiBot]",
    ],
)
def test_real_boilerplate_cues_match(text):
    assert boilerplate.is_boilerplate(text)


@pytest.mark.parametrize(
    "text",
    [
        # FMA:B S01E18 cue 108: spoken dialogue that shares the word "support".
        "Others support us silently.",
        # Demon Slayer S04E01 cue 292: the LAST cue of the episode, and real dialogue.
        "Thanks for watching him.",
        # truncated ASR partials must not match a credit/URL/stray template
        "to suppo",
        "to suppo...",
        "Thanks for",
        "subtitles by",
        "I love you.",
        "Wait here,",
    ],
)
def test_dialogue_and_partials_never_match(text):
    """Anchoring is the guard: only a WHOLE cue equal to a template matches.

    "Thanks for watching him." is the trailing cue of Demon Slayer S04E01 -- a substring
    filter would silently delete the episode's last line. "subtitles by" alone is not a
    credit either; the template requires the attribution that follows it.
    """
    assert not boilerplate.is_boilerplate(text)


# --- leading/trailing-only drop semantics -------------------------------------------


def test_leading_disclaimer_block_is_dropped_and_dialogue_survives():
    cues = srt_cues("afro_samurai_s01e01_head.srt")
    drops, audits = boilerplate.drop_indices(cues)
    assert drops == {0, 1, 2, 3}
    assert [c["text"] for c in cues if cues.index(c) not in drops] == ["Wait here,"]
    assert [a["run"] for a in audits] == ["leading"] * 4
    assert all(a["dropped"] for a in audits)


def test_trailing_thanks_is_dropped():
    cues = srt_cues("demon_slayer_s04e03_thanks_tail.srt")
    drops, audits = boilerplate.drop_indices(cues)
    assert drops == {2}
    assert [a["text"] for a in audits] == ["Thanks for watching!"]
    assert audits[0]["run"] == "trailing"


def test_a_trailing_dialogue_line_that_shares_words_is_kept():
    cues = srt_cues("demon_slayer_s04e01_watching_him.srt")
    drops, audits = boilerplate.drop_indices(cues)
    assert drops == set()
    assert audits == []


def test_midfile_credit_is_counted_never_dropped():
    cues = srt_cues("vinland_saga_s01e13_amara_mid.srt")
    drops, audits = boilerplate.drop_indices(cues)
    assert drops == set()
    assert [(a["run"], a["dropped"], a["text"]) for a in audits] == [
        ("midfile", False, "Subtitles by the Amara.org community")
    ]


def test_midfile_match_between_leading_and_trailing_runs_is_kept():
    texts = [
        "Thanks for watching!",
        "Real line.",
        "Subtitles by the Amara.org community",
        "Another line.",
        "Thanks for watching!",
    ]
    drops, audits = boilerplate.drop_indices(cards(texts))
    assert drops == {0, 4}
    mid = [a for a in audits if a["run"] == "midfile"]
    assert [a["text"] for a in mid] == ["Subtitles by the Amara.org community"]
    assert not mid[0]["dropped"]


def test_all_matching_episode_is_refused_rather_than_emptied():
    """If every cue matches, the template is wrong about this file -- not the file wrong
    about having dialogue. Refusing is the only safe direction: an empty srt passes a
    publish gate and loses an episode."""
    drops, _ = boilerplate.drop_indices(cards(["Thanks for watching!"] * 5))
    assert drops == set()


def test_run_longer_than_max_is_left_for_the_loop_arms():
    drops, _ = boilerplate.drop_indices(cards(["Thanks for watching!"] * (boilerplate.MAX_RUN + 1)))
    assert drops == set()


# --- counters and audit trail -------------------------------------------------------


def test_counters_and_per_cue_events_land_in_the_sidecar():
    rec = qc.Recorder()
    cues = srt_cues("afro_samurai_s01e01_head.srt")
    boilerplate.drop_indices(cues, rec=rec)
    assert rec.counters["dropped_boilerplate"] == 4
    assert rec.counters["boilerplate_midfile"] == 0
    dropped = [e for e in rec.priority_events if e["reason"] == "boilerplate"]
    assert [e["card_index"] for e in dropped] == [0, 1, 2, 3]
    assert {e["run"] for e in dropped} == {"leading"}
    assert dropped[0]["text"] == "This video is a derivative work of the Touhou Project."


def test_midfile_hits_are_counted_separately():
    rec = qc.Recorder()
    boilerplate.drop_indices(srt_cues("vinland_saga_s01e13_amara_mid.srt"), rec=rec)
    assert rec.counters["dropped_boilerplate"] == 0
    assert rec.counters["boilerplate_midfile"] == 1
    assert [e["text"] for e in rec.events] == ["Subtitles by the Amara.org community"]


def test_bare_call_needs_no_recorder():
    drops, audits = boilerplate.drop_indices(srt_cues("afro_samurai_s01e01_head.srt"))
    assert len(drops) == 4 and len(audits) == 4


# --- the sweep tool ------------------------------------------------------------------


def run_tool(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / "artifact_sweep.py"), *args],
        capture_output=True, text=True, cwd=cwd or REPO,
    )


def test_scan_reproduces_the_expected_hit_distribution(tmp_path):
    """A dry run over a fixture directory: the same shapes the library sweep found.

    The fixtures keep their real (non-sidecar) names on disk; the copies the scan walks get
    the delivered naming, because that is what the sweep filters on.
    """
    names = {
        "afro_samurai_s01e01_head.srt": "afro-s01e01.dub.en.srt",
        "demon_slayer_s04e03_thanks_tail.srt": "demon-s04e03.dub.en.srt",
        "vinland_saga_s01e13_amara_mid.srt": "vinland-s01e13.dub.en.srt",
        "jjk_s03e01_credit_header.ass": "jjk-s03e01.dub.en.ass",
        "demon_slayer_s04e01_watching_him.srt": "demon-s04e01-guard.dub.en.srt",
        "fmab_s01e18_others_support_guard.srt": "fmab-s01e18-guard.dub.en.srt",
    }
    for src, dst in names.items():
        (tmp_path / dst).write_bytes((ARTIFACTS / src).read_bytes())

    res = run_tool("scan", str(tmp_path))
    assert res.returncode == 0, res.stderr
    rows = {}
    for line in res.stdout.splitlines():
        path, hits, first, last = line.split("\t")
        rows[Path(path).name] = (int(hits), first)
    assert rows == {
        "afro-s01e01.dub.en.srt": (4, "This video is a derivative work of the Touhou Project."),
        "demon-s04e03.dub.en.srt": (1, "Thanks for watching!"),
        "vinland-s01e13.dub.en.srt": (1, "Subtitles by the Amara.org community"),
        # .ass lines: the credit is a header line, and so is pysubs2's URL comment.
        "jjk-s03e01.dub.en.ass": (2, "; https://pypi.python.org/pypi/pysubs2"),
    }
    # the guards never appear
    assert "demon-s04e01-guard.dub.en.srt" not in rows
    assert "fmab-s01e18-guard.dub.en.srt" not in rows


def test_scan_skips_backups_and_parked_copies(tmp_path):
    sample = (ARTIFACTS / "demon_slayer_s04e03_thanks_tail.srt").read_text()
    (tmp_path / "ep.dub.en.srt").write_text(sample)
    for name in ("ep.dub.en.srt.pre-artifact-fix-20261120", "ep.dub.en.srt.bak", "ep.dub.en.srt.stale"):
        (tmp_path / name).write_text(sample)
    res = run_tool("scan", str(tmp_path))
    assert len([ln for ln in res.stdout.splitlines() if ln]) == 1


def test_fix_renumbers_keeps_timestamps_and_writes_audit(tmp_path):
    src = ARTIFACTS / "afro_samurai_s01e01_head.srt"
    target = tmp_path / "ep.dub.en.srt"
    target.write_bytes(src.read_bytes())
    audit = tmp_path / "audit.jsonl"

    res = run_tool("fix", "--audit", str(audit), str(target))
    assert res.returncode == 0, res.stderr

    out = target.read_text()
    # one cue left, renumbered from 1, timestamp copied byte-for-byte
    assert out == "1\n00:00:24,530 --> 00:00:30,417\nWait here,\n\n"
    backups = list(tmp_path.glob("ep.dub.en.srt.pre-artifact-fix-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == src.read_bytes()

    doc = json.loads(audit.read_text().splitlines()[0])
    assert doc["dropped_cues"] == 4
    assert doc["kept"] == 1
    assert doc["backup"] == str(backups[0])
    assert doc["dropped_texts"] == [
        "This video is a derivative work of the Touhou Project.",
        "I'm sorry for the poor quality.",
        "I'm sorry for the lack of subtitles.",
        "I hope you enjoy the video.",
    ]


def test_fix_dry_run_writes_nothing(tmp_path):
    target = tmp_path / "ep.dub.en.srt"
    target.write_bytes((ARTIFACTS / "demon_slayer_s04e03_thanks_tail.srt").read_bytes())
    before = target.read_bytes()
    audit = tmp_path / "audit.jsonl"

    res = run_tool("fix", "--dry-run", "--audit", str(audit), str(target))
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["dropped_cues"] == 1
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*pre-artifact-fix*"))
    assert not audit.exists()


def test_fix_leaves_midfile_credits_alone(tmp_path):
    target = tmp_path / "ep.dub.en.srt"
    target.write_bytes((ARTIFACTS / "vinland_saga_s01e13_amara_mid.srt").read_bytes())
    before = target.read_bytes()

    res = run_tool("fix", "--audit", str(tmp_path / "audit.jsonl"), str(target))
    assert res.returncode == 0, res.stderr
    assert "mid hits not touched" in res.stdout
    assert target.read_bytes() == before


def test_fix_skips_ass(tmp_path):
    target = tmp_path / "ep.dub.en.ass"
    target.write_bytes((ARTIFACTS / "jjk_s03e01_credit_header.ass").read_bytes())
    res = run_tool("fix", str(target))
    assert "SKIP" in res.stdout
