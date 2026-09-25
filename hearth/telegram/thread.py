"""Durable Telegram thread: recent turns, last house action, pending confirm.

Stored in the same bounded callback KV as media context, scoped per speaker in
group chats. Full message bodies are clipped. This is what lets a follow-up
("and tomorrow?", "also dim the lights") see the previous house reply.
"""

from __future__ import annotations

from dataclasses import dataclass

from hearth.telegram.media.memory import ContextStore, storage_key

_PREFIX = "thread:"
_MAX_TURNS = 8
_MAX_CHARS = 240


def _clip(text: str) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:_MAX_CHARS]


@dataclass(frozen=True, slots=True)
class ChatThread:
    """What this chat just said, besides the media cards."""

    turns: tuple[tuple[str, str], ...] = ()
    last_house: str = ""
    awaiting_house_confirm: bool = False

    @property
    def present(self) -> bool:
        return bool(self.turns or self.last_house or self.awaiting_house_confirm)

    def as_messages(self) -> list[dict[str, str]]:
        """OpenAI-shaped history. A fresh list the agent may append to."""
        return [
            {"role": role, "content": text}
            for role, text in self.turns
            if role in {"user", "assistant"} and text
        ]

    def recent_lines(self) -> list[str]:
        return [f"{role}: {text}" for role, text in self.turns[-4:] if text]


class ThreadMemory:
    """Per-chat conversational thread with the media-context TTL."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    @staticmethod
    def _key(chat_id: int) -> str:
        return storage_key(_PREFIX, chat_id)

    @property
    def ttl_seconds(self) -> int:
        from hearth.config import settings

        return max(60, int(getattr(settings, "telegram_context_ttl_seconds", 1800)))

    def load(self, chat_id: int) -> ChatThread:
        payload = self.store.get_callback_media(self._key(chat_id))
        if not payload:
            return ChatThread()
        turns: list[tuple[str, str]] = []
        raw_turns = payload.get("turns")
        if isinstance(raw_turns, list):
            for item in raw_turns[-_MAX_TURNS:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "")
                text = _clip(str(item.get("text") or ""))
                if role in {"user", "assistant"} and text:
                    turns.append((role, text))
        return ChatThread(
            turns=tuple(turns),
            last_house=_clip(str(payload.get("last_house") or "")),
            awaiting_house_confirm=bool(payload.get("awaiting_house_confirm")),
        )

    def remember_exchange(
        self,
        chat_id: int,
        *,
        user: str,
        assistant: str,
        house: str = "",
        awaiting_house_confirm: bool = False,
    ) -> ChatThread:
        previous = self.load(chat_id)
        turns = list(previous.turns)
        user_text = _clip(user)
        assistant_text = _clip(assistant)
        if user_text:
            turns.append(("user", user_text))
        if assistant_text:
            turns.append(("assistant", assistant_text))
        thread = ChatThread(
            turns=tuple(turns[-_MAX_TURNS:]),
            last_house=_clip(house) or previous.last_house,
            awaiting_house_confirm=bool(awaiting_house_confirm),
        )
        self._save(chat_id, thread)
        return thread

    def dismiss_confirm(self, chat_id: int) -> None:
        current = self.load(chat_id)
        if not current.awaiting_house_confirm:
            return
        self._save(
            chat_id,
            ChatThread(
                turns=current.turns,
                last_house=current.last_house,
                awaiting_house_confirm=False,
            ),
        )

    def _save(self, chat_id: int, thread: ChatThread) -> None:
        self.store.put_callback_media(
            self._key(chat_id),
            {
                "turns": [{"role": role, "text": text} for role, text in thread.turns],
                "last_house": thread.last_house,
                "awaiting_house_confirm": thread.awaiting_house_confirm,
            },
            ttl_s=self.ttl_seconds,
        )


__all__ = ["ChatThread", "ThreadMemory"]
