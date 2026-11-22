"""subtitle_encoding: read the bytes the provider actually shipped.

The failure this guards is silent, not loud: UTF-8 read as cp1252 does not raise, it
produces "Ã¤Â¸Â–" and a repair pass then treats that as the witness's wording. The tests
assert the two directions -- a Japanese cp932 track is read as Japanese, and a UTF-8 file
is never allowed to come back mojibake -- plus the lossy path not raising on bytes that
are not a subtitle at all.
"""

import codecs
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import subtitle_encoding

JAPANESE = "1\n00:00:01,000 --> 00:00:03,000\nフリーレン、魔王は倒したわ。\n"
RUSSIAN = "1\n00:00:01,000 --> 00:00:03,000\nПривет, мир.\n"
GERMAN = "1\n00:00:01,000 --> 00:00:03,000\nGrüße aus der Küche.\n"


def _bytes(text, encoding, bom=b""):
    return bom + text.encode(encoding)


# --- declared encodings ---------------------------------------------------------------


def test_utf8_bom_is_trusted():
    got = subtitle_encoding.decode(_bytes("Hello.", "utf-8", codecs.BOM_UTF8))
    assert got.encoding == "utf-8-sig"
    assert got.text.startswith("Hello.")


def test_utf16_with_bom_is_trusted():
    got = subtitle_encoding.decode(_bytes("Hello.", "utf-16-le", codecs.BOM_UTF16_LE))
    assert got.encoding == "utf-16"
    assert got.text.strip() == "Hello."


def test_plain_utf8_wins_outright():
    got = subtitle_encoding.decode(_bytes("Hello.", "utf-8"))
    assert got.encoding == "utf-8"
    assert got.clean and got.text == "Hello."


# --- the ladder per language ----------------------------------------------------------


def test_japanese_cp932_is_read_as_japanese():
    got = subtitle_encoding.decode(_bytes(JAPANESE, "cp932"), language="ja")
    assert got.encoding in ("cp932", "shift_jis")
    assert "フリーレン" in got.text
    assert got.clean


def test_japanese_euc_jp_is_read_as_japanese():
    got = subtitle_encoding.decode(_bytes(JAPANESE, "euc_jp"), language="ja")
    assert "フリーレン" in got.text
    assert got.clean


def test_russian_cp1251_beats_cp1252():
    got = subtitle_encoding.decode(_bytes(RUSSIAN, "cp1251"), language="ru")
    assert got.encoding == "cp1251"
    assert "Привет" in got.text


def test_german_cp1252_is_the_default():
    got = subtitle_encoding.decode(_bytes(GERMAN, "cp1252"), language="de")
    assert got.encoding == "cp1252"
    assert "Grüße" in got.text


# --- the silent-corruption direction ---------------------------------------------------


def test_utf8_is_never_returned_as_mojibake():
    data = _bytes(GERMAN, "utf-8")
    got = subtitle_encoding.decode(data, language="de")
    assert "Ã" not in got.text
    assert "Grüße" in got.text


def test_a_japanese_track_is_not_decoded_as_latin():
    data = _bytes(JAPANESE, "cp932")
    got = subtitle_encoding.decode(data)
    assert "ãƒ•ãƒªãƒ¼ãƒ¬ãƒ³" not in got.text


# --- the lossy path --------------------------------------------------------------------


def test_binary_noise_does_not_raise():
    got = subtitle_encoding.decode(bytes(range(256)) * 4, language="ja")
    assert got.text
    assert got.encoding


def test_text_input_passes_through():
    got = subtitle_encoding.decode("Hello.")
    assert got.encoding == "text" and got.text == "Hello."


def test_describe_is_json_safe():
    payload = subtitle_encoding.decode(_bytes(JAPANESE, "cp932"), language="ja").describe()
    assert payload["encoding"] in ("cp932", "shift_jis")
    assert payload["clean"] is True
    assert isinstance(payload["tried"], list)
