"""The container extensions the pipeline treats as episodes.

Stdlib only, and a leaf on purpose: the shell-loop CLIs (library_queue.py,
watch_queue.py) run from a minimal app dir with no pysubs2, so a constant they need
cannot live in common.py. common re-exports it, so every `common.VIDEO_EXTS` caller
keeps working.
"""

VIDEO_EXTS = (".mkv", ".mp4", ".m4v")
