#!/usr/bin/env python3
"""Which cards in a dubtitle sidecar are a *song span* rather than dialogue.

A dub re-uses the Japanese opening/ending song, and the dub ASR is asked to transcribe
English speech. On sung Japanese it does what it does on any non-English audio: it emits
a stream of pseudo-romaji ("Osoresu ni susume yo.", "Furikaereba itsumo.") with the
occasional confident English line where the release sings an English adaptation. Measured
on Frieren S02E01 (whisper large-v3, English): the ending-song region 23:03-23:29 is seven
such cards, and 141 of its 2,155 word timings are romaji the dub never speaks.

Two things are wrong with treating those cards as dialogue:

  * the English-mishear guard scans them. A show glossary legitimately carries
    ``Furikaereba -> Frieren`` (it was admitted from a dialogue mishear), so the lyric line
    "Furikaereba itsumo." is reported as a known mishear forever -- and no correction can
    ever fix a lyric. That refusal parked Frieren S02E01 with no way out.
  * they are not English text, so counting them against the dialogue punctuation and
    layout budgets measures the song, not the subtitles.

So the class is recognised here, once, and every consumer treats the region as song:
``publish_gate`` leaves it out of its dialogue checks and its mishear scan, and
``gate_fix`` leaves its cards' bytes alone. Nothing is deleted and no word is changed --
the signs/lyrics path owns the span, and a styled sidecar still ships the release's own
lyric events exactly where they were.

## How a song span is recognised

Per card, two tests, both required:

  * **no English word at all** -- a single recognised English word ("no", "you", "the")
    disqualifies the card, because English dialogue is dense with them and romaji lines
    have none. At least two word tokens are required, so a one-word card ("Emily,") can
    never be classified on its own.
  * **romaji syllable shape** -- every token's consonant runs must be writable in romaji
    (a digraph sh/ch/ts, a doubled consonant, or nothing longer than one letter). This is
    what keeps *terse English* out: 86 Eighty-Six's radio chatter ("Confirming ID.",
    "Synchronization target.", "Weak Kingsley.") has no function word either, but its
    clusters (nf/rm/ng, snc/tn, ck) are unspellable in romaji. Measured 2026-11-21 against
    the live artifacts: without this second test the detector swallowed eleven lines of
    86's military dialogue and two of Cyberpunk's -- including name-bearing lines, which is
    exactly where a mishear lives.

Per region: at least ``MIN_LYRIC_CUES`` lyric-shaped cards within ``SONG_GAP`` seconds of
each other, which makes the region a *span* from the first lyric card's start to the last
lyric card's end. Merging on gaps (not on adjacency) is deliberate: the Frieren ending
alternates romaji and English-adaptation lines -- "Osoresu ni susume yo. / Holy brave. /
Furikaereba itsumo. / Only trace." -- so the English lines fall inside the span and are
handled with it, while a single foreign-looking card in dialogue never becomes a song.

Deliberately conservative in four ways: a span longer than ``MAX_SONG_SPAN`` is not
recognised (a misclassification, not a song); a span covering more than
``MAX_SONG_SHARE`` of the file's cards is not recognised either (a song region is a small
part of an episode -- a majority-foreign file is a broken transcript, not a concert); the
region must hold two lyric-shaped cards; and a false positive costs only an exclusion from
the dialogue checks, never a deleted or rewritten card.

Stdlib only, and no import of ``common``: ``publish_gate`` runs under the shell-loop
harness's curated package slice, which has no ``pysubs2``.
"""

from __future__ import annotations

import re

# The English words that make a card dialogue: function words and the most common verbs
# and adverbs, plus the contraction fragments a `[A-Za-z]+` tokenizer produces ("don't"
# arrives as "don" + "t"). A romaji lyric line contains none of them.
ENGLISH = frozenset(
    """
a about above after again against all almost alone along already also although always am
an and another any anyone anything are aren around as ask asked askin at away back be
became because become been before began begin behind being believe below best better
between big both bring brought but buy by call came can cannot cant care carry come comes
coming could couldn did didn do does doesn doing don done dont down during each either
else enough even ever every everyone everything exactly far feel few find fine first five
for found four from get gets getting give given go goes going gone good got gotten great
grow had hadn has hasn have haven having he hear heard her here hers herself hey hi him
himself his hold home how however i if in instead into is isn it its itself just keep kind
know knew last later least leave left less let like listen little live long look looking
lot made make makes making many may maybe me mean might mine more most much must my myself
need never new next nice night no nobody none nor not nothing now of off often oh okay ok
old on once one only open or other our ours ourselves out over own part people perhaps
place please put quite rather really right said same saw say says see seem seemed seen
self she should shouldn since so some someone something sometimes soon still stop such
sure take talk tell than thank thanks that the their theirs them themselves then there
these they thing things think this those though thought three through time to today
together told too took toward two under until up upon us use used very wait want was wasn
way we well went were weren what whatever when where whether which while who whole whom
why will with within without won word work would wouldn yeah yes yet you your yours
yourself
""".split()
)

ENGLISH = frozenset(w.lower() for w in ENGLISH)

_TOKEN = re.compile(r"[A-Za-z]+")
_CONSONANT = frozenset("bcdfghjklmnpqrstvwxz")  # y is a vowel in romaji (yo, kya)
# Romaji's whole syllable system is consonant+vowel: a consonant run longer than one letter
# is only legal as a digraph it spells (sh, ch, ts), a doubled consonant (donna, dokka) or
# the moraic n. English words are full of runs it cannot write -- "confirming" (nf/rm/ng),
# "synchronization" (snc/tn), "crickets" (cr/ck), "helpless" (lp/ss) -- which is what
# separates terse English "Confirming ID." from a romaji "Furikaereba itsumo.".
_ROMAJI_DIGRAPHS = frozenset({"sh", "ch", "ts"})


def _romaji_shaped(token):
    """True when the token's consonant runs are all writable in romaji."""
    run = []
    for ch in token.lower():
        if ch in _CONSONANT:
            run.append(ch)
            continue
        if len(run) > 1 and not _is_legal_run(run):
            return False
        run = []
    return len(run) <= 1 or _is_legal_run(run)


def _is_legal_run(run):
    return "".join(run) in _ROMAJI_DIGRAPHS or len(set(run)) == 1


SONG_GAP = 8.0
MIN_LYRIC_CUES = 2
MAX_SONG_SPAN = 180.0
# A song is a small fraction of an episode. A region that holds a quarter of the file is
# a broken transcript (blocks of foreign-looking gibberish), not an OP/ED, and excluding it
# from the dialogue checks would hand the file a free pass: Frieren S02E01's ending is 15
# of 336 cards (4.5%), and a two-minute ED inside a 24-minute episode stays under 15%.
MAX_SONG_SHARE = 0.25
MIN_LYRIC_TOKENS = 2


def tokens(text):
    """The card's word tokens, casefolded: punctuation, newlines and music marks dropped."""
    return [t.lower() for t in _TOKEN.findall(text.replace("\\N", " "))]


def is_lyric_shaped(text):
    """True when this card reads as a sung romaji lyric rather than English dialogue.

    Two independent tests, both required:

      * no recognised English word in the card at all, and
      * every token's consonant runs are writable in romaji (``_romaji_shaped``).

    The second test is what keeps terse English out: "Confirming ID.", "Weak Kingsley."
    and "*crickets chirping*" contain no function word, but their consonant clusters are
    unspellable in romaji. A card with fewer than ``MIN_LYRIC_TOKENS`` word tokens is never
    lyric-shaped: one word is not enough evidence, and short dialogue cards are common.
    """
    toks = tokens(text)
    if len(toks) < MIN_LYRIC_TOKENS or any(t in ENGLISH for t in toks):
        return False
    return all(_romaji_shaped(t) for t in toks)


def spans(cues):
    """The song spans in a cue list, as ``[(start, end), ...]`` in file order.

    ``cues`` is a list of ``(start, end, text)``. Lyric-shaped cards are grouped when their
    start is within ``SONG_GAP`` seconds of the previous lyric card's end; a group of
    ``MIN_LYRIC_CUES`` or more becomes one span from its first start to its last end.
    A group longer than ``MAX_SONG_SPAN``, or one covering more than ``MAX_SONG_SHARE`` of
    the file's cards, is not a song and is ignored.
    """
    groups = []
    for start, end, text in cues:
        if not is_lyric_shaped(text):
            continue
        if groups and start - groups[-1][-1][1] <= SONG_GAP:
            groups[-1].append((start, end))
        else:
            groups.append([(start, end)])
    out = []
    budget = MAX_SONG_SHARE * len(cues)
    for group in groups:
        if len(group) < MIN_LYRIC_CUES:
            continue
        start, end = group[0][0], group[-1][1]
        if end - start > MAX_SONG_SPAN:
            continue
        covered = sum(1 for s, e, _ in cues if start < e and end > s)
        if covered > budget:
            continue
        out.append((start, end))
    return out


def indexes(cues):
    """The indexes of the cards inside any song span."""
    found = spans(cues)
    return {i for i, (start, end, _) in enumerate(cues) if any(start < s_end and end > s_start for s_start, s_end in found)}


def describe(cues):
    """A compact, JSON-safe summary of the spans found, for the gate's audit output."""
    return [
        {"start": round(start, 2), "end": round(end, 2), "cards": sum(1 for s, e, _ in cues if start <= s < end)}
        for start, end in spans(cues)
    ]
