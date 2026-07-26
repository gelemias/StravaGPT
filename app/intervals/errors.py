from __future__ import annotations

import json
from typing import Any


MAX_BODY_SNIPPET = 400


class IntervalsError(RuntimeError):
    """Base class for every error raised by the Intervals.icu integration."""


class IntervalsConfigurationError(IntervalsError):
    """Raised when required configuration (an API key) is missing."""


class IntervalsValidationError(IntervalsError):
    """Raised when the caller's workout JSON cannot be converted safely."""


STATUS_HINTS = {
    400: "Intervals.icu rejected the payload. Check the event fields and the description syntax.",
    401: "Authentication failed. Check INTERVALS_API_KEY (Intervals.icu > Settings > Developer).",
    403: "Authenticated but not allowed. Confirm the API key belongs to this athlete id.",
    404: "Endpoint or athlete not found. Check INTERVALS_ATHLETE_ID (0 means 'the key owner').",
    405: "Method not allowed for this endpoint.",
    422: "Intervals.icu could not process the payload. Check dates, sport type and targets.",
    429: "Rate limited by Intervals.icu. Wait a moment and retry.",
}


def _body_snippet(body: Any) -> str:
    if body is None or body == "":
        return ""
    if isinstance(body, (dict, list)):
        try:
            text = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
    else:
        text = str(body)
    text = " ".join(text.split())
    if len(text) > MAX_BODY_SNIPPET:
        text = text[:MAX_BODY_SNIPPET] + "..."
    return text


class IntervalsAPIError(IntervalsError):
    """Raised for any non-2xx response from the Intervals.icu API.

    The message is built to be readable by a human (or by an LLM reading a tool
    error) and never contains the API key.
    """

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        body: Any = None,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.status_code = status_code
        self.body = body

        parts = [f"Intervals.icu API {status_code} on {self.method} {path}."]
        hint = STATUS_HINTS.get(status_code)
        if hint is None and status_code >= 500:
            hint = "Intervals.icu returned a server error. This is usually transient; retry later."
        if hint:
            parts.append(hint)
        snippet = _body_snippet(body)
        if snippet:
            parts.append(f"Response: {snippet}")
        super().__init__(" ".join(parts))
