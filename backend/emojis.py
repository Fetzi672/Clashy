"""Application emojis of this bot (uploaded once, IDs in emojis.json).

``emoji("th17")`` returns ``<:th17:id>``; unknown names return the fallback, so a
missing emoji never breaks an embed.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emojis.json")
_by_key: dict[str, tuple[str, str]] = {}


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def load(path: str = _PATH) -> int:
    _by_key.clear()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    for item in data.get("items", []):
        name, emoji_id = item.get("name"), item.get("id")
        if not name or not emoji_id:
            continue
        for alias in [name, *item.get("aliases", [])]:
            _by_key.setdefault(_key(alias), (name, str(emoji_id)))
    return len(data.get("items", []))


def emoji(name: Any, fallback: str = "") -> str:
    hit = _by_key.get(_key(name))
    return f"<:{hit[0]}:{hit[1]}>" if hit else fallback


def th(level: Any) -> str:
    try:
        level = int(level)
    except (TypeError, ValueError):
        return "🏰"
    return emoji(f"th{level}", f"TH{level}") if level > 0 else "🏰"


def unit(name: Any, fallback: str = "") -> str:
    """Troop, spell, hero or equipment by its game name, e.g. ``unit("Electro Dragon")``."""
    return emoji(name, fallback)


STAR = lambda: emoji("war_star", "⭐")  # noqa: E731
NO_STAR = lambda: emoji("no_star", "☆")  # noqa: E731
SWORD = lambda: emoji("war_sword", "⚔️")  # noqa: E731
SHIELD = lambda: emoji("war_shield", "🛡️")  # noqa: E731
DONATED = lambda: emoji("donation_up", "📤")  # noqa: E731
RECEIVED = lambda: emoji("donation_down", "📥")  # noqa: E731


def stars(count: Any, total: int = 3) -> str:
    try:
        count = max(0, min(total, int(count)))
    except (TypeError, ValueError):
        count = 0
    return STAR() * count + NO_STAR() * (total - count)


load()
