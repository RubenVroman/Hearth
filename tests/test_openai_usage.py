"""OpenAI spend / usage monitor — never invents billed numbers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from hearth.app import app
from hearth.config import settings
from hearth.openai_usage import (
    LocalUsageLedger,
    record_chat_usage,
    record_realtime_usage,
    record_responses_usage,
    record_transcription_usage,
    spend_monitor,
)


def test_spend_endpoint_requires_auth():
    with TestClient(app) as bare:
        response = bare.get("/api/openai/spend")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_spend_endpoint_without_admin_key_is_explicit_unavailable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "openai_admin_key", "")
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")

    response = client.get("/api/openai/spend?days=7")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mode"] == "unavailable"
    assert body["costs"]["available"] is False
    assert body["costs"]["error"] == "missing_key"
    assert body["usage"]["available"] is False
    assert "Admin API key" in body["costs"]["message"]
    assert body["list_pricing"]["label"] == "official list pricing (not your invoice)"
    assert body["security"]["keys_never_sent_to_browser"] is True
    # No fabricated totals
    assert body["costs"].get("summary") is None
    assert "total" not in body["costs"]


def test_pricing_endpoint_returns_official_list_only(client):
    response = client.get("/api/openai/pricing")
    assert response.status_code == 200
    body = response.json()
    assert "not your invoice" in body["label"]
    assert body["source"].startswith("https://")
    assert any(m["id"] == "gpt-4o-mini" for m in body["models"])


def test_local_ledger_records_measured_tokens_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    ledger = LocalUsageLedger()
    ledger.record(
        model="gpt-4o-mini",
        kind="chat",
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cached_input_tokens=200,
    )
    snap = ledger.snapshot()
    assert snap["totals"]["total_tokens"] == 1500
    assert snap["not_openai_billed"] is True
    est = snap["list_price_estimate"]
    assert est["available"] is True
    assert est["estimated_usd"] is not None
    assert "not OpenAI-billed" in est["label"]
    # 800 uncached * 0.15/1M + 200 cached * 0.075/1M + 500 * 0.60/1M
    expected = (800 / 1e6) * 0.15 + (200 / 1e6) * 0.075 + (500 / 1e6) * 0.60
    assert abs(est["estimated_usd"] - expected) < 1e-9


def test_record_chat_usage_ignores_missing_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    from hearth import openai_usage

    openai_usage.local_ledger = LocalUsageLedger()
    record_chat_usage(SimpleNamespace(usage=None), model="gpt-4o-mini")
    assert openai_usage.local_ledger.snapshot()["totals"]["requests"] == 0

    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2),
    )
    record_chat_usage(SimpleNamespace(usage=usage), model="gpt-4o-mini", kind="chat")
    snap = openai_usage.local_ledger.snapshot()
    assert snap["totals"]["input_tokens"] == 10
    assert snap["totals"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_spend_monitor_surfaces_openai_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    monkeypatch.setattr(settings, "openai_admin_key", "sk-admin-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-proj-test")
    from hearth import openai_usage

    openai_usage.local_ledger = LocalUsageLedger()

    rejected = {
        "ok": False,
        "status_code": 401,
        "error": "Incorrect API key provided",
        "body": {"error": {"message": "Incorrect API key provided"}},
    }

    with patch("hearth.openai_usage._paginated_buckets", new=AsyncMock(return_value=rejected)):
        payload = await spend_monitor(days=7)

    assert payload["mode"] == "unavailable"
    assert payload["costs"]["available"] is False
    assert payload["costs"]["error"] == "openai_rejected"
    assert payload["costs"]["status_code"] == 401
    assert "Incorrect API key" in payload["costs"]["message"]
    assert payload["costs"].get("summary") is None


@pytest.mark.asyncio
async def test_spend_monitor_with_real_cost_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    monkeypatch.setattr(settings, "openai_admin_key", "sk-admin-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-proj-test")
    from hearth import openai_usage

    openai_usage.local_ledger = LocalUsageLedger()

    cost_ok = {
        "ok": True,
        "status_code": 200,
        "buckets": [
            {
                "object": "bucket",
                "start_time": 1,
                "end_time": 2,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": 1.25, "currency": "usd"},
                        "line_item": "Chat Completions",
                    }
                ],
            }
        ],
        "meta": {},
    }
    usage_ok = {
        "ok": True,
        "status_code": 200,
        "buckets": [
            {
                "object": "bucket",
                "start_time": 1,
                "end_time": 2,
                "results": [
                    {
                        "model": "gpt-4o-mini",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "input_cached_tokens": 0,
                        "input_audio_tokens": 0,
                        "output_audio_tokens": 0,
                        "num_model_requests": 2,
                    }
                ],
            }
        ],
        "meta": {},
    }

    async def fake_paginated(path, **_kwargs):
        if path.endswith("/costs"):
            return cost_ok
        return usage_ok

    with patch("hearth.openai_usage._paginated_buckets", new=AsyncMock(side_effect=fake_paginated)):
        payload = await spend_monitor(days=7)

    assert payload["mode"] == "openai_billed"
    assert payload["costs"]["summary"]["total"] == 1.25
    assert payload["usage"]["summary"]["totals"]["input_tokens"] == 100
    assert payload["usage"]["summary"]["by_model"][0]["model"] == "gpt-4o-mini"


def test_status_includes_openai_admin_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_admin_key", "")
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["openai_admin"] is False
    monkeypatch.setattr(settings, "openai_admin_key", "sk-admin-present")
    status2 = client.get("/api/status")
    assert status2.json()["openai_admin"] is True


@pytest.fixture
def usage_ledger(tmp_path, monkeypatch):
    from hearth import openai_usage

    monkeypatch.setattr(settings, "memory_db_path", tmp_path / "hearth-memory.db")
    ledger = LocalUsageLedger()
    monkeypatch.setattr(openai_usage, "local_ledger", ledger)
    return ledger


def realtime_response(response_id="resp_voice_1", status="completed"):
    return {
        "id": response_id, "status": status,
        "output": [{"content": [{"transcript": "Private conversation must not be stored"}]}],
        "usage": {
            "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
            "input_token_details": {
                "text_tokens": 300, "audio_tokens": 600, "image_tokens": 100,
                "cached_tokens": 350,
                "cached_tokens_details": {
                    "text_tokens": 100, "audio_tokens": 200, "image_tokens": 50,
                },
            },
            "output_token_details": {"text_tokens": 50, "audio_tokens": 150},
        },
    }


def test_realtime_measures_modalities_and_cached_subsets(usage_ledger):
    record_realtime_usage(realtime_response(), model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    counts = snap["totals"]
    assert counts["requests"] == 1
    assert counts["total_tokens"] == 1200
    assert counts["input_tokens"] == 1000
    assert counts["cached_input_tokens"] == 350
    assert counts["input_audio_tokens"] == 600
    assert counts["cached_input_audio_tokens"] == 200
    assert counts["output_audio_tokens"] == 150
    expected = (200 * 4 + 100 * .4 + 400 * 32 + 200 * .4
                + 50 * 5 + 50 * .5 + 50 * 24 + 150 * 64) / 1e6
    assert snap["list_price_estimate"]["estimated_usd"] == pytest.approx(expected)
    assert snap["list_price_estimate"]["complete"] is True
    assert snap["not_openai_billed"] is True
    assert snap["by_model"][0]["kinds"]["realtime"]["requests"] == 1
    assert "Private conversation" not in json.dumps(snap)
    assert "duration-based transcription" in snap["list_price_estimate"]["excludes"]


def test_realtime_duplicate_delivery_is_not_another_request_even_after_restart(usage_ledger):
    from hearth import openai_usage

    response = realtime_response()
    record_realtime_usage(response, model="gpt-realtime-2.1")
    reloaded = LocalUsageLedger()
    # Replay the provider id after reconnect/process restart.
    with patch.object(openai_usage, "local_ledger", reloaded):
        record_realtime_usage(response, model="gpt-realtime-2.1")
        assert LocalUsageLedger().snapshot()["duplicate_events_ignored"] == 1
        record_realtime_usage(realtime_response("resp_voice_2"), model="gpt-realtime-2.1")
    snap = reloaded.snapshot()
    assert snap["totals"]["requests"] == 2
    assert snap["totals"]["total_tokens"] == 2400
    assert snap["duplicate_events_ignored"] == 1
    assert len(snap["recent_responses"]) == 2


def test_cancelled_and_zero_token_failed_responses_are_visible(usage_ledger):
    record_realtime_usage(realtime_response(status="cancelled"), model="gpt-realtime-2.1")
    record_realtime_usage({"id": "resp_failed", "status": "failed", "usage": {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    }}, model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["requests"] == 2
    assert snap["totals"]["total_tokens"] == 1200
    assert snap["totals"]["by_status"] == {"cancelled": 1, "failed": 1}
    assert snap["recent_responses"][-1]["status"] == "failed"


@pytest.mark.parametrize("missing", ["input_token_details", "cached_tokens_details",
                                      "output_token_details"])
def test_realtime_incomplete_details_never_invent_a_text_price(usage_ledger, missing):
    response = realtime_response()
    if missing == "cached_tokens_details":
        del response["usage"]["input_token_details"][missing]
    else:
        del response["usage"][missing]
    record_realtime_usage(response, model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["total_tokens"] == 1200
    assert snap["list_price_estimate"]["available"] is False
    assert snap["list_price_estimate"]["estimated_usd"] is None
    assert "incomplete" in snap["list_price_estimate"]["by_model"][0]["reason"]


def test_telemetry_retention_is_bounded_without_dropping_aggregate_counts(usage_ledger, monkeypatch):
    from hearth import openai_usage

    monkeypatch.setattr(openai_usage, "_RECENT_RESPONSE_LIMIT", 3)
    monkeypatch.setattr(openai_usage, "_DEDUPE_RESPONSE_LIMIT", 5)
    for index in range(8):
        record_realtime_usage(realtime_response(f"resp_{index}"), model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["requests"] == 8
    assert [row["response_id"] for row in snap["recent_responses"]] == [
        "resp_5", "resp_6", "resp_7",
    ]
    stored = json.loads(openai_usage._usage_store_path().read_text())
    assert len(stored["response_ids"]) == 5
    assert "Private conversation" not in json.dumps(stored)


@pytest.mark.parametrize("recorder,usage", [
    (record_chat_usage, {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 30},
    }),
    (record_responses_usage, {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 20},
        "output_tokens_details": {"reasoning_tokens": 30},
    }),
])
def test_reasoning_is_a_subset_and_response_ids_dedupe(usage_ledger, recorder, usage):
    response = {"id": "resp_reasoning", "usage": usage}
    recorder(response, model="gpt-4o-mini")
    recorder(response, model="gpt-4o-mini")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["requests"] == 1
    assert snap["totals"]["reasoning_output_tokens"] == 30
    assert snap["totals"]["output_tokens"] == 50
    assert snap["totals"]["total_tokens"] == 150
    assert snap["totals"]["cached_input_tokens"] == 20


def test_sdk_objects_and_bad_optional_usage_fields_do_not_break_a_turn(usage_ledger):
    record_realtime_usage(SimpleNamespace(id="resp_object", status={"bad": "shape"},
        usage=SimpleNamespace(input_tokens=5, output_tokens=None, total_tokens=5,
            input_token_details=SimpleNamespace(text_tokens=5, cached_tokens="bad"))),
        model="gpt-realtime-2.1")
    record_realtime_usage({"id": "no_usage", "usage": {}}, model="gpt-realtime-2.1")
    record_realtime_usage({"id": "no_usage"}, model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["requests"] == 1
    assert snap["totals"]["input_tokens"] == 5
    assert snap["totals"]["by_status"] == {"unknown": 1}


def test_legacy_ledger_totals_survive_new_detail_fields(usage_ledger):
    from hearth import openai_usage

    usage_ledger.record(model="gpt-4o-mini", kind="chat", input_tokens=20, output_tokens=5)
    path = openai_usage._usage_store_path()
    data = json.loads(path.read_text())
    data["version"] = 1
    for key in ("response_ids", "recent_responses", "duplicate_events_ignored"):
        data.pop(key, None)
    path.write_text(json.dumps(data))
    restored = LocalUsageLedger()
    restored.record(model="gpt-4o-mini", kind="chat", input_tokens=10, output_tokens=5)
    assert restored.snapshot()["totals"]["total_tokens"] == 40
    assert restored.snapshot()["totals"]["requests"] == 2


@pytest.mark.asyncio
async def test_vision_accounts_for_provider_usage_even_when_result_is_incomplete(usage_ledger):
    from hearth.telegram.media.image_requests import OpenAIVisionProvider, ImageSchemaError

    provider = OpenAIVisionProvider(api_key="test-key", model="gpt-4o-mini")
    response = SimpleNamespace(
        id="chatcmpl_vision", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                                   total_tokens=120),
        choices=[SimpleNamespace(finish_reason="length",
                                 message=SimpleNamespace(refusal=None, content="{"))],
    )
    create = AsyncMock(return_value=response)
    provider._key = "test-key"
    provider._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    with pytest.raises(ImageSchemaError):
        await provider.identify(b"synthetic", "image/png", "", limit=8)
    assert create.await_count == 1
    snap = usage_ledger.snapshot()
    assert snap["by_model"][0]["kinds"]["telegram_vision"]["total_tokens"] == 120


def test_estimate_marks_unknown_model_subtotal_as_partial(usage_ledger):
    usage_ledger.record(model="gpt-4o-mini", kind="chat", input_tokens=100)
    usage_ledger.record(model="unknown-model", kind="chat", input_tokens=500)
    estimate = usage_ledger.snapshot()["list_price_estimate"]
    assert estimate["available"] is True
    assert estimate["complete"] is False
    assert estimate["estimated_usd"] == pytest.approx(100 * .15 / 1e6)


def test_input_transcription_is_separate_measured_usage_without_storing_text(usage_ledger):
    event = {"item_id": "item_speech", "event_id": "event_a", "content_index": 0,
             "transcript": "Private spoken request",
             "usage": {"type": "tokens", "input_tokens": 17, "output_tokens": 9,
                       "total_tokens": 26, "input_token_details": {
                           "text_tokens": 0, "audio_tokens": 17}}}
    record_transcription_usage(event, model="gpt-4o-mini-transcribe")
    event["event_id"] = "event_replayed"
    record_transcription_usage(event, model="gpt-4o-mini-transcribe")
    record_realtime_usage(realtime_response(), model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["requests"] == 2
    assert snap["totals"]["total_tokens"] == 1226
    assert snap["duplicate_events_ignored"] == 1
    transcription = next(row for row in snap["by_model"] if row["model"] == "gpt-4o-mini-transcribe")
    assert transcription["kinds"]["input_transcription"]["input_audio_tokens"] == 17
    assert transcription["output_text_tokens"] == 9
    assert snap["list_price_estimate"]["complete"] is False  # no guessed transcription rate
    assert "Private spoken request" not in json.dumps(snap)


def test_duration_transcription_does_not_invent_token_counts(usage_ledger):
    record_transcription_usage({"item_id": "item_duration", "usage": {
        "type": "duration", "seconds": 4.1,
    }}, model="whisper-1")
    assert usage_ledger.snapshot()["totals"]["requests"] == 0


def test_realtime_total_only_usage_is_not_a_zero_dollar_estimate(usage_ledger):
    record_realtime_usage({"id": "resp_total", "usage": {"total_tokens": 100}},
                          model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["total_tokens"] == 100
    assert snap["totals"]["incomplete_realtime_usage_requests"] == 1
    assert snap["list_price_estimate"]["available"] is False
    assert snap["list_price_estimate"]["complete"] is False


def test_inconsistent_response_breakdowns_cannot_cancel_in_aggregate(usage_ledger):
    first = realtime_response("resp_under")
    second = realtime_response("resp_over")
    first["usage"]["input_token_details"]["text_tokens"] = 200
    second["usage"]["input_token_details"]["text_tokens"] = 400
    record_realtime_usage(first, model="gpt-realtime-2.1")
    record_realtime_usage(second, model="gpt-realtime-2.1")
    snap = usage_ledger.snapshot()
    assert snap["totals"]["input_tokens"] == 2000
    assert snap["totals"]["input_text_tokens"] == 600
    assert snap["totals"]["incomplete_realtime_usage_requests"] == 2
    assert snap["list_price_estimate"]["available"] is False


@pytest.mark.asyncio
async def test_paid_valid_vision_result_survives_usage_ledger_failure(monkeypatch):
    from hearth.telegram.media.image_requests import OpenAIVisionProvider, VisionResult

    expected = VisionResult(kind="not_media", candidates=[], more_visible=False)
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(refusal=None, content=expected.model_dump_json())
    )]))
    provider = OpenAIVisionProvider(api_key="test-key", model="gpt-4o-mini")
    provider._key = "test-key"
    provider._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("hearth.openai_usage.record_chat_usage",
                        Mock(side_effect=RuntimeError("ledger failed")))
    result = await provider.identify(b"synthetic", "image/png", "", limit=8)
    assert result == expected
    create.assert_awaited_once()
