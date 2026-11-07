#!/usr/bin/env python3
"""Watched-folder importer (designed for a Syncthing-synced folder).

Polls INBOX_DIR for workout exports synced in by Syncthing, runs them
through the existing deterministic import pipeline
(seed_aliases -> detect.parse_file -> Canonicalizer -> db.ingest) into
DB_PATH. On success the file is DELETED (the inbox is a normal two-way
Syncthing folder, so deleting on the server propagates back and clears the file off
the phone — that's the intended "consume" semantics, not a mistake). On
failure the file is moved to INBOX_DIR/failed/ and kept, so a bad file is
never silently lost. A successful file is first copied to ARCHIVE_DIR, so an
import that "succeeded" with bad data can still be diagnosed from the raw
export. Every processed file (success or failure) gets one
JSONL line appended to LOG_PATH as the durable record, since a successful
import leaves no file behind to inspect afterward.

This is a dumb polling loop by design: Syncthing writes new files via an
atomic rename from a temp name, so a simple "is this file stable and not a
known temp pattern" check is robust without needing inotify or any
Syncthing-specific API. No LLM involved anywhere in this path — ingestion is
fully deterministic, same as scripts/backfill.py.

Env config:
    COACH_DATA_DIR (required)         - data directory (holds the store, log, archive, aliases.yaml)
    INBOX_DIR     default "$COACH_DATA_DIR/inbox" - watched folder (e.g. a Syncthing target)
    DB_PATH       default "$COACH_DATA_DIR/training.db"
    LOG_PATH      default "$COACH_DATA_DIR/import-log.jsonl" - append-only JSONL audit
                                        log (lives in the data dir, NOT the
                                        synced inbox, so it never syncs to
                                        the phone)
    ARCHIVE_DIR   default "$COACH_DATA_DIR/import-archive" - raw copy of every
                                        successful import (data dir, not the
                                        synced inbox)
    POLL_SECONDS  default 30         - seconds between polls
    CORE_DIR      default repo root  - where the strength_coach package is
                                        found (added to sys.path)
    STABILITY_SECONDS default 5      - a file must be untouched for at least
                                        this long (by mtime) before it's
                                        considered done syncing
"""

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CORE_DIR = os.environ.get(
    "CORE_DIR", str(Path(__file__).resolve().parent.parent))
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

DATA_DIR = os.environ.get("COACH_DATA_DIR", "")
INBOX_DIR = Path(os.environ.get("INBOX_DIR", os.path.join(DATA_DIR, "inbox")))
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "training.db"))
LOG_PATH = Path(os.environ.get(
    "LOG_PATH", os.path.join(DATA_DIR, "import-log.jsonl")))
ARCHIVE_DIR = Path(os.environ.get(
    "ARCHIVE_DIR", os.path.join(DATA_DIR, "import-archive")))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30"))
STABILITY_SECONDS = float(os.environ.get("STABILITY_SECONDS", "5"))

RECOGNIZED_EXTENSIONS = {".xlsx", ".db", ".csv", ".json"}

# Syncthing writes new files under a temp name, then atomically renames them
# into place once fully synced. These substrings/prefixes identify files
# still in flight (or otherwise not meant to be picked up).
_TEMP_MARKERS = (".syncthing.", ".tmp")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    # stderr for hygiene/consistency with the other importer modules: none of
    # them should reconfigure a stdout root-logger handler, in case they're
    # ever imported into a stdio process (see wearable_poller.py).
    stream=sys.stderr,
)
log = logging.getLogger("importer")


def _is_temp_or_hidden(name: str) -> bool:
    if name.startswith("."):
        return True
    return any(marker in name for marker in _TEMP_MARKERS)


def _is_stable(path: Path) -> bool:
    """A file is considered done syncing once it hasn't been touched for at
    least STABILITY_SECONDS. Cheap and stateless: no need to remember mtimes
    across polls, we just check age against wall clock each time."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age >= STABILITY_SECONDS


def _candidates(inbox: Path):
    """Yield stable, non-temp files with a recognized extension directly in
    the inbox (not recursing into failed/)."""
    try:
        entries = sorted(inbox.iterdir())
    except OSError as e:
        log.error("could not list inbox %s: %s", inbox, e)
        return
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.parent != inbox:
            continue
        if _is_temp_or_hidden(entry.name):
            continue
        if entry.suffix.lower() not in RECOGNIZED_EXTENSIONS:
            continue
        if not _is_stable(entry):
            log.info("skipping %s: not yet stable (still syncing?)", entry.name)
            continue
        yield entry


def _unique_dest(dest_dir: Path, filename: str) -> Path:
    """Avoid clobbering an existing file of the same name in failed/ by
    appending a numeric suffix."""
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}.{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _archive(path: Path):
    """Copy a successfully imported file to ARCHIVE_DIR. Best-effort: the
    import already committed, so a failed copy is logged, not raised. Returns
    the archive path, or None."""
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = _unique_dest(ARCHIVE_DIR, path.name)
        shutil.copy2(str(path), str(dest))
        return str(dest)
    except OSError as e:
        log.error("could not archive %s to %s: %s", path.name, ARCHIVE_DIR, e)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(event: dict) -> None:
    """Append one JSONL line to LOG_PATH — the durable record, since a
    successful import deletes the source file and leaves nothing else to
    inspect. Best-effort: a logging failure must not crash the import loop
    (the import itself already succeeded/failed and is done)."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        log.error("could not write to import log %s: %s", LOG_PATH, e)


def _import_one(conn, path: Path) -> dict:
    """Run the pipeline for one file. Returns the ingest summary dict.
    Raises on any failure (unrecognized format, parse error, ingest error) so
    the caller can route the file to failed/."""
    from strength_coach import db
    from strength_coach.adapters import detect
    from strength_coach.canonical import Canonicalizer

    parsed = detect.parse_file(str(path))
    canon = Canonicalizer(conn)
    return db.ingest(conn, parsed, canon.resolve)


def run_once(conn, inbox: Path, failed_dir: Path) -> int:
    """Process every currently-stable candidate file in the inbox. Returns
    the number of files handled (success or failure)."""
    handled = 0
    for path in _candidates(inbox):
        handled += 1
        try:
            summary = _import_one(conn, path)
            log.info(
                "imported %s: %d new / %d already-logged (%s)",
                path.name, summary["new_count"], summary["updated_count"],
                summary["source"],
            )
            _append_log({
                "ts": _now_iso(),
                "file": path.name,
                "source": summary["source"],
                "new": summary["new_count"],
                "already_logged": summary["updated_count"],
                "status": "ok",
                "archived_to": _archive(path),
            })
            # Success consumes the file: this inbox is a normal two-way
            # Syncthing folder, so deleting here propagates back and clears
            # it off the phone too — that's the intended behavior, not a
            # bug. The import is already durably in training.db + the log.
            path.unlink()
        except Exception as e:  # noqa: BLE001 - never let one bad file kill the loop
            log.error("error importing %s: %s", path.name, e)
            moved_to = None
            try:
                dest = _unique_dest(failed_dir, path.name)
                shutil.move(str(path), str(dest))
                moved_to = str(dest)
            except OSError as move_err:
                log.error(
                    "could not move failed file %s to %s: %s",
                    path.name, failed_dir, move_err,
                )
            _append_log({
                "ts": _now_iso(),
                "file": path.name,
                "status": "failed",
                "error": str(e),
                "moved_to": moved_to,
            })
    return handled


def main() -> int:
    from strength_coach import aliases, db
    from strength_coach import config as scconfig
    from strength_coach.canonical import seed_aliases

    # Validate before the loop: a malformed aliases.yaml must stop the importer
    # with a fix, not be retried and logged forever.
    try:
        scconfig.require(DATA_DIR, need_db=False)
    except scconfig.ConfigError as e:
        log.error("%s", e)
        return 2

    failed_dir = INBOX_DIR / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "importer starting: inbox=%s db=%s log=%s poll=%ss stability=%ss",
        INBOX_DIR, DB_PATH, LOG_PATH, POLL_SECONDS, STABILITY_SECONDS,
    )

    while True:
        try:
            conn = db.connect(DB_PATH)
            try:
                db.init_schema(conn)
                # Re-read every poll so an edited aliases.yaml applies to the
                # next file without a restart. A file broken mid-run raises here
                # and the poll is skipped (logged) rather than importing with
                # half the user's mappings.
                seed_aliases(conn, aliases.load(DATA_DIR))
                run_once(conn, INBOX_DIR, failed_dir)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 - keep the loop alive across polls
            log.error("poll cycle failed: %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
