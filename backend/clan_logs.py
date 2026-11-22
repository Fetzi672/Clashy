"""Clan logs for Clashy: joins/leaves, donations, upgrades, name/role/league changes.

Same events and same look as SBJ Protocol, but produced by Clashy itself:
- clan + member list every minute  -> joins, leaves, donations
- every member's profile every 5 min -> upgrades (TH, troops, heroes, spells,
  equipment), name, role and league changes

Safety rules
- The first look at a clan/player is only a baseline: nothing is posted for it.
- A failed API call changes nothing; the old snapshot stays and is compared later.
- New snapshot and the events it produced are saved in ONE atomic write, so a
  crash can neither lose events nor create them twice.
- Each event is sent per channel at most once (stored "done" list + a Discord
  nonce for the gap between sending and saving). Unsendable events expire after
  2 hours instead of piling up.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import discord
from discord.http import handle_message_parameters
from discord.utils import MISSING

import clan_level_card
import clan_log_render as R
import maintenance as M

log = logging.getLogger(__name__)

LOG_TYPES = R.LOG_TYPES + ("clan_level", "maintenance",)  # + member_join, member_leave, donations, war, *_upgrade, *_change
LABELS = {
    "war": "Krieg (jeder Angriff live)",
    "member_join": "Beitritte",
    "member_leave": "Austritte",
    "donations": "Spenden",
    "townhall_upgrade": "Rathaus-Upgrades",
    "troop_upgrade": "Truppen-Upgrades",
    "hero_upgrade": "Helden-Upgrades",
    "spell_upgrade": "Zauber-Upgrades",
    "hero_equipment_upgrade": "Ausrüstungs-Upgrades",
    "name_change": "Namensänderungen",
    "role_change": "Rollenänderungen",
    "league_change": "Ligawechsel",
    "clan_level": "Clan-Level-Aufstiege (Bildkarte)",
    "maintenance": "Supercell-Wartung (Beginn/Ende)",
}
BADGE_URL = re.compile(r"https://api-assets\.clashofclans\.com/badges/(?:70|200|512)/[A-Za-z0-9_-]+\.png")
MAX_BADGE_BYTES = 512 * 1024
ROLE_NAMES = {"member": "Member", "admin": "Elder", "coLeader": "Co-Leader", "leader": "Leader"}
PLAYER_INTERVAL = 300.0
EVENT_TTL = 2 * 3600.0
MAX_SENDS_PER_TICK = 30
ITEM_FIELDS = (("troop", "troops"), ("hero", "heroes"), ("spell", "spells"), ("hero_equipment", "heroEquipment"))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _league(data: dict) -> Optional[str]:
    league = data.get("leagueTier") or data.get("league")
    return league.get("name") if isinstance(league, dict) else None


def compact_player(p: dict) -> dict:
    items = {}
    for kind, field in ITEM_FIELDS:
        for item in p.get(field) or []:
            if isinstance(item, dict) and item.get("name"):
                items[f"{kind}|{item['name']}|{item.get('village') or ''}"] = _int(item.get("level"))
    return {"name": p.get("name"), "role": p.get("role"), "townHallLevel": p.get("townHallLevel"),
            "league": _league(p), "items": items}


def player_events(tag: str, old: dict, new: dict, clan: dict, stamp: str = "") -> list[dict]:
    player = {"tag": tag, "name": new.get("name"), "townHallLevel": new.get("townHallLevel")}
    out = []
    if _int(new.get("townHallLevel")) > _int(old.get("townHallLevel")) > 0:
        out.append(("player_upgrade", f"th|{new['townHallLevel']}",
                    {"upgrade_type": "townhall", "name": "Town Hall", "village": None,
                     "old_level": old["townHallLevel"], "new_level": new["townHallLevel"]}))
    old_items = old.get("items") or {}
    for key in sorted(new.get("items") or {}):
        kind, name, village = key.split("|", 2)
        level, before = new["items"][key], old_items.get(key)
        if before is None or level > before:
            out.append(("player_upgrade", f"{key}|{level}",
                        {"upgrade_type": kind, "name": name, "village": village or None,
                         "old_level": before, "new_level": level}))
    for field in ("name", "role", "league"):
        before, after = old.get(field), new.get(field)
        if before != after and before is not None and after is not None:
            if field == "role":
                before, after = ROLE_NAMES.get(before, before), ROLE_NAMES.get(after, after)
            out.append(("player_change", f"{field}|{before}|{after}|{stamp}",
                        {"field": field, "old_value": before, "new_value": after}))
    return [{"event_type": et, "player_tag": tag, "clan_tag": clan.get("tag"), "key": f"{et}|{tag}|{k}",
             "payload": {"player": player, "clan": clan, **payload}} for et, k, payload in out]


def clan_events(old: dict, new: dict, clan: dict, stamp: str) -> list[dict]:
    out = []
    for tag in sorted(new.keys() - old.keys()):
        out.append({"event_type": "clan_member_join", "player_tag": tag, "clan_tag": clan["tag"],
                    "key": f"join|{tag}|{stamp}", "payload": {"clan": clan, "member": new[tag]}})
    for tag in sorted(old.keys() - new.keys()):
        out.append({"event_type": "clan_member_leave", "player_tag": tag, "clan_tag": clan["tag"],
                    "key": f"leave|{tag}|{stamp}", "payload": {"clan": clan, "member": old[tag]}})
    donors = []
    for tag in sorted(old.keys() & new.keys()):
        d = max(0, _int(new[tag].get("donations")) - _int(old[tag].get("donations")))
        r = max(0, _int(new[tag].get("donationsReceived")) - _int(old[tag].get("donationsReceived")))
        if d or r:
            donors.append({"player_tag": tag, "player_name": new[tag].get("name") or tag,
                           "donated_delta": d, "received_delta": r})
    if donors:
        out.append({"event_type": "clan_donation", "player_tag": None, "clan_tag": clan["tag"],
                    "key": f"donation|{stamp}",
                    "payload": {"clan": clan, "members": donors,
                                "donated_delta": sum(x["donated_delta"] for x in donors),
                                "received_delta": sum(x["received_delta"] for x in donors)}})
    return out


def level_event(old_level: Any, clan_raw: dict, clan: dict) -> list[dict]:
    """One event per level reached; ``old_level`` is the highest level seen so far (``None`` = baseline)."""
    new = _int(clan_raw.get("clanLevel"))
    if old_level is None or new <= _int(old_level):
        return []
    return [{"event_type": "clan_level_up", "log_type": "clan_level", "player_tag": None, "clan_tag": clan["tag"],
             "key": f"clan_level|{clan['tag']}|{new}", "payload": {"clan": clan, "level": {"old": _int(old_level), "new": new}}}]


async def fetch_badge(url: str) -> bytes | None:
    """The clan badge, only from Supercell's asset CDN; any failure gives the card's fallback shield."""
    if not BADGE_URL.fullmatch(url or ""):
        return None
    try:
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > MAX_BADGE_BYTES:
                        return None
                return bytes(data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("clan badge unavailable (%s)", type(exc).__name__)
        return None


async def render_level(item: dict, badge_fetcher=fetch_badge) -> dict:
    """``## <clan> Reaches Clan Level N- MM/DD/YYYY`` plus the PNG card; never pings (Clashy has no clan role)."""
    payload = item.get("payload") or {}
    clan = payload.get("clan") or {}
    level = _int((payload.get("level") or {}).get("new"))
    if level < 2:
        raise ValueError("clan level event without a new level")
    urls = clan.get("badgeUrls") or {}
    badge = await badge_fetcher(str(urls.get("large") or urls.get("medium") or urls.get("small") or ""))
    png = await asyncio.to_thread(clan_level_card.render, level, badge)
    day = datetime.fromtimestamp(float(item.get("created") or time.time()), timezone.utc)
    name = R._safe_text(clan.get("name") or clan.get("tag"), "Clan")
    return {"content": f"## {name} Reaches Clan Level {level}- {day:%m/%d/%Y}",
            "file": discord.File(io.BytesIO(png), filename=f"clan-level-{level}.png")}


def _member_view(m: dict) -> dict:
    return {"tag": m.get("tag"), "name": m.get("name"), "role": ROLE_NAMES.get(m.get("role"), m.get("role")),
            "townHallLevel": m.get("townHallLevel"), "donations": m.get("donations"),
            "donationsReceived": m.get("donationsReceived")}


def nonce(channel_id: int, key: str) -> str:
    return "cl-" + hashlib.sha256(f"{channel_id}|{key}".encode()).hexdigest()[:20]


async def send_rendered(channel, rendered: dict, key: str):
    state = channel._state
    flags = MISSING
    content = rendered.get("content")
    if isinstance(content, str):
        content = R._truncate_discord(content, 2000)
        flags = discord.MessageFlags._from_value(0)
        flags.suppress_embeds = True
    with handle_message_parameters(
        content=content if content is not None else MISSING,
        embed=rendered.get("embed", MISSING),
        file=rendered.get("file", MISSING),
        nonce=nonce(channel.id, key),
        allowed_mentions=discord.AllowedMentions.none(),
        previous_allowed_mentions=getattr(state, "allowed_mentions", None),
        flags=flags,
    ) as params:
        if params.payload is not None:
            params.payload["enforce_nonce"] = True
        elif not (params.multipart and params.multipart[0].get("name") == "payload_json"
                  and '"enforce_nonce":true' in params.multipart[0]["value"]):
            raise TypeError("file upload without enforce_nonce")  # discord.py sets it; refuse if it ever stops
        data = await state.http.send_message(channel.id, params=params)
    return state.create_message(channel=channel, data=data)


class ClanLogs:
    def __init__(self, store, bot, client_getter: Callable, clan_tag_getter: Callable,
                 sender=send_rendered, clock=time.time):
        self.store, self.bot = store, bot
        self.client, self.clan_tag = client_getter, clan_tag_getter
        self.sender, self.clock = sender, clock
        self.badge_fetcher = fetch_badge
        self._lock = asyncio.Lock()
        self._deliver_lock = asyncio.Lock()
        self.maintenance = M.MaintenanceMonitor(lambda: self.data()["maintenance"], self.store.save)

    # ---------------- config ----------------
    def data(self) -> dict:
        d = self.store.load()
        d.setdefault("config", {})
        d.setdefault("members", None)
        d.setdefault("players", {})
        d.setdefault("players_checked", 0.0)
        d.setdefault("outbox", [])
        d.setdefault("clan_level", None)
        d.setdefault("maintenance", {"in_maintenance": False, "started_at": None})
        return d

    def channel_types(self, channel_id: int) -> list[str]:
        return list((self.data()["config"].get(str(channel_id)) or {}).get("types", []))

    def set_channel_types(self, channel_id: int, types: list[str]) -> None:
        cfg = self.data()["config"]
        types = [t for t in LOG_TYPES if t in set(types) and t != "war"]
        if types:
            old = cfg.get(str(channel_id)) or {}
            cfg[str(channel_id)] = {"types": types, "since": old.get("since", self.clock())}
        else:
            cfg.pop(str(channel_id), None)
        self.store.save()

    def overview(self) -> dict[int, list[str]]:
        return {int(k): v.get("types", []) for k, v in self.data()["config"].items()}

    # ---------------- producer ----------------
    def _enqueue(self, data: dict, events: list[dict]) -> None:
        now = self.clock()
        for e in events:
            log_type = e.get("log_type") or R.route_event(e)
            if log_type:
                data["outbox"].append({**e, "log_type": log_type, "created": now, "done": []})

    async def poll(self) -> int:
        """Fetch clan (and players when due); store snapshots + events atomically."""
        data = self.data()
        if not data["config"]:
            data["members"], data["players"], data["clan_level"] = None, {}, None
            return 0
        client = self.client()
        clan_raw = await client.get_clan(self.clan_tag())
        if not clan_raw or not clan_raw.get("tag") or "memberList" not in clan_raw:
            return 0
        members_list = clan_raw.get("memberList") or []
        if _int(clan_raw.get("members")) > 0 and not members_list:
            return 0
        clan = {"tag": clan_raw["tag"], "name": clan_raw.get("name"), "badgeUrls": clan_raw.get("badgeUrls")}
        new_members = {m["tag"]: _member_view(m) for m in members_list if m.get("tag")}
        events = level_event(data["clan_level"], clan_raw, clan)
        if _int(clan_raw.get("clanLevel")) > _int(data["clan_level"]):
            data["clan_level"] = _int(clan_raw.get("clanLevel"))           # highest level seen: a dip never re-posts
        if data["members"] is not None:
            events += clan_events(data["members"], new_members, clan, f"{self.clock():.0f}")
        data["members"] = new_members
        for gone in set(data["players"]) - set(new_members):
            data["players"].pop(gone, None)  # rejoin starts with a fresh baseline

        if self.clock() - data["players_checked"] >= PLAYER_INTERVAL:
            tags = sorted(new_members)
            results = await asyncio.gather(*(client.get_player(t) for t in tags), return_exceptions=True)
            for tag, player in zip(tags, results):
                if isinstance(player, BaseException) or not player or not player.get("tag"):
                    continue  # keep the old snapshot, compare next time
                fresh = compact_player(player)
                old = data["players"].get(tag)
                if old is not None:
                    events += player_events(tag, old, fresh, clan, f"{self.clock():.0f}")
                data["players"][tag] = fresh
            data["players_checked"] = self.clock()
        self._enqueue(data, events)
        self.store.save()
        return len(events)

    # ---------------- delivery ----------------
    async def deliver(self) -> int:
        async with self._deliver_lock:  # tick and the maintenance probe both deliver
            return await self._deliver()

    async def _deliver(self) -> int:
        data = self.data()
        now, sent, keep = self.clock(), 0, []
        config = data["config"]
        for item in data["outbox"]:
            targets = [int(cid) for cid, c in config.items()
                       if item["log_type"] in c.get("types", []) and item["created"] >= c.get("since", 0) - 1]
            pending = [cid for cid in targets if cid not in item["done"]]
            if not pending or now - item["created"] > EVENT_TTL:
                continue
            keep.append(item)
            if sent >= MAX_SENDS_PER_TICK:
                continue
            for cid in pending:
                channel = self.bot.get_channel(cid)
                if channel is None or sent >= MAX_SENDS_PER_TICK:
                    continue
                try:
                    if item["log_type"] == "maintenance":
                        rendered = {"content": M.render(item["payload"]["kind"], item["payload"].get("duration_seconds"))}
                    elif item["log_type"] == "clan_level":
                        rendered = await render_level(item, self.badge_fetcher)
                    else:
                        rendered = R.render_event(item)
                    await self.sender(channel, rendered, item["key"])
                except Exception as exc:
                    log.warning("clan log post failed channel=%s type=%s (%s), retrying", cid, item["log_type"],
                                type(exc).__name__)
                    continue
                item["done"].append(cid)
                sent += 1
                self.store.save()
        if len(keep) != len(data["outbox"]):
            data["outbox"][:] = [i for i in keep]
            self.store.save()
        return sent

    async def check_maintenance(self) -> Optional[dict]:
        """One probe; a confirmed start/end is queued for every maintenance channel."""
        result = await M.probe(self.client())
        event = self.maintenance.observe(result, self.clock())
        if event:
            data = self.data()
            self._enqueue(data, [{"event_type": f"maintenance_{event['kind']}", "log_type": "maintenance",
                                  "key": f"maintenance|{event['kind']}|{event['started_at']}", "payload": event,
                                  "clan_tag": None, "player_tag": None}])
            self.store.save()
            await self.deliver()
        return event

    async def tick(self) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            try:
                await self.poll()
            except Exception as exc:  # API down etc.: still deliver what is queued
                log.warning("clan log poll skipped (%s: %s)", type(exc).__name__, exc)
            return await self.deliver()
