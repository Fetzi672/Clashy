"""Clash of Clans API client with timeouts, bounded retries and a short shared cache.

Before: no timeout (one hung request froze a loop for good) and any error came
back as ``{}``/``[]``, which the panels then rendered as "Unknown / 0 members".
Now a temporary failure (429, 5xx, maintenance, timeout) raises ``CocApiError``
after bounded retries, so the caller keeps the last good panel instead of
overwriting it with empty data. "Not available" answers (404, private war log
403) keep their old meaning (empty result / None).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}
NOT_AVAILABLE = {403, 404}


class CocApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _tag(tag: str) -> str:
    return quote(tag.strip().upper() if tag.startswith("#") else "#" + tag.strip().upper(), safe="")


class COCClient:
    BASE_URL = "https://api.clashofclans.com/v1"

    def __init__(self, token: str, *, timeout: float = 20.0, attempts: int = 3, cache_seconds: float = 60.0,
                 concurrency: int = 6, session: Optional[aiohttp.ClientSession] = None, sleep=asyncio.sleep,
                 clock=time.monotonic):
        self.token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.attempts = max(1, attempts)
        self.cache_seconds = cache_seconds
        self.session = session
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._flights: dict[str, asyncio.Future] = {}
        self._sleep = sleep
        self._clock = clock

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def _fetch(self, path: str) -> tuple[int, Any]:
        session = await self.get_session()
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        last = "unknown"
        for attempt in range(self.attempts):
            try:
                async with self._sem:
                    async with session.get(self.BASE_URL + path, headers=headers, timeout=self.timeout) as resp:
                        if resp.status == 200:
                            return 200, await resp.json()
                        body = None
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            pass
                        if resp.status in NOT_AVAILABLE:
                            return resp.status, body
                        reason = (body or {}).get("reason") if isinstance(body, dict) else None
                        last = f"HTTP {resp.status}{' ' + reason if reason else ''}"
                        if resp.status not in RETRYABLE:
                            raise CocApiError(f"COC API {last}", resp.status)
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (2 ** attempt)
            except CocApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = type(exc).__name__
                delay = 1.5 * (2 ** attempt)
            if attempt + 1 < self.attempts:
                await self._sleep(min(delay, 15.0) + random.uniform(0, 0.5))
        raise CocApiError(f"COC API unavailable ({last})")

    async def _get(self, path: str, *, cache: bool = True) -> tuple[int, Any]:
        """Shared result for identical requests within ``cache_seconds``."""
        now = self._clock()
        if cache:
            hit = self._cache.get(path)
            if hit and now - hit[0] < self.cache_seconds:
                return hit[1]
            flight = self._flights.get(path)
            if flight:
                return await asyncio.shield(flight)
        future = asyncio.get_running_loop().create_future()
        if cache:
            self._flights[path] = future
        try:
            result = await self._fetch(path)
            if cache:
                self._cache[path] = (self._clock(), result)
            future.set_result(result)
            return result
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved: no "never retrieved" warning without waiters
            raise
        finally:
            self._flights.pop(path, None)

    # ----- endpoints (return contracts unchanged for "not available") -----
    async def get_clan(self, clan_tag: str) -> dict:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}")
        return data if status == 200 and isinstance(data, dict) else {}

    async def get_player(self, player_tag: str) -> dict:
        status, data = await self._get(f"/players/{_tag(player_tag)}")
        return data if status == 200 and isinstance(data, dict) else {}

    async def get_clan_members(self, clan_tag: str, limit: int = 50) -> list:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}/members?limit={limit}")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def get_clan_war_log(self, clan_tag: str) -> list:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}/warlog")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def get_current_war(self, clan_tag: str) -> Optional[dict]:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}/currentwar", cache=True)
        return data if status == 200 else None

    async def get_cwl_league_group(self, clan_tag: str) -> Optional[dict]:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}/currentwar/leaguegroup")
        return data if status == 200 else None

    async def get_cwl_war(self, war_tag: str) -> Optional[dict]:
        status, data = await self._get(f"/clanwarleagues/wars/{_tag(war_tag)}")
        return data if status == 200 else None

    async def get_locations(self) -> list:
        status, data = await self._get("/locations")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def get_location_clan_rankings(self, location_id: int, limit: int = 10) -> list:
        status, data = await self._get(f"/locations/{int(location_id)}/rankings/clans?limit={int(limit)}")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def get_location_player_rankings(self, location_id: int, limit: int = 10) -> list:
        status, data = await self._get(f"/locations/{int(location_id)}/rankings/players?limit={int(limit)}")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def get_goldpass_current(self) -> Optional[dict]:
        status, data = await self._get("/goldpass/seasons/current")
        return data if status == 200 else None

    async def get_capital_raid_seasons(self, clan_tag: str, limit: int = 1) -> list:
        status, data = await self._get(f"/clans/{_tag(clan_tag)}/capitalraidseasons?limit={int(limit)}")
        return list((data or {}).get("items", [])) if status == 200 else []

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
