from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings
from app.storage import Storage
from app.strava import (
    StravaAuthRequiredError,
    StravaClient,
    StravaConfigurationError,
)


logger = logging.getLogger(__name__)


async def sync_on_startup(settings: Settings, storage: Storage) -> None:
    if not settings.sync_on_startup:
        return
    if storage.get_token() is None:
        logger.info("Skipping startup Strava sync because no token is stored yet.")
        return

    strava = StravaClient(settings, storage)
    after = storage.latest_activity_epoch()
    try:
        result = await strava.sync_activities(
            max_pages=settings.startup_sync_max_pages,
            after=after,
            per_page=settings.startup_sync_per_page,
        )
    except (StravaAuthRequiredError, StravaConfigurationError, httpx.HTTPError) as exc:
        logger.warning("Startup Strava sync failed: %s", exc)
        return
    except Exception:
        logger.exception("Unexpected startup Strava sync failure.")
        return
    logger.info("Startup Strava sync complete: %s", result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    storage = Storage(
        settings.database_path,
        turso_database_url=settings.turso_database_url,
        turso_auth_token=settings.turso_auth_token,
    )
    storage.init_db()
    startup_sync_task = asyncio.create_task(sync_on_startup(settings, storage))
    yield
    await startup_sync_task


app = FastAPI(title="stravaGPT", version="0.1.0", lifespan=lifespan)


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> Storage:
    return Storage(
        settings.database_path,
        turso_database_url=settings.turso_database_url,
        turso_auth_token=settings.turso_auth_token,
    )


def get_strava_client(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> StravaClient:
    return StravaClient(settings, storage)


def require_chatgpt_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if not settings.chatgpt_api_key:
        return

    expected = settings.chatgpt_api_key
    provided = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
        )


@app.get("/health")
def health(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> dict[str, object]:
    storage.init_db()
    return {
        "ok": True,
        "storage_backend": settings.storage_backend,
        "database_path": settings.database_path if settings.storage_backend == "sqlite" else None,
        "strava_configured": settings.strava_configured,
        "authorized": storage.get_token() is not None,
        "sync_on_startup": settings.sync_on_startup,
        "chatgpt_api_key_required": bool(settings.chatgpt_api_key),
    }


@app.get("/debug/tls")
def debug_tls(
    strava: Annotated[StravaClient, Depends(get_strava_client)],
) -> dict[str, object]:
    return strava.tls_info()


@app.get("/chatgpt/openapi.json", include_in_schema=False)
def chatgpt_openapi(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    base_url = settings.public_base_url or str(request.base_url).rstrip("/")
    security = [{"apiKeyAuth": []}] if settings.chatgpt_api_key else []
    components = {"schemas": {}}
    if settings.chatgpt_api_key:
        components["securitySchemes"] = {
            "apiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "stravaGPT Training API",
            "version": "0.1.0",
            "description": "Read synced Strava training data and trigger incremental activity syncs.",
        },
        "servers": [{"url": base_url}],
        "components": components,
        "paths": {
            "/training/context": {
                "get": {
                    "operationId": "getTrainingContext",
                    "summary": "Get training summary and recent activities",
                    "description": (
                        "Returns aggregate training metrics for a period plus the most recent "
                        "synced activities. Use this before answering training questions."
                    ),
                    "security": security,
                    "parameters": [
                        {
                            "name": "days",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 3660, "default": 30},
                            "description": "Number of days to summarize.",
                        },
                        {
                            "name": "recent_limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                            "description": "Number of recent activities to include.",
                        },
                    ],
                    "responses": {"200": {"description": "Training context returned."}},
                }
            },
            "/training/summary": {
                "get": {
                    "operationId": "getTrainingSummary",
                    "summary": "Get aggregate training summary",
                    "security": security,
                    "parameters": [
                        {
                            "name": "days",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 3660, "default": 30},
                            "description": "Number of days to summarize.",
                        }
                    ],
                    "responses": {"200": {"description": "Training summary returned."}},
                }
            },
            "/activities": {
                "get": {
                    "operationId": "listActivities",
                    "summary": "List synced Strava activities",
                    "security": security,
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                            "description": "Maximum number of activities to return.",
                        },
                        {
                            "name": "offset",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 0, "default": 0},
                            "description": "Number of activities to skip.",
                        },
                    ],
                    "responses": {"200": {"description": "Activities returned."}},
                }
            },
            "/activities/sync": {
                "post": {
                    "operationId": "syncActivities",
                    "summary": "Sync latest Strava activities",
                    "description": "Fetches activities from Strava and stores them locally.",
                    "security": security,
                    "parameters": [
                        {
                            "name": "max_pages",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
                            "description": "Maximum Strava result pages to fetch.",
                        },
                        {
                            "name": "since_latest",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": True},
                            "description": "When true, only fetch activities after the latest local activity.",
                        },
                    ],
                    "responses": {"200": {"description": "Sync result returned."}},
                }
            },
        },
    }


@app.get("/auth/login")
def login(
    storage: Annotated[Storage, Depends(get_storage)],
    strava: Annotated[StravaClient, Depends(get_strava_client)],
) -> RedirectResponse:
    try:
        state = storage.create_auth_state()
        return RedirectResponse(strava.authorization_url(state))
    except StravaConfigurationError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/auth/callback")
async def callback(
    strava: Annotated[StravaClient, Depends(get_strava_client)],
    storage: Annotated[Storage, Depends(get_storage)],
    code: str | None = None,
    state: str | None = None,
    scope: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if error:
        raise HTTPException(status_code=400, detail=f"Strava authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Strava authorization code or state.")
    if not storage.consume_auth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
    try:
        token = await strava.exchange_code(code)
    except (StravaConfigurationError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}") from exc
    return {
        "authorized": True,
        "athlete_id": token.athlete_id,
        "scope": token.scope or scope,
        "next": "POST /activities/sync",
    }


@app.post("/activities/sync")
async def sync_activities(
    _: Annotated[None, Depends(require_chatgpt_api_key)],
    strava: Annotated[StravaClient, Depends(get_strava_client)],
    storage: Annotated[Storage, Depends(get_storage)],
    max_pages: Annotated[int, Query(ge=1, le=20)] = 3,
    since_latest: bool = True,
) -> dict[str, int | bool]:
    after = storage.latest_activity_epoch() if since_latest else None
    try:
        result = await strava.sync_activities(max_pages=max_pages, after=after)
    except StravaAuthRequiredError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (StravaConfigurationError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"Strava sync failed: {exc}") from exc
    return {**result, "since_latest": since_latest}


@app.get("/activities")
def list_activities(
    _: Annotated[None, Depends(require_chatgpt_api_key)],
    storage: Annotated[Storage, Depends(get_storage)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    activities = storage.list_activities(limit=limit, offset=offset)
    return {"activities": activities, "limit": limit, "offset": offset}


@app.get("/training/summary")
def training_summary(
    _: Annotated[None, Depends(require_chatgpt_api_key)],
    storage: Annotated[Storage, Depends(get_storage)],
    days: Annotated[int, Query(ge=1, le=3660)] = 30,
) -> dict[str, object]:
    since_epoch = int(time.time()) - days * 24 * 60 * 60
    summary = storage.summarize_training(since_epoch)
    return {"days": days, **summary}


@app.get("/training/context")
def training_context(
    _: Annotated[None, Depends(require_chatgpt_api_key)],
    storage: Annotated[Storage, Depends(get_storage)],
    days: Annotated[int, Query(ge=1, le=3660)] = 30,
    recent_limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    since_epoch = int(time.time()) - days * 24 * 60 * 60
    return {
        "days": days,
        "summary": storage.summarize_training(since_epoch),
        "recent_activities": storage.list_activities(limit=recent_limit),
    }
