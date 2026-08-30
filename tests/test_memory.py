from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hearth.agent.loop import AgentLoop, route_intent
from hearth.agent.prompts import compose_system_prompt, SYSTEM_PROMPT
from hearth.agent.registry import registry
from hearth.config import settings
from hearth.memory.embed import cosine, pack_vector, unpack_vector
from hearth.memory.redact import REDACTED, redact
from hearth.memory.retrieve import prompt_block, search
from hearth.memory.store import (
    SCHEMA_VERSION,
    add_summary,
    counts,
    export_snapshot,
    forget,
    init_memory_db,
    list_preferences,
    log_house_event,
    persist_turn,
    prune,
    remember_preference,
    schema_version,
)


def test_schema_initializes_and_is_idempotent():
    init_memory_db()
    init_memory_db()
    assert schema_version() == SCHEMA_VERSION
    assert schema_version() == 1
    snap = counts()
    assert snap["preferences"] == 0
    assert snap["turns"] == 0


def test_redact_strips_keys_tokens_passwords_and_env_assignments(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-live-hearth-secret-key")
    monkeypatch.setattr(settings, "plex_token", "plexSecretToken99")
    text = (
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuv "
        "HA_TOKEN=abc "
        "password=super-secret-pass "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaaaaabbbbbbbcccccc.dddddeeeeeefffff "
        "X-Auth-Token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaaaaabbbbbbbcccccc.dddddeeeeeefffff "
        f"live {settings.openai_api_key} and plex {settings.plex_token}"
    )
    out = redact(text)
    assert "sk-proj-" not in out
    assert "sk-live-hearth-secret-key" not in out
    assert "plexSecretToken99" not in out
    assert "super-secret-pass" not in out
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
    assert REDACTED in out


def test_remember_redacts_secrets_on_write(monkeypatch):
    monkeypatch.setattr(settings, "ha_token", "ha-long-lived-token-value")
    row = remember_preference("ha", f"token is {settings.ha_token}")
    assert row["ok"] is True
    assert settings.ha_token not in row["value"]
    stored = list_preferences()[0]
    assert settings.ha_token not in stored["value"]
    dump = export_snapshot()
    blob = str(dump)
    assert settings.ha_token not in blob


def test_retrieve_returns_small_slice_not_the_whole_store():
    for i in range(40):
        remember_preference(f"fact-{i}", f"stable fact number {i} about the house")
    session = persist_turn("user", "turn the kitchen lights on")
    assert session is not None
    persist_turn("assistant", "Kitchen is on.", session_id=session["session_id"])
    add_summary(
        session["session_id"],
        "Ruben asked about kitchen lights.",
        covers_until_ts=session["ts"],
        source="heuristic",
    )
    block = prompt_block("kitchen lights")
    assert "Retrieved house memory" in block
    assert len(block) <= 2200
    assert "stable fact number 0" in block or "fact-" in block
    # Must not inline every stored row.
    assert block.count("stable fact number") <= 24


async def test_fts_search_finds_preference_without_embeddings():
    remember_preference("coffee", "Pour-over in the morning, not espresso")
    hits = await search("pour-over coffee")
    assert hits
    assert any("Pour-over" in hit["text"] for hit in hits)


def test_prune_drops_old_turns_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "memory_retention_days", 1)
    monkeypatch.setattr(settings, "memory_max_turns", 3)
    persist_turn("user", "old enough to drop")
    persist_turn("assistant", "reply")
    # Backdate the first turn.
    from hearth.memory.store import connect

    conn = connect()
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn.execute("UPDATE turns SET ts = ? WHERE role = 'user'", (old,))
    conn.commit()
    persist_turn("user", "recent a")
    persist_turn("assistant", "recent b")
    persist_turn("user", "recent c")
    persist_turn("assistant", "recent d")
    result = prune()
    assert result["ok"] is True
    assert counts()["turns"] <= 3
    texts = [row["text"] for row in connect().execute("SELECT text FROM turns").fetchall()]
    assert "old enough to drop" not in texts


async def test_forget_requires_confirm_then_deletes():
    remember_preference("coffee", "Pour-over")
    dry = await registry.call("memory_forget", {"key": "coffee"})
    assert dry.needs_confirm
    assert dry.dry_run
    assert list_preferences()
    live = await registry.call("memory_forget", {"key": "coffee", "confirm": True})
    assert live.ok
    assert not live.needs_confirm
    assert list_preferences() == []


async def test_export_and_purge_require_confirm(isolated_workspace):
    remember_preference("tv", "LG in the living room")
    dry = await registry.call("memory_export", {})
    assert dry.needs_confirm
    live = await registry.call("memory_export", {"confirm": True})
    assert live.ok
    assert live.data["path"].startswith("memory/")
    written = isolated_workspace / live.data["path"]
    assert written.is_file()
    body = written.read_text(encoding="utf-8")
    assert "embeddings" not in body.lower() or "omitted" in body.lower()
    dry_purge = await registry.call("memory_purge", {"kind": "preferences"})
    assert dry_purge.needs_confirm
    purged = await registry.call("memory_purge", {"kind": "preferences", "confirm": True})
    assert purged.ok
    assert list_preferences() == []


async def test_agent_loop_injects_retrieved_memory_into_openai_prompt(monkeypatch):
    remember_preference("coffee", "Pour-over in the morning")
    captured: dict = {}

    class FakeMessage:
        content = "Got it — pour-over."
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, api_key=None):
            self.chat = FakeChat()

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-hearth-memory")
    with patch("openai.AsyncOpenAI", FakeClient):
        loop = AgentLoop()
        out = await loop.run("what coffee do I like?")
    assert out["mode"] == "openai"
    system = captured["messages"][0]["content"]
    assert captured["messages"][0]["role"] == "system"
    assert "Pour-over in the morning" in system
    assert SYSTEM_PROMPT.split("\n", 1)[0] in system
    assert "Retrieved house memory" in system
    # Whole store is not dumped: only a slice.
    assert len(system) < len(SYSTEM_PROMPT) + 2500


def test_local_router_remember_list_and_persists_conversation(client):
    remember = client.post("/api/chat", json={"message": "remember that I like pour-over coffee"})
    assert remember.status_code == 200
    assert remember.json()["tools"][0]["name"] == "memory_remember"
    assert "pour-over" in remember.json()["reply"].lower()

    listed = client.post("/api/chat", json={"message": "what do you remember"})
    assert listed.json()["tools"][0]["name"] == "memory_list"
    assert "pour-over" in listed.json()["reply"].lower()

    status = client.get("/api/memory").json()
    assert status["enabled"] is True
    assert status["counts"]["turns"] >= 2
    assert any("pour-over" in (p.get("value") or "").lower() for p in status["preferences"])


def test_house_events_off_by_default_and_opt_in(client, monkeypatch):
    before = client.get("/api/memory").json()
    assert before["store_house_events"] is False
    client.post(
        "/api/invoke",
        json={"tool": "radarr_add", "args": {"query": "Dune", "confirm": True}},
    )
    after = client.get("/api/memory").json()
    assert after["house_events"] == []

    monkeypatch.setattr(settings, "memory_store_house_events", True)
    client.post(
        "/api/invoke",
        json={"tool": "radarr_add", "args": {"query": "Dune", "confirm": True}},
    )
    enabled = client.get("/api/memory").json()
    assert enabled["house_events"]
    assert "radarr" in enabled["house_events"][0]["title"].lower() or "dune" in enabled["house_events"][0]["title"].lower()


def test_memory_api_requires_auth_and_confirm():
    from fastapi.testclient import TestClient
    from hearth.app import app

    with TestClient(app) as anon:
        assert anon.get("/api/memory").status_code == 401
        assert anon.post("/api/memory/remember", json={"value": "x"}).status_code == 401


def test_memory_api_forget_and_export_need_confirm(client):
    client.post("/api/memory/remember", json={"key": "scene", "value": "movie night"})
    denied = client.post("/api/memory/forget", json={"key": "scene"})
    assert denied.status_code == 400
    forgotten = client.post("/api/memory/forget", json={"key": "scene", "confirm": True})
    assert forgotten.status_code == 200
    export_denied = client.post("/api/memory/export")
    assert export_denied.status_code == 400
    exported = client.post("/api/memory/export?confirm=true")
    assert exported.status_code == 200
    assert "preferences" in exported.json()
    assert "vector" not in str(exported.json()).lower()


def test_intent_memory_routes():
    assert route_intent("remember that I like Dune")["tool"] == "memory_remember"
    assert route_intent("forget that Dune")["tool"] == "memory_forget"
    assert route_intent("what do you remember")["tool"] == "memory_list"
    assert route_intent("do you remember my coffee")["tool"] == "memory_search"


def test_embedding_helpers_roundtrip_without_openai():
    vec = [0.0, 0.5, 1.0]
    blob = pack_vector(vec)
    assert unpack_vector(blob) == vec
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0


def test_compose_system_prompt_is_the_realtime_hook():
    remember_preference("avr", "Denon lives in the living room")
    text = compose_system_prompt("denon volume")
    assert "Denon lives in the living room" in text
    assert "Retrieved house memory" in text


async def test_summarize_rolls_up_long_sessions(monkeypatch):
    monkeypatch.setattr(settings, "memory_summarize_after", 4)
    from hearth.memory.store import ensure_session, latest_summary
    from hearth.memory.summarize import maybe_summarize

    sid = ensure_session("chat")
    for i in range(3):
        persist_turn("user", f"ask {i} about kitchen lights", session_id=sid)
        persist_turn("assistant", f"Kitchen update {i}.", session_id=sid)
    row = await maybe_summarize(sid)
    assert row is not None
    assert "kitchen" in row["text"].lower() or "asked" in row["text"].lower()
    assert latest_summary(sid)["source"] == "heuristic"


def test_log_house_event_respects_flag(monkeypatch):
    monkeypatch.setattr(settings, "memory_store_house_events", False)
    assert log_house_event("Grabbed Dune", "radarr") is None
    monkeypatch.setattr(settings, "memory_store_house_events", True)
    row = log_house_event("Grabbed Dune", "queued in Radarr", kind="media", tool_name="radarr_add")
    assert row is not None
    assert "Dune" in row["title"]
