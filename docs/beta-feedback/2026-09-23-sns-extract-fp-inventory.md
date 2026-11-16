# Signs/songs classifier: false-positive inventory

Opened 2026-09-23 as a template for the extract-only prototype (`tools/sns_extract.py`).
Filled in 2026-11-16 from a measurement of `dub_signs_merge.keep_event` itself.

## What was measured

The first episode of each of the 27 shows in one library. For every episode, the tracks
`common.signs_sub_streams` actually hands to the merge were extracted and every event
classified.

- 4 shows have no in-scope ASS track (Blue Eye Samurai, Cowboy Bebop, Cyberpunk
  Edgerunners, Fullmetal Alchemist Brotherhood).
- 18 shows have a track that names itself as the signs track, so only that track is read.
- 5 shows are read as mixed dialogue+signs tracks (Afro Samurai, Berserk (1997), Dororo,
  JUJUTSU KAISEN, Samurai Champloo).

The question: which events are kept **only** because of a bare alignment (`\an1`, `\an3`
to `\an9`) or a bare fade (`\fad`, `\fade`), with no `\pos`, `\move`, `\k`, `\p`, `\clip`
or `\t` and no song-family style?

## Result

350 such events, in these classes:

| tag           | style class                           | events | what they are |
| :------------ | :------------------------------------ | -----: | :------------ |
| fade          | other (`OP English`, `ED_Eng`, `op`, `OP1`, `EDTL`, `ED - English`) | 239 | English song translations. All wanted. |
| fade          | keep-style (`OP - Romaji`, `Japanese Lyrics`, a title style)        |  79 | Song lyrics and one title. All wanted. |
| align + fade  | keep-style / other (`OP - Romaji`, `OP - English`)                  |   8 | Song lyrics. All wanted. |
| align         | other (`Crybaby - TS`)                                              |   3 | Typeset name cards. All wanted. |
| **align**     | **dialogue-named (`Default`, `main`)**                              | **9** | **Fansub dialogue. All false positives.** |
| **fade**      | **dialogue-named (`Default`)**                                      | **1** | **Fansub dialogue. False positive.** |

All ten false positives are in the two mixed tracks that carry top-aligned dialogue:

- Berserk (1997) S01E01, 3 lines, e.g. `[main] {\an8}They're back again...`
- JUJUTSU KAISEN S01E01, 7 lines, e.g. `[Default] {\an8}Check this out!` and
  `[Default] {\fade(0,300)}It didn't feel like I was, though.`

In the output each one rendered at the top of the screen over the dub: the fansub's
translation of the Japanese script, next to different dub wording at the bottom.

## What this rules out

"A bare `\an` or `\fad` is not sign evidence" as a general rule. It would remove the 10
false positives and 239 wanted song-translation lines with them, because the fade is the
only thing keeping those lines: their styles (`OP English`, `ED_Eng`, `op`, `OP1`) match
neither `KEEP_STYLE` nor `SONG_FAMILY_STYLE`.

## Rule adopted

In a track that did not name itself as the signs track, on a dialogue-named style
(`WEAK_DROP_STYLE`), a bare alignment or fade does not override the style guess. Tags
that place, draw, clip or transform still do. Everything else is unchanged.

On the 27 episodes this removes exactly the 10 lines above and adds or removes nothing
else (kept events: Berserk 58 → 55, JUJUTSU KAISEN 623 → 616, 25 other shows identical).

## Not fixed here

- `common.is_dialogue_event` applies the same alignment test when it picks repair and
  timing references, so top-aligned fansub dialogue is not used as a reference. A test
  pins that behaviour (`test_is_dialogue_event_rejects_top_aligned_an_tag`), and changing
  it alters what the repair stage anchors on. Left for a separate decision.
- A signs track that names itself is not held to the stricter rule. No measured track
  showed dialogue there.
- False negatives in named signs tracks. The same measurement found 57 real sign and song
  events dropped from three of them (Chainsaw Man 35 opening-translation lines on style
  `OP`; Steins;Gate 13 on `op trans` and 5 phone-screen signs; Devilman Crybaby 4 typeset
  name cards). Separate fix.
