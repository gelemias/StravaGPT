from __future__ import annotations

import ssl
import time
from typing import Any
from urllib.parse import urlencode

import certifi
import httpx

try:
    import truststore
except ImportError:  # pragma: no cover - fallback for environments not reinstalled yet.
    truststore = None

from app.config import Settings
from app.storage import Storage, StoredToken


STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE_URL = "https://www.strava.com/api/v3"


def strava_ssl_context() -> ssl.SSLContext:
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context(cafile=certifi.where())


def strava_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20,
        verify=strava_ssl_context(),
        trust_env=False,
    )


class StravaConfigurationError(RuntimeError):
    pass


class StravaAuthRequiredError(RuntimeError):
    pass


class StravaClient:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage

    def authorization_url(self, state: str) -> str:
        self._require_config()
        query = urlencode(
            {
                "client_id": self.settings.strava_client_id,
                "redirect_uri": self.settings.strava_redirect_uri,
                "response_type": "code",
                "approval_prompt": "auto",
                "scope": self.settings.strava_scopes,
                "state": state,
            }
        )
        return f"{STRAVA_AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str) -> StoredToken:
        self._require_config()
        payload = await self._post_token(
            {
                "client_id": self.settings.strava_client_id,
                "client_secret": self.settings.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            }
        )
        return self.storage.save_token(payload)

    async def get_access_token(self) -> str:
        token = self.storage.get_token()
        if token is None:
            raise StravaAuthRequiredError("Authorize with Strava before syncing activities.")
        if token.expires_at <= int(time.time()) + 60:
            token = await self.refresh_token(token)
        return token.access_token

    async def refresh_token(self, token: StoredToken) -> StoredToken:
        self._require_config()
        payload = await self._post_token(
            {
                "client_id": self.settings.strava_client_id,
                "client_secret": self.settings.strava_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            }
        )
        if "athlete" not in payload and token.athlete_id is not None:
            payload["athlete"] = {"id": token.athlete_id}
        if "scope" not in payload:
            payload["scope"] = token.scope
        return self.storage.save_token(payload)

    async def fetch_activities(
        self,
        *,
        after: int | None = None,
        before: int | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        access_token = await self.get_access_token()
        params = {
            "page": page,
            "per_page": per_page,
        }
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        async with strava_http_client() as client:
            response = await client.get(
                f"{STRAVA_API_BASE_URL}/athlete/activities",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            response.raise_for_status()
            return response.json()

    async def sync_activities(
        self,
        *,
        max_pages: int = 3,
        after: int | None = None,
        per_page: int = 100,
    ) -> dict[str, int]:
        saved = 0
        fetched = 0
        for page in range(1, max_pages + 1):
            activities = await self.fetch_activities(
                after=after,
                page=page,
                per_page=per_page,
            )
            fetched += len(activities)
            saved += self.storage.upsert_activities(activities)
            if len(activities) < per_page:
                break
        return {"fetched": fetched, "saved": saved}

    async def _post_token(self, data: dict[str, Any]) -> dict[str, Any]:
        async with strava_http_client() as client:
            response = await client.post(STRAVA_TOKEN_URL, data=data)
            response.raise_for_status()
            return response.json()

    def tls_info(self) -> dict[str, object]:
        return {
            "certifi_bundle": certifi.where(),
            "truststore_available": truststore is not None,
            "trust_provider": "operating_system" if truststore is not None else "certifi",
            "trust_env": False,
        }

    def _require_config(self) -> None:
        if not self.settings.strava_configured:
            raise StravaConfigurationError(
                "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET before using Strava OAuth."
            )
