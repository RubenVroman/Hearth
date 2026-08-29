from __future__ import annotations

from pathlib import Path

import pytest

from hearth.config import settings
from hearth.tools.builtin import register_builtin_tools


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
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "vault_echo.py").write_text(
        Path(__file__).resolve().parents[1].joinpath("workspace/skills/vault_echo.py").read_text(),
        encoding="utf-8",
    )
    register_builtin_tools()
    return tmp_path
