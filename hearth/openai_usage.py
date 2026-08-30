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
    "as_of": "2026-08-30",
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

# Map model id → rate keys for local estimate math (text chat / embeddings only).
_RATE_BY_MODEL: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "text-embedding-3-small": {"input": 0.02, "cached_input": 0.02, "output": 0.0},
}


def _usage_store_path() -> Path:
    return Path(settings.memory_db_path).resolve().parent / "hearth-openai-local-usage.json"


class LocalUsageLedger:
    """Persist token counts returned by OpenAI on Hearth's own API calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "by_model": {},
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
    ) -> None:
        """Record only numeric fields supplied by OpenAI ``usage`` objects."""
        model = (model or "unknown").strip() or "unknown"
        kind = (kind or "other").strip() or "other"
        inp = max(0, int(input_tokens or 0))
        out = max(0, int(output_tokens or 0))
        cached = max(0, int(cached_input_tokens or 0))
        total = max(0, int(total_tokens or 0))
        if total <= 0:
            total = inp + out
        if inp <= 0 and out <= 0 and total <= 0:
            return

        with self._lock:
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
                target["input_tokens"] = int(target.get("input_tokens") or 0) + inp
                target["output_tokens"] = int(target.get("output_tokens") or 0) + out
                target["total_tokens"] = int(target.get("total_tokens") or 0) + total
                target["cached_input_tokens"] = int(target.get("cached_input_tokens") or 0) + cached
            self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        }


local_ledger = LocalUsageLedger()


def record_chat_usage(response: Any, *, model: str, kind: str = "chat") -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    details = getattr(usage, "prompt_tokens_details", None)
    cached = 0
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    local_ledger.record(
        model=model or settings.openai_model,
        kind=kind,
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cached_input_tokens=cached,
    )


def record_embedding_usage(response: Any, *, model: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or prompt)
    local_ledger.record(
        model=model or settings.memory_embedding_model,
        kind="embeddings",
        input_tokens=prompt or total,
        output_tokens=0,
        total_tokens=total,
        cached_input_tokens=0,
    )


def record_responses_usage(response: Any, *, model: str, kind: str = "responses") -> None:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return
    if isinstance(usage, dict):
        inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (inp + out))
        cached = 0
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
    else:
        inp = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or (inp + out))
        details = getattr(usage, "input_tokens_details", None) or getattr(
            usage, "prompt_tokens_details", None
        )
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    local_ledger.record(
        model=model or settings.openai_model,
        kind=kind,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=total,
        cached_input_tokens=cached,
    )


def _local_list_price_estimates(data: dict[str, Any]) -> dict[str, Any]:
    """Multiply measured local tokens by official list rates. Never claim this is billed."""
    rows: list[dict[str, Any]] = []
    usd_total = 0.0
    any_priced = False
    for model, row in (data.get("by_model") or {}).items():
        rates = _RATE_BY_MODEL.get(model)
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
