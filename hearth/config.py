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
    # Admin API key for organization Costs/Usage endpoints (not for model inference).
    # Create at https://platform.openai.com/settings/organization/admin-keys
    openai_admin_key: str = Field(default="", alias="OPENAI_ADMIN_KEY")
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
    # Optional exact scene id for "movie night" / "lights down". Empty asks HA
    # to resolve the friendly name "Movie night", avoiding install-specific ids.
    ha_movie_night_scene: str = Field(default="", alias="HA_MOVIE_NIGHT_SCENE")
    # Optional comfort devices. Empty = discover climate / purifier / feeder in HA.
    ha_climate_entity: str = Field(default="", alias="HA_CLIMATE_ENTITY")
    ha_purifier_entity: str = Field(default="", alias="HA_PURIFIER_ENTITY")
    ha_feeder_entity: str = Field(default="", alias="HA_FEEDER_ENTITY")
    # Live HA calls are retried and writes are verified. These deliberately live
    # in Hearth rather than relying only on TCP retries: an accepted service call
    # can still leave a slow TV/receiver in the old state for a few seconds.
    ha_request_retries: int = Field(default=3, alias="HA_REQUEST_RETRIES")
    ha_retry_base_seconds: float = Field(default=0.25, alias="HA_RETRY_BASE_SECONDS")
    ha_verify_timeout_seconds: float = Field(default=6.0, alias="HA_VERIFY_TIMEOUT_SECONDS")
    ha_verify_poll_interval: float = Field(default=0.4, alias="HA_VERIFY_POLL_INTERVAL")
    ha_entity_cache_seconds: float = Field(default=45.0, alias="HA_ENTITY_CACHE_SECONDS")

    # --- House devices beyond media: PetZero feeders + Tuya OEM (Smart Life) ---
    # Tuya entity ids are pairing-specific, so every role is a comma-separated
    # candidate list instead of one id. Hearth tries each candidate against live
    # HA state and falls back to keyword discovery, so a wrong default degrades
    # into "which entity did you mean" rather than controlling the wrong device.
    # Run the ha_discover_entities tool after pairing and paste the real ids here.
    ha_pet_feeder_entities: str = Field(
        default=(
            "button.pet_feeder_feed,button.petzero_feed,"
            "switch.pet_feeder_feed,switch.pet_feeder"
        ),
        alias="HA_PET_FEEDER_ENTITIES",
    )
    # Tuya feeders usually expose portions as a number entity rather than as
    # service data on the feed button.
    ha_pet_feeder_portion_entities: str = Field(
        default="number.pet_feeder_portion,number.petzero_portion,number.pet_feeder_manual_feed",
        alias="HA_PET_FEEDER_PORTION_ENTITIES",
    )
    ha_pet_feeder_schedule_entities: str = Field(
        default="switch.pet_feeder_schedule,switch.petzero_schedule,switch.pet_feeder_auto_feed",
        alias="HA_PET_FEEDER_SCHEDULE_ENTITIES",
    )
    ha_pet_feeder_default_portions: int = Field(
        default=1,
        ge=1,
        alias="HA_PET_FEEDER_DEFAULT_PORTIONS",
    )
    ha_pet_feeder_max_portions: int = Field(
        default=6,
        ge=1,
        alias="HA_PET_FEEDER_MAX_PORTIONS",
    )
    # Dispensed food cannot be undone, and voice/Telegram make a double-feed easy.
    # A repeat inside this window needs force=true instead of a silent second meal.
    ha_pet_feeder_cooldown_seconds: float = Field(
        default=600.0,
        ge=0.0,
        alias="HA_PET_FEEDER_COOLDOWN_SECONDS",
    )
    ha_airco_entities: str = Field(
        default=(
            "climate.airco,climate.air_conditioner,"
            "climate.airconditioner,climate.living_room_ac"
        ),
        alias="HA_AIRCO_ENTITIES",
    )
    ha_airco_default_temperature: float = Field(
        default=21.0,
        alias="HA_AIRCO_DEFAULT_TEMPERATURE",
    )
    # "Airco on" has to pick a real hvac mode; an air conditioner cools by default.
    ha_airco_default_mode: str = Field(default="cool", alias="HA_AIRCO_DEFAULT_MODE")
    # Guardrails for spoken numbers: "airco 2" and "airco 45" are misheard, not requests.
    ha_airco_min_temperature: float = Field(default=16.0, alias="HA_AIRCO_MIN_TEMPERATURE")
    ha_airco_max_temperature: float = Field(default=30.0, alias="HA_AIRCO_MAX_TEMPERATURE")
    # KPT Air Purifier is a fan in Tuya Local; some OEM builds expose it as a
    # humidifier or a plain switch, so all three domains stay in the candidates.
    ha_air_purifier_entities: str = Field(
        default="fan.air_purifier,fan.kpt_air_purifier,humidifier.air_purifier,switch.air_purifier",
        alias="HA_AIR_PURIFIER_ENTITIES",
    )
    # The Denon is the switching/audio hub. Activity commands wake the chain in
    # order and route its input before playback is sent to the Apple TV.
    receiver_centric: bool = Field(default=True, alias="HEARTH_RECEIVER_CENTRIC")
    ha_avr_apple_tv_source: str = Field(default="Media Player", alias="HA_AVR_APPLE_TV_SOURCE")
    ha_avr_tv_source: str = Field(default="TV Audio", alias="HA_AVR_TV_SOURCE")
    ha_media_settle_seconds: float = Field(default=0.5, alias="HA_MEDIA_SETTLE_SECONDS")
    # Prefer Infuse (Firecore) over the Plex tvOS app when playing on Apple TV.
    # Set to "plex" to keep the Plex-client playMedia path as the default.
    apple_tv_player: str = Field(default="infuse", alias="HEARTH_APPLE_TV_PLAYER")
    # Bundle id / app id for launch_app fallback (HA media_content_type=app).
    infuse_app_id: str = Field(default="com.firecore.infuse", alias="INFUSE_APP_ID")
    # Videoland on LG webOS — launch via media_player.select_source (friendly name).
    # Title playback / profile select are NOT supported by HA webostv; see videoland tool.
    videoland_source: str = Field(default="Videoland", alias="VIDEOLAND_SOURCE")
    # Optional raw webOS app id for an experimental contentId launch attempt only.
    # Leave empty unless you have verified the id on this TV (listApps / HA source).
    videoland_app_id: str = Field(default="", alias="VIDEOLAND_APP_ID")

    plex_url: str = Field(default="http://host.docker.internal:32400", alias="PLEX_URL")
    plex_token: str = Field(default="", alias="PLEX_TOKEN")
    # Optional default Plex client name / substring (e.g. "Apple TV", "LG", "Living Room").
    plex_default_player: str = Field(default="", alias="PLEX_DEFAULT_PLAYER")
    # When play/confirm finds no clients, re-poll /clients for this long (seconds).
    plex_client_wait_seconds: float = Field(default=12.0, alias="PLEX_CLIENT_WAIT_SECONDS")
    plex_client_poll_interval: float = Field(default=1.5, alias="PLEX_CLIENT_POLL_INTERVAL")
    # A playMedia HTTP 2xx only means PMS accepted the command. Observe a matching
    # playing session before telling the house that playback actually started.
    plex_play_verify_timeout_seconds: float = Field(
        default=6.0,
        alias="PLEX_PLAY_VERIFY_TIMEOUT_SECONDS",
    )
    plex_play_verify_poll_interval: float = Field(
        default=0.5,
        alias="PLEX_PLAY_VERIFY_POLL_INTERVAL",
    )

    radarr_url: str = Field(default="http://host.docker.internal:7878", alias="RADARR_URL")
    radarr_api_key: str = Field(default="", alias="RADARR_API_KEY")
    sonarr_url: str = Field(default="http://host.docker.internal:8989", alias="SONARR_URL")
    sonarr_api_key: str = Field(default="", alias="SONARR_API_KEY")
    overseerr_url: str = Field(default="http://host.docker.internal:5055", alias="OVERSEERR_URL")
    overseerr_api_key: str = Field(default="", alias="OVERSEERR_API_KEY")
    # Failed/stalled grab: blocklist + alternate *arr release (not Overseerr re-POST).
    download_max_retries: int = Field(default=3, alias="HEARTH_DOWNLOAD_MAX_RETRIES")
    # Zero-progress "downloading" for this long → treat as stalled (seconds).
    download_stall_idle_seconds: float = Field(
        default=20 * 60,
        alias="HEARTH_DOWNLOAD_STALL_IDLE_SECONDS",
    )

    docker_socket: str = Field(default="/var/run/docker.sock", alias="DOCKER_SOCKET")

    # Weather (Open-Meteo — no API key). Defaults near Ghent / VAULT.
    weather_latitude: float = Field(default=51.05, alias="HEARTH_WEATHER_LAT")
    weather_longitude: float = Field(default=3.72, alias="HEARTH_WEATHER_LON")
    weather_place: str = Field(default="Home", alias="HEARTH_WEATHER_PLACE")
    weather_force_mock: bool = Field(default=False, alias="HEARTH_WEATHER_MOCK")

    # Live web search (house tool). Prefer OpenAI hosted web_search via OPENAI_API_KEY.
    # Optional Brave key skips the extra model hop and returns structured snippets.
    # Empty keys + HEARTH_MOCK_IF_UNCONFIGURED → fixtures; otherwise DuckDuckGo HTML lite.
    brave_search_api_key: str = Field(default="", alias="BRAVE_SEARCH_API_KEY")
    web_search_force_mock: bool = Field(default=False, alias="HEARTH_WEB_SEARCH_MOCK")

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

    # Telegram house bot. Routine HA commands share the normal tool/Jev path;
    # Overseerr remains the sole media search/request backend, while
    # Radarr/Sonarr are observed only for download progress. The bot is off
    # unless its token and at least one allowlisted chat are configured.
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_ids: str = Field(default="", alias="TELEGRAM_CHAT_IDS")
    # Optional comma-separated Telegram user ids (house members). Empty = any
    # member of an allowlisted group may request (bot still ignored).
    telegram_user_ids: str = Field(default="", alias="TELEGRAM_USER_IDS")
    # Operational kill switch. A configured bot otherwise runs one getUpdates
    # long-poller; Hearth does not expose a Telegram webhook.
    telegram_poll: bool = Field(default=True, alias="TELEGRAM_POLL")
    telegram_rate_limit_per_minute: int = Field(
        default=6,
        alias="TELEGRAM_RATE_LIMIT_PER_MINUTE",
    )
    telegram_max_title_length: int = Field(default=200, alias="TELEGRAM_MAX_TITLE_LENGTH")
    telegram_progress_interval_seconds: float = Field(
        default=45.0,
        alias="TELEGRAM_PROGRESS_INTERVAL_SECONDS",
    )
    telegram_concurrency: int = Field(default=4, ge=1, le=32, alias="TELEGRAM_CONCURRENCY")
    telegram_callback_ttl_seconds: int = Field(
        default=6 * 60 * 60,
        ge=60,
        alias="TELEGRAM_CALLBACK_TTL_SECONDS",
    )
    telegram_db_path: Path = Field(
        default=Path("./data/hearth-telegram.db"),
        alias="TELEGRAM_DB_PATH",
    )
    # Magic lanes on top of the Jev router. Each one degrades to the plain title
    # search when disabled or when its Overseerr route is unavailable.
    telegram_mood_lane: bool = Field(default=True, alias="HEARTH_TELEGRAM_MOOD_LANE")
    telegram_person_lane: bool = Field(default=True, alias="HEARTH_TELEGRAM_PERSON_LANE")
    telegram_similar_lane: bool = Field(default=True, alias="HEARTH_TELEGRAM_SIMILAR_LANE")
    telegram_batch_lane: bool = Field(default=True, alias="HEARTH_TELEGRAM_BATCH_LANE")
    telegram_batch_max_items: int = Field(
        default=4,
        ge=2,
        le=8,
        alias="HEARTH_TELEGRAM_BATCH_MAX_ITEMS",
    )
    # In-thread follow-up memory ("the sequel", "all of them", "more like that").
    telegram_context_ttl_seconds: int = Field(
        default=30 * 60,
        ge=60,
        le=24 * 60 * 60,
        alias="HEARTH_TELEGRAM_CONTEXT_TTL_SECONDS",
    )
    # House-butler phrasing. Off keeps the plain operational sentences.
    telegram_butler_voice: bool = Field(default=True, alias="HEARTH_TELEGRAM_BUTLER_VOICE")

    # Plex-aware Get/Play buttons + status marks (Overseerr mediaStatus).
    telegram_status_truth: bool = Field(default=True, alias="HEARTH_TELEGRAM_STATUS_TRUTH")
    # House night-mode moods (Friday night for us / kids for Parel / cooking).
    telegram_house_nights: bool = Field(default=True, alias="HEARTH_TELEGRAM_HOUSE_NIGHTS")
    # After queuing Part One, remember Part Two for "what's next" / sequel.
    telegram_watch_next: bool = Field(default=True, alias="HEARTH_TELEGRAM_WATCH_NEXT")
    # Confidence-scaled butler verbosity (terse on exact Get, warmer on mood).
    telegram_voice_verbosity: bool = Field(default=True, alias="HEARTH_TELEGRAM_VOICE_VERBOSITY")
    # Remote Play button / "put it on the TV" via Infuse or Plex.
    telegram_play_lane: bool = Field(default=True, alias="HEARTH_TELEGRAM_PLAY_LANE")

    # TypeSafe Jev (System One) — cheap typed decision gate before gpt/tools.
    # Off by default. When enabled, shadow mode logs only (does not enforce).
    # API key stays on the VAULT host .env; never log it.
    typesafe_api_key: str = Field(default="", alias="TYPESAFE_API_KEY")
    jev_enabled: bool = Field(default=False, alias="HEARTH_JEV_ENABLED")
    jev_shadow: bool = Field(default=True, alias="HEARTH_JEV_SHADOW")
    # Pin with e.g. jev-1.13.0 once thresholds are tuned; alias moves with releases.
    jev_model: str = Field(default="jev-latest", alias="HEARTH_JEV_MODEL")
    jev_domain_confidence: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_DOMAIN_CONFIDENCE",
    )
    jev_cancel_threshold: float = Field(
        default=0.78,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_CANCEL_THRESHOLD",
    )
    jev_confirm_threshold: float = Field(
        default=0.78,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_CONFIRM_THRESHOLD",
    )
    jev_media_ask_confidence: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_MEDIA_ASK_CONFIDENCE",
    )
    # Confidence floor for the house-device router and the device tool gate.
    jev_device_confidence: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_DEVICE_CONFIDENCE",
    )
    jev_needs_llm_threshold: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_NEEDS_LLM_THRESHOLD",
    )
    jev_multi_item_threshold: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        alias="HEARTH_JEV_MULTI_ITEM_THRESHOLD",
    )

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def typesafe_configured(self) -> bool:
        return bool(self.typesafe_api_key.strip())

    @property
    def openai_admin_configured(self) -> bool:
        return bool(self.openai_admin_key.strip())

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
    def brave_search_configured(self) -> bool:
        return bool(self.brave_search_api_key.strip())

    @property
    def web_search_live(self) -> bool:
        """True when a live search backend key is present (OpenAI and/or Brave)."""
        return self.openai_configured or self.brave_search_configured

    @property
    def delivery_address_configured(self) -> bool:
        return bool(
            self.hearth_delivery_street.strip()
            and self.hearth_delivery_postcode.strip()
            and self.hearth_delivery_city.strip()
        )

    @staticmethod
    def _parse_entity_list(raw: str) -> list[str]:
        """Split a comma/semicolon entity candidate list, preserving order."""
        out: list[str] = []
        for part in (raw or "").replace(";", ",").split(","):
            entity_id = part.strip()
            if entity_id and entity_id not in out:
                out.append(entity_id)
        return out

    @property
    def pet_feeder_entity_list(self) -> list[str]:
        return self._parse_entity_list(self.ha_pet_feeder_entities)

    @property
    def pet_feeder_portion_entity_list(self) -> list[str]:
        return self._parse_entity_list(self.ha_pet_feeder_portion_entities)

    @property
    def pet_feeder_schedule_entity_list(self) -> list[str]:
        return self._parse_entity_list(self.ha_pet_feeder_schedule_entities)

    @property
    def airco_entity_list(self) -> list[str]:
        return self._parse_entity_list(self.ha_airco_entities)

    @property
    def air_purifier_entity_list(self) -> list[str]:
        return self._parse_entity_list(self.ha_air_purifier_entities)

    @staticmethod
    def _parse_id_list(raw: str) -> list[int]:
        out: list[int] = []
        for part in (raw or "").replace(";", ",").split(","):
            bit = part.strip()
            if not bit:
                continue
            try:
                out.append(int(bit))
            except ValueError:
                continue
        return out

    @staticmethod
    def _id_list_valid(raw: str) -> bool:
        parts = [part.strip() for part in (raw or "").replace(";", ",").split(",")]
        values = [part for part in parts if part]
        if not values:
            return True
        try:
            for value in values:
                int(value)
        except ValueError:
            return False
        return True

    @property
    def telegram_chat_id_list(self) -> list[int]:
        return self._parse_id_list(self.telegram_chat_ids)

    @property
    def telegram_user_id_list(self) -> list[int]:
        """Empty list means no user allowlist (any group member may request)."""
        return self._parse_id_list(self.telegram_user_ids)

    @property
    def telegram_chat_ids_valid(self) -> bool:
        return self._id_list_valid(self.telegram_chat_ids)

    @property
    def telegram_user_ids_valid(self) -> bool:
        return self._id_list_valid(self.telegram_user_ids)

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.telegram_bot_token.strip()
            and self.telegram_chat_id_list
            and self.telegram_chat_ids_valid
            and self.telegram_user_ids_valid
        )


settings = Settings()
