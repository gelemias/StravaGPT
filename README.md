# Intervals.icu MCP server (+ legacy stravaGPT)

An MCP server that writes planned workouts onto your [Intervals.icu](https://intervals.icu)
calendar, so an assistant can plan a training week and push it straight to your
watch through the Intervals.icu → COROS sync.

The repository still contains the original stravaGPT ChatGPT Actions backend.
The two are independent: everything under `app/intervals/` plus
`app/mcp_server.py` has no imports from the Strava modules, so the Strava side
can be deleted without touching the MCP server.

## Tools

| Tool | What it does | Endpoint |
| --- | --- | --- |
| `push_workouts` | Creates or updates planned workouts. Idempotent: the upsert matches on `external_id`, so re-pushing the same id updates instead of duplicating. | `POST /athlete/{id}/events/bulk?upsert=true` |
| `list_events` | Reads the calendar between two dates. | `GET /athlete/{id}/events?oldest=&newest=` |
| `delete_events` | Deletes events by `external_id` or by numeric `id`. | `PUT /athlete/{id}/events/bulk-delete` |
| `get_sport_settings` | Reads thresholds and zones, to validate targets before writing them. | `GET /athlete/{id}/sport-settings` |

`push_workouts` also accepts `dry_run=true`, which returns the generated
Intervals.icu syntax and the estimated duration **without** calling the API. Use
it to iterate on a workout before writing anything to the calendar.

## Prerequisites for the COROS sync

The MCP server writes to Intervals.icu; Intervals.icu is what talks to your
watch. Configure these once, in Intervals.icu itself — no code involved:

1. **Settings → Connections**: link COROS and enable **Upload planned workouts**.
2. **Settings → Sport Settings**: set your **threshold pace** for Run. Without it
   COROS cannot build pace targets on the watch, and `moving_time` estimates for
   distance-based steps fall back to a rough default.

## Setup

1. Create a personal API key in Intervals.icu: **Settings → Developer**.
2. Copy the environment template and fill in the key:

```bash
cp .env.example .env
```

```env
INTERVALS_API_KEY=your-intervals-api-key
```

The key is only ever read from the environment. It is never logged, never
returned by a tool and never written to the repository (`.env` is gitignored).

3. Install and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The MCP endpoint is now at `http://localhost:8000/mcp`.

4. Verify the whole chain with the smoke test before loading a real week. It
   pushes ONE throwaway workout, checks it landed, deletes it and checks it is
   gone:

```bash
python scripts/smoke_intervals.py
python scripts/smoke_intervals.py --date 2026-07-29 --keep   # leave it on the calendar
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `INTERVALS_API_KEY` | – | **Required.** Personal API key. |
| `INTERVALS_ATHLETE_ID` | `0` | `0` means "the athlete who owns the key". |
| `INTERVALS_MAX_FUTURE_DAYS` | `7` | How far ahead `push_workouts` accepts dates. |
| `MCP_API_KEY` | – | Protects `/mcp`. Sent as `X-API-Key` or `Authorization: Bearer`. |
| `MCP_PATH_TOKEN` | – | Serves MCP at `/mcp/<token>` instead, for clients that cannot send headers. |
| `PUBLIC_BASE_URL` | – | **Required when deployed.** See the note on HTTP 421 below. |
| `MCP_ALLOWED_HOSTS` | localhost + `PUBLIC_BASE_URL` host | Comma-separated `Host` allow-list. `*` disables the check. |
| `INTERVALS_BASE_URL` | `https://intervals.icu/api/v1` | Override the API base URL. |
| `INTERVALS_TIMEOUT_SECONDS` | `30` | HTTP timeout. |
| `INTERVALS_TRUST_ENV` | `false` | Set to `true` to honour `HTTPS_PROXY` and friends. |

## The workout input format

You write this; the server converts it to the native Intervals.icu syntax.

```json
{
  "date": "2026-07-29",
  "sport": "Run",
  "name": "Umbral 4x2km",
  "target_type": "pace",
  "external_id": "w2-wed",
  "steps": [
    {"type": "warmup", "duration": "15min", "target": "easy"},
    {"type": "interval", "repeat": 4,
     "work": {"distance": "2000m", "target": "4:00-4:02/km"},
     "recovery": {"duration": "2min", "target": "easy"}},
    {"type": "cooldown", "duration": "10min", "target": "easy"}
  ]
}
```

becomes

```text
- 15m 70% Warmup

4x
- 2000m 4:00-4:02/km
- 2m 70%

- 10m 70% Cooldown
```

### Fields

- `date` — `YYYY-MM-DD`. `start_time_local` defaults to `00:00:00`.
- `target_type` — `"pace"` or `"hr"`, **exactly one per workout** (see the COROS
  constraints below). It sets the event's `target` to `PACE` or `HR`.
- `external_id` — your own primary key; the upsert matches on it.
- `sport` — an Intervals.icu activity type such as `Run` or `Ride`. Casing is
  normalized, unknown values are passed through.
- `steps[].type` — `warmup`, `cooldown`, `interval`, `work`, `steady`,
  `recovery`, `rest`. Only `interval` takes `repeat`/`work`/`recovery`; every
  other type takes exactly one of `duration` or `distance`.
- `notes` — optional free text appended after the steps.
- `moving_time` — optional override, in seconds, of the estimated duration.

### Durations, distances and targets

- **Duration**: `15min`, `15m`, `90s`, `1h30m`, `2:30`, `1:00:00`. A bare number
  is rejected on purpose, because `"15"` is ambiguous.
- **Distance**: `2000m`, `2km`, `5k`, `1mi`.
- **Target**, any of:
  - a percentage of threshold: `75%`, `95-100%` (valid for both target types),
  - an absolute pace: `4:00/km`, `4:00-4:02/km`, `6:26/mi`,
  - a heart rate: `150bpm`, `150-155bpm`,
  - a name: `rest`, `recovery`, `easy`, `endurance`, `steady`, `marathon`,
    `tempo`, `threshold`, `interval`, `vo2max`, `repetition`, `sprint`.

Names resolve to a percentage of threshold, and the mapping differs per target
type (`easy` is `70%` of threshold pace but `68%` of LTHR). Those percentages are
**heuristics**: verify the first week against how you actually train, and tune
them in `PACE_ALIASES` / `HR_ALIASES` in `app/intervals/workout_dsl.py`.

`moving_time` is estimated from the steps: time-based steps count directly, and
distance-based steps are converted using the step's absolute pace, or your
threshold pace from Sport Settings, or a `5:00/km` fallback (which adds a
warning to the response).

## Domain constraints, enforced

Both come from the COROS transfer, not from Intervals.icu:

1. **Only about 7 days of planned workouts reach the watch.** `push_workouts`
   rejects the whole batch when a date is in the past or more than
   `INTERVALS_MAX_FUTURE_DAYS` ahead, naming each offending workout. Pass
   `allow_beyond_coros_window=true` to store them in Intervals.icu anyway; the
   dates then come back as warnings instead of an error.
2. **Never mix pace and HR targets in one workout.** COROS forces every block to
   the intensity type of the first one, so a `150bpm` target inside a
   `target_type: "pace"` workout is rejected, naming the workout, the step number
   and the offending value. Percentages are neutral and work for both.

`external_id` values must also be unique within a batch, otherwise the upsert
would overwrite itself.

## Deploy

Same method as before — Render Free, one service for both the MCP server and
the legacy Strava API:

```bash
# Build command
pip install -e .
# Start command
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Render environment variables:

```env
PYTHON_VERSION=3.12.0
INTERVALS_API_KEY=your-intervals-api-key
MCP_API_KEY=your-long-random-secret
PUBLIC_BASE_URL=https://your-service-name.onrender.com
```

### If every request answers HTTP 421 Misdirected Request

The MCP SDK blocks unknown `Host` headers to prevent DNS rebinding attacks, and
its built-in allow-list is localhost only. Set `PUBLIC_BASE_URL` to your
deployed URL (or list the hostnames in `MCP_ALLOWED_HOSTS`). `GET /health`
reports the active allow-list under `mcp.allowed_hosts`, so you can check what
the running service accepts.

### Serving only the MCP server

Once stravaGPT is retired, drop `app/config.py`, `app/storage.py`,
`app/strava.py`, `app/main.py` and their tests, and start the standalone app:

```bash
uvicorn --factory app.mcp_server:build_standalone_app --host 0.0.0.0 --port $PORT
```

## Add the server to Claude

### Claude Code

```bash
claude mcp add --transport http intervals-icu https://your-service-name.onrender.com/mcp \
  --header "X-API-Key: your-long-random-secret"
```

Locally, use `http://localhost:8000/mcp`. Check it with `/mcp` inside Claude Code.

### Claude.ai / Claude Desktop custom connector

Add a custom connector pointing at `https://your-service-name.onrender.com/mcp`.
Custom connectors cannot send arbitrary headers, so `MCP_API_KEY` is not usable
there. Set `MCP_PATH_TOKEN` to a long random string instead and register the
resulting URL, which keeps the secret in the path:

```env
MCP_PATH_TOKEN=8f3c1d9a4b7e2f60
```

```text
https://your-service-name.onrender.com/mcp/8f3c1d9a4b7e2f60
```

Treat that URL as a credential. Both mechanisms can be enabled at once.

### Suggested workflow

Ask for a dry run first, then push:

> Plan my next 7 days of running and show me the Intervals.icu syntax with
> `dry_run` before writing anything.

## Tests

```bash
pytest                       # everything
pytest tests/test_workout_dsl.py tests/test_intervals_rules.py tests/test_mcp_endpoint.py
```

The Intervals.icu tests never touch the network: the converter and the domain
rules are unit tested, and `tests/test_mcp_endpoint.py` drives a real MCP
handshake over HTTP against the app. Only `scripts/smoke_intervals.py` talks to
the live API.

`tests/test_chatgpt_openapi.py::test_duplicate_leading_slashes_are_normalized`
fails unless `CHATGPT_API_KEY` is set in the environment. That is a pre-existing
failure in the legacy Strava tests.

---

# Legacy: stravaGPT

A FastAPI backend that connects to Strava, syncs activities into SQLite or
Turso, and exposes a hand-written OpenAPI schema for a ChatGPT Action. It is
unchanged and independent of the MCP server above.

## Strava setup

1. Create a Strava API app at [Strava API Settings](https://www.strava.com/settings/api).
2. Set the app callback domain to `localhost`.
3. Fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
4. Open `http://localhost:8000/auth/login`, then sync:

```bash
curl -X POST "http://localhost:8000/activities/sync?max_pages=3&since_latest=false"
```

After the first backfill, the default sync mode only fetches activities newer
than the latest local activity:

```bash
curl -X POST "http://localhost:8000/activities/sync"
```

## Strava API

- `GET /health` — service, database and MCP health.
- `GET /auth/login` — redirects to Strava OAuth.
- `GET /auth/callback` — stores OAuth tokens after authorization.
- `POST /activities/sync?max_pages=3` — fetches recent activities.
- `GET /activities?limit=20` — returns synced activities.
- `GET /training/summary?days=30` — distance, time and elevation totals.
- `GET /training/context?days=30&recent_limit=20` — a ChatGPT-friendly context packet.
- `GET /chatgpt/openapi.json` — minimal OpenAPI schema for ChatGPT Actions.

## Startup sync

Enabled by default, and only runs if Strava is already authorized. It is
incremental: it asks Strava for activities after the latest one stored locally.

```env
SYNC_ON_STARTUP=true
STARTUP_SYNC_MAX_PAGES=1
STARTUP_SYNC_PER_PAGE=30
```

## Turso storage

```env
TURSO_DATABASE_URL=libsql://your-db-your-org.turso.io
TURSO_AUTH_TOKEN=your-turso-token
```

When `TURSO_DATABASE_URL` is set, the app uses Turso instead of the local
`DATABASE_PATH` SQLite file.

```bash
turso db create stravagpt
turso db show --url stravagpt
turso db tokens create stravagpt
```

## Connect to ChatGPT

1. Expose the API over HTTPS, for example `ngrok http 8000`.
2. Set `PUBLIC_BASE_URL` to the HTTPS URL and restart.
3. In the GPT editor, import the Action schema from
   `https://your-tunnel-url/chatgpt/openapi.json`.

If `CHATGPT_API_KEY` is set, configure the Action authentication as API key; the
schema expects it in the `X-API-Key` header.

## Notes

Credentials and activities are stored locally. Keep `.env`, `token.json` and
database files private — all three are gitignored.

The Strava client uses the operating system trust store when `truststore` is
installed, then falls back to Certifi. Inspect what the running service uses at
`GET /debug/tls`.
