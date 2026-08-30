from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hearth.app import app
from hearth.auth.db import reset_engine
from hearth.config import settings
from hearth.runtime import runtime
from hearth.tools.builtin import register_builtin_tools

TEST_ADMIN_EMAIL = "admin@hearth.test"
TEST_ADMIN_PASSWORD = "test-house-passphrase"
TEST_APP_SECRET = "test-app-secret-key-not-for-production-use"


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "workspace_path", tmp_path)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "openai_admin_key", "")
    monkeypatch.setattr(settings, "ha_token", "")
    monkeypatch.setattr(settings, "plex_token", "")
    monkeypatch.setattr(settings, "mock_if_unconfigured", True)
    monkeypatch.setattr(settings, "token", "")
    monkeypatch.setattr(settings, "cos_webhook", "")
    monkeypatch.setattr(settings, "cos_webhook_key", "")
    monkeypatch.setattr(settings, "radarr_api_key", "")
    monkeypatch.setattr(settings, "sonarr_api_key", "")
    monkeypatch.setattr(settings, "overseerr_api_key", "")
    monkeypatch.setattr(settings, "thuisbezorgd_api_key", "")
    monkeypatch.setattr(settings, "thuisbezorgd_email", "")
    monkeypatch.setattr(settings, "thuisbezorgd_password", "")
    monkeypatch.setattr(settings, "thuisbezorgd_session_token", "")
    monkeypatch.setattr(settings, "hearth_delivery_street", "")
    monkeypatch.setattr(settings, "hearth_delivery_postcode", "")
    monkeypatch.setattr(settings, "hearth_delivery_city", "")
    monkeypatch.setattr(settings, "weather_force_mock", True)
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    monkeypatch.setattr(settings, "web_search_force_mock", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_ids", "")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    monkeypatch.setattr(settings, "telegram_poll", True)
    monkeypatch.setattr(settings, "telegram_webhook_local", False)
    monkeypatch.setattr(settings, "auth_db_path", tmp_path / "hearth-auth.db")
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    monkeypatch.setattr(settings, "memory_enabled", True)
    monkeypatch.setattr(settings, "memory_store_conversations", True)
    monkeypatch.setattr(settings, "memory_store_house_events", False)
    monkeypatch.setattr(settings, "memory_embeddings", False)
    monkeypatch.setattr(settings, "memory_inject", True)
    monkeypatch.setattr(settings, "memory_prune_interval_minutes", 0)
    monkeypatch.setattr(settings, "app_secret_key", TEST_APP_SECRET)
    monkeypatch.setattr(settings, "algorithm", "HS256")
    monkeypatch.setattr(settings, "access_token_expire_minutes", 30)
    monkeypatch.setattr(settings, "refresh_token_expire_days", 7)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "cookie_samesite", "lax")
    monkeypatch.setattr(settings, "admin_email", TEST_ADMIN_EMAIL)
    monkeypatch.setattr(settings, "admin_password", TEST_ADMIN_PASSWORD)
    from hearth.fixtures import mock_thuisbezorgd
    from hearth.tools.thuisbezorgd import thuisbezorgd

    mock_thuisbezorgd.clear_cart()
    mock_thuisbezorgd.orders.clear()
    mock_thuisbezorgd._order_seq = 0
    thuisbezorgd._session_token = ""
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "vault_echo.py").write_text(
        Path(__file__).resolve().parents[1].joinpath("workspace/skills/vault_echo.py").read_text(),
        encoding="utf-8",
    )
    reset_engine()
    runtime.widgets.clear()
    runtime.pending = None
    runtime.last_tools.clear()
    runtime.transcript.clear()
    runtime.set_status("idle")
    runtime._error_until = 0.0
    from hearth.tools.websearch import reset_rate_limit

    reset_rate_limit()
    from hearth.telegram import telegram_inbox

    telegram_inbox.inbox.reset()
    telegram_inbox.running = False
    from hearth.memory.store import init_memory_db, reset_memory

    reset_memory()
    init_memory_db()
    register_builtin_tools()
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        login = test_client.post(
            "/auth/token",
            json={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert token
        test_client.headers.update({"X-Auth-Token": token})
        yield test_client
