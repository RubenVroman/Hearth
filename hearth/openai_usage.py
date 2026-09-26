"""OpenAI org usage/cost proxy + local measured-token ledger.

Hard rules:
- Never invent account charges or token counts.
- Org Costs/Usage APIs require an Admin API key (OPENAI_ADMIN_KEY).
- List prices are official public rates, labeled as not the invoice.
- Local estimates only multiply *measured* ``response.usage`` token fields
  by those list prices, and are labeled as local estimates — not billed.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from hearth.config import settings

OPENAI_API_BASE = "https://api.openai.com/v1"
OFFICIAL_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
ADMIN_KEYS_URL = "https://platform.openai.com/settings/organization/admin-keys"

# Official list prices for models Hearth commonly uses (USD per 1M tokens unless noted).
# Sourced from OpenAI model/pricing docs — NOT account-specific invoice amounts.
# Update the as_of / source fields when refreshing; never treat these as billed spend.
OFFICIAL_LIST_PRICING: dict[str, Any] = {
    "label": "official list pricing (not your invoice)",
    "source": OFFICIAL_PRICING_URL,
    "as_of": "2026-09-26",
    "unit": "USD per 1M tokens unless noted",
    "models": [
        {
            "id": "gpt-4o-mini",
            "input_per_1m": 0.15,
            "cached_input_per_1m": 0.075,
            "output_per_1m": 0.60,
            "source": "https://developers.openai.com/api/docs/models/gpt-4o-mini",
        },
        {
            "id": "gpt-realtime-2.1",
            "notes": "Realtime multimodal; audio and text priced separately",
            "audio_input_per_1m": 32.0,
            "audio_cached_input_per_1m": 0.40,
            "audio_output_per_1m": 64.0,
            "text_input_per_1m": 4.0,
            "text_cached_input_per_1m": 0.40,
            "text_output_per_1m": 24.0,
            "image_input_per_1m": 5.0,
            "image_cached_input_per_1m": 0.50,
            "source": OFFICIAL_PRICING_URL,
        },
        {
            "id": "text-embedding-3-small",
            "input_per_1m": 0.02,
            "output_per_1m": None,
            "source": "https://developers.openai.com/api/docs/models/text-embedding-3-small",
        },
    ],
}

# Map model id → rate keys for local estimate math (text chat / embeddings).
_RATE_BY_MODEL: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "text-embedding-3-small": {"input": 0.02, "cached_input": 0.02, "output": 0.0},
}

_RECENT_RESPONSE_LIMIT = 128
_DEDUPE_RESPONSE_LIMIT = 2048
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "input_text_tokens", "input_audio_tokens", "input_image_tokens",
    "cached_input_text_tokens", "cached_input_audio_tokens", "cached_input_image_tokens",
    "output_text_tokens", "output_audio_tokens", "reasoning_output_tokens",
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _count(value: Any) -> int:
    # Telemetry must never break a voice turn if a provider field is absent/malformed.
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _response_id(response: Any) -> str:
    value = _field(response, "id")
    return value if isinstance(value, str) and len(value) <= 128 else ""


def _complete_realtime_breakdown(row: dict[str, Any]) -> bool:
    if _count(row.get("incomplete_realtime_usage_requests")):
        return False
    inp = _count(row.get("input_tokens"))
    out = _count(row.get("output_tokens"))
    kinds = ("text", "audio", "image")
    return (
        _count(row.get("total_tokens")) == inp + out
        and sum(_count(row.get(f"input_{kind}_tokens")) for kind in kinds) == inp
        and sum(_count(row.get(f"cached_input_{kind}_tokens")) for kind in kinds)
        == _count(row.get("cached_input_tokens"))
        and sum(_count(row.get(f"output_{kind}_tokens")) for kind in ("text", "audio")) == out
        and all(_count(row.get(f"cached_input_{kind}_tokens"))
                <= _count(row.get(f"input_{kind}_tokens")) for kind in kinds)
    )


def _usage_store_path() -> Path:
    return Path(settings.memory_db_path).resolve().parent / "hearth-openai-local-usage.json"


class LocalUsageLedger:
    """Persist token counts returned by OpenAI on Hearth's own API calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = self._load()
        self._seen_responses = set(self._data.get("response_ids") or [])

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 2,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "by_model": {},
            "response_ids": [],
            "recent_responses": [],
            "duplicate_events_ignored": 0,
            "totals": {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
            },
        }

    def _load(self) -> dict[str, Any]:
        path = _usage_store_path()
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "by_model" in raw:
                    return raw
        except Exception:  # noqa: BLE001
            pass
        return self._empty()

    def _save_unlocked(self) -> None:
        path = _usage_store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001 — house keeps working if disk fails
            pass

    def record(
        self,
        *,
        model: str,
        kind: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cached_input_tokens: int = 0,
        token_details: dict[str, int] | None = None,
        response_id: str = "",
        status: str = "completed",
        record_zero_usage: bool = False,
    ) -> None:
        """Record only numeric fields supplied by OpenAI ``usage`` objects."""
        model = (model or "unknown").strip() or "unknown"
        kind = (kind or "other").strip() or "other"
        inp = _count(input_tokens)
        out = _count(output_tokens)
        cached = min(inp, _count(cached_input_tokens))
        total = _count(total_tokens)
        if total <= 0:
            total = inp + out
        if inp <= 0 and out <= 0 and total <= 0 and not record_zero_usage:
            return
        counts = {key: _count((token_details or {}).get(key)) for key in _TOKEN_FIELDS}
        counts.update(input_tokens=inp, output_tokens=out, total_tokens=total,
                      cached_input_tokens=cached)
        counts["incomplete_realtime_usage_requests"] = int(
            kind == "realtime" and not _complete_realtime_breakdown(counts)
        )
        response_id = response_id if isinstance(response_id, str) and len(response_id) <= 128 else ""
        if not isinstance(status, str) or status not in {
            "completed", "cancelled", "failed", "incomplete"
        }:
            status = "unknown"

        with self._lock:
            if response_id and response_id in self._seen_responses:
                self._data["duplicate_events_ignored"] = (
                    _count(self._data.get("duplicate_events_ignored")) + 1
                )
                self._save_unlocked()
                return
            row = self._data["by_model"].setdefault(
                model,
                {
                    "model": model,
                    "kinds": {},
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                },
            )
            kind_row = row["kinds"].setdefault(
                kind,
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                },
            )
            for target in (row, kind_row, self._data["totals"]):
                target["requests"] = int(target.get("requests") or 0) + 1
                for key, value in counts.items():
                    target[key] = _count(target.get(key)) + value
                statuses = target.setdefault("by_status", {})
                statuses[status] = _count(statuses.get(status)) + 1
            now = datetime.now(timezone.utc).isoformat()
            self._data["updated_at"] = now
            self._data["version"] = 2
            recent = self._data.setdefault("recent_responses", [])
            recent.append({"at": now, "response_id": response_id or None, "model": model,
                           "kind": kind, "status": status, **counts})
            del recent[:-_RECENT_RESPONSE_LIMIT]
            if response_id:
                ids = self._data.setdefault("response_ids", [])
                ids.append(response_id)
                self._seen_responses.add(response_id)
                while len(ids) > _DEDUPE_RESPONSE_LIMIT:
                    self._seen_responses.discard(ids.pop(0))
            self._save_unlocked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self._data))
        estimates = _local_list_price_estimates(data)
        return {
            "available": True,
            "label": "Hearth-tracked local usage (measured tokens from our API responses)",
            "not_openai_billed": True,
            "path": str(_usage_store_path()),
            "started_at": data.get("started_at"),
            "updated_at": data.get("updated_at"),
            "totals": data.get("totals") or {},
            "by_model": list((data.get("by_model") or {}).values()),
            "list_price_estimate": estimates,
            "recent_responses": data.get("recent_responses") or [],
            "duplicate_events_ignored": data.get("duplicate_events_ignored", 0),
            "coverage": (
                "Only provider usage received by Hearth is counted; missing responses and "
                "voice sessions without a server sideband are not included. Local token "
                "estimates exclude separately billed tools, duration-based transcription, "
                "and models without a verified rate mapping."
            ),
            "retention": {"recent_responses": _RECENT_RESPONSE_LIMIT,
                          "deduplicated_response_ids": _DEDUPE_RESPONSE_LIMIT,
                          "aggregate_totals": "since started_at", "conversation_content": False},
        }


local_ledger = LocalUsageLedger()


def record_chat_usage(response: Any, *, model: str, kind: str = "chat") -> None:
    usage = _field(response, "usage")
    if usage is None:
        return
    details = _field(usage, "prompt_tokens_details")
    local_ledger.record(
        model=model or settings.openai_model,
        kind=kind,
        input_tokens=_count(_field(usage, "prompt_tokens")),
        output_tokens=_count(_field(usage, "completion_tokens")),
        total_tokens=_count(_field(usage, "total_tokens")),
        cached_input_tokens=_count(_field(details, "cached_tokens")),
        token_details={"reasoning_output_tokens": _count(
            _field(_field(usage, "completion_tokens_details"), "reasoning_tokens"))},
        response_id=_response_id(response),
    )


def record_embedding_usage(response: Any, *, model: str) -> None:
    usage = _field(response, "usage")
    if usage is None:
        return
    prompt = _count(_field(usage, "prompt_tokens"))
    total = _count(_field(usage, "total_tokens")) or prompt
    local_ledger.record(
        model=model or settings.memory_embedding_model,
        kind="embeddings",
        input_tokens=prompt or total,
        output_tokens=0,
        total_tokens=total,
        cached_input_tokens=0,
        response_id=_response_id(response),
    )


def record_responses_usage(response: Any, *, model: str, kind: str = "responses") -> None:
    usage = _field(response, "usage")
    if usage is None:
        return
    inp = _count(_field(usage, "input_tokens") or _field(usage, "prompt_tokens"))
    out = _count(_field(usage, "output_tokens") or _field(usage, "completion_tokens"))
    details = _field(usage, "input_tokens_details") or _field(usage, "prompt_tokens_details")
    local_ledger.record(
        model=model or settings.openai_model,
        kind=kind,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=_count(_field(usage, "total_tokens")) or (inp + out),
        cached_input_tokens=_count(_field(details, "cached_tokens")),
        token_details={"reasoning_output_tokens": _count(
            _field(_field(usage, "output_tokens_details"), "reasoning_tokens"))},
        response_id=_response_id(response),
        status=_field(response, "status", "completed"),
    )


def record_realtime_usage(response: Any, *, model: str) -> None:
    """Record a provider response.done, including spent tokens on interrupted replies.

    Realtime uses singular ``input_token_details``, unlike the Responses API.
    Cached/reasoning counts are subsets, never additional input/output tokens.
    Only ids, status and usage are retained; never transcript/audio/tool arguments.
    """
    usage = _field(response, "usage")
    if usage is None or not any(
        _field(usage, key) is not None for key in ("input_tokens", "output_tokens", "total_tokens")
    ):
        return
    inp = _field(usage, "input_token_details")
    out = _field(usage, "output_token_details")
    cached = _field(inp, "cached_tokens_details")
    details = {f"input_{kind}_tokens": _count(_field(inp, f"{kind}_tokens"))
               for kind in ("text", "audio", "image")}
    details.update({f"cached_input_{kind}_tokens": _count(_field(cached, f"{kind}_tokens"))
                    for kind in ("text", "audio", "image")})
    details.update({f"output_{kind}_tokens": _count(_field(out, f"{kind}_tokens"))
                    for kind in ("text", "audio")})
    details["reasoning_output_tokens"] = _count(_field(out, "reasoning_tokens"))
    local_ledger.record(
        model=model or settings.openai_realtime_model,
        kind="realtime",
        input_tokens=_count(_field(usage, "input_tokens")),
        output_tokens=_count(_field(usage, "output_tokens")),
        total_tokens=_count(_field(usage, "total_tokens")),
        cached_input_tokens=_count(_field(inp, "cached_tokens")),
        token_details=details,
        response_id=_response_id(response),
        status=_field(response, "status", "unknown"),
        record_zero_usage=True,
    )


def record_transcription_usage(event: Any, *, model: str) -> None:
    """Account for the separately billed input transcription, never its text.

    Token usage is distinct from the speech-to-speech response usage. Duration
    usage (e.g. Whisper) must not be converted to invented token counts.
    """
    usage = _field(event, "usage")
    if _field(usage, "type") != "tokens":
        return
    if not any(_field(usage, key) is not None
               for key in ("input_tokens", "output_tokens", "total_tokens")):
        return
    inp = _field(usage, "input_token_details")
    item_id = _field(event, "item_id") or _field(event, "event_id")
    identity = ""
    if isinstance(item_id, str) and len(item_id) <= 100:
        identity = f"transcription:{item_id}:{_count(_field(event, 'content_index'))}"
    local_ledger.record(
        model=model,
        kind="input_transcription",
        input_tokens=_count(_field(usage, "input_tokens")),
        output_tokens=_count(_field(usage, "output_tokens")),
        total_tokens=_count(_field(usage, "total_tokens")),
        token_details={
            "input_text_tokens": _count(_field(inp, "text_tokens")),
            "input_audio_tokens": _count(_field(inp, "audio_tokens")),
            "output_text_tokens": _count(_field(usage, "output_tokens")),
        },
        response_id=identity,
        record_zero_usage=True,
    )


def _realtime_list_price_estimate(row: dict[str, Any]) -> float | None:
    """Do not silently price unknown audio/cache modalities at text rates."""
    if not _complete_realtime_breakdown(row):
        return None
    rates = next(item for item in OFFICIAL_LIST_PRICING["models"]
                 if item["id"] == "gpt-realtime-2.1")
    cost = 0.0
    for kind in ("text", "audio", "image"):
        kind_input = _count(row.get(f"input_{kind}_tokens"))
        kind_cached = _count(row.get(f"cached_input_{kind}_tokens"))
        cost += ((kind_input - kind_cached) * rates[f"{kind}_input_per_1m"]
                 + kind_cached * rates[f"{kind}_cached_input_per_1m"])
    for kind in ("text", "audio"):
        cost += _count(row.get(f"output_{kind}_tokens")) * rates[f"{kind}_output_per_1m"]
    return cost / 1_000_000.0


def _local_list_price_estimates(data: dict[str, Any]) -> dict[str, Any]:
    """Multiply measured local tokens by official list rates. Never claim this is billed."""
    rows: list[dict[str, Any]] = []
    usd_total = 0.0
    any_priced = False
    for model, row in (data.get("by_model") or {}).items():
        rates = _RATE_BY_MODEL.get(model)
        if model == "gpt-realtime-2.1":
            cost = _realtime_list_price_estimate(row)
            estimate = {"model": model, "available": cost is not None,
                        "input_tokens": row.get("input_tokens"),
                        "output_tokens": row.get("output_tokens")}
            if cost is None:
                estimate["reason"] = "incomplete measured audio/text/image or cache breakdown"
            else:
                usd_total += cost
                any_priced = True
                estimate.update(estimated_usd=round(cost, 6), rate_source=OFFICIAL_PRICING_URL)
            rows.append(estimate)
            continue
        if not rates:
            rows.append(
                {
                    "model": model,
                    "available": False,
                    "reason": "no official list rate mapped for this model id",
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                }
            )
            continue
        inp = int(row.get("input_tokens") or 0)
        cached = int(row.get("cached_input_tokens") or 0)
        # Prefer attributing cached tokens at cached rate when known; remainder at input.
        uncached = max(0, inp - cached)
        out = int(row.get("output_tokens") or 0)
        cost = (
            (uncached / 1_000_000.0) * rates["input"]
            + (cached / 1_000_000.0) * rates["cached_input"]
            + (out / 1_000_000.0) * rates["output"]
        )
        usd_total += cost
        any_priced = True
        rows.append(
            {
                "model": model,
                "available": True,
                "input_tokens": inp,
                "cached_input_tokens": cached,
                "output_tokens": out,
                "estimated_usd": round(cost, 6),
                "rate_source": OFFICIAL_PRICING_URL,
            }
        )
    return {
        "label": "local estimate from measured tokens × official list pricing — not OpenAI-billed",
        "available": any_priced,
        "currency": "usd",
        "estimated_usd": round(usd_total, 6) if any_priced else None,
        "by_model": rows,
        "complete": bool(rows) and all(row["available"] for row in rows),
        "excludes": ["unobserved usage", "separately billed tools", "duration-based transcription"],
    }


def _admin_key() -> str:
    return (settings.openai_admin_key or "").strip()


def _project_key() -> str:
    return (settings.openai_api_key or "").strip()


def _auth_key_for_org_apis() -> tuple[str | None, str]:
    """Org Costs/Usage require an Admin API key. Prefer OPENAI_ADMIN_KEY."""
    admin = _admin_key()
    if admin:
        return admin, "OPENAI_ADMIN_KEY"
    # Documented as insufficient for org admin endpoints; still try so the UI
    # can show the real OpenAI error instead of inventing a failure mode.
    project = _project_key()
    if project:
        return project, "OPENAI_API_KEY"
    return None, "none"


async def _openai_get(
    path: str,
    *,
    params: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    url = f"{OPENAI_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
    try:
        body: Any = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": (response.text or "")[:500]}
    if response.status_code >= 400:
        message = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("code") or "")
            elif err:
                message = str(err)
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": message or f"OpenAI HTTP {response.status_code}",
            "body": body if isinstance(body, dict) else {"detail": str(body)[:400]},
        }
    if not isinstance(body, dict):
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": "unexpected OpenAI response shape",
            "body": {"detail": str(body)[:400]},
        }
    return {"ok": True, "status_code": response.status_code, "data": body}


async def _paginated_buckets(
    path: str,
    *,
    params: dict[str, Any],
    api_key: str,
    max_pages: int = 8,
) -> dict[str, Any]:
    page = params.get("page")
    all_buckets: list[dict[str, Any]] = []
    last_meta: dict[str, Any] = {}
    for _ in range(max_pages):
        query = dict(params)
        if page:
            query["page"] = page
        result = await _openai_get(path, params=query, api_key=api_key)
        if not result.get("ok"):
            return result
        data = result["data"]
        buckets = data.get("data") or []
        if isinstance(buckets, list):
            all_buckets.extend(b for b in buckets if isinstance(b, dict))
        last_meta = {
            "object": data.get("object"),
            "has_more": data.get("has_more"),
            "next_page": data.get("next_page"),
        }
        page = data.get("next_page")
        if not page:
            break
    return {"ok": True, "status_code": 200, "buckets": all_buckets, "meta": last_meta}


def _default_window(days: int = 30) -> tuple[int, int]:
    end = int(time.time())
    start = end - max(1, days) * 24 * 60 * 60
    # Align start to UTC midnight-ish for cleaner daily buckets (optional).
    start = start - (start % 86400)
    return start, end


def _summarize_costs(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0.0
    currency = "usd"
    by_line: dict[str, float] = {}
    days: list[dict[str, Any]] = []
    for bucket in buckets:
        day_amount = 0.0
        results = bucket.get("results") or []
        for row in results:
            if not isinstance(row, dict):
                continue
            amount = row.get("amount") or {}
            if not isinstance(amount, dict):
                continue
            value = amount.get("value")
            if not isinstance(value, (int, float)):
                continue
            day_amount += float(value)
            total += float(value)
            cur = amount.get("currency")
            if isinstance(cur, str) and cur:
                currency = cur.lower()
            line = row.get("line_item") or "unspecified"
            by_line[str(line)] = by_line.get(str(line), 0.0) + float(value)
        days.append(
            {
                "start_time": bucket.get("start_time"),
                "end_time": bucket.get("end_time"),
                "amount": round(day_amount, 6),
            }
        )
    return {
        "currency": currency,
        "total": round(total, 6),
        "by_line_item": [
            {"line_item": k, "amount": round(v, 6)} for k, v in sorted(by_line.items())
        ],
        "days": days,
    }


def _summarize_completions(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_cached_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
        "num_model_requests": 0,
    }
    by_model: dict[str, dict[str, int]] = {}
    days: list[dict[str, Any]] = []
    for bucket in buckets:
        day = {
            "start_time": bucket.get("start_time"),
            "end_time": bucket.get("end_time"),
            "input_tokens": 0,
            "output_tokens": 0,
            "num_model_requests": 0,
        }
        for row in bucket.get("results") or []:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model") or "unknown")
            model_row = by_model.setdefault(
                model,
                {
                    "model": model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "input_cached_tokens": 0,
                    "input_audio_tokens": 0,
                    "output_audio_tokens": 0,
                    "num_model_requests": 0,
                },
            )
            for key in totals:
                val = int(row.get(key) or 0)
                totals[key] += val
                model_row[key] += val
                if key in day:
                    day[key] += val
        days.append(day)
    return {
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda r: r["model"]),
        "days": days,
    }


async def fetch_organization_costs(*, days: int = 30) -> dict[str, Any]:
    key, key_source = _auth_key_for_org_apis()
    if not key:
        return {
            "available": False,
            "source": "openai_organization_costs",
            "error": "missing_key",
            "message": (
                "Set OPENAI_ADMIN_KEY in the host .env (Admin API key from "
                f"{ADMIN_KEYS_URL}). A normal project OPENAI_API_KEY cannot read org costs."
            ),
            "key_source": key_source,
            "requires_admin_key": True,
        }
    start, end = _default_window(days)
    result = await _paginated_buckets(
        "/organization/costs",
        params={
            "start_time": start,
            "end_time": end,
            "bucket_width": "1d",
            "limit": min(180, max(1, days)),
            "group_by": ["line_item"],
        },
        api_key=key,
    )
    if not result.get("ok"):
        return {
            "available": False,
            "source": "openai_organization_costs",
            "error": "openai_rejected",
            "status_code": result.get("status_code"),
            "message": result.get("error") or "OpenAI rejected the costs request",
            "detail": result.get("body"),
            "key_source": key_source,
            "requires_admin_key": key_source != "OPENAI_ADMIN_KEY"
            or int(result.get("status_code") or 0) in {401, 403},
        }
    summary = _summarize_costs(result.get("buckets") or [])
    return {
        "available": True,
        "source": "openai_organization_costs",
        "label": "OpenAI organization costs (billed amounts from Costs API)",
        "key_source": key_source,
        "window": {"start_time": start, "end_time": end, "days": days},
        "summary": summary,
        "raw_bucket_count": len(result.get("buckets") or []),
    }


async def fetch_organization_completions_usage(*, days: int = 30) -> dict[str, Any]:
    key, key_source = _auth_key_for_org_apis()
    if not key:
        return {
            "available": False,
            "source": "openai_organization_usage_completions",
            "error": "missing_key",
            "message": (
                "Set OPENAI_ADMIN_KEY in the host .env to read organization usage. "
                f"Create one at {ADMIN_KEYS_URL}."
            ),
            "key_source": key_source,
            "requires_admin_key": True,
        }
    start, end = _default_window(days)
    result = await _paginated_buckets(
        "/organization/usage/completions",
        params={
            "start_time": start,
            "end_time": end,
            "bucket_width": "1d",
            "limit": min(31, max(1, days)),
            "group_by": ["model"],
        },
        api_key=key,
    )
    if not result.get("ok"):
        return {
            "available": False,
            "source": "openai_organization_usage_completions",
            "error": "openai_rejected",
            "status_code": result.get("status_code"),
            "message": result.get("error") or "OpenAI rejected the usage request",
            "detail": result.get("body"),
            "key_source": key_source,
            "requires_admin_key": key_source != "OPENAI_ADMIN_KEY"
            or int(result.get("status_code") or 0) in {401, 403},
        }
    summary = _summarize_completions(result.get("buckets") or [])
    return {
        "available": True,
        "source": "openai_organization_usage_completions",
        "label": "OpenAI organization completions usage (token counts from Usage API)",
        "key_source": key_source,
        "window": {"start_time": start, "end_time": end, "days": days},
        "summary": summary,
        "raw_bucket_count": len(result.get("buckets") or []),
    }


def official_list_pricing() -> dict[str, Any]:
    return dict(OFFICIAL_LIST_PRICING)


async def spend_monitor(*, days: int = 30) -> dict[str, Any]:
    """Combined payload for the UI — never fabricates billed numbers."""
    days = max(1, min(180, int(days or 30)))
    costs = await fetch_organization_costs(days=days)
    usage = await fetch_organization_completions_usage(days=days)
    local = local_ledger.snapshot()
    pricing = official_list_pricing()

    admin_configured = bool(_admin_key())
    project_configured = bool(_project_key())

    if costs.get("available"):
        mode = "openai_billed"
    elif usage.get("available"):
        mode = "openai_usage_only"
    elif local.get("totals", {}).get("total_tokens"):
        mode = "local_estimate_only"
    else:
        mode = "unavailable"

    guidance: list[str] = []
    if not admin_configured:
        guidance.append(
            "Organization Costs and Usage APIs need an Admin API key "
            f"(OPENAI_ADMIN_KEY). Create one at {ADMIN_KEYS_URL} and keep it in the host .env only."
        )
    if costs.get("error") == "openai_rejected" or usage.get("error") == "openai_rejected":
        guidance.append(
            "OpenAI rejected the org API call — check that OPENAI_ADMIN_KEY is a current "
            "Admin key with organization read access (project keys are not enough)."
        )
    if mode in {"local_estimate_only", "unavailable"} and project_configured:
        guidance.append(
            "Hearth can still show local estimates from measured token fields on its own "
            "API responses, labeled as estimates — not OpenAI-billed."
        )
    if not project_configured and not admin_configured:
        guidance.append("No OpenAI keys configured. Set OPENAI_API_KEY (and optionally OPENAI_ADMIN_KEY).")

    return {
        "ok": True,
        "mode": mode,
        "days": days,
        "openai_project_key_configured": project_configured,
        "openai_admin_key_configured": admin_configured,
        "costs": costs,
        "usage": usage,
        "local": local,
        "list_pricing": pricing,
        "guidance": guidance,
        "security": {
            "keys_never_sent_to_browser": True,
            "note": "Hearth proxies OpenAI server-side. Secrets stay in the host .env / VAULT.",
        },
    }
