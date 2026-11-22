"""dubtitlerr.core.refs - acquiring a reference subtitle from outside the release.

The pipeline can only mine names and check wording against a track the release ships. When
a show ships none, this package goes and gets one (Jimaku, the Kitsunekko successor), reads
it in whatever encoding it was saved in, and refuses it unless it provably belongs to the
video. ``subtitle_fit`` is the refusal; ``subtitle_encoding`` is the reading.

Nothing here is imported at package import time on purpose.
"""
