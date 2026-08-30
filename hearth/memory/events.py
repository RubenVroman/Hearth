"""Optional house-event log. Off by default; notable confirmed writes only."""

from __future__ import annotations

import json
import random
from typing import Any

from hearth.agent.registry import ToolResult, ToolSpec
from hearth.config import settings
from hearth.memory import store
from hearth.memory.redact import redact, redact_obj

# Read/search tools are not house history. Memory tools would recurse.
_NOTABLE = {
    "ha_call_service",
    "ha_device_control",
    "ha_media_control",
    "media_activity",
    "radarr_add",
    "sonarr_add",
    "overseerr_request",
    "docker_stop",
    "workspace_write",
    "workspace_delete",
    "chief_of_staff",
}


def _sample_ok() -> bool:
    rate = float(settings.memory_house_event_sample)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


def on_tool_result(spec: ToolSpec, result: ToolResult) -> None:
    if not store.memory_enabled() or not settings.memory_store_house_events:
        return
    if spec.name not in _NOTABLE:
        return
    if spec.name.startswith("memory_"):
        return
    if result.needs_confirm or result.dry_run:
        return
    if not result.ok:
        return
    if not _sample_ok():
        return
    data = redact_obj(result.data) if isinstance(result.data, dict) else {"result": redact(str(result.data))}
    title = _title(spec.name, data if isinstance(data, dict) else {})
    detail = json.dumps(data, default=str)[:1500]
    row = store.log_house_event(
        title,
        detail,
        kind=_kind(spec.name),
        tool_name=spec.name,
        ok=True,
        notable=True,
    )
    if row:
        _schedule_embed("house_event", row["id"], f"{row['title']} {row['detail']}")


def _kind(name: str) -> str:
    if name.startswith("ha_"):
        return "ha"
    if name in {"radarr_add", "sonarr_add", "overseerr_request"}:
        return "media"
    if name.startswith("docker"):
        return "docker"
    if name.startswith("workspace"):
        return "workspace"
    if name == "chief_of_staff":
        return "cos"
    return "tool"


def _title(name: str, data: dict[str, Any]) -> str:
    if name == "ha_call_service":
        entity = data.get("entity") or {}
        if isinstance(entity, dict):
            return f"HA {entity.get('entity_id') or 'entity'} → {entity.get('state') or 'updated'}"
        return "HA service"
    if name == "radarr_add":
        added = data.get("added") or {}
        return f"Grabbed {added.get('title') or 'a movie'} in Radarr"
    if name == "sonarr_add":
        added = data.get("added") or {}
        return f"Grabbed {added.get('title') or 'a show'} in Sonarr"
    if name == "overseerr_request":
        item = data.get("requested") or {}
        return f"Requested {item.get('title') or 'title'} in Overseerr"
    if name == "chief_of_staff":
        return "Escalated to Chief of Staff"
    return name


def _schedule_embed(kind: str, owner_id: str, text: str) -> None:
    """Best-effort: embed from async loops; skip if we are not in one."""
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_embed(kind, owner_id, text))


async def _embed(kind: str, owner_id: str, text: str) -> None:
    from hearth.config import settings as cfg
    from hearth.memory.embed import embed_one, embeddings_enabled, pack_vector

    if not embeddings_enabled():
        return
    vec = await embed_one(text)
    if not vec:
        return
    store.put_embedding(kind, owner_id, cfg.memory_embedding_model, pack_vector(vec), len(vec))
