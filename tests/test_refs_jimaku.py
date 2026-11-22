"""refs.jimaku: the client's contract with a real API.

Two things are pinned here because both fail silently in the other direction:

  * the auth header is the **raw key**, not ``Bearer <key>`` -- a Bearer prefix 401s every
    request on this API;
  * a download URL is checked against the https host allowlist before it is fetched, because
    the URL comes from the API response and the container is not a proxy for arbitrary
    addresses.

The opener is injected, so nothing here touches the network.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dubtitlerr.core.refs import jimaku


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        status, body = self.responses.pop(0)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return jimaku.Response(status, payload, {})


def _client(responses, key="secret-key"):
    opener = Opener(responses)
    return jimaku.Jimaku(key, opener=opener), opener


# --- auth -----------------------------------------------------------------------------


def test_auth_header_is_the_raw_key():
    client, opener = _client([(200, [{"id": 7, "name": "Frieren"}])])
    client.entries(anilist_id=154587)
    url, headers = opener.calls[0]
    assert headers["Authorization"] == "secret-key"
    assert not headers["Authorization"].lower().startswith("bearer")


def test_missing_key_is_an_auth_error_before_any_request():
    client = jimaku.Jimaku("")
    assert not client.configured
    with pytest.raises(jimaku.JimakuAuthError):
        client.entries(query="Frieren")


def test_401_is_an_auth_error_not_an_empty_result():
    client, _ = _client([(401, {"error": "unauthorized", "code": 7})])
    with pytest.raises(jimaku.JimakuAuthError):
        client.entries(query="Frieren")


def test_429_is_a_rate_limit():
    client, _ = _client([(429, b"")])
    with pytest.raises(jimaku.JimakuRateLimitError):
        client.entries(query="Frieren")


# --- URLs -----------------------------------------------------------------------------


def test_anilist_search_builds_the_documented_url():
    client, opener = _client([(200, [{"id": 7}])])
    client.entries(anilist_id=154587)
    assert opener.calls[0][0] == "https://jimaku.cc/api/entries/search?anilist_id=154587"


def test_name_search_is_url_encoded():
    client, opener = _client([(200, [])])
    client.entries(query="Frieren: Beyond Journey's End")
    assert "query=Frieren%3A+Beyond+Journey%27s+End" in opener.calls[0][0]


def test_a_single_object_answer_is_wrapped():
    client, _ = _client([(200, {"id": 7, "name": "Frieren"})])
    assert client.entries(query="Frieren") == [{"id": 7, "name": "Frieren"}]


def test_files_filters_entries_without_a_url():
    client, _ = _client([(200, [{"name": "a.ass", "url": "https://jimaku.cc/a"}, {"name": "b.ass"}])])
    assert client.files(7) == [{"name": "a.ass", "url": "https://jimaku.cc/a"}]


# --- the SSRF surface -----------------------------------------------------------------


def test_download_refuses_another_host():
    client = jimaku.Jimaku("k", opener=Opener([]))
    with pytest.raises(jimaku.JimakuError) as caught:
        client.download("https://example.com/file.zip")
    assert "refusing host" in str(caught.value)


def test_download_refuses_plain_http():
    client = jimaku.Jimaku("k", opener=Opener([]))
    with pytest.raises(jimaku.JimakuError) as caught:
        client.download("http://jimaku.cc/file.zip")
    assert "non-https" in str(caught.value)


def test_download_reads_jimaku_bytes():
    client, opener = _client([(200, b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")])
    data = client.download("https://jimaku.cc/entry/7/files/1")
    assert data.startswith(b"1\n00:00:01")
    assert opener.calls[0][1]["Authorization"] == "secret-key"
