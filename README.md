# stravaGPT

A small FastAPI backend that connects to your Strava account, syncs activities into SQLite, and exposes local JSON endpoints that can later power ChatGPT training questions.

## What This Gives You

- Strava OAuth login and callback handling.
- Automatic access-token refresh using the stored refresh token.
- Activity sync from Strava into local SQLite or Turso/libSQL.
- Local API endpoints for synced activities and simple training summary stats.

## Setup

1. Create a Strava API app at [Strava API Settings](https://www.strava.com/settings/api).
2. Set the app callback domain to `localhost`.
3. Copy the environment template:

```bash
cp .env.example .env
```

4. Fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
   Optionally set `CHATGPT_API_KEY` before exposing the service outside your machine.
5. Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

6. Run the API:

```bash
uvicorn app.main:app --reload
```

7. Open the Strava authorization URL:

```bash
open http://localhost:8000/auth/login
```

After the callback succeeds, sync activities:

```bash
curl -X POST "http://localhost:8000/activities/sync?max_pages=3&since_latest=false"
```

After the first backfill, the default sync mode only fetches activities newer than the latest local activity:

```bash
curl -X POST "http://localhost:8000/activities/sync"
```

## API

- `GET /health` - service and database health.
- `GET /auth/login` - redirects you to Strava OAuth.
- `GET /auth/callback` - stores Strava OAuth tokens after authorization.
- `POST /activities/sync?max_pages=3` - fetches recent Strava activities.
- `GET /activities?limit=20` - returns synced activities.
- `GET /training/summary?days=30` - returns basic distance, time, and elevation totals.
- `GET /training/context?days=30&recent_limit=20` - returns a ChatGPT-friendly context packet.
- `GET /chatgpt/openapi.json` - returns a minimal OpenAPI schema for ChatGPT Actions.

## Startup Sync

The service can refresh recent activities whenever it starts. This is enabled by default and only runs if you have already authorized Strava.

```env
SYNC_ON_STARTUP=true
STARTUP_SYNC_MAX_PAGES=1
STARTUP_SYNC_PER_PAGE=30
```

Startup sync is incremental: it asks Strava for activities after the latest activity already stored locally.

## Turso Storage

For a free hosted database, create a Turso database and set these environment variables:

```env
TURSO_DATABASE_URL=libsql://your-db-your-org.turso.io
TURSO_AUTH_TOKEN=your-turso-token
```

When `TURSO_DATABASE_URL` is set, the app uses Turso instead of the local `DATABASE_PATH` SQLite file.

Typical Turso CLI flow:

```bash
turso db create stravagpt
turso db show --url stravagpt
turso db tokens create stravagpt
```

On Render Free, keep the same build and start commands:

```bash
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Then set the Render environment variables:

```env
PYTHON_VERSION=3.12.0
TURSO_DATABASE_URL=libsql://your-db-your-org.turso.io
TURSO_AUTH_TOKEN=your-turso-token
PUBLIC_BASE_URL=https://your-service-name.onrender.com
STRAVA_REDIRECT_URI=https://your-service-name.onrender.com/auth/callback
CHATGPT_API_KEY=your-long-random-secret
STRAVA_CLIENT_ID=your-strava-client-id
STRAVA_CLIENT_SECRET=your-strava-client-secret
```

## Connect to ChatGPT

The simplest path is a Custom GPT Action:

1. Start the API locally with `uvicorn app.main:app --reload`.
2. Expose it with an HTTPS tunnel, for example `ngrok http 8000`.
3. Set `PUBLIC_BASE_URL` in `.env` to the HTTPS tunnel URL and restart the API.
4. In the GPT editor, add an Action and import the schema from:

```text
https://your-tunnel-url/chatgpt/openapi.json
```

If `CHATGPT_API_KEY` is set, configure the Action authentication as API key and use the same value. The schema expects the key in the `X-API-Key` header.

Ask your GPT to call `getTrainingContext` before answering questions about your training.

## Notes

This project stores credentials and activities locally in SQLite. Keep `.env`, `token.json`, and database files private.

If OAuth fails with a local certificate error, reinstall the project dependencies inside the virtualenv:

```bash
pip install -e ".[dev]"
```

The Strava client uses the operating system trust store when `truststore` is installed, then falls back to Certifi. This helps on networks that inspect TLS traffic with a locally trusted certificate authority, without disabling TLS checks. You can inspect what the running service is using at `GET /debug/tls`.
