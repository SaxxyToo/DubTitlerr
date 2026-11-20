"""Over HTTP, against a real review-server process: the exact call an operator's monitor makes.

The v0.4.0 failure was visible only here -- `GET /healthz` answering 503 "no heartbeat yet"
forever while Docker's TCP healthcheck read healthy. This test starts the real server, writes the
heartbeat through the real CLI (what gen_loop.sh calls), and reads both health routes plus
`loop.state` back, so the writer and the readers are proven to agree over the wire rather than in
process. It also runs inside the throwaway dash container from `scripts/check.sh --dash`.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KEY = "smoke-key"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"X-Api-Key": KEY})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode())


@pytest.fixture
def server(tmp_path):
    """A real review server on a free loopback port, with its own config dir."""
    config = tmp_path / "config"
    (config / "requests").mkdir(parents=True)
    port = _free_port()
    env = dict(
        os.environ,
        CONFIG_DIR=str(config),
        REQUESTS_DIR=str(config / "requests"),
        REVIEW_PORT=str(port),
        REVIEW_BIND="127.0.0.1",
        REVIEW_TOKEN=KEY,
        HEARTBEAT_PATH=str(config / "heartbeat.json"),
        STOP_FLAG=str(tmp_path / "stop"),
        PYTHONPATH=str(REPO),
    )
    env.pop("REVIEW_AUTH", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "dubtitlerr.review.review_server"],
        env=env,
        cwd=str(REPO),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while True:
            try:
                _get(port, "/healthz")
                break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError("review server exited: " + proc.stderr.read().decode()) from None
                if time.time() > deadline:
                    raise AssertionError("review server did not answer within 30s") from None
                time.sleep(0.2)
        yield port, config
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _write_heartbeat(config, *args):
    env = dict(os.environ, HEARTBEAT_PATH=str(config / "heartbeat.json"), PYTHONPATH=str(REPO))
    proc = subprocess.run(
        [sys.executable, "-m", "dubtitlerr.core.heartbeat", *args],
        env=env,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_healthz_over_http_goes_200_once_the_loop_reports(server, tmp_path):
    port, config = server

    # No document: what v0.4.0 answered forever.
    assert _get(port, "/healthz") == (503, {"ok": False, "reasons": ["no heartbeat yet"]})
    assert _get(port, "/api/v1/health") == (503, {"ok": False, "status": 503, "reasons": ["no heartbeat yet"]})
    assert _get(port, "/api/v1/system/status")[1]["loop"]["state"] == "unknown"

    # A sweep start: the loop is alive but has not completed a sweep yet (documented as 503).
    _write_heartbeat(config, "sweep-start", "--root", str(tmp_path))
    status, body = _get(port, "/healthz")
    assert status == 503 and "stale: no sweep has completed yet" in body["reasons"]
    assert _get(port, "/api/v1/system/status")[1]["loop"]["state"] == "running"

    # A finished sweep: healthy, and the loop reports a real state instead of "unknown".
    _write_heartbeat(config, "show", "--name", "Frieren")
    _write_heartbeat(config, "sweep-end", "--root", str(tmp_path), "--shows", "1", "--failed", "0")

    assert _get(port, "/healthz") == (200, {"ok": True})
    assert _get(port, "/api/v1/health") == (200, {"ok": True, "status": 200})
    loop = _get(port, "/api/v1/system/status")[1]["loop"]
    assert loop["state"] == "idle" and loop["state"] != "unknown"
    assert loop["current_show"] is None
    assert loop["last_sweep_end"] is not None
    assert loop["roots_readable"] is True and loop["order_file_present"] is True


def test_health_goes_back_to_503_when_the_sweep_grows_stale(server, tmp_path):
    port, config = server
    _write_heartbeat(config, "sweep-start", "--root", str(tmp_path))
    _write_heartbeat(config, "sweep-end", "--root", str(tmp_path))

    assert _get(port, "/healthz")[0] == 200

    # Rewrite the document as if the last sweep ended 19 hours ago (3 x the default 6 h idle),
    # moving the start with it: a recent start would read as a sweep in progress, not a stall.
    path = config / "heartbeat.json"
    doc = json.loads(path.read_text())
    doc["last_sweep_end"] = time.time() - 19 * 3600
    doc["last_sweep_start"] = doc["last_sweep_end"] - 60
    path.write_text(json.dumps(doc))

    status, body = _get(port, "/healthz")
    assert status == 503 and any(r.startswith("stale: last sweep ended") for r in body["reasons"])
    assert _get(port, "/api/v1/system/status")[1]["loop"]["state"] == "stalled"
