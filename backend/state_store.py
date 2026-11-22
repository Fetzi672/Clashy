"""One shared, crash-safe state document.

Before: every helper re-read state.json, awaited Discord, then wrote its whole
(now stale) copy back. Two loops running at once overwrote each other's message
IDs, and a write interrupted mid-way left a broken file that loaded as "empty",
so every panel was posted again. Now there is exactly one in-memory dict for the
whole process, mutations happen synchronously on the event loop, and every save
is atomic (temp file + fsync + rename) with the previous good file kept as backup.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_KEYS = {
    "messages": dict,
    "posted_wars": list,
    "posted_cwl_wars": list,
    "channels": dict,
    "channel_clan_tags": dict,
}


def _normalize(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("state root must be an object")
    for key, factory in DEFAULT_KEYS.items():
        if not isinstance(data.get(key), factory):
            data[key] = factory()
    return data


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.backup_path = path + ".bak"
        self._data: dict | None = None
        self.loaded_from: str | None = None

    def _read(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as handle:
            return _normalize(json.load(handle))

    def load(self) -> dict:
        """Return the shared state dict (loaded once)."""
        if self._data is not None:
            return self._data
        for candidate in (self.path, self.backup_path):
            if not os.path.exists(candidate):
                continue
            try:
                self._data = self._read(candidate)
                self.loaded_from = candidate
                if candidate != self.path:
                    log.warning("state.json unreadable, recovered from backup %s", candidate)
                return self._data
            except (OSError, ValueError) as exc:
                log.error("state file %s unreadable: %s", candidate, exc)
        self._data = _normalize({})
        self.loaded_from = None
        return self._data

    def save(self, _state: dict | None = None) -> None:
        """Persist the shared dict atomically. The argument is accepted for the old
        ``save_state(state)`` call style; it is always the same object."""
        data = self.load()
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.exists(self.path):
                try:
                    self._read(self.path)  # only a readable file becomes the backup
                    shutil.copy2(self.path, self.backup_path)
                except (OSError, ValueError):
                    pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def remember_ids(existing: list, new_ids, limit: int = 500) -> list:
    """Keep IDs in the order they were recorded (oldest dropped first).

    The old version sorted war *tags* alphabetically and cut the "smallest", which
    could forget a recent CWL war and post it again."""
    seen: dict[str, None] = {}
    for value in list(existing) + list(new_ids):
        text = str(value)
        seen.pop(text, None)
        seen[text] = None
    return list(seen)[-limit:]
