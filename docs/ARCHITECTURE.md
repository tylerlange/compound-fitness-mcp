# Architecture

Companion to [TOOLS.md](TOOLS.md) (callable-by-callable reference) and the
repo `README.md`. When a design decision changes, edit the relevant section in
the same commit.

**What this is:** a source-agnostic training-data store plus a coaching agent
that reasons over it. The driving requirement is that the coach can analyze
any part of a multi-year history at any time and compare across it ("2026
bench vs 2019 bench"), so the internal model is neutral and every source maps
into it.

---

## 1. System shape and data flow

```
export files (MacroFactor .xlsx / StrongLifts .db / Liftin .csv / Liftosaur .json)
      |  (dropped in a watched folder, or scripts/backfill.py)
      v
detect.py --> one deterministic adapter --> NormalizedSession/Set (units->lb, dates->ISO)
      |
      v
canonical.Canonicalizer  (raw name -> canonical exercise id, aliases seeded)
      |
      v
db.ingest  -->  training.db  (SQLite, WAL, one file, outside the repo)
                    ^
                    | read-only queries
analytics.py  <-----+   (e1RM, trends, consistency, readouts: computed at query time)
      ^
      | MCP tools (agent/mcp_server.py, stdio)
      v
Pydantic-AI agent (agent/coach.py)  <--  COACH_MODEL (any pydantic-ai model string)
      ^
      | OpenAI-compatible /v1/chat/completions (agent/service.py, FastAPI)
      v
any chat client
```

Two kinds of state live in the data directory (`COACH_DATA_DIR`, outside the
repo):

- `training.db`: SQLite. Single human-driven writer plus a file importer, so
  SQLite in WAL mode is the right size; there is no server.
- `gym.md` and `coaching-profile.md`: free-form markdown the coach reads
  through tools.

## 2. The neutral store (schema)

Activity-agnostic, so non-strength sources land with no migration.

- `sessions(id, date, activity_type, source, workout, source_session_key,
  duration_s, notes, confidence, source_import_id, created_at)`. Identity is
  `(source, date, workout, source_session_key)`. `activity_type` defaults to
  `strength`; wearable data populates `walk` / `run` / `hike` / `bike` /
  `cardio`.
- `sets(id, session_id, exercise_id, set_type, weight_lb, reps, rir, seq)`:
  resistance work.
- `session_metrics(session_id, metric, value, unit)`: generic key/value for
  non-set activity (distance_m, elevation_gain_m, calories, avg_hr).
- `daily_metrics(id, date, source, metric, value, unit, created_at)`: daily
  recovery scalars (resting HR, HRV, sleep minutes). These are not tied to any
  session, and forcing a fake session to hang them on would corrupt the
  consistency metric. Identity is `(date, source, metric)`, upserted.
- `exercises(id, canonical_name)` and `exercise_aliases(alias, source,
  exercise_id)`: the canonicalization layer.
- `program_snapshots`, `imports`, `away_periods`, `coaching_notes`.

Derived numbers (e1RM, trends) are computed at query time and never stored, so
the math can change without reprocessing history.

## 3. Ingestion (deterministic)

Every adapter is deterministic. `detect.py` identifies the source by content
(sheet names, table set, CSV header, JSON shape), never by guess. Each adapter
normalizes its own units (to lb) and dates (to ISO) and maps to
`NormalizedSession` / `NormalizedSet`. Source files are opened read-only.

**Dedup is session-level replace.** Re-importing an overlapping window, or a
workout edited in the source app, replaces that session's sets wholesale, so
corrections propagate and imports are idempotent. `ingest` reports `new_count`
and `updated_count` separately so a rolling export is described honestly.

**Canonicalization** is what makes cross-source comparison work: the same lift
is named differently across apps, so each raw name resolves to one canonical
exercise (exact, normalized, or seeded alias). Without it, comparisons
silently split a lift into two series.

## 4. Query-time analytics

Pure functions over rows (`analytics.py`). Two strength representations are
kept distinct: the literal weight lifted, and an estimated 1RM (RIR-adjusted
Epley, only from sets of 12 reps or fewer, always labeled an estimate). Trends
use a best-per-session series with a deadband so noise is not read as signal.
Consistency counts distinct training days, bridging away periods.

## 5. Agent runtime

- `agent/mcp_server.py`: the tool surface, as an MCP server over stdio. It
  only wraps library functions. Read tools open the DB with `mode=ro`.
- `agent/coach.py`: a Pydantic-AI `Agent` with no tools of its own. It
  discovers tools over MCP. The model is `COACH_MODEL`, so swapping providers
  is an env var change plus the provider's API key.
- `agent/service.py`: FastAPI app exposing `/v1/chat/completions` (streaming
  and non-streaming), `/v1/models` and `/health`. The MCP subprocess is
  spawned once at startup and reused. Retries on empty or transient failures.
  Request-supplied system messages are dropped; the agent owns its prompt.
- `persona-system-prompt.md`: the system prompt, loaded at startup.

## 6. Security posture

Capability scoping, not prompt wording, is the boundary.

- Tools can touch only `training.db` and two hardcoded markdown filenames in
  the data dir. No shell, no network, no path from model input.
- Read tools open the DB read-only, so they cannot write even if a tool bug
  or a prompt injection tried.
- Writes are small and bounded: away periods, coaching notes, `merge_exercises`
  and `remove_import`. The last two are destructive and sit behind a two-step
  gate enforced in code (`strength_coach/confirm.py`, wired in
  `agent/mcp_server.py`):
  1. Called without a `confirm_token`, the tool opens the DB read-only, writes
     nothing, and returns a preview of exactly what would change (row ids and
     counts) plus a token.
  2. Called again with the same arguments and the token, the tool re-plans on
     a read-only connection and checks the token before any write connection
     is opened. A token is random (`secrets`), stored only in the server
     process, single use, expires after `COACH_CONFIRM_TTL_S` (default 600),
     and is bound to the tool, a SHA-256 of the exact arguments, and a
     fingerprint of the previewed row ids. Missing, unknown, expired, reused,
     mismatched or stale (rows changed since the preview) all refuse without
     writing. Any attempt burns the token.
  Every preview, rejection and apply appends a line to `audit.jsonl`
  (`COACH_AUDIT_LOG`). Restarting the MCP server invalidates outstanding tokens.
  The gate guarantees a preview always happens and the apply matches it. It
  does not prove a human approved: the agent relays the user's yes, so showing
  the preview and waiting for that yes is a persona-level control. A fully
  hijacked model can still make the two calls back to back.
- Untrusted text (exercise names, workout labels, filenames, notes) flows into
  tool output the model reads. Every read tool wraps its output in one
  `untrusted_data` envelope with a fixed notice, and `</` in the payload is
  rewritten to `<\/` (still valid JSON) so content cannot close the envelope.
  That is a delimiting convention. The persona tells the model to treat the
  envelope as data, which is prompt-level; the offline injection tests and the
  `injection_no_destructive_call` eval cover it.
- Training data is never written by the model. The importer is deterministic
  code.
- Secrets (model API keys) come from the environment and are never in the
  repo. `.env` files are git-ignored; `.env.example` has placeholders only.

Worst case for a misbehaving model: a bad coaching note, a bad merge, or a
removed import. All are visible and recoverable by re-importing the source
export.

## 7. Operations

Tests (Python 3.10+, `pip install -r requirements-dev.txt`):

```
python -m pytest -q
```

Generate demo data and run the agent:

```
python scripts/make_synthetic_data.py --out ./demo-data
export COACH_DATA_DIR=./demo-data
cp .env.example .env   # fill in a model API key
set -a; . ./.env; set +a
cd agent && uvicorn service:app --port 8000
```

Backfill from real exports (add `--reset` to rebuild; this deletes the db file,
including away periods and coaching notes):

```
python scripts/backfill.py --db "$COACH_DATA_DIR/training.db" path/to/export.xlsx
```

## 8. Known limitations

- **Human confirmation** on `remove_import` and `merge_exercises` is a prompt
  instruction. The code enforces preview-then-apply with a server-side token,
  not that a person said yes.
- **Hosted models intermittently return empty turns** under load. The service
  retries; a streamed response that already emitted text cannot be retried.
- **Consistency labels:** `current_streak_weeks` anchors to the last training
  week, not today. Pair it with `days_since_last`.
- **Dual-implement classification** is name-based. Extend the marker and
  exclusion sets when new exercises appear.
- **Evals call a live model** and cost money per run. They are lenient
  keyword and number-range checks, not exact matches.
- Nutrition is out of scope (no adapter, no schema).

## 9. Design decision log

- **SQLite over flat JSON.** The requirement is slicing across many records
  (cross-year comparison) with a single writer. SQLite gives real querying as
  one portable file, with no server to run.
- **Neutral store over per-app schema.** Designing around one app's schema
  makes every other source fight the model. Sources are additive adapters.
- **An owned agent runtime instead of chat-UI middleware.** The first version
  ran as a tool inside a chat UI, which owned the tool-calling loop. That broke
  in two ways. With the provider's native function-calling, the UI dropped a
  provider-specific field that must be replayed on the tool-result turn, so
  every tool call returned an empty response. With the UI's prompt-based
  fallback, the model had to plan all tool calls in one generation and could
  not see one result before choosing the next, which pushed every tool to
  return a complete answer in one call. Both are constraints of middleware
  the project did not control, and the failure mode (a silently blank answer)
  was hard to debug and could be triggered by a UI setting change. Moving to
  Pydantic-AI plus an MCP tool server put the loop, the retries, the logging
  and the model choice in code that has tests and evals. The chat UI is now an
  optional client of an OpenAI-compatible endpoint.
- **Tools as an MCP server.** The tool surface is a standard protocol, usable
  by the agent here or by any other MCP client, and it can be tested without a
  model.
- **Dual-implement lifts: park and flag.** Dumbbell and dual-cable lifts can
  be logged per hand or as combined load, and the convention cannot be
  recovered per row. Within one period the numbers are real and are kept; only
  cross-year comparison is unreliable. `is_dual_implement` flags these lifts,
  `trend` returns `not_comparable` with no cross-year percentage, and the
  caveat text rides along in tool output. The adapter stays faithful to the
  source value.
- **Consistency stays strength-only by default.** Counting cardio in the
  headline consistency number would inflate the metric the feature exists to
  keep honest. `activity_types` defaults to strength; `get_context` reports
  strength-only and all-activity side by side.
- **No synthetic readiness score.** The wearable data does not provide one, and
  inventing a composite would be false precision. The coach reasons from the
  raw sleep, resting HR and HRV numbers and says what it sees.
- **Wearable strength-training records are excluded from cardio.** A watch's
  auto-detected lifting session duplicates one already logged in the strength
  app, so ingesting both would double-count a training day. (The wearable
  connectors are not part of this public copy; the rule is documented because
  the schema and tools assume it.)
