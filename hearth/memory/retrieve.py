"""Retrieve a small relevant slice. Never dump the whole store into the model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hearth.config import settings
from hearth.memory import store
from hearth.memory.embed import cosine, embeddings_enabled, unpack_vector
from hearth.memory.redact import redact

MAX_BLOCK_CHARS = 1800
MAX_PREFERENCES = 20
MAX_HITS = 6


def _recency_score(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 3600.0)
    # 1.0 now, ~0.5 after 2 days, still a bit after two weeks.
    return 1.0 / (1.0 + age_hours / 48.0)


def _hit_text(item: dict[str, Any]) -> str:
    if item.get("text"):
        return str(item["text"])
    if item.get("key"):
        return f"{item.get('key')}: {item.get('value')}"
    if item.get("title"):
        detail = item.get("detail") or ""
        return f"{item['title']}: {detail}".strip(": ")
    return str(item.get("body") or "")


async def search(query: str, *, k: int | None = None) -> list[dict[str, Any]]:
    """Keyword (FTS5) plus optional cosine over stored embeddings."""
    if not store.memory_enabled():
        return []
    limit = k or max(1, int(settings.memory_retrieve_k))
    q = redact(query).strip()
    scored: dict[tuple[str, str], dict[str, Any]] = {}

    def bump(kind: str, owner_id: str, score: float, source: str, payload: dict[str, Any]) -> None:
        key = (kind, owner_id)
        current = scored.get(key)
        if current is None or score > float(current["score"]):
            row = dict(payload)
            row["kind"] = kind
            row["id"] = owner_id
            row["score"] = score
            row["source"] = source
            scored[key] = row

    if q:
        for row in store.fts_search(q, limit=max(limit * 4, 12)):
            owner_kind = str(row["owner_kind"])
            owner_id = str(row["owner_id"])
            rank = float(row.get("rank") or 0.0)
            # bm25: more negative is better; convert to 0..1-ish.
            fts_score = 1.0 / (1.0 + max(0.0, rank + 10.0))
            payload = store.lookup_owner(owner_kind, owner_id) or {
                "kind": owner_kind,
                "text": row.get("body"),
            }
            recency = _recency_score(str(payload.get("ts") or payload.get("updated_at") or ""))
            bump(owner_kind, owner_id, 0.7 * fts_score + 0.3 * recency, "fts", payload)

    query_vec: list[float] | None = None
    if q and embeddings_enabled():
        from hearth.memory.embed import embed_one

        query_vec = await embed_one(q)
    if query_vec:
        for row in store.embeddings_for(
            ["preference", "summary", "house_event", "turn"],
            limit=400,
        ):
            vec = unpack_vector(row["vector"])
            sim = cosine(query_vec, vec)
            if sim < 0.18:
                continue
            owner_kind = str(row["owner_kind"])
            owner_id = str(row["owner_id"])
            payload = store.lookup_owner(owner_kind, owner_id)
            if payload is None:
                continue
            recency = _recency_score(str(payload.get("ts") or payload.get("updated_at") or row.get("ts")))
            bump(owner_kind, owner_id, 0.75 * sim + 0.25 * recency, "embedding", payload)

    ranked = sorted(scored.values(), key=lambda item: float(item["score"]), reverse=True)
    out = []
    for item in ranked[:limit]:
        text = redact(_hit_text(item)).strip()
        if not text:
            continue
        out.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "text": text[:400],
                "score": round(float(item.get("score") or 0.0), 4),
                "source": item.get("source"),
                "ts": item.get("ts") or item.get("updated_at"),
            }
        )
    return out


def _preference_lines(limit: int = MAX_PREFERENCES) -> list[str]:
    lines = []
    for pref in store.list_preferences(limit=limit):
        lines.append(f"- {pref['key']}: {pref['value']}")
    return lines


def prompt_block(query: str = "", *, include_recent_turns: bool = True, hits: list[dict[str, Any]] | None = None) -> str:
    """Compact text injected into the system prompt for chat and Realtime."""
    if not store.memory_enabled() or not settings.memory_inject:
        return ""
    sections: list[str] = []
    prefs = _preference_lines()
    if prefs:
        sections.append("Preferences:\n" + "\n".join(prefs))

    session_id = store.kv_get("current_session_id")
    if session_id:
        summary = store.latest_summary(session_id)
        if summary and summary.get("text"):
            sections.append("Session summary:\n" + redact(str(summary["text"]))[:500])
        if include_recent_turns:
            turns = store.recent_turns(session_id, limit=4)
            if turns:
                bits = []
                for turn in turns:
                    role = turn.get("role") or "user"
                    text = redact(str(turn.get("text") or "")).replace("\n", " ")[:180]
                    bits.append(f"{role}: {text}")
                sections.append("Recent turns:\n" + "\n".join(bits))

    if hits:
        lines = []
        for hit in hits[:MAX_HITS]:
            kind = hit.get("kind") or "note"
            text = redact(str(hit.get("text") or "")).replace("\n", " ")[:220]
            if text:
                lines.append(f"- [{kind}] {text}")
        if lines:
            sections.append("Relevant:\n" + "\n".join(lines))

    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > MAX_BLOCK_CHARS:
        body = body[: MAX_BLOCK_CHARS - 3] + "..."
    return (
        "## Retrieved house memory\n"
        "This is a small slice, not the whole store. Use it when relevant. "
        "Do not invent extra facts. Never quote secrets.\n\n"
        + body
    )


async def prompt_block_async(query: str = "", *, include_recent_turns: bool = True) -> str:
    hits = await search(query, k=int(settings.memory_retrieve_k)) if query.strip() else []
    return prompt_block(query, include_recent_turns=include_recent_turns, hits=hits)


def status_snapshot() -> dict[str, Any]:
    enabled = store.memory_enabled()
    return {
        "enabled": enabled,
        "db": str(store.db_path()),
        "schema_version": store.schema_version() if enabled else 0,
        "store_conversations": bool(settings.memory_store_conversations),
        "store_house_events": bool(settings.memory_store_house_events),
        "embeddings": embeddings_enabled(),
        "embedding_model": settings.memory_embedding_model if embeddings_enabled() else "",
        "inject": bool(settings.memory_inject),
        "retention_days": int(settings.memory_retention_days),
        "house_event_retention_days": int(settings.memory_house_event_retention_days),
        "counts": store.counts() if enabled else {},
        "last_prune_at": store.kv_get("last_prune_at") if enabled else None,
        "privacy": (
            "Secrets are redacted on write. Embeddings (when on) send redacted text to OpenAI. "
            "The model only sees a retrieved slice, not the whole database."
        ),
    }
