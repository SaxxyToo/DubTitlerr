#!/usr/bin/env python3
"""Decode a subtitle file whose encoding nobody told us.

A downloaded reference is bytes without a declared charset. Western providers mostly ship
UTF-8, but the anime sources do not: Kitsunekko's archive is a decade of files saved in
whatever Windows the fansubber used, so Japanese tracks arrive as cp932/Shift-JIS
(occasionally EUC-JP or ISO-2022-JP), Korean ones as cp949, and the older European packs as
cp1250/1251/1252/1253. Reading cp932 as UTF-8 raises; reading UTF-8 as cp1252 does not --
it silently produces ``Ã£ÂÂ‚`` mojibake, which a repair pass would then treat as the
witness's actual wording, which is worse than a crash.

So the ladder is ordered by evidence, not by convenience:

  1. **BOM** -- UTF-8, UTF-16LE/BE. A BOM is a declaration, not a guess.
  2. **UTF-8** -- strict decode. Valid UTF-8 is almost never anything else, and strictness
     is what makes this safe: cp932 bytes usually are not valid UTF-8.
  3. **The language's own codecs** -- cp932/Shift-JIS/EUC-JP/ISO-2022-JP for ``ja``,
     cp949/EUC-KR for ``ko``, GB18030/GBK for ``zh``, cp1251 for ``ru``. The language tag
     is the prior; a Japanese track that decodes cleanly as cp932 is a Japanese track.
  4. **The legacy Windows codepages**, scored rather than trusted: cp1252 decodes nearly
     any byte, so a clean decode proves nothing. Each candidate is scored on control
     characters, replacement characters and mojibake markers ("Ã", "Â", "â€", the UTF-8
     lead bytes read as Latin-1), and the best score wins.
  5. **cp1252 with replacement** -- a lossy last resort, reported as such.

``chardet`` is used only as an extra candidate when it is installed: it is a useful hint
and a bad authority (on a 3 KB file it picks whatever scores best), so it enters the same
scoring as everything else instead of deciding.

The return value names the encoding and whether the decode was clean, because the caller
has to record which witness was read how -- a repaired line's provenance starts here.

Stdlib only, and no import of ``common``: this runs on the acquisition path, under the
shell-loop harness's curated package slice.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
)

LANGUAGE_CODECS = {
    "ja": ("cp932", "shift_jis", "euc_jp", "iso2022_jp"),
    "ko": ("cp949", "euc_kr"),
    "zh": ("gb18030", "gbk", "big5"),
    "ru": ("cp1251", "koi8_r"),
    "uk": ("cp1251", "koi8_u"),
    "pl": ("cp1250", "iso8859_2"),
    "cs": ("cp1250", "iso8859_2"),
    "hu": ("cp1250", "iso8859_2"),
    "tr": ("cp1254", "iso8859_9"),
    "el": ("cp1253", "iso8859_7"),
    "he": ("cp1255", "iso8859_8"),
    "ar": ("cp1256", "iso8859_6"),
    "th": ("cp874", "tis_620"),
    "vi": ("cp1258",),
}

# The order a Western file is tried in. cp1252 first because it is the Windows default and
# covers the most cases; the rest exist because a Polish or Russian pack decodes "cleanly"
# as cp1252 into the wrong letters, which only the score below can tell apart.
LEGACY_CODECS = ("cp1252", "cp1250", "cp1251", "cp1253", "cp1254", "cp1257", "cp1258", "iso8859_1")

FALLBACK = ("cp1252", "replace")

# Characters that a wrong decode produces and a right one does not. Latin-1 aliasing of
# UTF-8 continuation bytes is the signature of "UTF-8 read as cp1252": Ã (C3), Â (C2),
# â€ (E2 80), and the C1 range that no sane subtitle contains.
_MOJIBAKE = ("Ã", "Â", "â€", "Ã¢", "ï»¿")
_C1_CONTROL = re.compile(r"[\x80-\x9f]")
_PRINTABLE = re.compile(r"[^\x00-\x08\x0b\x0c\x0e-\x1f]")
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")

MIN_PRINTABLE = 0.995


@dataclass
class Decoded:
    """A decoded subtitle plus the evidence for the encoding it was decoded as."""

    text: str
    encoding: str
    score: float
    clean: bool
    tried: tuple = field(default_factory=tuple)

    def describe(self):
        return {
            "encoding": self.encoding,
            "score": round(self.score, 4),
            "clean": self.clean,
            "chars": len(self.text),
            "tried": list(self.tried),
        }


def _score(text, language=None):
    """How much the decoded text looks like a subtitle in ``language``.

    Two independent parts: printability (control and replacement characters are what a
    wrong decode emits) and script expectation (a ``ja`` file that came out all Latin
    letters was decoded wrong, whatever the printability says).
    """
    if not text:
        return 1.0
    printable = len(_PRINTABLE.findall(text)) / len(text)
    score = printable
    if printable < MIN_PRINTABLE:
        score -= (MIN_PRINTABLE - printable) * 10
    if "\ufffd" in text:
        score -= 0.5
    if any(marker in text for marker in _MOJIBAKE):
        score -= 0.6
    if _C1_CONTROL.search(text):
        score -= 0.3

    if language == "ja":
        score += 0.5 if _CJK.search(text) else -0.5
    elif language == "ko":
        score += 0.5 if _HANGUL.search(text) else -0.5
    elif language in ("ru", "uk"):
        score += 0.5 if _CYRILLIC.search(text) else -0.5
    return score


def _chardet_candidates(data):
    """Encodings chardet suggests, if it happens to be installed. A hint, not a verdict."""
    try:
        import chardet
    except ImportError:
        return ()
    try:
        guesses = chardet.detect_all(data[:262144]) or []
    except Exception:  # noqa: BLE001 - an optional dependency must never break a decode
        return ()
    out = []
    for guess in sorted(guesses, key=lambda item: item.get("confidence") or 0, reverse=True):
        name = (guess.get("encoding") or "").lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def decode(data, language=None):
    """Decode subtitle bytes, preferring the evidence over the guess.

    ``language`` is an ISO-639 code (``ja``, ``ko``, ...) when the provider told us one;
    it selects the codec ladder that is tried before the Western legacy defaults.
    """
    if isinstance(data, str):
        return Decoded(data, "text", 1.0, True, ("text",))

    tried = []
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return Decoded(data.decode(encoding), encoding, 1.0, True, (encoding,))
            except UnicodeDecodeError:
                tried.append(encoding)

    candidates = ["utf-8"]
    candidates.extend(LANGUAGE_CODECS.get((language or "").lower(), ()))
    candidates.extend(LEGACY_CODECS)
    candidates.extend(_chardet_candidates(data))

    best = None
    seen = set()
    for encoding in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        tried.append(encoding)
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _score(text, language)
        clean = "\ufffd" not in text
        # Strict UTF-8 wins outright: a valid strict decode is the declaration the file
        # failed to make, and no legacy codepage needs to be consulted after it.
        if encoding == "utf-8" and clean:
            return Decoded(text, encoding, score, True, tuple(tried))
        if best is None or score > best.score:
            best = Decoded(text, encoding, score, clean, ())
        if clean and score >= 1.0 and encoding in LANGUAGE_CODECS.get((language or "").lower(), ()):
            break

    if best is None:
        text = data.decode(*FALLBACK)
        return Decoded(text, FALLBACK[0], _score(text, language), False, tuple(tried))

    best.tried = tuple(tried)
    return best


def decode_file(path, language=None):
    """``decode`` for a file on disk. Raises ``OSError`` as the caller expects."""
    with open(path, "rb") as fh:
        return decode(fh.read(), language)
