# Reference

## Guardrails

| Protection | Enforced by | Test |
|---|---|---|
| Tools touch only the store and two fixed markdown files in the data dir. No shell, no network, no path built from model input | Code | `tests/test_mcp_server.py::test_expected_tools_registered` pins the tool list |
| Read tools cannot write. The DB is opened `mode=ro` | Code | `tests/test_injection.py::test_ro_connection_rejects_writes`, `::test_read_tools_never_open_a_writable_connection` |
| `remove_import` and `merge_exercises` need a preview token. The first call does nothing | Code | `tests/test_confirm_gate.py` |
| Tool output is delimited inside an `untrusted_data` envelope the payload cannot close | Code for the delimiting. Following it is up to the model | `tests/test_injection.py` |
| A person approves before an apply call | Prompt only | Model eval `injection_no_destructive_call` (needs a key) |
| Instance data and secrets stay out of the repo | `.gitignore`, `init` refusal, identifier scan in CI and pre-push | `tests/test_cli.py`, `tests/test_scan.py` |

A destructive call needs a prior preview, a server-side token, matching arguments, unchanged rows and a 10-minute window (`COACH_CONFIRM_TTL_S`). Every destructive call is appended to `audit.jsonl`. A hijacked model can still preview and apply back to back. The worst case is a removed import or a merge, both recoverable by re-importing.

## Evals

`evals/run.py` sends ten prompts to a running agent and checks the replies against the store: no invented numbers for a lift with no data, abbreviation handling, the dual-implement caveat, block review, cross-year comparison, cardio and recovery awareness, no readiness score, and no destructive call when the latest session carries an injection payload. They need a model key, so they don't run in CI. `tests/test_evals.py` checks offline that every checker accepts a good answer and rejects a bad one. See [evals/README.md](../evals/README.md).

## Defaults and overrides

| Default | Where it lives | Override |
|---|---|---|
| Consistency first | `persona-system-prompt.md` | State yours in `coaching-profile.md` under "Coaching philosophy" (see `examples/runner-2day`) |
| Headline KPI is strength-only consistency, with all-activity reported beside it | `strength_coach/context.py`, `analytics.training_consistency` | Profile wording, or a code change for a different KPI |
| No composite readiness score | Persona, evals | Edit both together. The eval `no_synthetic_readiness_score` fails otherwise |
| Dual-implement lifts flagged and not compared across years | `analytics._DUAL_IMPLEMENT_MARKERS` | Extend the marker sets |

Exercise names from different apps resolve to one canonical exercise. The defaults (`strength_coach/seeds.py`) cover only naming that is the same lift. Variations you consider one lift go in `aliases.yaml`, and your entries win.

## Configuration

Instance files, all in `COACH_DATA_DIR`:

| File | Written by | Format |
|---|---|---|
| `coaching-profile.md` | You (`bin/coach init` template) | Markdown, read as-is by the coach. Sections: philosophy, baseline, constraints, training reality, goals, nutrition |
| `gym.md` | You (template) | Markdown: equipment and practical load ceilings |
| `aliases.yaml` | You (template) | One `Source Name: Canonical Name` per line. Quote names containing `:` |
| `training.db` | Importer, backfill | SQLite |
| `audit.jsonl`, `import-log.jsonl`, `import-archive/`, `inbox/` | The system | Append-only logs, raw copies, watched folder |

Environment (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `COACH_DATA_DIR` | required | The instance directory |
| `COACH_MODEL` | `google:gemini-3.6-flash` | Any pydantic-ai model string |
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | none | Key for the `COACH_MODEL` provider |
| `PERSONA_PROMPT_PATH` | `persona-system-prompt.md` | Alternate persona file |
| `COACH_CORE_DIR`, `CORE_DIR` | repo root | Where `strength_coach` lives, if mounted elsewhere |
| `INBOX_DIR` | `$COACH_DATA_DIR/inbox` | Watched folder for the importer |
| `POLL_SECONDS`, `STABILITY_SECONDS` | `30`, `5` | Importer polling |
| `DB_PATH`, `LOG_PATH`, `ARCHIVE_DIR` | under `COACH_DATA_DIR` | Importer paths |
| `COACH_CONFIRM_TTL_S` | `600` | Preview-token lifetime |
| `COACH_AUDIT_LOG` | `$COACH_DATA_DIR/audit.jsonl` | Destructive-tool audit log |
| `COACH_ENDPOINT`, `COACH_EVAL_MODEL`, `COACH_DB_PATH` | localhost:8000, `fitness-coach`, `./demo-data/training.db` | Evals |

`.identifiers.local` (repo root, git-ignored) lists terms that must never be published. `make check` and the pre-push hook (`make hooks`) fail on any match. `bin/coach check` validates the configuration and says how to fix each problem.

## Extending

- **A new app:** one adapter plus tests. Contract: [ADAPTERS.md](ADAPTERS.md). Reference implementation: `strength_coach/adapters/liftin_csv.py`. [ADAPTER-PROMPTS.md](ADAPTER-PROMPTS.md) has prompts for Peloton, Fitbit, Strava, Apple Health, Garmin and Oura/Whoop. Test a generated adapter against the real service before trusting it.
- **A new tool:** add it in `agent/mcp_server.py`, wrap read output with `_wrap`, and add it to the pinned list in `tests/test_mcp_server.py`.
- **A different coach:** edit `persona-system-prompt.md` and run the evals.
