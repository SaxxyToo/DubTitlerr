#!/usr/bin/env python3
"""Jimaku (jimaku.cc) — the Japanese subtitle archive, read over its JSON API.

Jimaku is the Kitsunekko successor: users upload Japanese subtitle files per anime entry,
keyed to AniList IDs. That is the one external source this pipeline wants, because the gap
it fills is names, not dialogue — ``glossary_acquire`` can only learn a show's names from a
fansub track, and Jimaku is where those tracks live for shows whose releases ship none.

Three details cost real debugging on the ported code (Sublarr, GPL-3.0), and all three are
encoded here rather than in a comment nobody reads:

  * **The auth header is the raw key.** Jimaku's OpenAPI document names the header
    ``Authorization`` and takes the key as its whole value. ``Bearer <key>`` returns 401 on
    every request, which reads as "wrong key" and sends you looking in the wrong place.
  * **An API key is required, including for search.** Verified live 2026-11-22: an
    unauthenticated ``GET /api/entries/search?query=frieren`` answers 401
    ``{"error":"unauthorized","code":7}``. The key is free from jimaku.cc.
  * **Search is by AniList ID when we have one, otherwise by name.** A name search is loose
    ("Frieren" also returns Dragon Ball), which is why ``match.select_entries`` exists.

Downloads are restricted to Jimaku's own hosts over https. A provider-controlled URL is an
SSRF surface, and "the API returned it" is not a reason to fetch an arbitrary address from
inside the container; an allowlist is cheap and the files are always on jimaku.cc.

Stdlib only (``urllib``), and no import of ``common``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API_BASE = "https://jimaku.cc/api"
ALLOWED_HOSTS = frozenset({"jimaku.cc", "www.jimaku.cc"})
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 20.0
USER_AGENT = "DubTitlerr/refs (+https://github.com/SaxxyToo/DubTitlerr)"


class JimakuError(Exception):
    """Anything that stops a Jimaku request from producing an answer."""


class JimakuAuthError(JimakuError):
    """The API key is missing, wrong, or the header was not the raw key."""


class JimakuRateLimitError(JimakuError):
    """Jimaku answered 429: back off before asking again."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JimakuError(f"jimaku: response is not JSON ({exc})") from exc


def _urllib_opener(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https enforced by _check_url
            start = response.geturl()
            _check_url(start)
            body = b""
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > MAX_BYTES:
                    raise JimakuError(f"jimaku: response over {MAX_BYTES} bytes")
            return Response(response.status, body, dict(response.headers))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read() or b"", dict(exc.headers or {}))
    except urllib.error.URLError as exc:
        raise JimakuError(f"jimaku: cannot reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc


def _check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise JimakuError(f"jimaku: refusing non-https url {parts.scheme}://{parts.netloc}")
    if parts.hostname not in ALLOWED_HOSTS:
        raise JimakuError(f"jimaku: refusing host {parts.hostname!r} (allowed: {sorted(ALLOWED_HOSTS)})")


class Jimaku:
    """A Jimaku client with one API key.

    ``opener`` is the injection seam for tests: it takes ``(url, headers, timeout)`` and
    returns a ``Response``. The default one is urllib with an https+host check on both the
    requested URL and any redirect target.
    """

    def __init__(self, api_key, *, base=API_BASE, timeout=TIMEOUT, opener=None, max_bytes=MAX_BYTES):
        self.api_key = (api_key or "").strip()
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = opener or _urllib_opener

    @property
    def configured(self):
        return bool(self.api_key)

    def _headers(self):
        if not self.api_key:
            raise JimakuAuthError("jimaku: no API key (set JIMAKU_API_KEY)")
        # The raw key, not "Bearer <key>" -- see the module docstring.
        return {"Authorization": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        _check_url(url)
        response = self._opener(url, self._headers(), self.timeout)
        if response.status in (401, 403):
            raise JimakuAuthError(f"jimaku: HTTP {response.status} for {path} (key rejected)")
        if response.status == 429:
            raise JimakuRateLimitError(f"jimaku: rate limited on {path}")
        if response.status != 200:
            raise JimakuError(f"jimaku: HTTP {response.status} for {path}")
        return response

    def entries(self, *, anilist_id=None, query=None):
        """Entries matching an AniList ID or a name. ``[]`` when Jimaku knows neither."""
        if anilist_id is not None:
            payload = self._get("/entries/search", {"anilist_id": int(anilist_id)}).json()
        elif query:
            payload = self._get("/entries/search", {"query": query}).json()
        else:
            raise JimakuError("jimaku: entries() needs an AniList ID or a query")
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise JimakuError("jimaku: entries payload is not a list")
        return [entry for entry in payload if isinstance(entry, dict) and entry.get("id") is not None]

    def files(self, entry_id):
        """The files listed on one entry: ``[{"name", "url", "size", ...}, ...]``."""
        payload = self._get(f"/entries/{int(entry_id)}/files").json()
        if not isinstance(payload, list):
            raise JimakuError("jimaku: files payload is not a list")
        return [item for item in payload if isinstance(item, dict) and item.get("name") and item.get("url")]

    def download(self, url):
        """The bytes behind a file's ``url``. Same host rules as every other request."""
        _check_url(url)
        response = self._opener(url, self._headers(), self.timeout)
        if response.status in (401, 403):
            raise JimakuAuthError(f"jimaku: HTTP {response.status} downloading {url}")
        if response.status != 200:
            raise JimakuError(f"jimaku: HTTP {response.status} downloading {url}")
        if len(response.body) > self.max_bytes:
            raise JimakuError(f"jimaku: download over {self.max_bytes} bytes")
        return response.body
