"""Call-count regressions for supporting models; all providers are local fakes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hearth.config import settings
from hearth.memory import store
from hearth.memory import summarize
from hearth.memory.embed import pack_vector
from hearth.memory.retrieve import prompt_block_async, search, session_context_block
from hearth.telegram.media import catalog


def _response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _provider(monkeypatch, create):
    opened = []
    closed = []

    class Client:
        def __init__(self, **options):
            opened.append(options)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            self.embeddings = SimpleNamespace(create=create)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.append(True)

    monkeypatch.setattr("openai.AsyncOpenAI", Client)
    monkeypatch.setattr(settings, "openai_api_key", "test-provider-key")
    monkeypatch.setattr("hearth.openai_usage.record_chat_usage", Mock())
    monkeypatch.setattr("hearth.openai_usage.record_embedding_usage", Mock())
    return opened, closed


def _conversation(monkeypatch):
    monkeypatch.setattr(settings, "memory_summarize_after", 4)
    sid = store.ensure_session("chat")
    for index in range(2):
        store.persist_turn("user", f"kitchen question {index}", session_id=sid)
        store.persist_turn("assistant", f"kitchen answer {index}", session_id=sid)
    return sid


async def test_slow_summary_survives_caller_deadline_and_is_paid_once(monkeypatch):
    sid = _conversation(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return _response("The kitchen lights were discussed.")

    opened, closed = _provider(monkeypatch, complete)
    first = asyncio.create_task(summarize.maybe_summarize(sid))
    await started.wait()
    first.cancel()  # AgentLoop's short user-response deadline expires.
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(summarize.maybe_summarize(sid))
    await asyncio.sleep(0)
    release.set()
    row = await second
    assert row["source"] == "openai"
    assert len(calls) == 1
    assert len(opened) == len(closed) == 1
    assert opened[0]["max_retries"] == 0
    assert await summarize.maybe_summarize(sid) is None
    assert len(calls) == 1
    assert store.counts()["summaries"] == 1


async def test_summary_deadline_persists_fallback_without_repaying(monkeypatch):
    sid = _conversation(monkeypatch)
    monkeypatch.setattr(summarize, "SUMMARY_TIMEOUT_SECONDS", 0.01)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()

    _, closed = _provider(monkeypatch, complete)
    row = await summarize.maybe_summarize(sid)
    assert row["source"] == "heuristic"
    assert "kitchen" in row["text"]
    assert await summarize.maybe_summarize(sid) is None
    assert len(calls) == len(closed) == 1


async def test_finishing_summary_cannot_restore_forgotten_conversation(monkeypatch):
    sid = _conversation(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def complete(**_kwargs):
        started.set()
        await release.wait()
        return _response("Previously remembered kitchen details.")

    _provider(monkeypatch, complete)
    pending = asyncio.create_task(summarize.maybe_summarize(sid))
    await started.wait()
    store.purge(conversations=True)
    release.set()
    assert await pending is None
    assert store.counts()["summaries"] == 0


async def test_disabled_memory_injection_does_no_retrieval(monkeypatch):
    monkeypatch.setattr(settings, "memory_inject", False)
    retrieve = AsyncMock(side_effect=AssertionError("Discarded retrieval must not run"))
    monkeypatch.setattr("hearth.memory.retrieve.search", retrieve)
    assert await prompt_block_async("what coffee do I like?") == ""
    retrieve.assert_not_awaited()


@pytest.mark.parametrize("stored_model", [None, "a-different-embedding-model"])
async def test_empty_or_incompatible_vector_index_skips_paid_query(monkeypatch, stored_model):
    monkeypatch.setattr(settings, "memory_embeddings", True)
    monkeypatch.setattr(settings, "openai_api_key", "test-provider-key")
    pref = store.remember_preference("coffee", "Pour-over in the morning")
    if stored_model:
        store.put_embedding("preference", pref["id"], stored_model, pack_vector([1, 0]), 2)
    embed = AsyncMock(side_effect=AssertionError("No compatible vectors to search"))
    monkeypatch.setattr("hearth.memory.embed.embed_one", embed)
    hits = await search("coffee")
    assert any("Pour-over" in hit["text"] for hit in hits)
    embed.assert_not_awaited()


async def test_compatible_vector_index_keeps_semantic_recall(monkeypatch):
    monkeypatch.setattr(settings, "memory_embeddings", True)
    monkeypatch.setattr(settings, "openai_api_key", "test-provider-key")
    pref = store.remember_preference("coffee", "Pour-over in the morning")
    store.put_embedding(
        "preference", pref["id"], settings.memory_embedding_model, pack_vector([1, 0]), 2
    )
    embed = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr("hearth.memory.embed.embed_one", embed)
    hits = await search("favorite beverage")
    assert any(hit["source"] == "embedding" and "Pour-over" in hit["text"] for hit in hits)
    embed.assert_awaited_once_with("favorite beverage")


async def test_live_voice_context_keeps_prior_memory_without_reinjecting_own_turns():
    prior = store.persist_turn("user", "My favorite classic is Alien.", channel="voice")
    snapshot = session_context_block()
    current = store.persist_turn("user", "Tell me about Alien's director.", channel="voice")
    store.remember_preference("viewing", "Prefer original-language audio")
    block = await prompt_block_async(
        "Alien", session_context=snapshot, exclude_turn_ids={current["id"]}
    )
    assert "My favorite classic is Alien." in block
    assert "Prefer original-language audio" in block
    assert "Tell me about Alien's director." not in block
    hits = await search("Alien", k=1, exclude_turn_ids={current["id"]})
    assert hits[0]["id"] == prior["id"]


async def test_empty_voice_bootstrap_stays_empty_when_live_turns_arrive():
    snapshot = session_context_block()
    assert snapshot == ""
    current = store.persist_turn("user", "Tell me about Alien.", channel="voice")
    block = await prompt_block_async(
        "Alien", session_context=snapshot, exclude_turn_ids={current["id"]}
    )
    assert block == ""


async def test_excluded_turn_only_vector_index_does_not_embed_query(monkeypatch):
    monkeypatch.setattr(settings, "memory_embeddings", True)
    monkeypatch.setattr(settings, "openai_api_key", "test-provider-key")
    current = store.persist_turn("user", "New current voice turn")
    store.put_embedding(
        "turn", current["id"], settings.memory_embedding_model, pack_vector([1, 0]), 2
    )
    embed = AsyncMock(side_effect=AssertionError("Only excluded conversation vectors exist"))
    monkeypatch.setattr("hearth.memory.embed.embed_one", embed)
    assert await search("voice", exclude_turn_ids={current["id"]}) == []
    embed.assert_not_awaited()


@pytest.mark.parametrize("mode", ["guess", "answer"])
async def test_catalog_calls_are_single_attempt_closed_and_metered(monkeypatch, mode):
    payload = (
        {"search_title": "Alien", "year": 1979, "confidence": 0.95, "media_kind": "movie"}
        if mode == "guess" else
        {"answer": "It was released in 1979.", "search_title": "Alien", "year": 1979}
    )
    complete = AsyncMock(return_value=_response(json.dumps(payload)))
    opened, closed = _provider(monkeypatch, complete)
    if mode == "guess":
        result = await catalog.guess_catalog_titles("horror aboard a spaceship")
        assert result[0].search_title == "Alien"
    else:
        result = await catalog.answer_catalog_question("When did Alien come out?")
        assert result["answer"] == "It was released in 1979."
    complete.assert_awaited_once()
    assert opened[0]["max_retries"] == 0
    assert opened[0]["timeout"] == catalog.CATALOG_TIMEOUT_SECONDS
    assert len(closed) == 1
    from hearth.openai_usage import record_chat_usage

    assert record_chat_usage.call_args.kwargs["kind"] == "telegram_catalog"


async def test_embedding_client_closes_and_disables_implicit_retries(monkeypatch):
    from hearth.memory.embed import EMBED_TIMEOUT_SECONDS, embed_one

    monkeypatch.setattr(settings, "memory_embeddings", True)
    complete = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])])
    )
    opened, closed = _provider(monkeypatch, complete)
    assert await embed_one("coffee") == [1.0, 0.0]
    complete.assert_awaited_once()
    assert opened[0]["max_retries"] == 0
    assert opened[0]["timeout"] == EMBED_TIMEOUT_SECONDS
    assert len(closed) == 1


async def test_paid_catalog_answer_survives_usage_ledger_failure(monkeypatch, caplog):
    complete = AsyncMock(return_value=_response(json.dumps({"answer": "Released in 1979."})))
    _provider(monkeypatch, complete)
    monkeypatch.setattr("hearth.openai_usage.record_chat_usage",
                        Mock(side_effect=RuntimeError("PRIVATE_PROVIDER_PAYLOAD")))
    result = await catalog.answer_catalog_question("When did Alien come out?")
    assert result["answer"] == "Released in 1979."
    complete.assert_awaited_once()
    assert "RuntimeError" in caplog.text
    assert "PRIVATE_PROVIDER_PAYLOAD" not in caplog.text
