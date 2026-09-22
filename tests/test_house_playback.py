from __future__ import annotations

from typing import Any

import pytest

from hearth.agent.loop import route_intent
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.runtime import Widget, runtime
from hearth.telegram.media.play import PlayOutcome
from hearth.telegram.models import MediaHit
from hearth.tools.ha import HomeAssistant
from hearth.tools.infuse import Infuse
from hearth.tools.plex import Plex


async def test_media_path_rejects_unknown_denon_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistant()
    calls: list[tuple[str, str, str | None]] = []

    async def resolve(device: str) -> dict[str, Any]:
        assert device == "avr"
        return {
            "ok": True,
            "entity_id": "media_player.receiver",
            "state": {
                "state": "on",
                "attributes": {"source_list": ["Blu-ray", "TV Audio"]},
            },
        }

    async def control(
        device: str,
        action: str,
        *,
        source: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((device, action, source))
        return {
            "ok": True,
            "device": device,
            "action": action,
            "service": f"media_player.{action}",
        }

    monkeypatch.setattr(settings, "receiver_centric", True)
    monkeypatch.setattr(settings, "ha_avr_apple_tv_source", "Game Console")
    monkeypatch.setattr(client, "resolve_device_state", resolve)
    monkeypatch.setattr(client, "media_control", control)

    result = await client.activate_media_path("apple_tv")

    assert result["ok"] is False
    assert result["failed_steps"] == 1
    source_step = next(step for step in result["steps"] if step["step"] == "avr_source")
    assert source_step["ok"] is False
    assert source_step["available_sources"] == ["Blu-ray", "TV Audio"]
    assert "Game Console" in source_step["error"]
    assert all(action != "select_source" for _, action, _ in calls)


async def test_play_media_distinguishes_launch_from_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistant()
    state = {
        "entity_id": "media_player.apple_tv",
        "state": "idle",
        "attributes": {
            "app_name": "Infuse",
            "media_content_id": "infuse://movie/430231?play",
        },
    }

    async def resolve(_device: str) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "entity_id": "media_player.apple_tv",
            "state": state,
        }

    async def call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "accepted": True, "mode": "live", "attempts": 1}

    async def verify(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
        return state, True

    monkeypatch.setattr(client, "resolve_device_state", resolve)
    monkeypatch.setattr(client, "call_service", call)
    monkeypatch.setattr(client, "_verify_media_state", verify)

    result = await client.media_control(
        "apple_tv",
        "play_media",
        media_content_id="infuse://movie/430231?play",
        media_content_type="url",
    )

    assert result["ok"] is True
    assert result["launch_verified"] is True
    assert result["playback_confirmed"] is False


async def test_device_discovery_does_not_use_remote_as_play_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistant()

    async def entities() -> dict[str, Any]:
        return {
            "ok": True,
            "states": [
                {
                    "entity_id": "remote.apple_tv",
                    "state": "on",
                    "attributes": {"friendly_name": "Apple TV"},
                },
                {
                    "entity_id": "media_player.lg_tv",
                    "state": "on",
                    "attributes": {"friendly_name": "LG TV"},
                },
            ],
        }

    monkeypatch.setattr(client, "list_media_entities", entities)

    assert await client._find_by_hint("apple_tv") is None


async def test_infuse_reports_partial_media_path_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Infuse()

    async def plan(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "live",
            "deep_link": "infuse://movie/430231?play",
            "entity_id": "media_player.apple_tv",
            "item": {"title": "The Endless"},
            "tmdbId": 430231,
        }

    async def activity(_target: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "Denon source: receiver input did not change",
        }

    async def control(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "accepted": True,
            "mode": "live",
            "launch_verified": True,
            "playback_confirmed": True,
        }

    from importlib import import_module

    infuse_module = import_module("hearth.tools.infuse")
    monkeypatch.setattr(client, "resolve_play", plan)
    monkeypatch.setattr(infuse_module.ha, "activate_media_path", activity)
    monkeypatch.setattr(infuse_module.ha, "media_control", control)

    result = await client.play("The Endless")

    assert result["ok"] is False
    assert result["played"] is True
    assert result["launched"] is True
    assert "living-room path is not ready" in result["speak"]


async def test_movie_night_activates_scene_and_media_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_movie_night_scene", "")
    monkeypatch.setattr(settings, "receiver_centric", True)
    result = await registry.call("media_activity", {"activity": "movie_night"})

    assert result.ok
    assert result.data["scene"]["entity_id"] == "scene.movie_night"
    assert [step["step"] for step in result.data["steps"]] == [
        "movie_night_scene",
        "media_path",
    ]
    assert result.data["steps"][1]["activity"] == "apple_tv"


def test_house_command_routes_are_natural_and_config_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ha_movie_night_scene", "scene.cinema_time")

    assert route_intent("movie night") == {
        "tool": "media_activity",
        "args": {"activity": "movie_night"},
    }
    assert route_intent("lights down") == {
        "tool": "ha_device_control",
        "args": {
            "device": "scene.cinema_time",
            "domain": "scene",
            "action": "activate",
        },
    }
    assert route_intent("turn it down") == {
        "tool": "ha_media_control",
        "args": {"device": "avr", "action": "volume_down"},
    }
    assert route_intent("pause") == {
        "tool": "infuse_transport",
        "args": {"action": "pause"},
    }


def test_put_it_on_tv_uses_active_media_card() -> None:
    runtime.upsert_widget(
        Widget(
            id="media",
            kind="media",
            title="The Endless",
            data={
                "active_id": "plex:2042",
                "item": {
                    "id": "plex:2042",
                    "title": "The Endless",
                    "ratingKey": "2042",
                    "tmdbId": 430231,
                },
                "items": [
                    {
                        "id": "plex:2042",
                        "title": "The Endless",
                        "ratingKey": "2042",
                        "tmdbId": 430231,
                    }
                ],
            },
        )
    )

    assert route_intent("put it on the TV") == {
        "tool": "infuse_play",
        "args": {
            "query": "The Endless",
            "ratingKey": "2042",
            "tmdbId": 430231,
        },
    }


async def test_plex_verification_requires_matching_playing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Plex()
    monkeypatch.setattr(settings, "plex_token", "configured")
    monkeypatch.setattr(settings, "plex_play_verify_timeout_seconds", 0)
    monkeypatch.setattr(settings, "plex_play_verify_poll_interval", 0)

    async def wrong_session() -> dict[str, Any]:
        return {
            "mode": "live",
            "sessions": [
                {
                    "title": "Another Film",
                    "ratingKey": "99",
                    "state": "playing",
                    "player": "Apple TV",
                }
            ],
        }

    monkeypatch.setattr(client, "now_playing", wrong_session)
    result = await client._verify_playback(
        {"title": "The Endless", "ratingKey": "2042"},
        {"name": "Apple TV", "machineIdentifier": "client-apple"},
    )

    assert result["ok"] is False
    assert "No matching playing session" in result["error"]


async def test_telegram_put_it_on_tv_uses_thread_context(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hearth.telegram import bot as bot_module
    from hearth.telegram.bot import TelegramMediaBot
    from hearth.telegram.store import TelegramStore

    chat_id = -10042
    user_id = 42
    monkeypatch.setattr(settings, "telegram_bot_token", "123:test")
    monkeypatch.setattr(settings, "telegram_chat_ids", str(chat_id))
    monkeypatch.setattr(settings, "telegram_user_ids", str(user_id))
    monkeypatch.setattr(settings, "telegram_play_lane", True)
    store = TelegramStore(tmp_path / "telegram-house-play.db")
    bot = TelegramMediaBot(store, overseerr_client=object())
    bot.memory.remember(
        chat_id,
        hits=[
            MediaHit(
                media_type="movie",
                tmdb_id=430231,
                title="The Endless",
                year=2017,
                media_status=5,
            )
        ],
        ask_kind="exact_title",
        ask_text="The Endless",
        search_title="The Endless",
    )

    async def play(**kwargs: Any) -> PlayOutcome:
        assert kwargs["title"] == "The Endless"
        assert kwargs["tmdb_id"] == 430231
        return PlayOutcome(True, "Playing The Endless (2017) on Apple TV.", "infuse")

    async def classify(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("explicit house play must not enter media classification")

    monkeypatch.setattr(bot_module, "play_on_tv", play)
    monkeypatch.setattr(bot_module, "classify_media_ask", classify)
    try:
        reply = await bot.handle_message(
            {
                "message_id": 1,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": user_id, "is_bot": False},
                "text": "put it on the TV",
            }
        )
    finally:
        store.close()

    assert reply is not None
    assert reply.text == "Playing The Endless (2017) on Apple TV."
