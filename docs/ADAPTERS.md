# Adapter contract

An adapter turns one fitness source into the canonical models this repo stores.
Everything downstream (the SQLite store, the analytics, the MCP tools, the
agent) only ever sees the canonical models, so a new source needs no change
outside its own adapter, its tests and, for file formats, one line in
`strength_coach/adapters/detect.py`.

The shipped adapters are the reference: `liftin_csv.py` (smallest),
`macrofactor.py`, `stronglifts_db.py`, `liftosaur_json.py`, `alltrails_gpx.py`.
No adapter in this repo calls a vendor API. API-backed sources are covered by
the same contract; see "Two shapes of adapter" below.

## 1. Input

An adapter reads one of:

- **A file or export** (CSV, XLSX, SQLite, JSON, GPX, a zip). The adapter opens
  it read-only and never modifies or deletes it.
- **An API.** A fetch function takes a date window and credentials from the
  environment, and returns the same canonical models.

Either way the adapter is deterministic code. Given the same input it returns
the same output. There is no model call anywhere in an adapter or in the code
that writes its output.

## 2. Output: canonical models

Defined in `strength_coach/models.py`.

| Model | Used for | Key fields |
|---|---|---|
| `ParsedImport(source, filename, sessions, program_snapshot=None)` | One batch from one source | `source` is a short lowercase label, for example `strava` |
| `NormalizedSession` | One workout or activity | `date` (ISO `YYYY-MM-DD`), `source`, `activity_type`, `workout` (label), `source_session_key`, `duration_s`, `notes`, `sets`, `metrics`, `streams` |
| `NormalizedSet` | One set in a strength session | `exercise_raw` (the source's own name), `seq` (0-based order), `set_type`, `weight_lb`, `reps`, `rir` |
| `metrics` entries | Non-set numbers for a session | tuples of `(metric, value, unit)` |
| Daily metric | One recovery scalar per day | `db.upsert_daily_metric(conn, date, source, metric, value, unit)` |

Rules the adapter must follow:

- **Units:** weights in pounds (use `models.kg_to_lb`), dates as ISO strings,
  distances in meters (`distance_m`), elevation in meters (`elevation_gain_m`),
  durations in seconds. Convert in the adapter, not later.
- **Activity types:** use `strength` for lifting. Cardio uses one of `walk`,
  `run`, `hike`, `bike`, `spin`, `cardio` so the cardio tools pick it up. Map
  the vendor's types onto these in one lookup table inside the adapter.
- **Metric names:** reuse the names the analytics read (`distance_m`,
  `elevation_gain_m`, `calories`, `avg_hr` for sessions; `resting_hr`,
  `hrv_rmssd`, `sleep_minutes_asleep` for daily metrics). Add a new name only
  with a test that shows a tool reading it.
- **Exercise names:** pass the source's raw name in `exercise_raw`. Do not
  canonicalize in the adapter. `Canonicalizer` resolves names at ingest, and
  cross-source aliases live in `strength_coach/seeds.py`.
- **Unknown is None, not zero.** If the source did not record a value, leave
  it `None`. Never invent a default.

## 3. Idempotency keys

Re-importing the same data must not duplicate or drift it. The store enforces
identity on these keys:

- Sessions: `(source, date, workout, source_session_key)`. If two sessions can
  share a date and label, the adapter must set `source_session_key` to the
  vendor's stable id (activity id, or the full start timestamp). Leaving it
  empty for such a source silently merges distinct workouts.
- Daily metrics: `(date, source, metric)`, upserted in place.

On a repeat, `db.ingest` replaces the session's sets, metrics and streams
wholesale, so a corrected value in the source propagates. "Re-import is a
no-op" therefore means: the sessions, sets, metrics and daily-metric tables are
row-for-row identical afterward. The `imports` table gets one more bookkeeping
row per run, by design.

## 4. Failure behavior

Failure must be loud and must not leave partial data.

- **Raise on anything unexpected:** an unrecognized layout, a missing required
  column, an unparseable date, an unknown unit, an auth error, an HTTP error
  after retries. Use `UnknownSourceError` (from `adapters/detect.py`) for
  "this is not the format I handle".
- **Do not substitute silent NULLs or defaults** for values you could not
  parse. A skipped row is acceptable only if it is counted and reported in the
  return value or the log.
- **Parse everything before writing.** The adapter returns a `ParsedImport`;
  the caller ingests it in one transaction. A raise means nothing was written.
- **Where failures land.** For files, the watched-folder importer
  (`importer/watcher.py`) moves a file whose parse raised into
  `INBOX_DIR/failed/` and logs one `failed` line with the error. The file is
  never deleted. For API adapters, the runner script must exit non-zero, log
  the error, and write nothing.

## 5. Credentials

- Secrets live in environment variables or in a secret file whose path comes
  from an environment variable (for example `STRAVA_TOKEN_FILE`), kept outside
  the repo. The repo ships `.env.example` with placeholder names only.
- OAuth: store the refresh token in that file with permissions `0600`. Refresh
  access tokens before they expire. If the provider rotates refresh tokens,
  write the new one back atomically (write a temp file, then rename) before
  using the new access token.
- Never log tokens, never put them in a URL that gets logged, never commit
  them, never put them in a fixture. `scripts/scan_identifiers.py` runs in CI
  and fails on secret-shaped strings; fixtures must use obviously fake values.
- Document every variable you add in `.env.example` and the adapter's
  docstring.

## 6. Tests required

Every adapter ships with tests that run offline with no credentials
(`tests/test_<source>.py`):

1. **Fixture parse.** A small hand-made fixture of fake data (fixture files
   under `tests/fixtures/`, or built inline for API payloads). Assert the
   exact canonical output: dates, units, activity type, `source_session_key`.
2. **Unit conversion and edge cases:** metric vs imperial, missing optional
   fields stay `None`, empty input, a timezone boundary.
3. **Failure test.** A malformed or wrong-format input raises, and no rows are
   written.
4. **Idempotency test.** Ingest the fixture twice into a temp store. Assert the
   data tables are identical after the second run (see
   `tests/test_adapters.py` and `tests/test_db.py` for the pattern).
5. **Overlap/edit test.** Ingest, change one value in the fixture, ingest
   again: the session shows the new value and the row count is unchanged.
6. **API adapters only:** mock the HTTP layer. Test pagination, a refresh-token
   round trip, a 429 with `Retry-After` followed by success, and a persistent
   5xx that raises. No test touches the network.
7. **Secret hygiene:** the fixture and test files pass
   `python scripts/scan_identifiers.py`.

## 7. Two shapes of adapter

**File adapter.** Module `strength_coach/adapters/<source>.py` exposing
`SOURCE = "<source>"` and `parse(path) -> ParsedImport`. Add a content-based
fingerprint to `detect.detect_source` (never a filename guess, since a wrong
route corrupts data) and register `parse` in `_PARSERS`. The watched-folder
importer and `scripts/backfill.py` then pick it up with no other change.

**API adapter.** Module `strength_coach/adapters/<source>_api.py` exposing
`fetch(since, until, client) -> ParsedImport` (and daily metrics as a list of
`(date, metric, value, unit)` for recovery sources), plus a runner script
`scripts/sync_<source>.py` that reads credentials from the environment, calls
`fetch`, and ingests. The runner must support:

- `--dry-run`: fetch and parse, print counts by date and type, write nothing
  (open the store read-only or skip opening it).
- `--since YYYY-MM-DD`: bounded windows, so a first backfill can run in slices.
- Rate-limit handling: respect `Retry-After`, exponential backoff with jitter
  and a retry cap, and a request budget per run. Stop and report, do not spin.

## 8. No LLM in the write path

An adapter, its runner and the ingest code never call a model and never
parse model output into the store. A model may help you write an adapter. It
does not run inside one. This is what makes every row in the store traceable to
a source record.

## 9. Checklist for a new adapter

- [ ] Emits only canonical models; units and dates normalized
- [ ] Stable `source_session_key` where same-day sessions can collide
- [ ] Raises on unexpected input; no silent NULLs; nothing written on failure
- [ ] Credentials from env or a secret file outside the repo; `.env.example` updated
- [ ] Tests 1 to 7 above pass offline
- [ ] Re-import leaves the data tables unchanged
- [ ] `python scripts/scan_identifiers.py` reports 0 hits
- [ ] `docs/TOOLS.md` updated only if a tool or metric name changed
