"""TypeSafe System One client for Jev.

Prefers the official ``typesafe-sdk`` AsyncTypeSafeClient when installed;
otherwise POSTs to ``https://api.typesafe.ai/v1/systemone`` with httpx.
Never logs the API key.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from hearth.config import settings
from hearth.jev.schema import hearth_system_one_questions, parse_answers, JevAnswers

log = logging.getLogger("hearth.jev")

SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


class SystemOneClient(Protocol):
    async def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> JevAnswers: ...


class HttpSystemOneClient:
    """Thin bearer HTTP client to TypeSafe System One."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = SYSTEM_ONE_URL,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.typesafe_api_key).strip()
        self._model = (model or settings.jev_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._timeout = _timeout_seconds(timeout)
        self._transport = transport

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> JevAnswers:
        if not self._api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not configured")
        body = {
            "model": (model or self._model).strip() or DEFAULT_MODEL,
            "state": state,
            "questions": questions or hearth_system_one_questions(),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._timeout, connect=min(5.0, self._timeout))
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            response = await client.post(self._base_url, json=body, headers=headers)
        if response.status_code >= 400:
            # Never include Authorization or raw body secrets in logs.
            raise RuntimeError(
                f"TypeSafe System One HTTP {response.status_code}: {_clip(response.text)}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("TypeSafe System One returned a non-object JSON body")
        return parse_answers(data)


class SdkSystemOneClient:
    """Wrapper around typesafe_sdk.AsyncTypeSafeClient when available."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.typesafe_api_key).strip()
        self._model = (model or settings.jev_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._timeout = _timeout_seconds(timeout)

    async def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> JevAnswers:
        if not self._api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not configured")
        try:
            from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score
        except ImportError as exc:  # pragma: no cover — exercised via HTTP fallback
            raise RuntimeError("typesafe-sdk is not installed") from exc

        q_raw = questions or hearth_system_one_questions()
        q_objs = _to_sdk_questions(q_raw, Choice=Choice, Noul=Noul, Score=Score)
        async with AsyncTypeSafeClient(
            api_key=self._api_key,
            model=(model or self._model),
            timeout=self._timeout,
        ) as client:
            response = await client.system_one(state=state, questions=q_objs)

        payload = _sdk_response_to_dict(response)
        return parse_answers(payload)


def build_system_one_client(
    *,
    prefer_sdk: bool = True,
    api_key: str | None = None,
    model: str | None = None,
) -> SystemOneClient:
    """Prefer official SDK; fall back to HTTP if import fails."""
    if prefer_sdk:
        try:
            import typesafe_sdk  # noqa: F401

            return SdkSystemOneClient(api_key=api_key, model=model)
        except ImportError:
            log.info("typesafe-sdk not installed; using HTTP System One client")
    return HttpSystemOneClient(api_key=api_key, model=model)


def _to_sdk_questions(
    questions: dict[str, Any],
    *,
    Choice: Any,
    Noul: Any,
    Score: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in questions.items():
        if not isinstance(raw, dict):
            out[name] = raw
            continue
        qtype = str(raw.get("type") or "").lower()
        instructions = str(raw.get("instructions") or "")
        criteria = raw.get("criteria")
        if qtype == "choice":
            out[name] = Choice(instructions=instructions, criteria=criteria or {})
        elif qtype == "noul":
            kwargs: dict[str, Any] = {"instructions": instructions}
            if isinstance(criteria, dict):
                kwargs["criteria"] = criteria
            out[name] = Noul(**kwargs)
        elif qtype == "score":
            out[name] = Score(instructions=instructions, criteria=list(criteria or []))
        else:
            out[name] = raw
    return out


def _sdk_response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if isinstance(dumped, dict):
            return dumped
    answers: dict[str, Any] = {}
    for attr, key_type in (("choices", "choice"), ("nouls", "noul"), ("scores", "score")):
        block = getattr(response, attr, None)
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            answers[name] = _sdk_answer_dict(value, key_type)
    # Some SDK builds also expose .answers
    unified = getattr(response, "answers", None)
    if isinstance(unified, dict) and not answers:
        for name, value in unified.items():
            answers[name] = _sdk_answer_dict(value, str(getattr(value, "type", "") or "noul"))
    return {
        "model": str(getattr(response, "model", "") or ""),
        "answers": answers,
        "usage": getattr(response, "usage", None),
    }


def _sdk_answer_dict(value: Any, default_type: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    data: dict[str, Any] = {"type": getattr(value, "type", None) or default_type}
    for attr in ("choice", "noul", "score", "confidence", "probabilities", "legend"):
        if hasattr(value, attr):
            data[attr] = getattr(value, attr)
    return data


def _timeout_seconds(override: float | None) -> float:
    """Resolve the per-call HTTP budget from the override or HEARTH_JEV_TIMEOUT_SECONDS."""
    value = float(settings.jev_timeout_seconds) if override is None else float(override)
    return max(0.5, value)


def _clip(text: str, limit: int = 240) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


__all__ = [
    "DEFAULT_MODEL",
    "SYSTEM_ONE_URL",
    "HttpSystemOneClient",
    "SdkSystemOneClient",
    "SystemOneClient",
    "build_system_one_client",
]
