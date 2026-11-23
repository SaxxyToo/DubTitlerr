"""Isolated browser-test server. Never discovers or processes a real media library."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Deployed glossaries the page edits. One carries a flagged review queue so the Review queue tab has
# something real to decide: a respelling (accept -> hard fix), an additive expansion, a bare-string
# verifier entry and a tier-B no-match with no canonical.
GLOSSARIES = {
    "Neon Harbor": {
        "show": "Neon Harbor",
        "names": [],
        "phrases": [],
        "hard_fixes": {},
        "flagged": {
            "Usop": {
                "reason": "respelling-needs-review",
                "canonical": "Usopp",
                "variant_count": 9,
                "canonical_count": 3,
                "score": 0.91,
                "bound": 0.72,
                "context": ["Usop, wait!"],
                "source": "transcript",
                "scope": 12,
            },
            "Straw": {
                "reason": "common-word",
                "canonical": "Straw Hat Pirates",
                "variant_count": 5,
                "canonical_count": 5,
                "score": 0.8,
                "bound": 0.6,
                "context": [],
                "source": "transcript",
                "scope": 12,
            },
            "Dwarfs": "low-confidence",
            "Frier": {
                "reason": "no-wiki-match",
                "canonical": "",
                "variant_count": 7,
                "canonical_count": 0,
                "bound": 0.0,
                "context": ["Frier is late."],
                "source": "transcript",
                "scope": 12,
            },
        },
    },
    "Paper Moon": {"show": "Paper Moon"},
}


def main():
    with tempfile.TemporaryDirectory(prefix="dubtitlerr-ui-test-") as scratch:
        base = Path(scratch)
        media = base / "media"
        glossary_dir = base / "glossaries"
        glossary_dir.mkdir()
        ddir = base / "decisions"
        ddir.mkdir()
        os.environ["REVIEW_TOKEN"] = "browser-fixture-token"
        os.environ["REVIEW_USERNAME"] = "reviewer"
        os.environ["REVIEW_PASSWORD"] = "browser-fixture-password"
        os.environ.pop("REVIEW_AUTH", None)
        # The new pages' backends: admin-guarded run requests, a settings read and two backend probes.
        os.environ["ANIME_ROOT"] = str(media)
        os.environ["REQUESTS_DIR"] = str(base / "requests")
        os.environ["SCHEDULE_FILE"] = str(base / "schedule.json")
        os.environ["REPAIR_LLAMACPP_URL"] = "http://127.0.0.1:18842/healthz"  # this very server: the probe succeeds
        os.environ["VERIFY_LLAMACPP_URL"] = "http://127.0.0.1:9/"  # nothing listens here: the probe fails
        # The Transcription page's endpoint, so Fetch endpoint models answers from this fixture too.
        os.environ["ASR_URL"] = "http://127.0.0.1:18842/v1/audio/transcriptions"
        # Settings writes (settings.json, secrets.json) land in this directory.
        (base / "config").mkdir()
        os.environ["CONFIG_DIR"] = str(base / "config")
        os.environ["STOP_FLAG"] = str(base / "stop")  # a restart request lands here, never in /tmp
        os.environ["DT_CONTAINER_BOOT_ID"] = "fixture-boot-1"
        os.environ["REPAIR_PROFILES"] = "{}"  # retired: the General page lists it
        os.environ["LONGCATAI_API_KEY"] = "fixture-secret-key-value"  # must show as "set", never as a value
        os.environ.pop("COMMANDCODE_API_KEY", None)
        os.environ.pop("WEBHOOK_TOKEN", None)
        os.environ["HEARTBEAT_PATH"] = str(base / "heartbeat.json")
        os.environ["GLOSSARY_DIR"] = str(glossary_dir)
        os.environ["DECISIONS_DIR"] = str(ddir)
        from dubtitlerr.core import artifact_store, common, decisions, unanchored, unresolved
        from dubtitlerr.review import review_auth, review_settings
        from dubtitlerr.review import review_server as rs

        # Neon Harbor's copies have no English subtitle for the Japanese audio: the repair
        # stage records that, and the Glossary page must offer the per-show switch.
        unanchored.record("Neon Harbor", "Neon Harbor - S01E01.mkv", 12)

        common.heartbeat(last_sweep_end=time.time(), roots_readable=True, order_file_present=True)
        review_settings.EFFECTIVE_ENV_PATH = str(base / "no-effective.env")  # absent: settings come from the environment
        review_auth.import_env_admin()  # REVIEW_USERNAME/REVIEW_PASSWORD become the browser tests' admin account
        rs.ROOTS = [str(media)]
        decisions.GLOSSARY_DIR = str(glossary_dir)
        decisions.DECISIONS_DIR = str(ddir)
        # R5: queue rows carry the reference role the pipeline recorded for them. Some rows
        # deliberately keep the pre-R2 shape (a reference and no role) so the page's
        # "unclassified" label has something real to render, and Paper Moon S02E04 has no
        # reference at all ("unanchored"). Format: (original, proposed, stage, reason,
        # reference, ref fields).
        DUB = {"ref_role": "dub", "ref_provenance": "native", "ref_track_title": "English Dub"}
        TRANS = {"ref_role": "translation", "ref_provenance": "native", "ref_track_title": "Full Subtitles"}
        fixtures = [
            (
                "Neon Harbor",
                "S01E01",
                [
                    (
                        "We're looking for a factory.",
                        "We're looking for a needle.",
                        "repair_applied",
                        "accepted",
                        "We need to find the factory.",
                        DUB,
                    ),
                    (
                        "meet me at the harbor",
                        "Meet me at the harbor.",
                        "repair_applied",
                        "accepted",
                        "Meet at the port.",
                        TRANS,
                    ),
                    (
                        "The blue compass is ours.",
                        "The compass is ours.",
                        "repair_applied",
                        "accepted",
                        "That blue compass belongs to us.",
                        {},
                    ),
                    ("Wait for me!", "Weigh four me!", "repair", "rejected_guard", "Wait!", {}),
                ],
            ),
            (
                "Neon Harbor",
                "S01E02",
                [
                    (
                        "meet me at the harbor",
                        "Meet me at the harbor.",
                        "repair_applied",
                        "accepted",
                        "Meet at the port.",
                        TRANS,
                    ),
                    ("we can do this", "We can do this.", "repair_applied", "accepted", "We can manage.", {}),
                ],
            ),
            (
                "Paper Moon",
                "S02E03",
                [
                    ("The stars are bright.", "The scars are bright.", "repair_applied", "accepted", "Bright stars.", DUB),
                    (
                        "<img src=x onerror=alert(1)>",
                        "<script>bad</script>",
                        "repair_applied",
                        "accepted",
                        "Untrusted subtitle text.",
                        {},
                    ),
                ],
            ),
            (
                "Paper Moon",
                "S02E04",
                [
                    ("Stop there!", "Shop where!", "repair", "rejected_guard", "", {}),
                ],
            ),
        ]
        for show, ep, rows in fixtures:
            (glossary_dir / f"{show}.json").write_text(json.dumps(GLOSSARIES[show]))
            season = "Season 01" if "S01" in ep else "Season 02"
            folder = media / show / season
            folder.mkdir(parents=True, exist_ok=True)
            stem = str(folder / f"{show} - {ep}")
            cards = [{"start": 2 + i * 3, "end": 4 + i * 3, "text": row[0]} for i, row in enumerate(rows)]
            artifact_store.put_json(stem, "conf", cards)
            for orig, prop, stage, reason, ref, ref_fields in rows:
                unresolved.record(stem, stage, reason, original_text=orig, proposed_text=prop, reference=ref, **ref_fields)
            # A sine wave, not copyrighted dialogue. One tagged English audio stream.
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=330:duration=18",
                    "-metadata:s:a:0",
                    "language=eng",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    stem + ".mkv",
                ],
                check=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )

        class FixtureHandler(rs.Handler):
            def do_GET(self):
                # Model listings for the Fetch models button. Only the fixture serves these unauthenticated: a real
                # deployment asks the LLM server, not the review server.
                if self.path == "/v1/models":
                    return self._send(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": "qwen3-4b-instruct"}, {"id": "qwen3-8b"}, {"id": "whisper-large-v3-turbo"}],
                        },
                    )
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": "llama3.2:3b", "model": "llama3.2:3b"}]})
                return super().do_GET()

            def do_POST(self):
                if self.path == "/__test/reset" and self.headers.get("X-Api-Key") == "browser-fixture-token":
                    for file in ddir.glob("*.json"):
                        file.unlink()
                    for stem in artifact_store.stems(kind=unresolved.KIND):
                        rows = unresolved.items(stem)
                        for record in rows:
                            record["resolved"] = False
                        unresolved._rewrite(stem, rows)
                    rs._QUEUE_CACHE.clear()
                    rs._THROTTLE = rs.LoginThrottle()  # every test signs in from 127.0.0.1; no lockout may leak across tests
                    for file in (base / "requests").glob("*") if (base / "requests").is_dir() else []:
                        file.unlink()
                    for stale in ("settings.json", "settings.json.prev", "secrets.json", "secrets.json.prev"):
                        (base / "config" / stale).unlink(missing_ok=True)
                    (base / "stop").unlink(missing_ok=True)
                    (base / "schedule.json").unlink(missing_ok=True)
                    for show in ("Neon Harbor", "Paper Moon"):
                        (glossary_dir / f"{show}.json").write_text(json.dumps(GLOSSARIES[show]))
                    return self._send(200, {"ok": True})
                return super().do_POST()

        class FixtureServer(rs.BoundedHTTPServer):
            """The review server, quiet about clients that hang up mid-response.

            A browser that aborts a request (a `page.route(..., route.abort())`, or leaving the page
            while a response is in flight) closes its socket, and socketserver then prints a
            twenty-line BrokenPipeError traceback. It reads like the fixture crashed -- it did not:
            every other test kept using this server -- and it made a flaky contrast assertion look
            like a webserver failure (CI 2026-11-21, job 136719511316). The connection is already
            gone, so dropping it silently is the whole correct handling.
            """

            def handle_error(self, request, client_address):
                if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    return
                super().handle_error(request, client_address)

        srv = FixtureServer(("127.0.0.1", 18842), FixtureHandler)
        print("Synthetic review fixture ready on 127.0.0.1:18842", flush=True)
        try:
            srv.serve_forever()
        finally:
            srv.server_close()


if __name__ == "__main__":
    main()
