from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hearth.app import app
from hearth.auth.db import reset_engine
from hearth.config import settings
from hearth.tools.builtin import register_builtin_tools

TEST_ADMIN_EMAIL = "admin@hearth.test"
TEST_ADMIN_PASSWORD = "test-house-passphrase"
TEST_APP_SECRET = "test-app-secret-key-not-for-production-use"


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "workspace_path", tmp_path)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "ha_token", "")
    monkeypatch.setattr(settings, "plex_token", "")
    monkeypatch.setattr(settings, "mock_if_unconfigured", True)
    monkeypatch.setattr(settings, "token", "")
    monkeypatch.setattr(settings, "cos_webhook", "")
    monkeypatch.setattr(settings, "cos_webhook_key", "")
    monkeypatch.setattr(settings, "radarr_api_key", "")
    monkeypatch.setattr(settings, "sonarr_api_key", "")
    monkeypatch.setattr(settings, "overseerr_api_key", "")
    monkeypatch.setattr(settings, "auth_db_path", tmp_path / "hearth-auth.db")
    monkeypatch.setattr(settings, "app_secret_key", TEST_APP_SECRET)
    monkeypatch.setattr(settings, "algorithm", "HS256")
    monkeypatch.setattr(settings, "access_token_expire_minutes", 30)
    monkeypatch.setattr(settings, "refresh_token_expire_days", 7)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "cookie_samesite", "lax")
    monkeypatch.setattr(settings, "admin_email", TEST_ADMIN_EMAIL)
    monkeypatch.setattr(settings, "admin_password", TEST_ADMIN_PASSWORD)
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "vault_echo.py").write_text(
        Path(__file__).resolve().parents[1].joinpath("workspace/skills/vault_echo.py").read_text(),
        encoding="utf-8",
    )
    reset_engine()
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
