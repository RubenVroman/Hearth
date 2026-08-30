"""OpenAI spend / usage monitor — never invents billed numbers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from hearth.app import app
from hearth.config import settings
from hearth.openai_usage import (
    LocalUsageLedger,
    record_chat_usage,
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
