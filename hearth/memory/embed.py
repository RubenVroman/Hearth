"""Optional OpenAI embeddings. No local model — 512m container cannot host one.

When ``OPENAI_API_KEY`` is unset or ``HEARTH_MEMORY_EMBEDDINGS=false``, callers
fall back to FTS5. Using embeddings sends redacted text to OpenAI.
"""

from __future__ import annotations

import array
import math
from typing import Sequence

from hearth.config import settings


def embeddings_enabled() -> bool:
    return bool(settings.memory_embeddings) and bool(settings.openai_api_key.strip())


def pack_vector(values: Sequence[float]) -> bytes:
    return array.array("f", values).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    vec = array.array("f")
    vec.frombytes(blob)
    return list(vec)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for a, b in zip(left, right):
        dot += a * b
        na += a * a
        nb += b * b
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed redacted strings. Returns None when embeddings are off or the API fails."""
    if not embeddings_enabled():
        return None
    cleaned = [t.strip() for t in texts if t and t.strip()]
    if not cleaned:
        return None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.embeddings.create(
            model=settings.memory_embedding_model,
            input=cleaned,
        )
        try:
            from hearth.openai_usage import record_embedding_usage

            record_embedding_usage(response, model=settings.memory_embedding_model)
        except Exception:  # noqa: BLE001
            pass
        by_index = {item.index: list(item.embedding) for item in response.data}
        return [by_index[i] for i in range(len(cleaned))]
    except Exception:  # noqa: BLE001 — house keeps working on FTS
        return None


async def embed_one(text: str) -> list[float] | None:
    result = await embed_texts([text])
    if not result:
        return None
    return result[0]
