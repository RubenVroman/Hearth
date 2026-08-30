"""Retention prune: startup plus a background interval (disabled when interval is 0)."""

from __future__ import annotations

import asyncio
import logging

from hearth.config import settings
from hearth.memory import store

log = logging.getLogger("hearth.memory")


def run_prune() -> dict:
    if not store.memory_enabled():
        return {"ok": True, "skipped": True, "reason": "memory disabled"}
    return store.prune()


async def prune_loop(stop: asyncio.Event) -> None:
    minutes = int(settings.memory_prune_interval_minutes)
    if minutes <= 0:
        return
    interval = max(60, minutes * 60)
    try:
        run_prune()
    except Exception:  # noqa: BLE001
        log.exception("memory prune at startup failed")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                run_prune()
            except Exception:  # noqa: BLE001
                log.exception("memory prune failed")
