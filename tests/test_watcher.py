import importlib.util
import json
import os

WATCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "importer", "watcher.py",
)


def _load_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "import-log.jsonl"))
    monkeypatch.setenv("STABILITY_SECONDS", "0")
    spec = importlib.util.spec_from_file_location("watcher", WATCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _inbox(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "failed").mkdir(parents=True)
    (inbox / "mf.xlsx").write_bytes(b"raw export bytes")
    return inbox


def test_success_archives_raw_file_then_consumes_it(tmp_path, monkeypatch):
    w = _load_watcher(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "_import_one", lambda conn, path: {
        "source": "macrofactor", "new_count": 1, "updated_count": 0})
    inbox = _inbox(tmp_path)

    assert w.run_once(None, inbox, inbox / "failed") == 1

    assert not (inbox / "mf.xlsx").exists()
    assert (tmp_path / "archive" / "mf.xlsx").read_bytes() == b"raw export bytes"
    entry = json.loads((tmp_path / "import-log.jsonl").read_text().splitlines()[-1])
    assert entry["status"] == "ok"
    assert entry["archived_to"].endswith("mf.xlsx")


def test_failure_goes_to_failed_not_archive(tmp_path, monkeypatch):
    w = _load_watcher(tmp_path, monkeypatch)

    def boom(conn, path):
        raise ValueError("no weights")
    monkeypatch.setattr(w, "_import_one", boom)
    inbox = _inbox(tmp_path)

    w.run_once(None, inbox, inbox / "failed")

    assert (inbox / "failed" / "mf.xlsx").exists()
    assert not (tmp_path / "archive").exists()
