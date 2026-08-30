"""Summarize long sessions so the prompt stays small."""

from __future__ import annotations

from hearth.config import settings
from hearth.memory import store
from hearth.memory.redact import redact


def heuristic_summary(turns: list[dict]) -> str:
    users = [str(t.get("text") or "").strip() for t in turns if t.get("role") == "user"]
    assistants = [str(t.get("text") or "").strip() for t in turns if t.get("role") == "assistant"]
    tools = [str(t.get("tool_name") or "") for t in turns if t.get("tool_name")]
    parts: list[str] = []
    if users:
        asked = "; ".join(u[:160] for u in users[-8:] if u)
        if asked:
            parts.append(f"Ruben asked: {asked}")
    if assistants:
        last = assistants[-1][:200]
        if last:
            parts.append(f"Last reply: {last}")
    named = [t for t in tools if t]
    if named:
        uniq = []
        for name in named:
            if name not in uniq:
                uniq.append(name)
        parts.append("Tools: " + ", ".join(uniq[:12]))
    return redact(" ".join(parts))[:1500] or "Short house conversation."


async def maybe_summarize(session_id: str) -> dict | None:
    if not store.memory_enabled() or not session_id:
        return None
    session = store.session_row(session_id)
    if session is None:
        return None
    threshold = max(4, int(settings.memory_summarize_after))
    if int(session.get("turn_count") or 0) < threshold:
        return None
    existing = store.latest_summary(session_id)
    after = str(existing["covers_until_ts"]) if existing and existing.get("covers_until_ts") else None
    turns = store.turns_since(session_id, after, limit=80)
    if len(turns) < max(4, threshold // 2):
        return None
    covers = str(turns[-1]["ts"])
    text = heuristic_summary(turns)
    source = "heuristic"
    if settings.openai_configured:
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=settings.openai_api_key)
            blob = "\n".join(
                f"{t.get('role')}: {redact(str(t.get('text') or ''))[:400]}" for t in turns[-40:]
            )
            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize this house-agent conversation for later recall. "
                            "Keep facts, preferences, and notable house actions. "
                            "No secrets, tokens, or passwords. Max 120 words."
                        ),
                    },
                    {"role": "user", "content": blob},
                ],
                max_tokens=220,
            )
            try:
                from hearth.openai_usage import record_chat_usage

                record_chat_usage(response, model=settings.openai_model, kind="summary")
            except Exception:  # noqa: BLE001
                pass
            drafted = (response.choices[0].message.content or "").strip()
            if drafted:
                text = redact(drafted)[:1500]
                source = "openai"
        except Exception:  # noqa: BLE001
            source = "heuristic"
    row = store.add_summary(session_id, text, covers_until_ts=covers, source=source)
    await _embed_owner("summary", row["id"], row["text"])
    return row


async def _embed_owner(kind: str, owner_id: str, text: str) -> None:
    from hearth.memory.embed import embed_one, embeddings_enabled, pack_vector
    from hearth.config import settings as cfg

    if not embeddings_enabled() or not text.strip():
        return
    vec = await embed_one(text)
    if not vec:
        return
    store.put_embedding(kind, owner_id, cfg.memory_embedding_model, pack_vector(vec), len(vec))
