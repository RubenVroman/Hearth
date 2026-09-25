"""Vision providers. Titles in, nothing queued, images never logged.

The OpenAI adapter reuses the house ``OPENAI_API_KEY`` and a small multimodal
chat model. It is not a Cursor cloud agent and it does not call Grok. A local
adapter is a seam: unwired until a NAS model is actually configured. Tests use
the fixture adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from hearth.config import settings
from hearth.telegram.media.vision import VisionResult

log = logging.getLogger("hearth.telegram")



class VisionUnavailable(RuntimeError):
    """Timeout, HTTP error, or an unwired adapter. Not a title."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class VisionSchemaError(RuntimeError):
    """The model replied, but not with a usable title list. Do not fall back."""


class VisionProvider(Protocol):
    name: str

    async def identify(
        self,
        image: bytes,
        mime: str,
        caption: str | None,
    ) -> VisionResult: ...


class FixtureVisionProvider:
    """Test double. Records call metadata and does not retain the image."""

    name = "fixture"

    def __init__(self, result: VisionResult | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def identify(
        self,
        image: bytes,
        mime: str,
        caption: str | None,
    ) -> VisionResult:
        self.calls.append(
            {
                "mime": mime,
                "caption": caption or "",
                "nbytes": len(image),
            }
        )
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class LocalVisionProvider:
    """Residency-preserving seam. Unwired until a local model is configured."""

    name = "local"

    async def identify(
        self,
        image: bytes,
        mime: str,
        caption: str | None,
    ) -> VisionResult:
        del image, mime, caption
        raise VisionUnavailable("local vision adapter is not configured", retryable=False)


class OpenAIVisionProvider:
    """Compatible provider interface backed by pooled, strict structured vision."""

    name = "openai"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        from hearth.telegram.media.image_requests import OpenAIVisionProvider as StructuredProvider
        self._provider = StructuredProvider(api_key=api_key, model=model)

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def identify(self, image: bytes, mime: str, caption: str | None) -> VisionResult:
        from hearth.telegram.media.image_requests import ImageSchemaError, VisionError
        from hearth.telegram.media.vision import VisionCandidate
        try:
            result = await self._provider.identify(
                image, mime, caption or "", limit=settings.telegram_vision_list_cap,
            )
        except ImageSchemaError as exc:
            raise VisionSchemaError(str(exc)) from None
        except VisionError as exc:
            raise VisionUnavailable(str(exc), retryable=getattr(exc, "retryable", False)) from None
        return VisionResult(
            kind=("single" if len(result.candidates) == 1 else "list") if result.kind == "titles" else result.kind,
            candidates=tuple(VisionCandidate(
                title=item.title, year=item.year, media_type=item.media_type,
                confidence=item.confidence, season=item.season,
            ) for item in result.candidates),
            list_label=result.list_label, more_visible=result.more_visible,
        )


def build_vision_provider(name: str | None = None) -> VisionProvider:
    chosen = (name if name is not None else settings.telegram_vision_provider).strip().lower()
    if chosen == "openai":
        return OpenAIVisionProvider()
    if chosen == "local":
        return LocalVisionProvider()
    if chosen == "fixture":
        raise VisionUnavailable("fixture provider is test-only")
    raise VisionUnavailable("vision provider is not configured")


async def identify_image(
    provider: VisionProvider,
    image: bytes,
    mime: str,
    caption: str | None,
) -> VisionResult:
    """One identify, plus at most one configured fallback after a transport failure.

    A schema failure does not try a second model. That retry is how a refusal
    turns into a hallucinated title.
    """
    timeout = float(settings.telegram_vision_timeout_seconds)

    async def _once(current: VisionProvider) -> VisionResult:
        try:
            return await asyncio.wait_for(
                current.identify(image, mime, caption),
                timeout=timeout,
            )
        except (VisionSchemaError, VisionUnavailable):
            raise
        except TimeoutError as exc:
            raise VisionUnavailable("timeout") from exc
        except Exception as exc:  # noqa: BLE001
            raise VisionUnavailable(type(exc).__name__, retryable=False) from None

    try:
        return await _once(provider)
    except VisionSchemaError:
        raise
    except VisionUnavailable as exc:
        if not exc.retryable:
            raise
        fallback_name = (settings.telegram_vision_fallback or "").strip().lower()
        if not fallback_name or fallback_name == getattr(provider, "name", ""):
            raise
        log.info(
            "telegram vision fallback %s",
            {"from": getattr(provider, "name", ""), "to": fallback_name},
        )
        fallback = build_vision_provider(fallback_name)
        try:
            return await _once(fallback)
        finally:
            if hasattr(fallback, "aclose"):
                await fallback.aclose()
