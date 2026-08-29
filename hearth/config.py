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

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_realtime_model: str = Field(default="gpt-realtime", alias="OPENAI_REALTIME_MODEL")
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="marin", alias="OPENAI_TTS_VOICE")
    openai_transcribe_model: str = Field(default="whisper-1", alias="OPENAI_TRANSCRIBE_MODEL")

    ha_url: str = Field(default="http://homeassistant:8123", alias="HA_URL")
    ha_token: str = Field(default="", alias="HA_TOKEN")

    plex_url: str = Field(default="http://host.docker.internal:32400", alias="PLEX_URL")
    plex_token: str = Field(default="", alias="PLEX_TOKEN")

    docker_socket: str = Field(default="/var/run/docker.sock", alias="DOCKER_SOCKET")

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_token.strip())

    @property
    def plex_configured(self) -> bool:
        return bool(self.plex_token.strip())


settings = Settings()
