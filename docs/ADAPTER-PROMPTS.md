# Adapter prompts

Copy-paste prompts for building a new source adapter with a coding agent
(Claude Code, Codex or similar), run from the repo root. Each is
self-contained and follows the contract in [ADAPTERS.md](ADAPTERS.md).

Read the diff and run `make check` yourself before trusting the result. The
prompts make the agent verify against fixtures, and no fixture proves the real
service behaves the same. Vendor API details below were current when written
and change; each prompt tells the agent to check current documentation. Check the result against
the live service before trusting it.

## Peloton

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: Peloton workouts (rides, runs, strength classes) through a client library or the
member API you can reach. Peloton has no official public API for members, so check what
is current, prefer a maintained library, and say in the module docstring that the
endpoint is unofficial and may change.

Build the adapter as follows:
- File: strength_coach/adapters/peloton_api.py with fetch(since, until, client) -> ParsedImport, and scripts/sync_peloton.py as the runner.
- Map cycling to activity_type "bike", running to "run", walking to "walk", strength and other classes to "strength" or "cardio" as appropriate. Class title goes in workout. Use the workout id as source_session_key.
- Session metrics: distance_m, calories, avg_hr, plus output (kJ) and avg cadence/resistance as extra named metrics if present. If the per-second performance graph is available, emit it as a stream on the session (see NormalizedSession.streams); otherwise skip it.
- Auth: username and password (or a session token) from PELOTON_USERNAME and PELOTON_PASSWORD, or a token file at PELOTON_TOKEN_FILE. Never store them in the repo. If login fails, raise, do not retry in a loop.
- Rate limits: no published numbers, so be conservative: at most 1 request per second, exponential backoff with jitter on 429 and 5xx, a retry cap of 5, and a per-run request budget. Page through history in slices of 50 workouts.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_peloton.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```

## Google Health / Fitbit

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: Google Health / Fitbit data (activities, daily resting heart rate, HRV, sleep).
Check which API is current (Google has been moving the Fitbit Web API toward the Google
Health API) and which OAuth scopes each metric needs. Document the choice in the module
docstring.

- File: strength_coach/adapters/google_health_api.py with fetch(since, until, client) returning (ParsedImport for activities, list of (date, metric, value, unit) for daily metrics), and scripts/sync_google_health.py as the runner that ingests both.
- Activities: map walk/run/hike/bike to the matching activity_type, other cardio to "cardio". Use the activity log id as source_session_key. Metrics: distance_m, elevation_gain_m, calories, avg_hr.
- Daily metrics: resting_hr (bpm), hrv_rmssd (ms), sleep_minutes_asleep (min), written with db.upsert_daily_metric under source "wearable" or "google_health". The sleep date is the date you wake up. State this in the docstring and test it across midnight.
- Auth: OAuth 2.0 authorization code flow done once by a separate script (scripts/auth_google_health.py) that writes the token file to the path in GOOGLE_HEALTH_TOKEN_FILE with mode 0600. The sync runner reads client id and secret from GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET, refreshes the access token before expiry, and writes a rotated refresh token back atomically. Never print or log tokens.
- Rate limits: honor Retry-After and any per-hour quota headers; exponential backoff with jitter, cap 5 retries, stop and report when the quota is spent. Fetch in 30-day windows.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_google_health.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```

## Strava

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: Strava activities through the official API (OAuth 2.0).

- File: strength_coach/adapters/strava_api.py with fetch(since, until, client) -> ParsedImport, and scripts/sync_strava.py as the runner.
- Use the summary activities list endpoint with pagination, and fetch the detail endpoint only when a field you need (calories, elevation) is missing from the summary.
- Map sport_type to activity_type: Run/TrailRun to "run", Walk to "walk", Hike to "hike", Ride/VirtualRide/GravelRide to "bike", WeightTraining to "strength" (no sets available, so emit the session with no sets and a duration), everything else to "cardio". Use the Strava activity id as source_session_key. Date is the activity start_date_local date.
- Metrics: distance_m, elevation_gain_m (total_elevation_gain), calories, avg_hr (average_heartrate). Convert nothing that is already meters and seconds.
- Auth: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET from the environment; a refresh token in the file at STRAVA_TOKEN_FILE (mode 0600). Strava access tokens last about six hours and the refresh response can return a new refresh token, so write it back atomically on every refresh. A one-time scripts/auth_strava.py performs the authorization code exchange. Request only the activity:read scope (activity:read_all only if the user needs private activities).
- Rate limits: Strava enforces per-15-minute and per-day limits, reported in response headers. Read those headers, sleep until the window resets when you are near the limit, back off with jitter on 429, and stop with a clear message when the daily budget is spent. Verify the current limits in Strava's documentation.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_strava.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```

## Apple Health export

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: the Apple Health export.zip a user creates on their phone (Health app, profile,
Export All Health Data). It contains export.xml, which can be hundreds of megabytes.

- File: strength_coach/adapters/apple_health_xml.py exposing SOURCE = "apple_health" and parse(path) -> ParsedImport for workouts, plus a function daily_metrics(path) returning (date, metric, value, unit) tuples. Register the file in strength_coach/adapters/detect.py by content: a zip that contains export.xml whose root element is HealthData. Never route by filename.
- Stream the XML with xml.etree.ElementTree.iterparse and clear elements as you go. Never load the whole file. Add a test that parses a fixture with a few thousand fake records without holding them all in memory (assert on behavior, not memory numbers).
- Workouts: use Workout elements. Map workoutActivityType values (running, walking, hiking, cycling, traditional strength training and so on) to activity_type with a lookup table; an unmapped type goes to "cardio" and is counted in the dry-run report. Convert totalDistance and durations using each element's unit attributes (km, mi, min, sec). source_session_key is the workout startDate plus activity type.
- Daily metrics from Record elements: the resting heart rate quantity type to resting_hr, the HRV SDNN quantity type to a clearly named hrv_sdnn (SDNN is not RMSSD, so do not store it as hrv_rmssd), sleep analysis intervals summed into sleep_minutes_asleep, counting only asleep stages. Aggregate per day with a documented rule (mean for HR/HRV, sum for sleep), in the device timezone from the record.
- Auth: none, it is a local file. Treat the export as private: the fixture must be entirely fake, the path comes from an argument or INBOX_DIR, and nothing from a real export is ever committed.
- Rate limits: not applicable, say so in the docstring. Dry-run prints workout counts by type and daily-metric counts by metric.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_apple_health.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```

## Garmin Connect

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: Garmin Connect activities and daily health data. Garmin's official Health and
Activity APIs are for approved business partners, so choose and document one of two
routes, and prefer the first:
1. The account data export (Garmin account, data management, export), which gives you files: activity FIT or TCX files and daily summary JSON or CSV. Build a file adapter.
2. An unofficial client library against Garmin Connect. If you choose this, state in the docstring that it is unofficial, may break, and may conflict with Garmin's terms, and keep it optional.

- File: strength_coach/adapters/garmin_export.py exposing SOURCE = "garmin" and parse(path) -> ParsedImport for activities, plus daily_metrics(path). Detect by file content (FIT header bytes, TCX root element, JSON keys), never by name. Parse FIT with a maintained library if one is available offline in the environment, otherwise TCX/CSV only, and say so.
- Activities: map Garmin activity types to activity_type with a lookup table (running, walking, hiking, cycling, strength_training, and so on). Use the Garmin activity id as source_session_key. Metrics: distance_m, elevation_gain_m, calories, avg_hr. Strength activities with set data become NormalizedSets only if exercise names and reps are present, otherwise a set-less session.
- Daily metrics: resting_hr, sleep_minutes_asleep, and hrv_rmssd only if Garmin reports RMSSD (its overnight HRV is a different statistic: store it under a distinct name such as hrv_overnight_avg and document it).
- Auth: route 1 needs none. Route 2 reads GARMIN_EMAIL and GARMIN_PASSWORD from the environment, or caches a session token in the file at GARMIN_TOKEN_FILE (mode 0600); handle MFA by failing with a clear message, never by prompting a model.
- Rate limits: for route 2, at most one request every two seconds, backoff with jitter on 429 and 5xx, retry cap 5, no parallel requests.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_garmin.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```

## Oura or Whoop

```text
You are adding one data source adapter to the repository in the current directory
(Compound). Read these first and follow them exactly:
- docs/ADAPTERS.md (the adapter contract: input, canonical output, idempotency keys, failure behavior, credentials, required tests)
- strength_coach/models.py (the canonical models: ParsedImport, NormalizedSession, NormalizedSet)
- strength_coach/adapters/liftin_csv.py and strength_coach/adapters/base.py (reference adapter and unit helpers)
- strength_coach/db.py (ingest, upsert_daily_metric) and tests/test_adapters.py (test patterns)

Source: Oura or Whoop recovery data. Pick one provider per adapter (build oura_api.py or
whoop_api.py, not a merged module) and tell me which you chose at the top of your reply.

- File: strength_coach/adapters/<provider>_api.py with fetch(since, until, client) returning (ParsedImport for workouts, list of (date, metric, value, unit) for daily metrics), and scripts/sync_<provider>.py as the runner.
- Daily metrics, written with db.upsert_daily_metric: resting_hr (bpm), hrv_rmssd (ms, only if the provider reports RMSSD; otherwise a distinct name and a documented caveat), sleep_minutes_asleep (min). Do NOT import a vendor readiness, recovery or strain score as a metric. This project deliberately has no composite readiness score; the coach reasons from raw signals. If you want the vendor score, store it under a clearly vendor-prefixed name and add nothing that surfaces it in tools.
- Workouts: map the provider's sport to activity_type with a lookup table, use the workout id as source_session_key, emit distance_m, calories and avg_hr where present. The sleep or cycle date is the date the person wakes up; state that in the docstring and test it across midnight.
- Auth: OAuth 2.0. Client id and secret from <PROVIDER>_CLIENT_ID and <PROVIDER>_CLIENT_SECRET, tokens in the file at <PROVIDER>_TOKEN_FILE (mode 0600), a one-time scripts/auth_<provider>.py for the authorization code exchange. Refresh tokens may rotate on use: write the new one back atomically before making the next call. Check the provider's current docs, since personal access token support and scopes have changed over time.
- Rate limits: read the provider's documented limits and rate-limit headers, honor Retry-After, backoff with jitter, retry cap 5, fetch in 30-day windows, and stop with a clear message when a quota is spent.

Requirements for every adapter:
- Emit only the canonical models. Weights in pounds, dates ISO, distances and elevation in meters, durations in seconds. Unknown values stay None.
- Set source_session_key to the vendor's stable id so re-import replaces, never duplicates.
- Fail loudly: raise on unexpected input or API errors, never substitute silent NULLs, write nothing on failure.
- No LLM calls anywhere in the adapter, the runner or the write path.
- Credentials come from environment variables or a secret file whose path is an environment variable, kept outside the repo. Add placeholder names to .env.example. Never log, print, commit or put a token in a fixture.
- Add a --dry-run flag that fetches and parses, prints counts by date and activity type, and writes nothing. Add --since YYYY-MM-DD.
- Tests in tests/test_<provider>.py, offline, using fake fixture data you write yourself: parse output, unit conversion, a malformed-input failure test, an idempotency test (ingest twice, data tables identical), an edit-propagation test. Mock all HTTP.
- Do not commit, do not add a remote, do not touch anything outside this repo.

Acceptance: make check passes, and a second import of the same fixture changes no row in the sessions, sets, session_metrics or daily_metrics tables. Report what you could not verify against the real service.
```
