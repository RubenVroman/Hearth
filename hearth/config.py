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
    memory_db_path: Path = Field(default=Path("./data/hearth-memory.db"), alias="HEARTH_MEMORY_DB")
    memory_enabled: bool = Field(default=True, alias="HEARTH_MEMORY_ENABLED")
    memory_store_conversations: bool = Field(default=True, alias="HEARTH_MEMORY_STORE_CONVERSATIONS")
    memory_store_house_events: bool = Field(default=False, alias="HEARTH_MEMORY_STORE_HOUSE_EVENTS")
    memory_house_event_sample: float = Field(default=1.0, alias="HEARTH_MEMORY_HOUSE_EVENT_SAMPLE")
    memory_embeddings: bool = Field(default=True, alias="HEARTH_MEMORY_EMBEDDINGS")
    memory_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="HEARTH_MEMORY_EMBEDDING_MODEL",
    )
    memory_inject: bool = Field(default=True, alias="HEARTH_MEMORY_INJECT")
    memory_retention_days: int = Field(default=90, alias="HEARTH_MEMORY_RETENTION_DAYS")
    memory_house_event_retention_days: int = Field(
        default=30,
        alias="HEARTH_MEMORY_HOUSE_EVENT_RETENTION_DAYS",
    )
    memory_preference_retention_days: int = Field(
        default=0,
        alias="HEARTH_MEMORY_PREFERENCE_RETENTION_DAYS",
    )
    memory_summarize_after: int = Field(default=16, alias="HEARTH_MEMORY_SUMMARIZE_AFTER")
    memory_retrieve_k: int = Field(default=6, alias="HEARTH_MEMORY_RETRIEVE_K")
    memory_session_idle_minutes: int = Field(default=240, alias="HEARTH_MEMORY_SESSION_IDLE_MINUTES")
    memory_prune_interval_minutes: int = Field(default=60, alias="HEARTH_MEMORY_PRUNE_INTERVAL_MINUTES")
    memory_max_turns: int = Field(default=20000, alias="HEARTH_MEMORY_MAX_TURNS")
    memory_max_house_events: int = Field(default=5000, alias="HEARTH_MEMORY_MAX_HOUSE_EVENTS")

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
    # Optional overrides after HA pairing (entity_ids differ per install).
    ha_tv_entity: str = Field(default="media_player.lg_webos_tv", alias="HA_TV_ENTITY")
    ha_avr_entity: str = Field(default="media_player.denon_avr_x3700h", alias="HA_AVR_ENTITY")
    # Apple TV via HA apple_tv / pyatv — used to launch Infuse deep links + transport.
    ha_apple_tv_entity: str = Field(
        default="media_player.apple_tv",
        alias="HA_APPLE_TV_ENTITY",
    )
    # Prefer Infuse (Firecore) over the Plex tvOS app when playing on Apple TV.
    # Set to "plex" to keep the Plex-client playMedia path as the default.
    apple_tv_player: str = Field(default="infuse", alias="HEARTH_APPLE_TV_PLAYER")
    # Bundle id / app id for launch_app fallback (HA media_content_type=app).
    infuse_app_id: str = Field(default="com.firecore.infuse", alias="INFUSE_APP_ID")

    plex_url: str = Field(default="http://host.docker.internal:32400", alias="PLEX_URL")
    plex_token: str = Field(default="", alias="PLEX_TOKEN")
    # Optional default Plex client name / substring (e.g. "Apple TV", "LG", "Living Room").
    plex_default_player: str = Field(default="", alias="PLEX_DEFAULT_PLAYER")
    # When play/confirm finds no clients, re-poll /clients for this long (seconds).
    plex_client_wait_seconds: float = Field(default=12.0, alias="PLEX_CLIENT_WAIT_SECONDS")
    plex_client_poll_interval: float = Field(default=1.5, alias="PLEX_CLIENT_POLL_INTERVAL")

    radarr_url: str = Field(default="http://host.docker.internal:7878", alias="RADARR_URL")
    radarr_api_key: str = Field(default="", alias="RADARR_API_KEY")
    sonarr_url: str = Field(default="http://host.docker.internal:8989", alias="SONARR_URL")
    sonarr_api_key: str = Field(default="", alias="SONARR_API_KEY")
    overseerr_url: str = Field(default="http://host.docker.internal:5055", alias="OVERSEERR_URL")
    overseerr_api_key: str = Field(default="", alias="OVERSEERR_API_KEY")

    docker_socket: str = Field(default="/var/run/docker.sock", alias="DOCKER_SOCKET")

    # Weather (Open-Meteo — no API key). Defaults near Ghent / VAULT.
    weather_latitude: float = Field(default=51.05, alias="HEARTH_WEATHER_LAT")
    weather_longitude: float = Field(default=3.72, alias="HEARTH_WEATHER_LON")
    weather_place: str = Field(default="Home", alias="HEARTH_WEATHER_PLACE")
    weather_force_mock: bool = Field(default=False, alias="HEARTH_WEATHER_MOCK")

    # Glass overlay smart auto-hide (conversation context + idle).
    # Fresh: always show after a panel update. Idle: soft-hide when talk goes quiet.
    # Client uses a short grace before fading on unrelated turns (see app.js).
    overlay_fresh_seconds: int = Field(default=12, alias="HEARTH_OVERLAY_FRESH_SECONDS")
    overlay_idle_seconds: int = Field(default=55, alias="HEARTH_OVERLAY_IDLE_SECONDS")

    # Chief of Staff escalate (repo/code/PR). Empty webhook = not configured.
    cos_webhook: str = Field(default="", alias="HEARTH_COS_WEBHOOK")
    cos_webhook_key: str = Field(default="", alias="HEARTH_COS_WEBHOOK_KEY")
    cos_repo: str = Field(default="RubenVroman/Hearth", alias="HEARTH_COS_REPO")

    # House delivery address for food orders (never invent a street in code).
    hearth_delivery_street: str = Field(default="", alias="HEARTH_DELIVERY_STREET")
    hearth_delivery_postcode: str = Field(default="", alias="HEARTH_DELIVERY_POSTCODE")
    hearth_delivery_city: str = Field(default="", alias="HEARTH_DELIVERY_CITY")
    hearth_delivery_country: str = Field(default="NL", alias="HEARTH_DELIVERY_COUNTRY")

    # Thuisbezorgd / Just Eat Takeaway NL — credentials stay on the host .env only.
    # There is no public self-serve consumer ordering API; partner JE-API-KEY required
    # for live submit. Empty key → fixtures only (browse/cart/confirm still work).
    thuisbezorgd_api_base: str = Field(
        default="https://nl.api.just-eat.io",
        alias="THUISBEZORGD_API_BASE",
    )
    thuisbezorgd_api_key: str = Field(default="", alias="THUISBEZORGD_API_KEY")
    thuisbezorgd_tenant: str = Field(default="nl", alias="THUISBEZORGD_TENANT")
    thuisbezorgd_email: str = Field(default="", alias="THUISBEZORGD_EMAIL")
    thuisbezorgd_password: str = Field(default="", alias="THUISBEZORGD_PASSWORD")
    thuisbezorgd_session_token: str = Field(default="", alias="THUISBEZORGD_SESSION_TOKEN")

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

    @property
    def thuisbezorgd_configured(self) -> bool:
        return bool(self.thuisbezorgd_api_key.strip())

    @property
    def delivery_address_configured(self) -> bool:
        return bool(
            self.hearth_delivery_street.strip()
            and self.hearth_delivery_postcode.strip()
            and self.hearth_delivery_city.strip()
        )


settings = Settings()
