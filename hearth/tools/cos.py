"""Escalate repo/code/PR work to Chief of Staff. Hearth never edits GitHub."""

from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings

DEFAULT_REPO = "RubenVroman/Hearth"
# Documented auth header: Authorization: Bearer <HEARTH_COS_WEBHOOK_KEY>
AUTH_HEADER = "Authorization"


def not_configured_message() -> str:
    return (
        "Chief of Staff is not configured. Set HEARTH_COS_WEBHOOK in .env "
        "(optional HEARTH_COS_WEBHOOK_KEY is sent as 'Authorization: Bearer <key>'). "
        "Hearth does not edit GitHub, talk to Gridways, Discord, calendar, or teammate agents."
    )


def build_payload(args: dict[str, Any]) -> dict[str, Any]:
    said = str(args.get("said") or "").strip()
    task = str(args.get("task") or said).strip()
    repo = str(args.get("repo") or settings.cos_repo or DEFAULT_REPO).strip() or DEFAULT_REPO
    return {
        "source": "hearth",
        "task": task,
        "repo": repo,
        "confirm": True,
        "said": said or task,
    }


def cos_configured() -> bool:
    return bool(settings.cos_webhook.strip())


async def escalate(args: dict[str, Any]) -> dict[str, Any]:
    payload = build_payload(args)
    webhook = settings.cos_webhook.strip()
    if not webhook:
        return {
            "ok": False,
            "configured": False,
            "escalated": False,
            "error": not_configured_message(),
            "payload": payload,
        }

    headers = {"Content-Type": "application/json"}
    key = settings.cos_webhook_key.strip()
    if key:
        headers[AUTH_HEADER] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook, json=payload, headers=headers)
        return {
            "ok": response.is_success,
            "configured": True,
            "escalated": response.is_success,
            "repo": payload["repo"],
            "status_code": response.status_code,
            "payload": payload,
            "body": _clip(response.text),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "configured": True,
            "escalated": False,
            "error": f"Chief of Staff webhook failed: {exc}",
            "payload": payload,
        }


def _clip(text: str, limit: int = 500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"
