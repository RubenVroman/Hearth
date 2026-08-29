from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bind_host: str = Field(default="0.0.0.0", alias="HEARTH_BIND_HOST")
    port: int = Field(default=8787, alias="HEARTH_PORT")
    house_name: str = Field(default="VAULT", alias="HEARTH_HOUSE_NAME")
    owner: str = Field(default="Ruben", alias="HEARTH_OWNER")
    token: str = Field(default="", alias="HEARTH_TOKEN")
    mock_if_unconfigured: bool = Field(default=True, alias="HEARTH_MOCK_IF_UNCONFIGURED")

    workspace_path: Path = Field(default=Path("./workspace"), alias="WORKSPACE_PATH")
    auth_db_path: Path = Field(default=Path("./data/hearth-auth.db"), alias="HEARTH_AUTH_DB")

    app_secret_key: str = Field(default="", alias="APP_SECRET_KEY")
    algorithm: str = Field(default="HS256", alias="ALGORITHM")
    access_token_expire_minutes: int = Field(default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(default=14, alias="REFRESH_TOKEN_EXPIRE_DAYS")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")
    admin_email: str = Field(default="", alias="HEARTH_ADMIN_EMAIL")
    admin_password: str = Field(default="", alias="HEARTH_ADMIN_PASSWORD")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_realtime_model: str = Field(default="gpt-realtime-2.1", alias="OPENAI_REALTIME_MODEL")
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="marin", alias="OPENAI_TTS_VOICE")
    openai_transcribe_model: str = Field(default="whisper-1", alias="OPENAI_TRANSCRIBE_MODEL")

    ha_url: str = Field(default="http://homeassistant:8123", alias="HA_URL")
    ha_token: str = Field(default="", alias="HA_TOKEN")

    plex_url: str = Field(default="http://host.docker.internal:32400", alias="PLEX_URL")
    plex_token: str = Field(default="", alias="PLEX_TOKEN")

    radarr_url: str = Field(default="http://host.docker.internal:7878", alias="RADARR_URL")
    radarr_api_key: str = Field(default="", alias="RADARR_API_KEY")
    sonarr_url: str = Field(default="http://host.docker.internal:8989", alias="SONARR_URL")
    sonarr_api_key: str = Field(default="", alias="SONARR_API_KEY")
    overseerr_url: str = Field(default="http://host.docker.internal:5055", alias="OVERSEERR_URL")
    overseerr_api_key: str = Field(default="", alias="OVERSEERR_API_KEY")

    docker_socket: str = Field(default="/var/run/docker.sock", alias="DOCKER_SOCKET")

    # Chief of Staff escalate (repo/code/PR). Empty webhook = not configured.
    cos_webhook: str = Field(default="", alias="HEARTH_COS_WEBHOOK")
    cos_webhook_key: str = Field(default="", alias="HEARTH_COS_WEBHOOK_KEY")
    cos_repo: str = Field(default="RubenVroman/Hearth", alias="HEARTH_COS_REPO")

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_token.strip())

    @property
    def plex_configured(self) -> bool:
        return bool(self.plex_token.strip())

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_api_key.strip())

    @property
    def sonarr_configured(self) -> bool:
        return bool(self.sonarr_api_key.strip())

    @property
    def overseerr_configured(self) -> bool:
        return bool(self.overseerr_api_key.strip())


settings = Settings()
