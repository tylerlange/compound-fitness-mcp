"""Shared test helpers for the MCP server gate and injection tests."""
import importlib.util
import json
import os
import sqlite3

from strength_coach import synthetic
from strength_coach.confirm import ConfirmationGate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_SERVER_PATH = os.path.join(ROOT, "agent", "mcp_server.py")

TABLES = ("imports", "exercises", "exercise_aliases", "sessions", "sets",
          "session_metrics", "session_streams", "program_snapshots",
          "away_periods", "coaching_notes", "daily_metrics")


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def load_server():
    """A fresh copy of agent/mcp_server.py (so each test gets its own GATE)."""
    spec = importlib.util.spec_from_file_location("sc_mcp_fresh", MCP_SERVER_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_server(tmp_path, monkeypatch, inject=True, ttl=600):
    """Server module bound to a throwaway synthetic store and audit log, with
    an injected clock on its gate. Returns (module, clock, db_path)."""
    db_path = str(tmp_path / "training.db")
    synthetic.build(db_path, inject=inject)
    m = load_server()
    clock = FakeClock()
    monkeypatch.setattr(m, "DB_PATH", db_path)
    monkeypatch.setattr(m, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(m, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(m, "GATE", ConfirmationGate(ttl_s=ttl, clock=clock))
    return m, clock, db_path


def dump(db_path):
    """Every row of every table, for exact before/after comparison."""
    conn = sqlite3.connect(db_path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in TABLES}
    finally:
        conn.close()


def unwrap(text):
    assert text.count("<untrusted_data>") == 1, text
    return text.split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0]


def preview_of(text):
    """Parse a gated tool's preview response into its dict."""
    return json.loads(unwrap(text))
