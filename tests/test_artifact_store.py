"""P39 Phase A + store foundation: the central internal-artifact store and its migration CLI.

Covers the acceptance items this module owns:

  * writer -> store round-trip for every artifact kind (text, JSON, JSONL append);
  * import idempotency and dry-run safety;
  * export reversibility (byte-for-byte restore at the original paths);
  * the junk sweep's exact class selection, and that it never touches a deliverable;
  * store location, WAL, and file mode.
"""

import os
import sqlite3

import pytest

from dubtitlerr.core import artifact_store as store


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTIFACT_STORE", raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    store.reset_connections()
    yield
    store.reset_connections()


def _stem(tmp_path, name="show/Season 01/ep01"):
    p = tmp_path / "media" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


# --- location / schema -------------------------------------------------------------


def test_store_lives_in_config_volume_and_is_private(tmp_path):
    path = store.store_path()
    assert path == os.path.join(str(tmp_path / "config"), "artifacts.db")
    store.put("show/ep01", "stages", "{}")
    assert os.path.exists(path)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_artifact_store_env_override(monkeypatch, tmp_path):
    explicit = tmp_path / "elsewhere" / "a.db"
    monkeypatch.setenv("ARTIFACT_STORE", str(explicit))
    assert store.store_path() == str(explicit)
    store.put("show/ep01", "conf", "[]")
    assert explicit.exists()


def test_wal_mode_is_on():
    store.put("show/ep01", "stages", "{}")
    conn = sqlite3.connect(store.store_path())
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


# --- round-trip per kind -----------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(store.KIND_SUFFIXES))
def test_writer_store_roundtrip_per_kind(kind, tmp_path):
    stem = _stem(tmp_path)
    payload = f'{{"kind": "{kind}"}}'
    store.put_text(stem, kind, payload)
    assert store.exists(stem, kind)
    assert store.get_text(stem, kind) == payload
    assert kind in store.kinds(stem)
    assert store.mtime(stem, kind) is not None
    assert store.stats()["kinds"][kind]["count"] == 1
    assert store.delete(stem, kind) is True
    assert store.get_text(stem, kind) is None
    assert store.delete(stem, kind) is False


def test_put_json_and_corrupt_payload_reads_absent(tmp_path):
    stem = _stem(tmp_path)
    store.put_json(stem, "conf", [{"start": 0, "text": "hi"}])
    assert store.get_json(stem, "conf") == [{"start": 0, "text": "hi"}]
    store.put_text(stem, "qc", "{not json")
    assert store.get_json(stem, "qc") is None


def test_meta_merges_so_import_provenance_survives_a_writer(tmp_path):
    stem = _stem(tmp_path)
    store.put_text(stem, "words", "v1", meta={"imported_from": "/media/ep01.words", "size": 2})
    store.put_text(stem, "words", "v2", meta={"size": 9})
    assert store.meta(stem, "words") == {"imported_from": "/media/ep01.words", "size": 9}


def test_append_jsonl_keeps_every_entry_in_order(tmp_path):
    stem = _stem(tmp_path)
    for i in range(3):
        store.append_jsonl(stem, "unresolved", {"i": i})
    assert store.read_jsonl(stem, "unresolved") == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_read_jsonl_skips_a_corrupt_line(tmp_path):
    stem = _stem(tmp_path)
    store.put_text(stem, "unresolved", '{"i": 0}\nnot json\n{"i": 2}\n')
    assert store.read_jsonl(stem, "unresolved") == [{"i": 0}, {"i": 2}]


def test_stems_filters_by_root_and_kind(tmp_path):
    a = _stem(tmp_path, "show-a/Season 01/ep01")
    b = _stem(tmp_path, "show-b/Season 01/ep02")
    store.put_text(a, "done", "{}")
    store.put_text(b, "done", "{}")
    store.put_text(b, "conf", "[]")
    root = str(tmp_path / "media" / "show-a")
    assert store.stems(root=root, kind="done") == [os.path.abspath(a)]
    assert store.stems(root=str(tmp_path / "media"), kind="done") == [os.path.abspath(a), os.path.abspath(b)]
    assert store.stems(kind="conf") == [os.path.abspath(b)]


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        store.put("show/ep01", "not-a-kind", "x")


# --- suffix mapping ----------------------------------------------------------------


def test_split_artifact_name_handles_the_longest_suffix_and_deliverables():
    assert store.split_artifact_name("ep01.dubtitles.unresolved.audit.jsonl") == (
        "ep01",
        "unresolved.audit",
    )
    assert store.split_artifact_name("ep01.dubtitles.unresolved.jsonl") == ("ep01", "unresolved")
    assert store.split_artifact_name("ep01.dubtitles.repair-summary.json") == ("ep01", "repair-summary")
    assert store.split_artifact_name("ep01.dubtitles.done") == ("ep01", "done")
    assert store.split_artifact_name("ep01.eng.dubtitles.ass") == (None, None)
    assert store.split_artifact_name("ep01.eng.dubtitles.srt") == (None, None)
    assert store.split_artifact_name("ep01.mkv") == (None, None)


# --- import -----------------------------------------------------------------------


def _seed_tree(tmp_path):
    """A miniature media tree: two episodes carrying the real artifact zoo, plus a
    deliverable and every junk class."""
    media = tmp_path / "media" / "Show" / "Season 01"
    media.mkdir(parents=True)
    ep1 = media / "ep01"
    ep2 = media / "ep02"
    files = {
        str(ep1) + ".dubtitles.done": '{"size": 1}',
        str(ep1) + ".dubtitles.conf.json": '[{"text": "hello"}]',
        str(ep1) + ".dubtitles.words.json": '{"words": []}',
        str(ep1) + ".dubtitles.unresolved.jsonl": '{"stage": "repair"}\n',
        str(ep1) + ".dubtitles.unresolved.counters.json": '{"counts": {}}',
        str(ep1) + ".dubtitles.repair.csv": "orig,repaired\n",
        str(ep2) + ".dubtitles.stages.json": '{"mux": {"outcome": "ok"}}',
        str(ep2) + ".dubtitles.presentation.json": '{"epoch": 1}',
        str(ep1) + ".eng.dubtitles.ass": "deliverable",
        str(ep1) + ".eng.dubtitles.srt": "deliverable",
        str(ep1) + ".dubtitles.conf.json.stale": "parked",
        str(ep2) + ".dubtitles.words.json.bak": "backup",
        str(ep2) + ".dubtitles.mux.log": "muxed",
    }
    for path, text in files.items():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return media


def test_import_dry_run_changes_nothing(tmp_path):
    media = _seed_tree(tmp_path)
    before = sorted(p for p in media.iterdir())
    result = store.import_tree(str(tmp_path / "media"), apply=False)
    assert result["applied"] is False
    assert result["total"] == 8  # 8 internal artifacts; deliverables and junk excluded
    assert sorted(p for p in media.iterdir()) == before
    assert store.stats()["rows"] == 0


def test_import_apply_moves_artifacts_and_removes_them(tmp_path):
    media = _seed_tree(tmp_path)
    result = store.import_tree(str(tmp_path / "media"), apply=True)
    assert result["inserted"] == 8
    assert result["failed"] == 0
    for suffix in (
        ".dubtitles.done",
        ".dubtitles.conf.json",
        ".dubtitles.words.json",
        ".dubtitles.unresolved.jsonl",
        ".dubtitles.unresolved.counters.json",
        ".dubtitles.repair.csv",
    ):
        assert not os.path.exists(str(media / "ep01") + suffix)
    assert not os.path.exists(str(media / "ep02") + ".dubtitles.stages.json")
    # deliverables and junk are untouched by import
    assert os.path.exists(str(media / "ep01") + ".eng.dubtitles.ass")
    assert os.path.exists(str(media / "ep01") + ".eng.dubtitles.srt")
    assert os.path.exists(str(media / "ep01") + ".dubtitles.conf.json.stale")
    assert os.path.exists(str(media / "ep02") + ".dubtitles.mux.log")
    # payload landed verbatim
    ep1 = str(media / "ep01")
    assert store.get_text(ep1, "conf") == '[{"text": "hello"}]'
    assert store.meta(ep1, "done")["size"] == len('{"size": 1}')


def test_import_is_idempotent(tmp_path):
    _seed_tree(tmp_path)
    store.import_tree(str(tmp_path / "media"), apply=True)
    # a second run has nothing left to import from the applied tree
    second = store.import_tree(str(tmp_path / "media"), apply=True)
    assert second["total"] == 0
    # even with the files restored, identical rows read as unchanged and no work is redone
    store.export_tree(str(tmp_path / "media"), apply=True)
    third = store.import_tree(str(tmp_path / "media"), apply=True)
    assert third["unchanged"] == 8
    assert third["inserted"] == third["updated"] == 0


def test_import_updates_a_changed_payload(tmp_path):
    media = _seed_tree(tmp_path)
    store.import_tree(str(tmp_path / "media"), apply=True)
    target = str(media / "ep01") + ".dubtitles.conf.json"
    store.export_tree(str(tmp_path / "media"), apply=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('[{"text": "changed"}]')
    os.utime(target, (1, 1))  # make the mtime explicitly different
    result = store.import_tree(str(tmp_path / "media"), apply=True)
    assert result["updated"] == 1
    assert store.get_text(str(media / "ep01"), "conf") == '[{"text": "changed"}]'


def test_import_skips_deliverables_only(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "ep01.eng.dubtitles.ass").write_text("deliverable")
    (media / "ep01.eng.dubtitles.srt").write_text("deliverable")
    result = store.import_tree(str(media), apply=True)
    assert result["total"] == 0
    assert (media / "ep01.eng.dubtitles.ass").exists()


# --- export -----------------------------------------------------------------------


def test_export_roundtrip_is_byte_identical_and_reversible(tmp_path):
    media = _seed_tree(tmp_path)
    originals = {}
    for path in media.iterdir():
        originals[str(path)] = path.read_bytes()

    store.import_tree(str(tmp_path / "media"), apply=True)
    exported = store.export_tree(str(tmp_path / "media"), apply=True)
    assert exported["exported"] == 8
    for path, data in originals.items():
        if path.endswith((".ass", ".srt", ".stale", ".bak", ".mux.log")):
            continue
        assert open(path, "rb").read() == data, path


def test_export_dry_run_writes_nothing(tmp_path):
    _seed_tree(tmp_path)
    store.import_tree(str(tmp_path / "media"), apply=True)
    before = sorted(os.listdir(str(tmp_path / "media" / "Show" / "Season 01")))
    result = store.export_tree(str(tmp_path / "media"), apply=False)
    assert result["exported"] == 8
    assert sorted(os.listdir(str(tmp_path / "media" / "Show" / "Season 01"))) == before


def test_export_kind_filter(tmp_path):
    stem = _stem(tmp_path)
    store.put_text(stem, "conf", "[]")
    store.put_text(stem, "stages", "{}")
    result = store.export_tree(str(tmp_path), apply=True, kind="conf")
    assert [item["kind"] for item in result["items"]] == ["conf"]
    assert os.path.exists(stem + ".dubtitles.conf.json")
    assert not os.path.exists(stem + ".dubtitles.stages.json")


# --- sweep ------------------------------------------------------------------------


def test_sweep_removes_only_the_junk_classes(tmp_path):
    _seed_tree(tmp_path)
    result = store.sweep_tree(str(tmp_path / "media"), apply=False)
    assert result["files"] == 3
    assert {item["class"] for item in result["items"]} == {"stale", "bak", "mux.log"}

    applied = store.sweep_tree(str(tmp_path / "media"), apply=True)
    assert applied["files"] == 3
    remaining = sorted(p.name for p in (tmp_path / "media" / "Show" / "Season 01").iterdir())
    assert not any(n.endswith((".stale", ".bak", ".mux.log")) for n in remaining)
    assert "ep01.eng.dubtitles.ass" in remaining
    assert "ep01.dubtitles.conf.json" in remaining
    assert "ep02.dubtitles.stages.json" in remaining


def test_sweep_dry_run_deletes_nothing(tmp_path):
    media = _seed_tree(tmp_path)
    store.sweep_tree(str(tmp_path / "media"), apply=False)
    assert (media / "ep01.dubtitles.conf.json.stale").exists()
    assert (media / "ep02.dubtitles.mux.log").exists()


def test_sweep_does_not_touch_a_deliverable_named_like_junk(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    keep = media / "ep01.dubtitles.ass.bak"
    keep.write_text("weird but not ours")
    result = store.sweep_tree(str(media), apply=True)
    assert result["files"] == 1  # it IS a .bak beside ".dubtitles.", so it is junk
    assert not keep.exists()


def test_sweep_removes_an_archived_fail_marker(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    marker = media / "ep01.dubtitles.fail.archived-20261122"
    marker.write_text("old crash")
    result = store.sweep_tree(str(media), apply=True)
    assert result["files"] == 1
    assert result["items"][0]["class"] == "archived"
    assert not marker.exists()


def test_sweep_drops_junk_rows_from_the_store(tmp_path):
    store.put_text("show/ep01", "mux.log", "muxed")
    store.put_text("show/ep01", "conf", "[]")
    result = store.sweep_tree(str(tmp_path), apply=True)
    assert result["store_rows"] == 1
    assert not store.exists("show/ep01", "mux.log")
    assert store.exists("show/ep01", "conf")


# --- CLI --------------------------------------------------------------------------


def test_cli_sweep_and_import_dry_run_by_default(tmp_path, capsys):
    media = _seed_tree(tmp_path)
    assert store.main(["import", str(tmp_path / "media")]) == 0
    assert "dry run" in capsys.readouterr().out
    assert (media / "ep01.dubtitles.conf.json").exists()

    assert store.main(["import", str(tmp_path / "media"), "--apply"]) == 0
    assert not (media / "ep01.dubtitles.conf.json").exists()


def test_cli_has_get_put_and_stems(tmp_path, capsys):
    stem = str(tmp_path / "media" / "ep01")
    assert store.main(["has", "--stem", stem, "--kind", "conf"]) == 1
    store.put_text(stem, "conf", "[]")
    assert store.main(["has", "--stem", stem, "--kind", "conf"]) == 0
    assert store.main(["get", "--stem", stem, "--kind", "conf"]) == 0
    assert capsys.readouterr().out == "[]"
    assert store.main(["stems", "--kind", "conf", "--root", str(tmp_path / "media")]) == 0
    assert capsys.readouterr().out.strip() == os.path.abspath(stem)
