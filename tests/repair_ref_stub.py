"""Test helper: stub an episode's classified repair-reference tracks.

Cards in the repair loop take their reference from ``repair.reference_tracks`` (R2: one
classified track per card). Tests that exercise the card loop care about the reference
TEXT, not about track classification -- that is ``test_reftracks.py``'s job -- so they
stub ``repair.reference_tracks`` with one translation-role track holding the cues they
previously gave ``dialogue_intervals``.

Tests that exercise the ``REPAIR_REF_CLASSIC=1`` rollback path keep stubbing
``repair.dialogue_intervals`` directly.
"""

from dubtitlerr.core.reftracks import PROVENANCE_NATIVE, ROLE_TRANSLATION, Event, Track


def track(cues, role=ROLE_TRANSLATION, title="Full Subtitles", index=1, provenance=PROVENANCE_NATIVE):
    """One reference track wrapping plain ``(start, end, text)`` cues."""
    return Track(
        index=index,
        codec="ass",
        lang="en",
        title=title,
        provenance=provenance,
        role=role,
        role_confirmed=True,
        role_overlap=None,
        events=tuple(Event(float(s), float(e), t) for s, e, t in cues),
    )


def stub(cues, role=ROLE_TRANSLATION):
    """A ``repair.reference_tracks`` replacement returning one track holding ``cues``."""
    return lambda video, conf=None: [track(cues, role=role)]
