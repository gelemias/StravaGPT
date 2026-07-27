from __future__ import annotations

import json
import ssl
from typing import Any

import certifi
import httpx

try:
    import truststore
except ImportError:  # pragma: no cover - fallback for environments not reinstalled yet.
    truststore = None

from app.intervals.config import IntervalsSettings
from app.intervals.errors import IntervalsAPIError


# Intervals.icu personal API keys authenticate with HTTP Basic where the
# username is the literal string "API_KEY" and the password is the key itself.
BASIC_AUTH_USERNAME = "API_KEY"


def intervals_ssl_context() -> ssl.SSLContext:
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context(cafile=certifi.where())


def threshold_pace_seconds_per_km(
    sport_settings: Any,
    sport: str,
) -> float | None:
    """Read the athlete's threshold pace (seconds per km) for one sport.

    Intervals.icu returns sport settings as a list of entries, each covering one
    or more activity types. Pace values are metres per second. Returns ``None``
    when the athlete has not configured a threshold pace for the sport.
    """
    entries: list[dict[str, Any]]
    if isinstance(sport_settings, list):
        entries = [entry for entry in sport_settings if isinstance(entry, dict)]
    elif isinstance(sport_settings, dict):
        entries = [sport_settings]
    else:
        return None

    wanted = sport.strip().lower()
    candidates = [
        entry
        for entry in entries
        if any(str(item).strip().lower() == wanted for item in (entry.get("types") or []))
    ]
    if not candidates:
        return None

    for entry in candidates:
        for key in ("threshold_pace", "pace_threshold", "threshold_speed"):
            value = entry.get(key)
            if value is None:
                continue
            try:
                metres_per_second = float(value)
            except (TypeError, ValueError):
                continue
            # Sanity-check the unit: running and cycling threshold speeds live
            # well inside this range in m/s.
            if 0.5 <= metres_per_second <= 20.0:
                return 1000.0 / metres_per_second
    return None


def inspect_step_targets(event: dict[str, Any]) -> dict[str, Any]:
    """Summarise how Intervals.icu actually resolved a pushed workout.

    Used to answer three questions after a push without guessing at the
    workout_doc schema, which the official OpenAPI spec leaves untyped:
    did the steps get a target at all, is that target pace or power, and did a
    training load get computed.
    """
    doc = event.get("workout_doc")
    steps = doc.get("steps") if isinstance(doc, dict) else None
    serialized = json.dumps(doc, ensure_ascii=False).lower() if doc is not None else ""

    load_fields = {
        key: value
        for key, value in event.items()
        if "load" in key.lower() and isinstance(value, (int, float))
    }

    return {
        "has_workout_doc": doc is not None,
        "step_count": len(steps) if isinstance(steps, list) else None,
        "mentions_pace": "pace" in serialized,
        "mentions_power": "power" in serialized or '"watts"' in serialized,
        "load_fields": load_fields,
        "training_load": next(
            (value for value in load_fields.values() if value), 0
        ),
        "workout_doc": doc,
    }


def format_pace(seconds_per_km: float) -> str:
    total = int(round(seconds_per_km))
    return f"{total // 60}:{total % 60:02d}/km"


class IntervalsClient:
    """Thin async client for the Intervals.icu v1 API."""

    def __init__(self, settings: IntervalsSettings) -> None:
        self.settings = settings

    # -- plumbing ---------------------------------------------------------

    def _http_client(self) -> httpx.AsyncClient:
        api_key = self.settings.require_api_key()
        return httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            auth=httpx.BasicAuth(BASIC_AUTH_USERNAME, api_key),
            timeout=self.settings.timeout_seconds,
            verify=intervals_ssl_context(),
            trust_env=self.settings.trust_env,
            headers={"Accept": "application/json"},
        )

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError:
                return response.text
        return response.text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        async with self._http_client() as client:
            try:
                response = await client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                raise IntervalsAPIError(
                    method,
                    path,
                    0,
                    f"Could not reach Intervals.icu: {exc}",
                ) from exc

        body = self._parse_body(response)
        if response.status_code < 200 or response.status_code >= 300:
            raise IntervalsAPIError(method, path, response.status_code, body)
        return body

    # -- endpoints --------------------------------------------------------

    async def list_events(
        self,
        *,
        oldest: str,
        newest: str,
        category: str | None = None,
        resolve: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"oldest": oldest, "newest": newest}
        if category:
            params["category"] = category
        if resolve:
            # Asks Intervals.icu to return each step's target resolved into real
            # values instead of percentages, which is how you check that a step
            # came out as pace rather than power.
            params["resolve"] = "true"
        body = await self._request(
            "GET",
            f"{self.settings.athlete_path}/events",
            params=params,
        )
        if isinstance(body, list):
            return [event for event in body if isinstance(event, dict)]
        return []

    async def get_event(self, event_id: int, *, resolve: bool = True) -> Any:
        params = {"resolve": "true"} if resolve else None
        return await self._request(
            "GET",
            f"{self.settings.athlete_path}/events/{event_id}",
            params=params,
        )

    async def bulk_upsert_events(self, events: list[dict[str, Any]]) -> Any:
        return await self._request(
            "POST",
            f"{self.settings.athlete_path}/events/bulk",
            params={"upsert": "true"},
            json=events,
        )

    async def bulk_delete_events(self, references: list[dict[str, Any]]) -> Any:
        """Delete events by ``external_id`` or by numeric ``id``.

        The endpoint is documented as PUT; some deployments answer POST instead,
        so a 405 falls back to POST rather than failing the call.
        """
        path = f"{self.settings.athlete_path}/events/bulk-delete"
        try:
            return await self._request("PUT", path, json=references)
        except IntervalsAPIError as exc:
            if exc.status_code != 405:
                raise
            return await self._request("POST", path, json=references)

    async def get_sport_settings(self) -> Any:
        return await self._request("GET", f"{self.settings.athlete_path}/sport-settings")

    # -- helpers ----------------------------------------------------------

    async def threshold_paces(self, sports: list[str]) -> tuple[dict[str, float], list[str]]:
        """Best-effort lookup of threshold pace per sport.

        Never raises: a failure here only degrades the ``moving_time`` estimate,
        so the reason is returned as a warning instead.
        """
        warnings: list[str] = []
        if not sports:
            return {}, warnings
        try:
            settings_body = await self.get_sport_settings()
        except IntervalsAPIError as exc:
            warnings.append(
                f"Could not read sport settings, moving_time estimates may be rough ({exc})."
            )
            return {}, warnings

        paces: dict[str, float] = {}
        for sport in sports:
            pace = threshold_pace_seconds_per_km(settings_body, sport)
            if pace is None:
                warnings.append(
                    f"No threshold pace configured for '{sport}' in Intervals.icu Sport "
                    "Settings, so distance-based steps use a rough estimate."
                )
            else:
                paces[sport] = pace
        return paces, warnings
