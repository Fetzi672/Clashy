"""Supercell maintenance detection for Clashy (copied from the central data
service's app/maintenance.py, same rules, and SBJ Protocol's message text).

Supercell announces nothing. A probe every 30 s:
- "started": 2x HTTP 503 reason inMaintenance in a row (any other 503: 4x);
  the start time is the first failed probe
- "ended":   2x HTTP 200 in a row after that
Timeouts/other errors reset the streaks and never cause an event.
Only the confirmed state (in maintenance + start time) is persisted, so a restart
neither repeats nor loses a running maintenance.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

OK, MAINTENANCE, UNAVAILABLE, UNKNOWN = "ok", "maintenance", "unavailable", "unknown"
PROBE_URL = "https://api.clashofclans.com/v1/goldpass/seasons/current"


def classify(status: int, body: Any) -> str:
    if status == 200:
        return OK
    if status == 503:
        if isinstance(body, dict) and body.get("reason") == "inMaintenance":
            return MAINTENANCE
        return UNAVAILABLE
    return UNKNOWN


def format_duration(seconds: Any) -> str:
    try:
        minutes = max(1, round(float(seconds) / 60))
    except (TypeError, ValueError):
        return "a while"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins and not days else ""]
    return " ".join(p for p in parts if p) or "1m"


def render(kind: str, duration_seconds: Any = None) -> Optional[str]:
    if kind == "started":
        return "🛠️ **Maintenance break has started!**"
    if kind == "ended":
        return f"✅ **The maintenance break is ending soon!** (Started {format_duration(duration_seconds)} ago)"
    return None


async def probe(client, timeout: float = 10.0) -> str:
    """One request, no retries (a retry would only blur what was observed)."""
    import aiohttp
    try:
        session = await client.get_session()
        async with session.get(PROBE_URL, headers={"Authorization": f"Bearer {client.token}"},
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            body = None
            if resp.status == 503:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None
            return classify(resp.status, body)
    except asyncio.CancelledError:
        raise
    except Exception:
        return UNKNOWN


class MaintenanceMonitor:
    def __init__(self, state_getter, save, *, confirm_start=2, confirm_start_generic=4, confirm_end=2):
        self.state, self.save = state_getter, save
        self.confirm_start, self.confirm_start_generic, self.confirm_end = confirm_start, confirm_start_generic, confirm_end
        self._maint = self._down = self._ok = 0
        self._down_since: Optional[float] = None
        self._ok_since: Optional[float] = None

    def observe(self, result: str, now: float) -> Optional[dict]:
        st = self.state()
        if result in (MAINTENANCE, UNAVAILABLE):
            if self._down == 0:
                self._down_since = now
            self._down += 1
            self._maint = self._maint + 1 if result == MAINTENANCE else 0
            self._ok, self._ok_since = 0, None
        elif result == OK:
            if self._ok == 0:
                self._ok_since = now
            self._ok += 1
            self._down, self._maint, self._down_since = 0, 0, None
        else:
            self._down, self._maint, self._down_since = 0, 0, None
            self._ok, self._ok_since = 0, None

        if not st.get("in_maintenance"):
            if self._maint >= self.confirm_start or self._down >= self.confirm_start_generic:
                st.update(in_maintenance=True, started_at=float(self._down_since))
                self.save()
                log.info("maintenance started")
                return {"kind": "started", "started_at": st["started_at"], "at": now}
        elif self._ok >= self.confirm_end:
            started = st.get("started_at")
            st.update(in_maintenance=False, started_at=None)
            self.save()
            duration = (self._ok_since - started) if started else None
            log.info("maintenance ended (duration=%s)", duration)
            return {"kind": "ended", "started_at": started, "duration_seconds": duration, "at": now}
        return None
