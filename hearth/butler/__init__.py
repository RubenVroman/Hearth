"""Complementary house-butler behaviors around media, lights, and the shell.

These sit beside the Telegram media lanes (classify, moods, status, play).
They answer “what’s already on the shelf”, run named Home Assistant scenes,
and polish a queue with a Plex aside when the title is already in progress.
"""

from hearth.butler.phrases import classify_house_phrase, house_route
from hearth.butler.scenes import activate_preset
from hearth.butler.shelf import house_pulse, shelf_snapshot

__all__ = [
    "activate_preset",
    "classify_house_phrase",
    "house_pulse",
    "house_route",
    "shelf_snapshot",
]
