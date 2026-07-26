"""Integration tests for the MCP endpoint served by the FastAPI app."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.intervals.config import IntervalsSettings, get_intervals_settings
from app.mcp_server import mcp_endpoint_path

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

PROTOCOL_VERSION = "2025-06-18"


@pytest.fixture(scope="module")
def client():
    """A TestClient with lifespan running, so the MCP session manager starts.

    Module scoped on purpose: the Streamable HTTP session manager is a singleton
    that can only be started once per process, and TestClient only runs the app
    lifespan when used as a context manager. Environment setup lives in
    ``conftest.py`` because it has to happen before ``app.main`` is imported.
    """
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def rpc(client: TestClient, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post("/mcp", headers=MCP_HEADERS, json=payload)
    assert response.status_code == 200, response.text
    return _decode(response)


def _decode(response) -> dict:
    """Read either a plain JSON body or a single SSE event."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"No data frame in SSE response: {response.text!r}")
    return response.json()


def initialize(client: TestClient) -> dict:
    return rpc(
        client,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    )


def call_tool(client: TestClient, name: str, arguments: dict) -> dict:
    initialize(client)
    return rpc(client, "tools/call", {"name": name, "arguments": arguments}, request_id=2)


def tool_payload(result: dict) -> dict:
    """Extract the structured result of a tools/call response."""
    assert "error" not in result, result
    content = result["result"]
    if "structuredContent" in content:
        return content["structuredContent"]
    return json.loads(content["content"][0]["text"])


def test_health_never_exposes_the_secret_path_token():
    """/health is unauthenticated, so MCP_PATH_TOKEN must not appear in it."""
    from app.mcp_server import mcp_display_path

    settings = IntervalsSettings(INTERVALS_API_KEY="x", MCP_PATH_TOKEN="s3cret-token")

    # The router needs the real path, the health payload must not reveal it.
    assert mcp_endpoint_path(settings) == "/mcp/s3cret-token"
    assert mcp_display_path(settings) == "/mcp/<MCP_PATH_TOKEN>"
    assert "s3cret-token" not in mcp_display_path(settings)

    assert mcp_display_path(IntervalsSettings(INTERVALS_API_KEY="x")) == "/mcp"


def test_mcp_endpoint_is_served_at_the_expected_path():
    assert mcp_endpoint_path(IntervalsSettings(INTERVALS_API_KEY="x")) == "/mcp"
    assert (
        mcp_endpoint_path(IntervalsSettings(INTERVALS_API_KEY="x", MCP_PATH_TOKEN="s3cret"))
        == "/mcp/s3cret"
    )


def test_allowed_hosts_default_to_localhost_only():
    # MCP_ALLOWED_HOSTS is set for the whole test session, so override it here.
    settings = IntervalsSettings(INTERVALS_API_KEY="x", MCP_ALLOWED_HOSTS=None)

    assert "localhost:*" in settings.allowed_hosts
    assert settings.dns_rebinding_protection is True


def test_public_base_url_is_added_to_the_allowed_hosts():
    settings = IntervalsSettings(
        INTERVALS_API_KEY="x",
        MCP_ALLOWED_HOSTS=None,
        PUBLIC_BASE_URL="https://stravagpt.onrender.com/",
    )

    assert "stravagpt.onrender.com" in settings.allowed_hosts
    assert "https://stravagpt.onrender.com" in settings.allowed_origins


def test_allowed_hosts_can_be_overridden_and_disabled():
    explicit = IntervalsSettings(INTERVALS_API_KEY="x", MCP_ALLOWED_HOSTS="a.example, b.example")
    assert explicit.allowed_hosts == ["a.example", "b.example"]
    assert explicit.dns_rebinding_protection is True

    wildcard = IntervalsSettings(INTERVALS_API_KEY="x", MCP_ALLOWED_HOSTS="*")
    assert wildcard.dns_rebinding_protection is False


def test_missing_api_key_raises_a_configuration_error_naming_the_env_var():
    from app.intervals.errors import IntervalsConfigurationError

    with pytest.raises(IntervalsConfigurationError) as excinfo:
        IntervalsSettings(INTERVALS_API_KEY=None).require_api_key()

    assert "INTERVALS_API_KEY" in str(excinfo.value)


def test_api_key_guard_rejects_missing_and_wrong_keys():
    from app.mcp_server import ApiKeyGuard

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = ApiKeyGuard(inner, "s3cret")

    def run(headers):
        import asyncio

        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request"}

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            guard({"type": "http", "headers": headers}, receive, send)
        )
        return sent[0]["status"]

    assert run([]) == 401
    assert run([(b"x-api-key", b"wrong")]) == 401
    assert run([(b"x-api-key", b"s3cret")]) == 200
    assert run([(b"authorization", b"Bearer s3cret")]) == 200


def test_initialize_handshake(client):
    result = initialize(client)

    assert result["result"]["serverInfo"]["name"] == "intervals-icu"
    assert "COROS" in result["result"]["instructions"]


def test_tools_list_exposes_the_four_tools(client):
    initialize(client)

    result = rpc(client, "tools/list", {}, request_id=2)

    names = {tool["name"] for tool in result["result"]["tools"]}
    assert names == {"push_workouts", "list_events", "delete_events", "get_sport_settings"}


def test_push_workouts_schema_documents_the_workout_format(client):
    initialize(client)
    result = rpc(client, "tools/list", {}, request_id=2)

    tools = {tool["name"]: tool for tool in result["result"]["tools"]}
    schema = tools["push_workouts"]["inputSchema"]

    assert "workouts" in schema["properties"]
    assert "dry_run" in schema["properties"]
    assert "allow_beyond_coros_window" in schema["properties"]


def test_push_workouts_dry_run_renders_without_calling_the_api(client):
    from datetime import date, timedelta

    target_date = (date.today() + timedelta(days=2)).isoformat()

    payload = tool_payload(
        call_tool(
            client,
            "push_workouts",
            {
                "dry_run": True,
                "workouts": [
                    {
                        "date": target_date,
                        "sport": "Run",
                        "name": "Umbral 4x2km",
                        "target_type": "pace",
                        "external_id": "w2-wed",
                        "steps": [
                            {"type": "warmup", "duration": "15min", "target": "easy"},
                            {
                                "type": "interval",
                                "repeat": 4,
                                "work": {"distance": "2000m", "target": "4:00-4:02/km"},
                                "recovery": {"duration": "2min", "target": "easy"},
                            },
                            {"type": "cooldown", "duration": "10min", "target": "easy"},
                        ],
                    }
                ],
            },
        )
    )

    assert payload["dry_run"] is True
    assert payload["pushed"] == 0
    workout = payload["workouts"][0]
    assert workout["external_id"] == "w2-wed"
    assert workout["target"] == "PACE"
    assert workout["description"] == (
        "- 15m 70% Warmup\n\n4x\n- 2000m 4:00-4:02/km\n- 2m 70%\n\n- 10m 70% Cooldown"
    )


def test_push_workouts_beyond_the_coros_window_returns_a_readable_error(client):
    from datetime import date, timedelta

    result = call_tool(
        client,
        "push_workouts",
        {
            "dry_run": True,
            "workouts": [
                {
                    "date": (date.today() + timedelta(days=21)).isoformat(),
                    "name": "Too far",
                    "target_type": "pace",
                    "external_id": "too-far",
                    "steps": [{"type": "work", "duration": "30min", "target": "easy"}],
                }
            ],
        },
    )

    text = json.dumps(result)
    assert "COROS" in text
    assert "too-far" in text
    assert "allow_beyond_coros_window" in text


def test_push_workouts_mixing_pace_and_hr_returns_a_readable_error(client):
    from datetime import date, timedelta

    result = call_tool(
        client,
        "push_workouts",
        {
            "dry_run": True,
            "workouts": [
                {
                    "date": (date.today() + timedelta(days=1)).isoformat(),
                    "name": "Mixed",
                    "target_type": "pace",
                    "external_id": "mixed",
                    "steps": [
                        {"type": "warmup", "duration": "15min", "target": "easy"},
                        {"type": "work", "duration": "20min", "target": "150bpm"},
                    ],
                }
            ],
        },
    )

    text = json.dumps(result)
    assert "150bpm" in text
    assert "target_type='pace'" in text


def test_tool_without_an_api_key_reports_the_missing_configuration(client):
    from datetime import date, timedelta

    day = (date.today() + timedelta(days=1)).isoformat()
    result = call_tool(client, "list_events", {"oldest": day, "newest": day})

    assert "INTERVALS_API_KEY" in json.dumps(result)


def test_delete_events_without_references_is_rejected(client):
    result = call_tool(client, "delete_events", {})

    assert "Nothing to delete" in json.dumps(result)


def test_strava_routes_still_work_alongside_mcp(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mcp"]["path"] == "/mcp"
    assert body["mcp"]["intervals_api_key_configured"] is False
