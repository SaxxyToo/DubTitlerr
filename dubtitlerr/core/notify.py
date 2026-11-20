"""Outbound notifications: a provider list read from CONFIG_DIR, rendered per kind, delivered by urllib.

Stdlib only, deliberately: the shell loops call this module as a CLI at pass boundaries and they run
from a minimal app directory with no pysubs2 (see the harnesses in tests/test_gen_loop_*.py). common.py
imports pysubs2, so this leaf may not import it -- the same rule atomic_file.py and glossary_lock.py
follow. Nothing here is imported at module scope beyond the standard library.

Config (all optional; absent or empty is a silent no-op):

  settings.json   {"notifications": [{"name", "kind", "enabled", "triggers": [...], <kind fields>}]}
  secrets.json    {"notifications": {"<name>": {"url"|"token": "<secret>"}}}

Kinds v1: ``discord`` (secret url), ``ntfy`` (visible server + topic, optional secret token) and
``webhook`` (secret url). A provider sends only when ``enabled`` is true and its ``triggers`` names
this event. A delivery failure is logged to stderr and returned as ``{"name", "ok", "detail"}`` --
it never raises and never fails a run. The log line names the provider, event and outcome only: a
secret URL or token must not reach a log, so no message ever includes the URL or the exception text.

Transport: one ``_urlopen`` seam (tests replace it), POST via urllib with a single 10s timeout --
urllib exposes one timeout covering connect plus reads, and the approved design's 5s connect / 10s
read cannot be expressed separately (common.py documents the same limitation). Timeouts, 5xx and
URLError retry once; 4xx does not. Redirects are followed only into the same safe URL space, so a
redirect cannot smuggle a request to a metadata address.

URL safety mirrors settings_connections.host_problem (link-local 169.254.0.0/16 and fe80::/10,
metadata 100.100.100.200 and fd00:ec2::254, however spelled: dotted, decimal, hex, IPv4-mapped,
6to4, NAT64). Non-http(s) schemes and URLs carrying a user name or password are refused too. Loopback
and LAN hosts are allowed. The guard is duplicated rather than imported because importing the
settings stack would drag modules this leaf must not load; watch_queue.py duplicates clean_title for
the same reason.

The generic webhook envelope carries ``event, at, host, loop, summary, counts, detail``. It has no
``version``: the repo exposes no runtime application version (pyproject's 0.3.0 is packaging metadata
only), and inventing one here would be a lie downstream consumers cannot check.

CLI:
  python3 -m dubtitlerr.core.notify send --event E --summary S [--loop L] [--counts JSON] [--detail JSON]
  python3 -m dubtitlerr.core.notify subscribed --event E
  python3 -m dubtitlerr.core.notify test --name N

Exit 0 even when delivery fails (logged); exit 2 only for usage errors (unknown event, bad JSON,
unknown provider name). ``subscribed`` is the pass-level gate the loops use: exit 0 when at least
one enabled provider triggers that event, 1 otherwise. It reads settings.json and nothing else, no
network, so a per-episode emitter can decide ONCE per pass whether the per-episode CLI calls are
worth a subprocess at all (``episode.delivered`` is off by default).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EVENTS = (
    "run.requested",
    "sweep.completed",
    "merge.completed",
    "pass.failed",
    "review.pending",
    "episode.delivered",
    "episode.refused",
)
KINDS = ("discord", "ntfy", "webhook")
TEST_EVENT = "notification.test"
TEST_SUMMARY = "DubTitlerr test notification"
TIMEOUT_SECONDS = 10.0
USER_AGENT = "DubTitlerr-notify"

LINK_LOCAL = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))
NAT64_PREFIXES = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- URL safety -----------------------------------------------------------------------------------------------------------


def _unwrapped_addresses(addr):
    """Yield addr plus any IPv4 address it wraps via ipv4_mapped, 6to4, or NAT64 (64:ff9b::/96, 64:ff9b:1::/48)."""
    yield addr
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        yield mapped
    s24 = getattr(addr, "sixtofour", None)
    if s24 is not None:
        yield s24
    if addr.version == 6:
        for prefix in NAT64_PREFIXES:
            if addr in prefix:
                yield ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
                if prefix.prefixlen == 48:
                    b = addr.packed
                    yield ipaddress.IPv4Address(b[6:8] + b[9:11])


def _is_refused_address(addr) -> bool:
    for item in _unwrapped_addresses(addr):
        if item in METADATA_ADDRESSES:
            return True
        if any(item in net for net in LINK_LOCAL if item.version == net.version):
            return True
    return False


def host_problem(url: str) -> str:
    """Why the host of ``url`` is refused, or "". Link-local and metadata addresses are never a target,
    however they are spelled; loopback and LAN hosts are normal."""
    host = urllib.parse.urlsplit(url).hostname or ""
    candidates = []
    try:
        candidates.append(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        try:  # legacy IPv4 spellings: 2852039166, 0xA9FEA9FE, 169.254.43518
            candidates.append(ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host))))
        except (OSError, ValueError):
            pass
    for address in candidates:
        if _is_refused_address(address):
            return "must not point at a link-local or metadata address (169.254.0.0/16, fe80::/10)"
    return ""


def url_problem(url) -> str:
    """Why ``url`` may not be called, or "": http(s) only, no user name or password, no link-local/metadata host."""
    if not isinstance(url, str) or not url:
        return "must be an http(s) URL"
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        return "must be an http(s) URL without spaces or control characters"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "is not a URL"
    if parts.scheme.lower() not in ("http", "https"):
        return "must be an http(s) URL"
    if not parts.hostname:
        return "needs a host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "must not contain a user name or password"
    return host_problem(url)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only into the same safe URL space. urllib would otherwise follow an http(s)
    notification URL to a metadata address, or to file://, before any caller could inspect it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = url_problem(newurl)
        if problem:
            raise ValueError(f"refused redirect: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req, timeout=None):
    """The module's single transport seam (tests replace it)."""
    return urllib.request.build_opener(_GuardedRedirect).open(req, timeout=timeout)


# --- config ---------------------------------------------------------------------------------------------------------------


def _read_json(path: str):
    """The parsed document, or None. A missing file is silent (unconfigured); a malformed one is logged once."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log(f"notify: {os.path.basename(path)} unusable ({type(exc).__name__}); not sending")
        return None


def _providers(config_dir=None):
    """The provider list from settings.json alone. Never raises; an unusable document is a no-op."""
    base = config_dir if config_dir is not None else (os.environ.get("CONFIG_DIR") or "/config")
    settings = _read_json(os.path.join(base, "settings.json"))
    if not isinstance(settings, dict):
        return []
    raw = settings.get("notifications")
    if raw is None:
        return []
    if not isinstance(raw, list):
        _log("notify: settings.json notifications is not a list; not sending")
        return []
    return [item for item in raw if isinstance(item, dict)]


def _load(config_dir=None):
    """(providers, secrets) from settings.json/secrets.json. Never raises; unusable parts are ignored."""
    providers = _providers(config_dir)
    if not providers:
        return [], {}
    base = config_dir if config_dir is not None else (os.environ.get("CONFIG_DIR") or "/config")
    secrets: dict = {}
    secrets_doc = _read_json(os.path.join(base, "secrets.json"))
    if isinstance(secrets_doc, dict):
        raw_secrets = secrets_doc.get("notifications")
        if isinstance(raw_secrets, dict):
            secrets = {
                name: fields for name, fields in raw_secrets.items() if isinstance(name, str) and isinstance(fields, dict)
            }
    return providers, secrets


def _secret(fields: dict, field: str) -> str:
    """The secret string for ``field``, or "". One line, non-empty; anything else is treated as unset."""
    value = fields.get(field)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or any(ch in value for ch in "\r\n\x00"):
        return ""
    return value


# --- rendering and delivery -----------------------------------------------------------------------------------------------


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _priority(item: dict, event: str) -> str:
    configured = item.get("priority")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return "5" if event == "pass.failed" else "default"


def _tags(item: dict) -> str:
    tags = item.get("tags")
    if isinstance(tags, list):
        return ",".join(str(tag).strip() for tag in tags if str(tag).strip())
    if isinstance(tags, str):
        return tags.strip()
    return ""


def _fail(name: str, event: str, detail: str) -> dict:
    _log(f"notify: {name} {event} failed ({detail})")
    return {"name": name, "ok": False, "detail": detail}


def _send(name: str, event: str, url: str, body: bytes, headers: dict) -> dict:
    problem = url_problem(url)
    if problem:
        _log(f"notify: {name} {event} refused ({problem})")
        return {"name": name, "ok": False, "detail": f"refused: {problem}"}
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, **headers},
            method="POST",
        )
    except Exception as exc:  # noqa: BLE001 -- a malformed request is a delivery failure
        _log(f"notify: {name} {event} failed ({type(exc).__name__})")
        return {"name": name, "ok": False, "detail": type(exc).__name__}
    for attempt in (1, 2):
        retry = False
        try:
            with _urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 200) or 200)
                response.read()
            if 200 <= status < 300:
                _log(f"notify: {name} {event} ok (attempt {attempt})")
                return {"name": name, "ok": True, "detail": ""}
            detail = f"HTTP {status}"
        except urllib.error.HTTPError as exc:  # first: HTTPError is a URLError subclass
            detail = f"HTTP {exc.code}"
            retry = 500 <= exc.code < 600
        except (urllib.error.URLError, TimeoutError) as exc:
            detail = "timeout" if isinstance(exc, TimeoutError) else "network error"
            retry = True
        except Exception as exc:  # noqa: BLE001 -- a delivery failure must never escape
            detail = type(exc).__name__
        if retry and attempt == 1:
            _log(f"notify: {name} {event} attempt {attempt} failed ({detail}); retrying")
            continue
        _log(f"notify: {name} {event} failed ({detail})")
        return {"name": name, "ok": False, "detail": detail}
    return {"name": name, "ok": False, "detail": "delivery failed"}  # unreachable; keeps the contract


def _deliver(item: dict, secrets: dict, event: str, summary: str, counts, loop, host, detail) -> dict:
    """Render and send one provider. Always returns {name, ok, detail}; never raises."""
    raw_name = item.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "<unnamed>"
    kind = item.get("kind")
    if kind not in KINDS:
        _log(f"notify: {name} {event} skipped (unknown kind {kind!r})")
        return {"name": name, "ok": False, "detail": "unknown kind"}
    fields = secrets.get(name) if isinstance(name, str) else None
    fields = fields if isinstance(fields, dict) else {}

    if kind == "discord":
        url = _secret(fields, "url")
        if not url:
            return _fail(name, event, "missing url")
        try:
            body = json.dumps({"content": summary, "allowed_mentions": {"parse": []}}).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return _fail(name, event, type(exc).__name__)
        return _send(name, event, url, body, {"Content-Type": "application/json"})

    if kind == "ntfy":
        server = item.get("server")
        topic = item.get("topic")
        if not isinstance(server, str) or not server.strip():
            return _fail(name, event, "missing server")
        if not isinstance(topic, str) or not topic.strip():
            return _fail(name, event, "missing topic")
        url = server.strip().rstrip("/") + "/" + urllib.parse.quote(topic.strip(), safe="")
        headers = {
            "Title": event,
            "Priority": _priority(item, event),
            "Content-Type": "text/plain; charset=utf-8",
        }
        tags = _tags(item)
        if tags:
            headers["Tags"] = tags
        token = _secret(fields, "token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return _send(name, event, url, summary.encode("utf-8"), headers)

    url = _secret(fields, "url")
    if not url:
        return _fail(name, event, "missing url")
    envelope = {
        "event": event,
        "at": time.time(),
        "host": host if host is not None else _hostname(),
        "loop": loop,
        "summary": summary,
        "counts": {} if counts is None else counts,
        "detail": {} if detail is None else detail,
    }
    try:
        body = json.dumps(envelope).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return _fail(name, event, type(exc).__name__)
    return _send(name, event, url, body, {"Content-Type": "application/json"})


def _wanted(item: dict, event: str) -> bool:
    if not item.get("enabled", True):
        return False
    triggers = item.get("triggers")
    return isinstance(triggers, list) and event in triggers


def subscribed(event: str, *, config_dir=None) -> bool:
    """True when at least one enabled provider's triggers include ``event``.

    The pass-level gate for per-episode events: settings.json only, no secrets read, no network.
    Unknown events and missing configuration are False (nothing is subscribed to nothing).
    """
    if event not in EVENTS:
        return False
    return any(_wanted(item, event) for item in _providers(config_dir))


def emit(event, summary, *, counts=None, loop=None, host=None, detail=None, config_dir=None) -> list[dict]:
    """Send ``summary`` to every enabled provider whose triggers include ``event``.

    Returns one ``{name, ok, detail}`` per attempted provider. Missing/empty configuration and an
    unknown event are a logged no-op; delivery failures are logged and swallowed. Never raises.
    """
    if event not in EVENTS:
        _log(f"notify: unknown event {event!r}; not sending")
        return []
    providers, secrets = _load(config_dir)
    if not providers:
        return []
    resolved_host = host if host is not None else _hostname()
    results = []
    for item in providers:
        if not _wanted(item, event):
            continue
        if item.get("kind") not in KINDS:
            raw_name = item.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "<unnamed>"
            _log(f"notify: {name} {event} skipped (unknown kind {item.get('kind')!r})")
            continue
        try:
            results.append(_deliver(item, secrets, event, summary, counts, loop, resolved_host, detail))
        except Exception as exc:  # noqa: BLE001 -- emission must never fail the caller
            _log(f"notify: {item.get('name')} {event} failed ({type(exc).__name__})")
            results.append({"name": item.get("name") or "<unnamed>", "ok": False, "detail": type(exc).__name__})
    return results


def test_provider(name: str, *, config_dir=None) -> dict:
    """Send one test event to the named provider regardless of enabled/triggers. Raises LookupError for an unknown name."""
    providers, secrets = _load(config_dir)
    item = next((p for p in providers if p.get("name") == name), None)
    if item is None:
        raise LookupError(name)
    try:
        return _deliver(item, secrets, TEST_EVENT, TEST_SUMMARY, None, None, _hostname(), None)
    except Exception as exc:  # noqa: BLE001 -- the CLI exits 0 on a delivery problem, like emit
        _log(f"notify: {name} {TEST_EVENT} failed ({type(exc).__name__})")
        return {"name": name, "ok": False, "detail": type(exc).__name__}


# --- CLI ------------------------------------------------------------------------------------------------------------------


def _json_object(parser: argparse.ArgumentParser, flag: str, raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        parser.error(f"{flag} must be JSON")
    if not isinstance(value, dict):
        parser.error(f"{flag} must be a JSON object")
    return value


def _print_results(results) -> None:
    for result in results:
        if result["ok"]:
            print(f"{result['name']}: ok")
        else:
            print(f"{result['name']}: failed ({result['detail']})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m dubtitlerr.core.notify",
        description="Deliver DubTitlerr notification events to the configured providers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    send = commands.add_parser("send", help="emit one event to the configured providers")
    send.add_argument("--event", required=True, help="one of: " + ", ".join(EVENTS))
    send.add_argument("--summary", required=True, help="human-readable one-line summary")
    send.add_argument("--loop", default=None, help="loop or stage name for the webhook envelope")
    send.add_argument("--counts", default=None, help="JSON object of counts (webhook envelope)")
    send.add_argument("--detail", default=None, help="JSON object of extra detail (webhook envelope)")

    test = commands.add_parser("test", help="send one test event to one provider")
    test.add_argument("--name", required=True, help="provider name from settings.json")

    subscribed_cmd = commands.add_parser("subscribed", help="exit 0 when some enabled provider triggers an event")
    subscribed_cmd.add_argument("--event", required=True, help="one of: " + ", ".join(EVENTS))

    args = parser.parse_args(argv)
    if args.command == "send":
        if args.event not in EVENTS:
            parser.error("--event must be one of: " + ", ".join(EVENTS))
        counts = _json_object(parser, "--counts", args.counts)
        detail = _json_object(parser, "--detail", args.detail)
        _print_results(emit(args.event, args.summary, counts=counts, loop=args.loop, detail=detail))
        return 0
    if args.command == "subscribed":
        if args.event not in EVENTS:
            parser.error("--event must be one of: " + ", ".join(EVENTS))
        return 0 if subscribed(args.event) else 1
    try:
        result = test_provider(args.name)
    except LookupError:
        parser.error(f"--name: no notification provider named {args.name!r}")
    _print_results([result])
    return 0


if __name__ == "__main__":
    sys.exit(main())
