"""Agent-facing memory tools. Destructive forget/export/purge need confirm=true."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hearth.agent.registry import ToolSpec, registry
from hearth.config import settings
from hearth.memory import store
from hearth.memory.embed import embed_one, embeddings_enabled, pack_vector
from hearth.memory.redact import redact
from hearth.memory.retrieve import search


async def _embed_pref(pref: dict[str, Any]) -> None:
    if not embeddings_enabled() or not pref.get("ok"):
        return
    text = f"{pref.get('key')}: {pref.get('value')}"
    vec = await embed_one(text)
    if not vec:
        return
    store.put_embedding(
        "preference",
        str(pref["id"]),
        settings.memory_embedding_model,
        pack_vector(vec),
        len(vec),
    )


async def _remember(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled"}
    value = str(args.get("value") or args.get("text") or "").strip()
    key = str(args.get("key") or "").strip() or store.slug_key(value)
    category = str(args.get("category") or "general")
    if not value:
        return {"ok": False, "error": "value required"}
    row = store.remember_preference(key, value, category=category, source="explicit")
    await _embed_pref(row)
    return row


async def _forget(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled"}
    return store.forget(pref_id=str(args.get("id") or ""), key=str(args.get("key") or ""))


async def _search(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled", "hits": []}
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query required", "hits": []}
    hits = await search(query, k=int(args.get("limit") or settings.memory_retrieve_k))
    return {"ok": True, "query": redact(query), "hits": hits, "embeddings": embeddings_enabled()}


async def _list(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled"}
    kind = str(args.get("kind") or "preferences").lower()
    limit = int(args.get("limit") or 20)
    if kind in {"event", "events", "house", "house_events"}:
        return {
            "ok": True,
            "kind": "house_events",
            "store_house_events": bool(settings.memory_store_house_events),
            "items": store.recent_house_events(limit=limit),
        }
    return {
        "ok": True,
        "kind": "preferences",
        "items": store.list_preferences(limit=limit),
    }


async def _export(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled"}
    snapshot = store.export_snapshot()
    dest_dir = Path(settings.workspace_path) / "memory"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["exported_at"].replace(":", "").replace("+", "z")
    path = dest_dir / f"export-{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    rel = path.relative_to(Path(settings.workspace_path)).as_posix()
    return {
        "ok": True,
        "path": rel,
        "counts": snapshot["counts"],
        "note": "Wrote a redacted snapshot in the workspace. Embeddings omitted. Full dump was not sent to the model.",
    }


async def _purge(args: dict[str, Any]) -> dict[str, Any]:
    if not store.memory_enabled():
        return {"ok": False, "error": "house memory is disabled"}
    kind = str(args.get("kind") or "all").lower()
    conversations = kind in {"all", "conversations", "session", "sessions"}
    house_events = kind in {"all", "events", "house", "house_events"}
    preferences = kind in {"all", "preferences", "prefs"}
    if kind not in {"all", "conversations", "session", "sessions", "events", "house", "house_events", "preferences", "prefs"}:
        return {"ok": False, "error": "kind must be all, conversations, house_events, or preferences"}
    return store.purge(conversations=conversations, house_events=house_events, preferences=preferences)


def register_memory_tools() -> None:
    registry.register(
        ToolSpec(
            name="memory_remember",
            description=(
                "Store a stable house preference or fact Ruben wants remembered "
                "(coffee, default scene, favorite player). Not for secrets or API keys."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short slug, e.g. coffee or lights.evening"},
                    "value": {"type": "string", "description": "The fact to remember"},
                    "text": {"type": "string", "description": "Alternative to value when there is no key"},
                    "category": {"type": "string", "description": "general, media, lights, …"},
                },
            },
            handler=_remember,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_forget",
            description="Forget a stored preference by key or id. Destructive: dry-run unless confirm=true.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "id": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
            },
            handler=_forget,
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_search",
            description="Search house memory (preferences, summaries, notable events) by keyword. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=_search,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_list",
            description="List remembered preferences, or recent house events if kind=house_events. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "preferences (default) or house_events",
                    },
                    "limit": {"type": "integer"},
                },
            },
            handler=_list,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_export",
            description=(
                "Export a redacted snapshot of house memory into the workspace. "
                "Destructive/sensitive: dry-run unless confirm=true. Does not dump the store into the prompt."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
            },
            handler=_export,
            destructive=True,
        )
    )
    registry.register(
        ToolSpec(
            name="memory_purge",
            description=(
                "Delete stored memory. kind=all|conversations|house_events|preferences. "
                "Destructive: dry-run unless confirm=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "confirm": {"type": "boolean"},
                    "dry_run": {"type": "boolean"},
                },
            },
            handler=_purge,
            destructive=True,
        )
    )
