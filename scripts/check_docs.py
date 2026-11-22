#!/usr/bin/env python3
"""Fail when a tracked Markdown file links to a relative path that does not exist.

GitHub renders a missing relative link as a 404 with no warning at commit time, so a page
rename quietly breaks the README and every cross-reference in docs/wiki. This checks git's
index, not the working tree, so the CI run covers exactly what the release ships.

Anchor-only links and external URLs are out of scope: the first needs heading parsing, the
second needs the network. The repo's own pages are what rots.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)")
SKIP_PREFIXES = ("http://", "https://", "#", "mailto:", "<", "data:")


def tracked_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / name for name in result.stdout.split("\0") if name]


def broken_links() -> list[str]:
    broken: list[str] = []
    for path in tracked_markdown():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in LINK.finditer(text):
            target = match.group(1)
            if target.startswith(SKIP_PREFIXES):
                continue
            clean = target.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            if not (path.parent / clean).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    return broken


def main() -> int:
    broken = broken_links()
    if broken:
        print("broken relative links:")
        for entry in broken:
            print(f"  {entry}")
        return 1
    print(f"checked {len(tracked_markdown())} Markdown files: no broken relative links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
