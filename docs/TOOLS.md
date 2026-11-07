# Tool and function reference

Every callable in the coach: the MCP tools the model invokes, and the library
functions they wrap. If you add or change a callable, update this file in the
same commit.

- **Tool layer (MCP)**: `agent/mcp_server.py` is a stdio MCP server. Every tool
  is capability-scoped: it touches only the training SQLite file and two
  markdown files (`gym.md`, `coaching-profile.md`) under `COACH_DATA_DIR`. No
  shell, no network, no other filesystem access, and no path built from model
  input. Read tools open the DB read-only.
- **Library layer**: `strength_coach/` is pure Python with no agent or UI
  imports and is fully offline-testable. The tool layer is the only
  MCP-coupled piece.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the why.

---

## Tools the coach calls

Read tools return a JSON string (or file text for the two markdown readers).
They route through `_read_json`, which returns `{"error": ...}` instead of
raising, so a transient DB error never becomes a blank model turn. Write tools
return a short prose confirmation or error string.

### Write / mutate

| Tool | Signature | Purpose | Notes |
|---|---|---|---|
| `remove_import` | `(import_id, confirm_token="")` | Delete the sessions an import currently owns. | Two-step gate in code: without a token it only previews (session and set ids, counts) and returns a one-time token; with the token and the same arguments it applies. Persona gets the user's yes in between. Only removes sessions this import last touched (`source_import_id`); sessions a later import re-touched are kept. Find the id with `list_imports`. |
| `set_away_period` | `(start_date, end_date, reason="")` | Record travel or illness so consistency does not penalize the gap. | ISO `YYYY-MM-DD`. Weeks overlapping an away period are bridged in streaks and averages. |
| `remove_away_period` | `(period_id)` | Delete an away period. | Ids appear in `get_training_consistency` under `away_periods`. |
| `merge_exercises` | `(from_name, into_name, confirm_token="")` | Merge one canonical exercise into another (repoint sets, delete the empty one). | Same two-step gate as `remove_import`; the preview lists the set ids that would move. For unifying duplicate names. |
| `add_coaching_note` | `(note, kind="")` | Persist standing guidance (a preference, feedback, a goal). | Read back at the start of every conversation via `get_coaching_profile`. `kind` is a free label. |
| `remove_coaching_note` | `(note_id)` | Forget a coaching note. | Ids appear in the notes list inside `get_coaching_profile`. |

**Confirmation gate.** `remove_import` and `merge_exercises` return a preview
and write nothing unless called with a valid `confirm_token`. Tokens are
issued only by a preview call, are random, server-side, single use, expire
after `COACH_CONFIRM_TTL_S` seconds (default 600), and bind to the exact
arguments and the previewed row ids. A missing, unknown, expired, reused,
mismatched or stale token is refused with `Refused, nothing changed: <reason>`.
Each preview, rejection and apply is appended to `audit.jsonl`
(`COACH_AUDIT_LOG`). The gate does not verify a human said yes; see
ARCHITECTURE section 6.

**Output envelope.** Every read tool (and the previews) returns its payload
between `<untrusted_data>` tags after a fixed notice. Parse the JSON between
the tags. `</` inside the payload is escaped as `<\/`.

Importing export files is intentionally not a tool. See ARCHITECTURE, "No LLM
in the write path".

### Read

| Tool | Signature | Purpose | Notes |
|---|---|---|---|
| `get_context` | `()` | Bundled grounding: profile and notes, gym, consistency (strength-only and all-activity), program changes, cardio bests. | Call once at the start of a substantive coaching turn. |
| `get_training_overview` | `(window_days=180)` | "How am I doing / zoom out" in one call: consistency, per-lift windowed direction, biggest training gaps. | Dual-implement lifts carry a caveat; windowed numbers are kept. |
| `get_training_consistency` | `()` | Consistency metrics. | Days since last, current and longest weekly streak (away-bridged), days trained in last 28, week-by-week. Strength only. |
| `get_exercise_summary` | `(exercise, start_date=None, end_date=None)` | Best literal weight and best estimated 1RM for one lift. | Present the two numbers distinctly. Bodyweight lifts report best reps. |
| `compare_exercise_periods` | `(exercise, period_a_start, period_a_end, period_b_start, period_b_end)` | Bests for one lift across two windows. | Cross-source by construction (canonicalization). |
| `get_exercise_trend` | `(exercise)` | Whole-history direction of best-per-session e1RM. | Dual-implement lifts return `direction: "not_comparable"` with no `pct_change`. |
| `get_recent_sets` | `(exercise=None, start_date=None, end_date=None, limit=200)` | Raw working sets, most recent `limit`. | Warmups excluded. |
| `get_recent_workouts` | `(count=12)` | The last N workouts in full. | For block review and program critique. |
| `get_cardio_summary` | `(window_days=90)` | Non-strength activity totals per type over a trailing window. | Session count, distance, elevation, calories, duration. |
| `get_activity_records` | `(metric="distance", activity_type=None, window_days=None, limit=5)` | Best single cardio sessions ranked by distance, duration or elevation. | Returns the actual sessions. |
| `get_recovery_status` | `(window_days=14)` | Sleep, resting HR, HRV: latest value plus trailing average. | No composite readiness score, by design. |
| `list_exercises` | `()` | Every tracked exercise with set count and date range. | |
| `list_imports` | `()` | Every import batch, newest first. | Find the id for `remove_import`. |
| `get_program_changes` | `()` | Diff of the two latest program snapshots. | `need_two_snapshots` until two exist. |
| `get_gym_equipment` | `()` | `gym.md` verbatim. | Help message if absent. |
| `get_coaching_profile` | `()` | `coaching-profile.md` plus the running coaching notes. | Source of truth for who the user is and how to coach them. |

### Internal helpers (not exposed to the model)
- `_ro()`, `_rw()`: connections (read-only URI vs read-write plus schema init).
- `_read_json(fn)`: run `fn(conn)` on a read-only connection, JSON-encode, and
  turn any failure into `{"error": ...}`.
- `_read_data_file(filename, label)`: read a markdown file from the
  data dir with hardcoded filenames, so no model input reaches a path.

---

## Library functions (`strength_coach/`)

### `analytics.py`: query-time reads (pure functions over DB rows)
Derived numbers are computed on demand, never stored, so the math can evolve
without reprocessing history.

**Name resolution**
- `find_exercise(conn, name) -> canonical | None`: resolve a user/model name.
  Tries the query AND its loose plural/singular (both directions across a
  trailing "s": "squats"→"Squat", "calf raise"→"Calf Raises"): exact
  (case-insensitive) → normalized-form (incl. the alias table, e.g. the seeded
  abbreviations RDL→Romanian Deadlift, OHP→Overhead Press). Returns None otherwise
  → callers surface `suggest_exercises`. Deliberately does NOT resolve by
  substring or fuzzy match: both bind the WRONG lift ("rdl" is a unique substring
  of "Single Leg RDL", not "Romanian Deadlift"); a confident wrong number is worse
  than a no_match.
  Depluralize + exact/normalized still resolves the common loose cases in one shot.
- `suggest_exercises(conn, name, n=5) -> [names]`: did-you-mean list (substring
  then difflib-fuzzy on the normalized form).

**Strength math**
- `estimate_1rm(weight, reps, rir=None) -> float`: RIR-adjusted Epley
  `weight*(1+(reps+rir)/30)`, rounded 1dp; `rir=None` → 0 (plain Epley).
- `_best_e1rm_set(sets) -> set | None`: highest e1RM set considering only
  `reps <= E1RM_MAX_REPS` (12), so a high-rep back-off can't masquerade as a peak.
- `_is_bodyweight(sets) -> bool`: few sets carry load → track by reps not weight.
- `_top_set(sets) -> set | None`: heaviest working set (weight, then reps),
  warmups excluded.

**Trends**
- `_best_per_session_series(sets, bodyweight) -> [float]`: best value per training
  date, oldest first (e1RM for weighted, reps for bodyweight).
- `_direction(series, deadband=0.03) -> {direction, pct_change, n_sessions}` , 
  first-third vs last-third mean with a deadband; `insufficient_data` if <3 points.
- `trend(conn, exercise) -> {...}`: whole-history direction. **Dual-implement
  lifts return `direction: "not_comparable"` and no `pct_change`** (cross-year
  weights mix per-hand/total conventions).

**Summaries / comparison**
- `exercise_summary(conn, exercise, start=None, end=None)`: best literal + best
  e1RM (or best reps for bodyweight) over an optional window. Attaches
  `weight_convention_caveat` for dual-implement lifts.
- `compare_periods(conn, exercise, period_a, period_b)`: `exercise_summary` for
  two windows.
- `fetch_sets(conn, exercise=None, start=None, end=None, include_warmups=False)` , 
  the flexible working-set read everything else builds on.
- `list_tracked_exercises(conn)` / `list_imports(conn)`: inventory reads.

**Composed single-call answers** (built so a tool call never needs a
second round to be useful)
- `session_readout(conn, session_date=None)`: everything to coach one session in
  one call: the session(s) that day, per-exercise top set + all-time bests +
  trend direction + dual-implement flag, plus bundled `consistency` and a
  root-level `weight_convention_caveat` when any flagged lift is present.
  `no_data` (empty DB) / `no_session_on_date` (empty date) branches. Detects ANY session that date (not just ones with `sets`), so a cardio-only
  day no longer misreports as `no_session_on_date`; non-strength sessions
  render as a root-level `cardio_sessions` list (activity_type, duration_min,
  `session_metrics`) alongside `exercises`.
- `training_overview(conn, window_days=180, today=None, activity_types=STRENGTH_TYPES)` , 
  consistency + per-lift windowed trend (≥3 sessions in window, strength only , 
  only strength sessions carry `sets`) + `notable_gaps` (≥10 days between
  training days, away-excluded) in one call. Windowed numbers are kept for
  dual-implement lifts (within-period is real); caveat attached.
  `activity_types` scopes the bundled consistency + the gap detection (default
  strength-only; `None` for all logged activity).
- `training_consistency(conn, weeks=12, today=None, activity_types=STRENGTH_TYPES)` , 
  the KPI. Distinct training days; current streak (walk back weekly, away weeks
  bridge, a genuine miss ends it); longest streak; days-trained-last-28;
  avg/week over non-away weeks; `currently_away`. **Known semantics**:
  `current_streak_weeks` anchors to the last active week (pair with
  `days_since_last`). `activity_types` param: default `STRENGTH_TYPES`
  (today's behavior, unchanged: the #1 KPI can't be silently inflated by
  cardio); pass `None` for the all-activity number. `get_context` (MCP) calls
  both and reports them together.
- `cardio_summary(conn, window_days=90, today=None)`: Non-strength
  activity (walks/runs/hikes/bike/other cardio) over a trailing window:
  per-activity-type totals (session count, distance_m, calories, duration_min)
  + a combined total + most recent activity date. Reads `session_metrics`.
  `status: "no_data"` if nothing logged in the window.
- `recovery_status(conn, window_days=14, today=None)`: Recent
  recovery signals (sleep, resting HR, HRV) from `daily_metrics`: latest
  value + trailing-window average per metric. No synthetic readiness score.
  `status: "no_data"` if nothing logged in the window.
- `is_dual_implement(name) -> bool`: dumbbell/dual-cable classification. Markers
  `dumbbell / dual cable / cable fly / cable crossover`; exclusions
  `one arm / single arm / 1 arm / pullover` (unilateral or single-bell → per-hand
  == total → not ambiguous). Name-based; extend the
  sets when new exercises appear.
- `program_diff(conn)`: structural diff of the two latest program snapshots.
- `recent_workouts(conn, count=12)`: non-strength sessions now render
  `duration_min` + `metrics` (from `session_metrics`) instead of an empty
  `exercises` list; each workout entry carries `activity_type`.

Constants: `E1RM_MAX_REPS=12`, `_DUAL_IMPLEMENT_MARKERS`, `_SINGLE_IMPLEMENT_MARKERS`,
`_WEIGHT_CONVENTION_CAVEAT`, `STRENGTH_TYPES={"strength"}`, `CARDIO_TYPES=
{"walk","run","hike","bike","spin","cardio"}`.

### `db.py`: storage
- `connect(path, read_only=False)`: WAL + foreign keys; `read_only=True` opens a
  `mode=ro` URI (physically non-writable).
- `init_schema(conn)`: create tables (idempotent).
- `ingest(conn, parsed, resolver) -> summary`: insert/replace sessions; returns
  `new_count` / `updated_count` / `new_sessions` / `updated_sessions` (+ legacy
  `sessions`/`sets`). Checkpoints the WAL (TRUNCATE) after commit so readers
  aren't racing a large log.
- `_upsert_session(...)`: session identity is `(source, date, workout,
  source_session_key)`; on conflict, children are replaced wholesale (corrections
  propagate). Returns `{sets, is_new, date, workout}`.
- `remove_import(conn, import_id)`: delete sessions an import currently owns.
- `plan_remove_import(conn, import_id)`: read-only description of that delete
  by row id (what the preview returns).
- `add/get/remove_away_period`, `add/get/remove_coaching_note`: small CRUD.
- `upsert_daily_metric(conn, date, source, metric, value, unit=None) -> id` , 
  Insert-or-update one `daily_metrics` row; identity is `(date,
  source, metric)`, so a re-pull of the same day overwrites in place
  (idempotent by construction).
- `get_daily_metrics(conn, since=None) -> [dict]`: All daily
  recovery/wearable metrics, optionally from an ISO date onward.

### `canonical.py`, `adapters/`, `models.py`, `seeds.py`
- `canonical.normalize(name)`: lowercase, drop punctuation, collapse whitespace.
- `canonical.Canonicalizer` / `merge_exercise` / `seed_aliases`: resolve raw
  source names to exercise ids on import; collect newly-created names + near-match
  suggestions.
- `adapters/{macrofactor,stronglifts_db,liftin_csv,liftosaur_json}.py` +
  `detect.py`: one deterministic parser per source (units→lb, dates→ISO),
  content-based detection. All open source files read-only.
- `models.py`: `NormalizedSession` / `NormalizedSet` / `ParsedImport` (the neutral
  model every adapter maps into) + `kg_to_lb`.
- `seeds.py`: `DEFAULT_ALIASES` (generic name unification, seeded on import).
- `aliases.py`: loads `$COACH_DATA_DIR/aliases.yaml` (strict flat format) and
  merges it over the defaults; user entries win, a malformed file raises.
- `context.py`: the `untrusted_data` envelope and the `get_context` bundle,
  shared by the MCP server and `bin/coach context`.
- `config.py`: data-directory resolution and `check()`, used by every entry point.

### `synthetic.py`: fabricated demo data
- `build(db_path, end="2026-10-30", seed=7)`: writes a deterministic store
  with three strength blocks, cardio, daily recovery metrics, an away period,
  two program snapshots and two coaching notes. Same inputs, same rows.
  `scripts/make_synthetic_data.py` wraps it and also copies an example's
  `gym.md` and `coaching-profile.md` (default `examples/home-3day`) into the
  data directory.

### `importer/watcher.py`
- Polls an inbox directory for export files, ingests each through the same
  deterministic pipeline as `scripts/backfill.py`, archives the raw file, and
  writes one JSONL audit line per file. No model call anywhere in this path.
