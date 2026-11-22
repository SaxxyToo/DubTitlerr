"""refs.acquire: getting a reference, and refusing one that does not fit the video.

The admitted case uses a real artifact (Frieren S02E01's dubtitle file, 342 cues ending at
1,439.9 s) inside a real zip, served by a fake client: what is being tested is the admission
pipeline, not the fixture. The refused case is the same bytes against a 300 s video, which is
what another cut of the episode looks like to ``subtitle_fit``.
"""

import io
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core import subtitle_fit
from dubtitlerr.core.refs import acquire, jimaku

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "artifacts")
ARTIFACT = "frieren_s02e01_song.dubtitles.ass"


def _artifact_bytes():
    with open(os.path.join(FIXTURES, ARTIFACT), "rb") as fh:
        return fh.read()


def _zip(member_name, payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, payload)
    return buffer.getvalue()


class FakeClient:
    configured = True

    def __init__(self, entries, files, payloads):
        self._entries = entries
        self._files = files
        self._payloads = payloads

    def entries(self, *, anilist_id=None, query=None):
        return self._entries

    def files(self, entry_id):
        return self._files.get(entry_id, [])

    def download(self, url):
        return self._payloads[url]


def _client(*, remote_name="Frieren - 01.ja.zip", member=ARTIFACT, entries=None):
    url = "https://jimaku.cc/entry/7/files/1"
    return FakeClient(
        entries or [{"id": 7, "name": "Frieren", "anilist_id": 154587}],
        {7: [{"name": remote_name, "url": url}]},
        {url: _zip(member, _artifact_bytes())},
    )


# --- admission ------------------------------------------------------------------------


def test_a_fitting_reference_is_admitted():
    result = acquire.acquire(_client(), titles=["Frieren"], season=1, episode=1, duration=1440.0)
    assert result.ok
    assert result.chosen == ARTIFACT
    candidate = result.candidates[0]
    assert candidate.cues > 300
    assert candidate.encoding in ("utf-8", "utf-8-sig")
    assert candidate.verdict["ok"] is True
    assert candidate.verdict["defects"] == []


def test_the_wrong_cut_is_refused_with_its_reason():
    # Same bytes, a 300 s video: a double-length episode used against a TV cut.
    result = acquire.acquire(_client(), titles=["Frieren"], season=1, episode=1, duration=300.0)
    assert not result.ok
    assert result.chosen is None
    defects = result.candidates[0].verdict["defects"]
    assert any(item.startswith("overrun:") for item in defects)
    assert "admission:no_candidate_passed" in result.reasons
    assert result.candidates[0].verdict["ok"] is False


def test_a_file_for_another_episode_is_never_downloaded():
    result = acquire.acquire(_client(remote_name="Frieren - 02.ja.zip"), titles=["Frieren"], season=1, episode=1, duration=1440.0)
    assert not result.ok
    assert any("no_file_for_episode" in reason for reason in result.reasons)
    assert result.candidates == []


def test_no_api_key_is_reported_not_raised():
    client = FakeClient([], {}, {})
    client.configured = False
    result = acquire.acquire(client, titles=["Frieren"], season=1, episode=1)
    assert not result.ok
    assert result.reasons == ["jimaku:no_api_key"]


def test_no_episode_is_reported_not_guessed():
    result = acquire.acquire(_client(), titles=["Frieren"], season=1)
    assert not result.ok
    assert result.reasons == ["query:no_episode"]


# --- extraction -----------------------------------------------------------------------


def test_extract_passes_a_plain_subtitle_through():
    assert acquire.extract_subtitles("Show - 01.ja.ass", b"bytes") == [("show - 01.ja.ass", b"bytes")]


def test_extract_skips_traversal_and_non_subtitles():
    payload = _zip("../evil.ass", b"x")
    buffer = io.BytesIO(payload)
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("readme.txt", b"x")
        archive.writestr("good.ass", b"y")
    members = acquire.extract_subtitles("batch.zip", buffer.getvalue())
    assert [name for name, _ in members] == ["good.ass"]


def test_a_rar_without_bsdtar_is_an_error_not_a_silent_skip(monkeypatch):
    monkeypatch.setattr(acquire.shutil, "which", lambda _name: None)
    with pytest.raises(jimaku.JimakuError):
        acquire.extract_subtitles("batch.rar", b"Rar!")


# --- output ---------------------------------------------------------------------------


def test_save_writes_only_the_admitted_file(tmp_path):
    result = acquire.acquire(_client(), titles=["Frieren"], season=1, episode=1, duration=1440.0)
    written = acquire.save(result, str(tmp_path))
    assert [os.path.basename(path) for path in written] == [ARTIFACT]
    assert os.path.getsize(written[0]) == len(_artifact_bytes())


def test_probe_duration_returns_none_for_an_unreadable_video():
    assert acquire.probe_duration("/nonexistent/video.mkv") is None


def test_cli_without_a_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("JIMAKU_API_KEY", raising=False)
    assert acquire.main(["--episode", "1"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_witness_is_optional_but_honoured():
    witness = subtitle_fit.parse_cues(_artifact_bytes().decode("utf-8-sig"))
    result = acquire.acquire(_client(), titles=["Frieren"], season=1, episode=1, duration=1440.0, witness=witness)
    assert result.ok
    assert result.candidates[0].verdict["fit"]["share"] == 1.0
