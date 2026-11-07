# importer/

`watcher.py` is a deterministic, polling watched-folder importer. It was built
for a Syncthing-synced folder, but any directory works. No model is involved
anywhere in this path.

Each poll it lists `INBOX_DIR` (not recursive) and imports candidate files
through the same pipeline as `scripts/backfill.py`
(`seed_aliases` -> `detect.parse_file` -> `Canonicalizer` -> `db.ingest`).

- **Success:** the raw file is copied to `ARCHIVE_DIR`, one `ok` line is
  appended to the JSONL log, and the inbox file is deleted. The inbox is
  treated as a consume-on-success queue; with a two-way sync folder, the delete
  propagates back and clears the source device.
- **Failure:** one `failed` line is logged and the file is moved to
  `INBOX_DIR/failed/`, never deleted.

Import is idempotent (session-level upsert), so a re-processed or overlapping
file is safe.

## Run

```bash
python importer/watcher.py
```

## Environment

| Var | Default | Meaning |
|---|---|---|
| `COACH_DATA_DIR` | required | Data directory. Holds the store, log, archive and `aliases.yaml`. |
| `INBOX_DIR` | `$COACH_DATA_DIR/inbox` | Folder to watch. |
| `DB_PATH` | `$COACH_DATA_DIR/training.db` | Store to ingest into. |
| `LOG_PATH` | `$COACH_DATA_DIR/import-log.jsonl` | Append-only JSONL audit log, kept outside the inbox so it never syncs. |
| `ARCHIVE_DIR` | `$COACH_DATA_DIR/import-archive` | Raw copy of every successful import. |
| `POLL_SECONDS` | `30` | Seconds between polls. |
| `STABILITY_SECONDS` | `5` | A file must be untouched this long (by mtime) before it is treated as fully written. |
| `CORE_DIR` | repo root | Where `strength_coach` is found. |

## Candidate files

A file is a candidate if its extension is `.xlsx`, `.db`, `.csv` or `.json`,
its name does not start with `.` or contain `.syncthing.` or `.tmp`, and its
mtime is at least `STABILITY_SECONDS` old. Format detection is by content
(`strength_coach.adapters.detect`), not by extension.

## Log shape

```jsonc
// success
{"ts": "2026-07-22T20:47:00+00:00", "file": "export.xlsx", "source": "macrofactor", "new": 3, "already_logged": 51, "status": "ok"}
// failure
{"ts": "2026-07-22T20:47:35+00:00", "file": "junk.csv", "status": "failed", "error": "CSV with unrecognized columns", "moved_to": "inbox/failed/junk.csv"}
```

## Not included

The original deployment also had pollers that pulled wearable and cycling data
from vendor APIs with OAuth credentials. They are not part of this copy. The
`daily_metrics` table, the cardio tools and their tests remain, and the
synthetic generator fills them.
