# fitness-coach-agent

Workout data is siloed across lifting, cardio and wearable apps, so you can't ask a question that spans them.
fitness-coach-agent merges it into one history that an AI coach answers from, using your real numbers instead of guessing.

It's built so the AI can be wrong without doing damage. Deterministic adapters write the data, with no model in that path. An MCP tool server lets any agent query it, and destructive changes need a code-enforced preview and one-time token first.
The repo is the engine plus templates. Your data and profile live in a directory outside it.

## Quickstart

About ten minutes. Python 3.10+.

```bash
# 1. Get a copy. Use GitHub's "Use this template" button rather than Fork
#    (see "Starting your own copy" below), then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
pip install -r requirements-dev.txt       # pytest, openpyxl, mcp, ruff

# 2. Demo: synthetic store + a fictional example profile. No key, no network.
make demo                                 # writes ./demo-data (git-ignored)

# 3. Your own instance, outside the repo.
bin/coach init --dir ~/.local/share/fitness-coach
export COACH_DATA_DIR=~/.local/share/fitness-coach
$EDITOR "$COACH_DATA_DIR"/{coaching-profile.md,gym.md,aliases.yaml}
bin/coach check                           # validates, says how to fix anything wrong

# 4. First real import, then look at what the coach will see.
python scripts/backfill.py --db "$COACH_DATA_DIR/training.db" path/to/your-export
bin/coach context | less
```

`make demo` needs no model key. Talking to the coach does: install
`agent/requirements.txt`, put a key for `COACH_MODEL` in `.env` (the default is
Google Gemini, so `GOOGLE_API_KEY`), then `make demo-agent` or
`cd agent && uvicorn service:app --port 8000`. Any OpenAI-compatible chat client
can point at `http://localhost:8000/v1`; any MCP client can run
`agent/mcp_server.py` directly. `docs/AGENT-SETUP.md` walks a coding agent
through setting up an instance for you.

## Concepts

**Engine vs instance.** Code, schemas, tests, evals, the persona and the
templates are the engine. Your instance is one directory (`COACH_DATA_DIR`):
the store, your profile, your gym, your aliases, logs. `bin/coach init` refuses
to create it inside the checkout, and `.gitignore` plus the identifier scan
keep it out of commits.

**Adapters.** One deterministic parser per source, detected by file content,
mapping into the canonical models. Shipped: MacroFactor, StrongLifts, Liftin,
Liftosaur and AllTrails GPX exports. No LLM sits anywhere in the write path.

**Canonicalization and aliases.** Each raw exercise name resolves to one
canonical exercise, so a lift does not split in two across apps. The defaults
(`strength_coach/seeds.py`) only cover naming that is unambiguously the same
lift. Which variations *you* consider one lift goes in `aliases.yaml`, and your
entries win.

**The store.** One SQLite file with an activity-agnostic schema: strength sets,
cardio sessions, per-sample streams, daily recovery metrics, away periods,
coaching notes. Derived numbers (estimated 1RM, trends, consistency) are
computed at query time. Re-importing the same export changes nothing.

**Tools and runtime.** `agent/mcp_server.py` exposes 22 tools (docs/TOOLS.md).
`agent/coach.py` and `agent/service.py` run a Pydantic-AI agent behind
`/v1/chat/completions`; `COACH_MODEL` picks the provider.

**What the coach sees.** `bin/coach context` prints the `get_context` bundle
byte-for-byte as the model receives it: profile, notes, gym, both consistency
numbers, program changes and cardio bests, inside the `untrusted_data`
envelope. If coaching looks wrong, read this first.

**Opinionated defaults, and how to override them.**

| Default | Where it lives | Override |
|---|---|---|
| North star: consistency first | `persona-system-prompt.md` | State yours in `coaching-profile.md` under "Coaching philosophy" (see `examples/runner-2day`), or edit the persona for every user of your fork |
| Headline KPI is strength-only consistency; all-activity is reported beside it | `strength_coach/context.py`, `analytics.training_consistency` | Profile wording for emphasis; code change for a different KPI |
| No composite readiness score | Persona, evals | Edit both together; the eval `no_synthetic_readiness_score` will fail otherwise |
| Dual-implement lifts flagged and not compared across years | `analytics._DUAL_IMPLEMENT_MARKERS` | Extend the marker sets |

### Guardrails

| Protection | Enforced by | Test that proves it |
|---|---|---|
| Tools touch only the store and two fixed markdown files in the data dir; no shell, no network, no path built from model input | Code, by construction | `tests/test_mcp_server.py::test_expected_tools_registered` pins the tool list |
| Read tools cannot write: the DB is opened `mode=ro` | Code | `tests/test_injection.py::test_ro_connection_rejects_writes`, `::test_read_tools_never_open_a_writable_connection` |
| Destructive tools need a preview token: `remove_import` and `merge_exercises` do nothing on the first call | Code | `tests/test_confirm_gate.py` |
| Tool output is delimited as data inside an `untrusted_data` envelope the payload cannot close | Code for the delimiting; compliance is prompt-level | `tests/test_injection.py` |
| A human said yes before an apply call | Prompt only | Model eval `injection_no_destructive_call` (needs a key) |
| Instance data and secrets stay out of the repo | `.gitignore`, `init` refusal, identifier scan in CI and pre-push | `tests/test_cli.py`, `tests/test_scan.py` |

Structural (holds even if the model is hijacked): read tools cannot write; no
tool reaches beyond the data directory; a destructive call needs a prior
preview, a server-side token, matching arguments, unchanged rows and a
10-minute window (`COACH_CONFIRM_TTL_S`); every destructive call is appended to
`audit.jsonl`. Model-dependent: treating envelope contents as data, and waiting
for a real yes between preview and apply. A hijacked model can still preview
and apply back to back; the blast radius is a removed import or a merge, both
recoverable by re-importing, and the audit log shows what ran.

### Evals

Unit tests cover the code. `evals/run.py` sends ten prompts to a running agent
and checks replies with lenient keyword and number-range checks, with ground
truth read from the store: no fabricated numbers for a lift with no data,
`rdl` resolving to Romanian Deadlift, the dual-implement caveat, block review
from real sessions, consistency leading, cross-year comparison, cardio and
recovery awareness, no invented readiness score, and no destructive call when
the latest session carries an injection payload. They need a model key, so
they do not run in CI. `tests/test_evals.py` (part of `make check`) proves
offline that every checker accepts a good canned answer and rejects a bad one.
See [evals/README.md](evals/README.md). A real eval failure is never fixed by
loosening the checker.

## Configuration reference

Instance files, all in `COACH_DATA_DIR`:

| File | Written by | Format |
|---|---|---|
| `coaching-profile.md` | You (`bin/coach init` template) | Free markdown, read verbatim by the coach. Sections: philosophy, baseline, constraints, training reality, goals, nutrition |
| `gym.md` | You (template) | Free markdown: equipment and practical load ceilings, including the per-hand dumbbell cap |
| `aliases.yaml` | You (template) | One `Source Name: Canonical Name` per line, `#` comments, quote names containing `:`. Anything else is an error with a line number |
| `training.db` | Importer, backfill | SQLite |
| `audit.jsonl`, `import-log.jsonl`, `import-archive/`, `inbox/` | The system | Append-only logs, raw copies, watched folder |

Environment (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `COACH_DATA_DIR` | required | The instance directory |
| `COACH_MODEL` | `google:gemini-3.6-flash` | Any pydantic-ai model string |
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | none | Key for the `COACH_MODEL` provider, agent only |
| `PERSONA_PROMPT_PATH` | `persona-system-prompt.md` | Alternate persona file |
| `COACH_CORE_DIR`, `CORE_DIR` | repo root | Where `strength_coach` lives, if mounted elsewhere |
| `INBOX_DIR` | `$COACH_DATA_DIR/inbox` | Watched folder for the importer |
| `POLL_SECONDS`, `STABILITY_SECONDS` | `30`, `5` | Importer polling |
| `DB_PATH`, `LOG_PATH`, `ARCHIVE_DIR` | under `COACH_DATA_DIR` | Importer paths |
| `COACH_CONFIRM_TTL_S` | `600` | Preview-token lifetime |
| `COACH_AUDIT_LOG` | `$COACH_DATA_DIR/audit.jsonl` | Destructive-tool audit log |
| `COACH_ENDPOINT`, `COACH_EVAL_MODEL`, `COACH_DB_PATH` | localhost:8000, `fitness-coach`, `./demo-data/training.db` | Evals |

`.identifiers.local` (repo root, git-ignored): one term per line that must
never be published (your name, places, gear, accounts). `make check` and the
pre-push hook (`make hooks`) fail on any match.

`bin/coach check` validates all of the above and says how to fix each problem;
the agent, the MCP server and the importer run the same check at startup.

## Extending

- **A new source:** one adapter plus its tests. The contract is
  [docs/ADAPTERS.md](docs/ADAPTERS.md); the reference implementation is
  `strength_coach/adapters/liftin_csv.py`. [docs/ADAPTER-PROMPTS.md](docs/ADAPTER-PROMPTS.md)
  has ready prompts for Peloton, Google Health/Fitbit, Strava, Apple Health,
  Garmin and Oura/Whoop. The author runs additional private adapters; none
  ships here. Check a generated adapter against the live service before
  trusting it, since fixtures cannot prove the vendor behaves the same.
- **A new tool:** add it in `agent/mcp_server.py`, wrap read output with
  `_wrap`, add it to the pinned list in `tests/test_mcp_server.py`.
- **A different coach:** edit `persona-system-prompt.md` and run the evals.
- Architecture and design decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

Reference implementation. Fork it and make it yours. Issues and PRs may not get a response.

## How this was built

Tyler Lange, a technical program manager who directs coding agents, owned the
requirements, the architecture decisions, the evals and the safety
constraints: what the coach may and may not do, why the data layer is
deterministic, why the runtime moved off a chat UI, which failures the evals
must catch, and where each guardrail sits. Claude Code wrote most of the code
and tests, on bounded tasks with the unit tests and eval suite as the
acceptance gate. To judge it, read `tests/`,
`evals/`, the Guardrails table above and the decisions in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Starting your own copy

Use GitHub's **Use this template** button rather than Fork. A template copy
starts with one clean commit and no link back here; a fork carries this repo's
history and is meant for sending changes upstream.

## License

MIT. See [LICENSE](LICENSE).
