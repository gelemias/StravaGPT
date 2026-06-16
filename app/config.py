from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    strava_client_id: str | None = Field(default=None, alias="STRAVA_CLIENT_ID")
    strava_client_secret: str | None = Field(default=None, alias="STRAVA_CLIENT_SECRET")
    strava_redirect_uri: str = Field(
        default="http://localhost:8000/auth/callback",
        alias="STRAVA_REDIRECT_URI",
    )
    strava_scopes: str = Field(default="read,activity:read_all", alias="STRAVA_SCOPES")
    database_path: str = Field(default="./stravagpt.db", alias="DATABASE_PATH")
    chatgpt_api_key: str | None = Field(default=None, alias="CHATGPT_API_KEY")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    sync_on_startup: bool = Field(default=True, alias="SYNC_ON_STARTUP")
    startup_sync_max_pages: int = Field(
        default=1,
        ge=1,
        le=20,
        alias="STARTUP_SYNC_MAX_PAGES",
    )
    startup_sync_per_page: int = Field(
        default=30,
        ge=1,
        le=200,
        alias="STARTUP_SYNC_PER_PAGE",
    )

    @property
    def strava_configured(self) -> bool:
        return bool(self.strava_client_id) and bool(self.strava_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
