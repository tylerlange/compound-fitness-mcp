"""Exercise-name canonicalization across sources.

Three apps name the same lift three ways; without unifying them, a cross-source
query for "bench press" silently compares two different name strings and finds
nothing. This layer maps each source name to a canonical exercise id.

Matching, in order:
  1. exact alias hit for (name, source)
  2. exact alias hit for the name under any source
  3. normalized-form hit against an existing canonical name or alias
Anything unmatched auto-creates a new canonical exercise (so an import never
blocks) and is reported, so duplicates can be merged afterward with
``merge_exercise``. Fuzzy guessing is deliberately NOT auto-applied — it would
silently fuse distinct lifts; near-matches are surfaced as suggestions instead.
"""

import difflib
import re
import sqlite3

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^a-z0-9 ]+")


def normalize(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. 'Bench Press (Barbell)'
    and 'bench press  barbell' both -> 'bench press barbell'."""
    s = name.strip().lower()
    s = _NOISE.sub(" ", s)
    s = _WS.sub(" ", s)
    return s.strip()


class Canonicalizer:
    """Resolves raw source names to exercise ids, caching within an import so a
    name first seen (and auto-created) mid-import resolves consistently after.
    Tracks which names were newly created, and near-match suggestions, for
    reporting back to the user."""

    def __init__(self, conn: sqlite3.Connection, suggest_threshold: float = 0.86):
        self.conn = conn
        self.suggest_threshold = suggest_threshold
        self.newly_created: dict[str, int] = {}  # canonical_name -> id
        self.suggestions: dict[str, str] = {}  # new_name -> nearest existing canonical

    def _norm_index(self) -> dict[str, int]:
        """normalized-form -> exercise_id, over canonical names and all aliases."""
        idx: dict[str, int] = {}
        for row in self.conn.execute("SELECT id, canonical_name FROM exercises"):
            idx[normalize(row["canonical_name"])] = row["id"]
        for row in self.conn.execute("SELECT alias, exercise_id FROM exercise_aliases"):
            idx.setdefault(normalize(row["alias"]), row["exercise_id"])
        return idx

    def resolve(self, raw_name: str, source: str) -> int:
        raw_name = raw_name.strip()

        # 1. exact alias for this source
        row = self.conn.execute(
            "SELECT exercise_id FROM exercise_aliases WHERE alias=? AND source=?",
            (raw_name, source),
        ).fetchone()
        if row:
            return row["exercise_id"]

        # 2. exact alias under any source
        row = self.conn.execute(
            "SELECT exercise_id FROM exercise_aliases WHERE alias=? LIMIT 1", (raw_name,)
        ).fetchone()
        if row:
            self._add_alias(raw_name, source, row["exercise_id"])
            return row["exercise_id"]

        # 3. normalized-form hit against canonical names / aliases
        norm = normalize(raw_name)
        idx = self._norm_index()
        if norm in idx:
            self._add_alias(raw_name, source, idx[norm])
            return idx[norm]

        # 4. no match -> create new canonical, record a near-match suggestion
        return self._create(raw_name, source, idx)

    def _create(self, raw_name: str, source: str, norm_idx: dict[str, int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO exercises (canonical_name) VALUES (?)", (raw_name,)
        )
        exercise_id = cur.lastrowid
        self._add_alias(raw_name, source, exercise_id)
        self.newly_created[raw_name] = exercise_id

        # nearest existing canonical, for a "did you mean" merge suggestion
        candidates = list(norm_idx.keys())
        near = difflib.get_close_matches(
            normalize(raw_name), candidates, n=1, cutoff=self.suggest_threshold
        )
        if near:
            existing_id = norm_idx[near[0]]
            row = self.conn.execute(
                "SELECT canonical_name FROM exercises WHERE id=?", (existing_id,)
            ).fetchone()
            if row and row["canonical_name"] != raw_name:
                self.suggestions[raw_name] = row["canonical_name"]
        return exercise_id

    def _add_alias(self, alias: str, source: str, exercise_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO exercise_aliases (alias, source, exercise_id) VALUES (?, ?, ?)",
            (alias, source, exercise_id),
        )


def seed_aliases(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    """Seed canonical exercises + aliases up front to reduce post-import merging.
    ``mapping`` is {alias: canonical_name}; aliases are registered under source
    '*' (any source), matched via the normalized-form path. Re-seeding with a
    changed target repoints the alias, so an edited aliases.yaml takes effect
    on the next import (existing sets keep the exercise they were stored as)."""
    for alias, canonical in mapping.items():
        row = conn.execute(
            "SELECT id FROM exercises WHERE canonical_name=?", (canonical,)
        ).fetchone()
        if row:
            exercise_id = row["id"]
        else:
            exercise_id = conn.execute(
                "INSERT INTO exercises (canonical_name) VALUES (?)", (canonical,)
            ).lastrowid
        conn.execute(
            "INSERT INTO exercise_aliases (alias, source, exercise_id) VALUES (?, '*', ?) "
            "ON CONFLICT(alias, source) DO UPDATE SET exercise_id=excluded.exercise_id",
            (alias, exercise_id),
        )
    conn.commit()


def plan_merge_exercise(conn: sqlite3.Connection, from_name: str, into_name: str) -> dict:
    """Read-only description of exactly what `merge_exercise` would change, by
    row id. Never writes. Raises ValueError on the same conditions that make
    the merge itself invalid."""
    if from_name == into_name:
        raise ValueError("cannot merge an exercise into itself")
    frm = conn.execute(
        "SELECT id FROM exercises WHERE canonical_name=?", (from_name,)
    ).fetchone()
    into = conn.execute(
        "SELECT id FROM exercises WHERE canonical_name=?", (into_name,)
    ).fetchone()
    if not frm or not into:
        raise ValueError("both exercises must exist to merge")
    set_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sets WHERE exercise_id=? ORDER BY id", (frm["id"],))]
    aliases = [dict(r) for r in conn.execute(
        "SELECT alias, source FROM exercise_aliases WHERE exercise_id=? "
        "ORDER BY alias, source", (frm["id"],))]
    return {
        "action": "merge_exercises",
        "from": {"id": frm["id"], "name": from_name},
        "into": {"id": into["id"], "name": into_name},
        "set_ids": set_ids,
        "aliases": aliases,
        "exercise_ids_deleted": [frm["id"]],
    }


def merge_exercise(conn: sqlite3.Connection, from_name: str, into_name: str) -> dict:
    """Merge one canonical exercise into another: repoint sets + aliases, delete
    the now-empty source exercise. The correction path for auto-created dupes."""
    if from_name == into_name:
        raise ValueError("cannot merge an exercise into itself")
    frm = conn.execute(
        "SELECT id FROM exercises WHERE canonical_name=?", (from_name,)
    ).fetchone()
    into = conn.execute(
        "SELECT id FROM exercises WHERE canonical_name=?", (into_name,)
    ).fetchone()
    if not frm or not into:
        raise ValueError("both exercises must exist to merge")
    from_id, into_id = frm["id"], into["id"]
    n = conn.execute(
        "UPDATE sets SET exercise_id=? WHERE exercise_id=?", (into_id, from_id)
    ).rowcount
    conn.execute(
        "UPDATE OR IGNORE exercise_aliases SET exercise_id=? WHERE exercise_id=?",
        (into_id, from_id),
    )
    conn.execute("DELETE FROM exercise_aliases WHERE exercise_id=?", (from_id,))
    conn.execute("DELETE FROM exercises WHERE id=?", (from_id,))
    conn.commit()
    return {"merged": from_name, "into": into_name, "sets_repointed": n}
