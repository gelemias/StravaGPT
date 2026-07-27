"""MCP server exposing the Intervals.icu planned-workout tools.

Transport is Streamable HTTP so the same process that serves the REST API can
serve MCP. ``attach_mcp`` wires the endpoint into an existing FastAPI app;
``build_standalone_app`` runs it on its own.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.intervals.client import (
    IntervalsClient,
    format_pace,
    inspect_step_targets,
    threshold_pace_seconds_per_km,
)
from app.intervals.config import IntervalsSettings, get_intervals_settings
from app.intervals.errors import IntervalsError
from app.intervals.models import (
    WorkoutSpec,
    assert_unique_external_ids,
    build_event,
    check_coros_window,
)
from app.intervals.workout_dsl import render_workout


logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "intervals-icu"

INSTRUCTIONS = """\
Tools for managing planned workouts on an Intervals.icu calendar.

Workflow for pushing a training week:
1. Call get_sport_settings to read the athlete's threshold pace and zones.
2. Call push_workouts with dry_run=true to review the generated Intervals.icu
   step syntax and the estimated duration.
3. Call push_workouts for real. It upserts on external_id, so re-pushing the
   same external_id updates the workout instead of creating a duplicate.

A warmup or cooldown with neither "duration" nor "distance" is an open step: it
runs until the athlete presses the lap button. Every other step type still needs
a length.

Two hard constraints come from the COROS watch sync, not from Intervals.icu:
- Only about 7 days of planned workouts transfer to the watch, so push a rolling
  week rather than a whole training block.
- A workout must use either pace targets or heart-rate targets, never both:
  COROS forces every block to the intensity type of the first one.
"""


mcp = FastMCP(
    MCP_SERVER_NAME,
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)


def _client() -> IntervalsClient:
    return IntervalsClient(get_intervals_settings())


def _fail(exc: IntervalsError) -> ToolError:
    return ToolError(str(exc))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
async def push_workouts(
    workouts: Annotated[
        list[WorkoutSpec],
        Field(description="Workouts to create or update, in the friendly JSON format."),
    ],
    dry_run: Annotated[
        bool,
        Field(description="Render the Intervals.icu syntax and return it without calling the API."),
    ] = False,
    allow_beyond_coros_window: Annotated[
        bool,
        Field(
            description=(
                "Push workouts dated outside the COROS sync window anyway. They land in "
                "Intervals.icu but will not reach the watch."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Create or update planned workouts on the Intervals.icu calendar.

    Idempotent: the upsert matches on external_id, so pushing the same
    external_id again updates the existing workout instead of duplicating it.
    """
    if not workouts:
        raise ToolError("No workouts given. Pass at least one workout.")

    settings = get_intervals_settings()
    client = IntervalsClient(settings)
    warnings: list[str] = []

    try:
        assert_unique_external_ids(workouts)
        window_problems = check_coros_window(
            workouts,
            max_future_days=settings.max_future_days,
            today=date.today(),
            strict=not allow_beyond_coros_window,
        )
    except IntervalsError as exc:
        raise _fail(exc) from exc

    for problem in window_problems:
        warnings.append(f"Outside the COROS sync window: {problem}")

    # Threshold pace turns distance-based steps into a moving_time estimate. It is
    # fetched for dry runs too so the preview matches what would be written.
    threshold_paces: dict[str, float] = {}
    sports = sorted({spec.sport for spec in workouts})
    try:
        threshold_paces, pace_warnings = await client.threshold_paces(sports)
    except IntervalsError as exc:
        pace_warnings = [f"Could not read sport settings: {exc}"]
    warnings.extend(pace_warnings)

    events: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    try:
        for spec in workouts:
            rendered = render_workout(
                spec,
                threshold_pace_seconds_per_km=threshold_paces.get(spec.sport),
                open_step_style=settings.open_step_style,
                open_step_nominal_seconds=settings.open_step_nominal_seconds,
            )
            warnings.extend(rendered.warnings)
            events.append(build_event(spec, rendered.description, rendered.moving_time, rendered.target))
            previews.append(
                {
                    "external_id": spec.external_id,
                    "date": spec.date.isoformat(),
                    "name": spec.name,
                    "sport": spec.sport,
                    "target": rendered.target,
                    "moving_time_seconds": rendered.moving_time,
                    "moving_time_pretty": _pretty_seconds(rendered.moving_time),
                    "open_steps": rendered.open_steps,
                    "description": rendered.description,
                }
            )
    except IntervalsError as exc:
        raise _fail(exc) from exc

    if dry_run:
        return {
            "dry_run": True,
            "pushed": 0,
            "workouts": previews,
            "warnings": warnings,
            "next_step": "Re-run with dry_run=false to write these workouts to the calendar.",
        }

    try:
        response = await client.bulk_upsert_events(events)
    except IntervalsError as exc:
        raise _fail(exc) from exc

    created_ids = _extract_event_ids(response)
    return {
        "dry_run": False,
        "pushed": len(events),
        "workouts": previews,
        "event_ids": created_ids,
        "warnings": warnings,
    }


@mcp.tool()
async def list_events(
    oldest: Annotated[str, Field(description="Start of the range, as YYYY-MM-DD.")],
    newest: Annotated[str, Field(description="End of the range, as YYYY-MM-DD.")],
    category: Annotated[
        str | None,
        Field(description="Filter by category, for example 'WORKOUT'. Omit for everything."),
    ] = None,
    include_description: Annotated[
        bool,
        Field(description="Include the full step description of each event."),
    ] = False,
    inspect_targets: Annotated[
        bool,
        Field(
            description=(
                "Resolve each step's target and report whether it came out as pace or "
                "power, and whether a training load was computed. Use this to verify a "
                "push actually reached the watch as a pace workout."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Read the Intervals.icu calendar between two dates."""
    oldest_date = _parse_date(oldest, "oldest")
    newest_date = _parse_date(newest, "newest")
    if newest_date < oldest_date:
        raise ToolError(f"'newest' ({newest}) is before 'oldest' ({oldest}).")

    try:
        events = await _client().list_events(
            oldest=oldest_date.isoformat(),
            newest=newest_date.isoformat(),
            category=category,
            resolve=inspect_targets,
        )
    except IntervalsError as exc:
        raise _fail(exc) from exc

    summaries = [_summarize_event(event, include_description=include_description) for event in events]
    if inspect_targets:
        for summary, event in zip(summaries, events, strict=True):
            report = inspect_step_targets(event)
            report.pop("workout_doc", None)
            summary["targets"] = report
    return {
        "oldest": oldest_date.isoformat(),
        "newest": newest_date.isoformat(),
        "category": category,
        "count": len(summaries),
        "events": summaries,
    }


@mcp.tool()
async def delete_events(
    external_ids: Annotated[
        list[str] | None,
        Field(description="Delete by your own external_id values."),
    ] = None,
    ids: Annotated[
        list[int] | None,
        Field(description="Delete by Intervals.icu numeric event id."),
    ] = None,
) -> dict[str, Any]:
    """Delete calendar events by external_id or by Intervals.icu event id."""
    references: list[dict[str, Any]] = []
    for external_id in external_ids or []:
        cleaned = str(external_id).strip()
        if cleaned:
            references.append({"external_id": cleaned})
    for event_id in ids or []:
        references.append({"id": int(event_id)})

    if not references:
        raise ToolError(
            "Nothing to delete. Pass external_ids (your own keys) or ids (Intervals.icu event ids)."
        )

    try:
        response = await _client().bulk_delete_events(references)
    except IntervalsError as exc:
        raise _fail(exc) from exc

    return {
        "requested": len(references),
        "deleted_references": references,
        "response": response,
    }


@mcp.tool()
async def get_sport_settings(
    sport: Annotated[
        str | None,
        Field(description="Only return settings for this activity type, for example 'Run'."),
    ] = None,
) -> dict[str, Any]:
    """Read the athlete's sport settings: thresholds and training zones.

    Use this to check the threshold pace before writing pace targets, and to
    confirm a threshold is configured at all (COROS needs it to build the
    workout on the watch).
    """
    try:
        body = await _client().get_sport_settings()
    except IntervalsError as exc:
        raise _fail(exc) from exc

    entries: list[dict[str, Any]]
    if isinstance(body, list):
        entries = [entry for entry in body if isinstance(entry, dict)]
    elif isinstance(body, dict):
        entries = [body]
    else:
        entries = []

    if sport:
        wanted = sport.strip().lower()
        entries = [
            entry
            for entry in entries
            if any(str(item).strip().lower() == wanted for item in (entry.get("types") or []))
        ]

    derived = []
    for entry in entries:
        types = [str(item) for item in (entry.get("types") or [])]
        pace = None
        for candidate in types:
            pace = threshold_pace_seconds_per_km(entry, candidate)
            if pace is not None:
                break
        derived.append(
            {
                "types": types,
                "threshold_pace_per_km": format_pace(pace) if pace else None,
                "threshold_pace_configured": pace is not None,
            }
        )

    return {
        "sport": sport,
        "count": len(entries),
        "summary": derived,
        "settings": entries,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ToolError(f"'{field_name}' must be a date as YYYY-MM-DD, got {value!r}.") from exc


def _pretty_seconds(seconds: int) -> str:
    delta = timedelta(seconds=int(seconds))
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"


def _extract_event_ids(response: Any) -> list[Any]:
    if isinstance(response, list):
        return [item.get("id") for item in response if isinstance(item, dict) and "id" in item]
    if isinstance(response, dict) and "id" in response:
        return [response["id"]]
    return []


def _summarize_event(event: dict[str, Any], *, include_description: bool) -> dict[str, Any]:
    summary = {
        "id": event.get("id"),
        "external_id": event.get("external_id"),
        "start_date_local": event.get("start_date_local"),
        "name": event.get("name"),
        "type": event.get("type"),
        "category": event.get("category"),
        "target": event.get("target"),
        "moving_time": event.get("moving_time"),
    }
    if include_description:
        summary["description"] = event.get("description")
    return summary


# --------------------------------------------------------------------------
# HTTP wiring
# --------------------------------------------------------------------------


def mcp_endpoint_path(settings: IntervalsSettings) -> str:
    """The public path of the MCP endpoint.

    Claude.ai custom connectors cannot send custom headers, so an unguessable
    path segment (``MCP_PATH_TOKEN``) is offered as an alternative to
    ``MCP_API_KEY``.
    """
    token = (settings.mcp_path_token or "").strip("/ ")
    return f"/mcp/{token}" if token else "/mcp"


def mcp_display_path(settings: IntervalsSettings) -> str:
    """The MCP path with the secret token redacted.

    ``MCP_PATH_TOKEN`` is a credential: it is the whole authentication story for
    clients that cannot send headers. Never return the real path from an
    unauthenticated endpoint such as /health.
    """
    token = (settings.mcp_path_token or "").strip("/ ")
    return "/mcp/<MCP_PATH_TOKEN>" if token else "/mcp"


class ApiKeyGuard:
    """ASGI middleware requiring ``MCP_API_KEY`` via header, when configured."""

    def __init__(self, app: Any, expected_key: str) -> None:
        self.app = app
        self.expected_key = expected_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        provided = headers.get("x-api-key")
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()

        if not provided or not secrets.compare_digest(provided, self.expected_key):
            await self._unauthorized(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _unauthorized(send: Send) -> None:
        body = b'{"error":"Missing or invalid MCP API key."}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _mcp_routes(settings: IntervalsSettings) -> list[Route]:
    """Build the MCP route(s), guarded by MCP_API_KEY when it is set."""
    path = mcp_endpoint_path(settings)
    mcp.settings.streamable_http_path = path
    # Must be set before streamable_http_app() builds the session manager.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.dns_rebinding_protection,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    http_app = mcp.streamable_http_app()

    first = http_app.routes[0]
    endpoint = getattr(first, "app", None) or first.endpoint

    # The two mechanisms are alternatives, not requirements to combine. A secret
    # path IS the credential, so requiring the header on top of it would lock out
    # exactly the header-less clients it exists for: those clients get 401, read
    # it as "this server speaks OAuth", and fail on dynamic client registration.
    routes: list[Route] = []
    if settings.mcp_path_token:
        routes.append(Route(path, endpoint=endpoint))
        if settings.mcp_api_key:
            # Keep header auth usable in parallel, on the plain /mcp path.
            routes.append(Route("/mcp", endpoint=ApiKeyGuard(endpoint, settings.mcp_api_key)))
    elif settings.mcp_api_key:
        routes.append(Route(path, endpoint=ApiKeyGuard(endpoint, settings.mcp_api_key)))
    else:
        routes.append(Route(path, endpoint=endpoint))
    return routes


@asynccontextmanager
async def mcp_session_lifespan() -> AsyncIterator[None]:
    """Run the Streamable HTTP session manager.

    Mounted ASGI apps do not receive lifespan events, so the host application
    must drive the session manager itself.
    """
    async with mcp.session_manager.run():
        yield


def attach_mcp(app: Any, settings: IntervalsSettings | None = None) -> str:
    """Register the MCP endpoint on an existing FastAPI/Starlette app.

    The route is appended directly instead of mounted so the path matches
    exactly and clients are never redirected. Remember to run
    ``mcp_session_lifespan`` from the host app's lifespan.
    """
    settings = settings or get_intervals_settings()
    routes = _mcp_routes(settings)
    app.router.routes.extend(routes)
    path = routes[0].path if routes else mcp_endpoint_path(settings)
    if not settings.mcp_api_key and not settings.mcp_path_token:
        logger.warning(
            "MCP endpoint %s is unauthenticated. Set MCP_API_KEY or MCP_PATH_TOKEN "
            "before exposing it publicly.",
            path,
        )
    if settings.dns_rebinding_protection and not settings.public_base_url and not settings.mcp_allowed_hosts:
        logger.warning(
            "MCP endpoint %s only accepts requests with a localhost Host header. "
            "Set PUBLIC_BASE_URL (or MCP_ALLOWED_HOSTS) to the deployed hostname, "
            "otherwise remote clients get HTTP 421 Misdirected Request.",
            path,
        )
    return path


def build_standalone_app(settings: IntervalsSettings | None = None) -> Any:
    """Return an ASGI app serving only the Intervals.icu MCP endpoint.

    Use this once StravaGPT is retired::

        uvicorn --factory app.mcp_server:build_standalone_app --host 0.0.0.0 --port $PORT
    """
    from starlette.applications import Starlette

    settings = settings or get_intervals_settings()
    routes = _mcp_routes(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_session_lifespan():
            yield

    return Starlette(routes=routes, lifespan=lifespan)
