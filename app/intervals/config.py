from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.intervals.errors import IntervalsConfigurationError


class IntervalsSettings(BaseSettings):
    """Configuration for the Intervals.icu MCP server.

    The API key is only ever read from the environment (or a local ``.env``
    file). It is never written to logs, tool responses or error messages.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str | None = Field(default=None, alias="INTERVALS_API_KEY")
    athlete_id: str = Field(default="0", alias="INTERVALS_ATHLETE_ID")
    base_url: str = Field(default="https://intervals.icu/api/v1", alias="INTERVALS_BASE_URL")
    timeout_seconds: float = Field(default=30.0, gt=0, alias="INTERVALS_TIMEOUT_SECONDS")
    trust_env: bool = Field(default=False, alias="INTERVALS_TRUST_ENV")

    # COROS only accepts about a week of planned workouts in advance.
    max_future_days: int = Field(default=7, ge=0, le=365, alias="INTERVALS_MAX_FUTURE_DAYS")

    # How to render a warm-up/cool-down left open for the lap button.
    #   "no_duration" emits the step with no time or distance at all.
    #   "nominal"     emits a normal timed step, as a fallback if Intervals.icu
    #                 refuses to parse a step without a length.
    open_step_style: Literal["no_duration", "nominal"] = Field(
        default="no_duration",
        alias="INTERVALS_OPEN_STEP_STYLE",
    )
    open_step_nominal_seconds: int = Field(
        default=600,
        ge=1,
        alias="INTERVALS_OPEN_STEP_NOMINAL_SECONDS",
    )

    # Optional protection for the public /mcp endpoint.
    mcp_api_key: str | None = Field(default=None, alias="MCP_API_KEY")
    mcp_path_token: str | None = Field(default=None, alias="MCP_PATH_TOKEN")

    # The MCP SDK blocks unknown Host headers to stop DNS rebinding attacks, and
    # its default allow-list is localhost only. A deployed server therefore has
    # to declare its own hostname or every request answers 421.
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    mcp_allowed_hosts: str | None = Field(default=None, alias="MCP_ALLOWED_HOSTS")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def allowed_hosts(self) -> list[str]:
        """Host header values the MCP endpoint accepts.

        ``MCP_ALLOWED_HOSTS`` is a comma-separated override; ``*`` disables the
        check. Otherwise localhost is always allowed, plus the host of
        ``PUBLIC_BASE_URL`` when it is set.
        """
        if self.mcp_allowed_hosts:
            return [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]

        hosts = ["127.0.0.1", "localhost", "[::1]", "127.0.0.1:*", "localhost:*", "[::1]:*"]
        public_host = self._public_host()
        if public_host:
            hosts.extend([public_host, f"{public_host}:*"])
        return hosts

    @property
    def allowed_origins(self) -> list[str]:
        origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
        public_host = self._public_host()
        if public_host:
            origins.extend([f"https://{public_host}", f"https://{public_host}:*"])
        return origins

    @property
    def dns_rebinding_protection(self) -> bool:
        return "*" not in self.allowed_hosts

    def _public_host(self) -> str | None:
        if not self.public_base_url:
            return None
        raw = self.public_base_url.strip().rstrip("/")
        without_scheme = raw.split("://", 1)[-1]
        host = without_scheme.split("/", 1)[0]
        return host or None

    @property
    def athlete_path(self) -> str:
        return f"/athlete/{self.athlete_id}"

    def require_api_key(self) -> str:
        if not self.api_key:
            raise IntervalsConfigurationError(
                "INTERVALS_API_KEY is not set. Create a personal API key in "
                "Intervals.icu (Settings > Developer) and export it before starting the server."
            )
        return self.api_key


@lru_cache
def get_intervals_settings() -> IntervalsSettings:
    return IntervalsSettings()
