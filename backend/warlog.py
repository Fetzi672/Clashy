"""Live war log, same format as SBJ Protocol's war log, but self-contained in Clashy.

Every minute the current war (normal war, or the CWL war of the current round) is
read and compared with what was already posted:
- matchup / war start / war end   -> one embed each
- every new attack or defense      -> one line, e.g.
  ⚔️ <th17>**Name⁴** ⭐⭐☆ **87%** <th16>⁶

Nothing is posted twice: posted keys are stored in state.json right after each
message, and every message carries a deterministic nonce Discord deduplicates on
(covers a crash between sending and saving). When the log is switched on, the
attacks of the running war count as already seen, so no history is dumped.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

import discord
from discord.http import handle_message_parameters
from discord.utils import MISSING

import emojis as E

log = logging.getLogger(__name__)

MAX_POSTS_PER_TICK = 25
_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
STATE_TITLES = {"preparation": "War Matchup", "inWar": "War Started", "warEnded": "War Ended"}
STATE_COLORS = {"preparation": discord.Color.blue(), "inWar": discord.Color.orange(), "warEnded": discord.Color.dark_grey()}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "Unknown", limit: int = 64) -> str:
    text = discord.utils.escape_markdown(" ".join(str(value or "").split()))[:limit]
    return text or default


def war_id(war: dict) -> Optional[str]:
    start = war.get("preparationStartTime") or war.get("startTime")
    opponent = (war.get("opponent") or {}).get("tag")
    return f"{start}|{opponent}" if start and opponent else None


def attacks(war: dict) -> list[dict]:
    """All attacks of both sides, oldest first, with attacker/defender member data."""
    members = {}
    for side in ("clan", "opponent"):
        for m in (war.get(side) or {}).get("members") or []:
            if isinstance(m, dict) and m.get("tag"):
                members[m["tag"]] = m
    out = []
    for side in ("clan", "opponent"):
        for m in (war.get(side) or {}).get("members") or []:
            for a in (m or {}).get("attacks") or []:
                if not isinstance(a, dict):
                    continue
                out.append({**a, "side": side,
                            "key": f"{a.get('order')}|{a.get('attackerTag')}|{a.get('defenderTag')}",
                            "attacker": members.get(a.get("attackerTag"), {}),
                            "defender": members.get(a.get("defenderTag"), {})})
    return sorted(out, key=lambda a: (_int(a.get("order"), 10**9), a["key"]))


def render_attack(a: dict) -> str:
    stars = max(0, min(3, _int(a.get("stars"))))
    destruction = max(0, min(100, _int(a.get("destructionPercentage"))))
    attacker, defender = a.get("attacker") or {}, a.get("defender") or {}
    direction = E.emoji("war_attack", "⚔️") if a.get("side") == "clan" else E.emoji("war_defense", "🛡️")
    a_pos = str(_int(attacker.get("mapPosition"), 0) or "").translate(_SUPERSCRIPT)
    d_pos = str(_int(defender.get("mapPosition"), 0) or "").translate(_SUPERSCRIPT)
    name = _text(attacker.get("name") or a.get("attackerTag"), "Attacker")
    return (f"{direction} {E.th(attacker.get('townhallLevel') or attacker.get('townHallLevel'))}"
            f"**{name}{a_pos}** {E.stars(stars)} **{destruction}%** "
            f"{E.th(defender.get('townhallLevel') or defender.get('townHallLevel'))}{d_pos}")


def render_state(war: dict, *, cwl: bool) -> discord.Embed:
    state = war.get("state")
    clan, opp = war.get("clan") or {}, war.get("opponent") or {}
    title = STATE_TITLES.get(state, "War Update") + (" (CWL)" if cwl else "")
    embed = discord.Embed(
        title=title,
        description=f"{E.SWORD()} **{_text(clan.get('name'))}** vs **{_text(opp.get('name'))}**",
        color=STATE_COLORS.get(state, discord.Color.orange()),
    )
    if war.get("teamSize"):
        embed.add_field(name="Team Size", value=f"{_int(war.get('teamSize'))} vs {_int(war.get('teamSize'))}", inline=True)
    if state in ("inWar", "warEnded"):
        for side in (clan, opp):
            embed.add_field(
                name=_text(side.get("name")),
                value=f"{E.STAR()} {_int(side.get('stars'))} • {float(side.get('destructionPercentage') or 0):.1f}% • "
                      f"{_int(side.get('attacks'))} attacks",
                inline=True,
            )
    if state == "warEnded":
        per = _int(war.get("attacksPerMember"), 1 if cwl else 2)
        missing = [(m.get("mapPosition") or 99, _text(m.get("name")), per - len(m.get("attacks") or []))
                   for m in clan.get("members") or [] if len(m.get("attacks") or []) < per]
        if missing:
            lines = [f"**{n}** • {left} missing" for _, n, left in sorted(missing)[:15]]
            if len(missing) > 15:
                lines.append(f"… and {len(missing) - 15} more")
            embed.add_field(name="Missed Attacks", value="\n".join(lines), inline=False)
        cs, os_ = _int(clan.get("stars")), _int(opp.get("stars"))
        cd, od = float(clan.get("destructionPercentage") or 0), float(opp.get("destructionPercentage") or 0)
        result = "Win" if (cs, cd) > (os_, od) else "Loss" if (cs, cd) < (os_, od) else "Tie"
        embed.add_field(name="Result", value=f"**{result}**", inline=False)
    badge = ((opp.get("badgeUrls") or {}).get("medium"))
    if badge:
        embed.set_thumbnail(url=badge)
    return embed


def nonce(channel_id: int, key: str) -> str:
    return "wl-" + hashlib.sha256(f"{channel_id}|{key}".encode()).hexdigest()[:20]


async def send_with_nonce(channel, *, content=None, embed=None, key: str):
    """channel.send with a Discord-enforced nonce: a resend within minutes is dropped."""
    state = channel._state
    with handle_message_parameters(
        content=content if content is not None else MISSING,
        embed=embed if embed is not None else MISSING,
        nonce=nonce(channel.id, key),
        allowed_mentions=discord.AllowedMentions.none(),
        previous_allowed_mentions=getattr(state, "allowed_mentions", None),
    ) as params:
        params.payload["enforce_nonce"] = True
        data = await state.http.send_message(channel.id, params=params)
    return state.create_message(channel=channel, data=data)


async def find_current_wars(client, clan_tag: str) -> list[tuple[dict, bool]]:
    """[(war, is_cwl)]. A normal war, or during CWL our wars of the last three rounds
    (just ended, in progress, in preparation) - oldest state first."""
    war = await client.get_current_war(clan_tag)
    if war and war.get("state") in ("preparation", "inWar", "warEnded"):
        return [(war, False)]
    group = await client.get_cwl_league_group(clan_tag)
    if not group or group.get("state") not in ("preparation", "inWar", "ended"):
        return []
    own = clan_tag.upper()
    # The newest round is usually in preparation while the one before is being fought:
    # look at the last two rounds and prefer the war in progress.
    rounds = [r for r in group.get("rounds") or [] if any(t and t != "#0" for t in r.get("warTags") or [])]
    found = {}
    for rnd in rounds[-3:]:
        for tag in rnd.get("warTags") or []:
            if not tag or tag == "#0":
                continue
            cwl = await client.get_cwl_war(tag)
            if not cwl:
                continue
            if (cwl.get("opponent") or {}).get("tag", "").upper() == own:
                cwl = {**cwl, "clan": cwl["opponent"], "opponent": cwl["clan"]}
            if (cwl.get("clan") or {}).get("tag", "").upper() == own:
                found.setdefault(cwl.get("state"), cwl)
                break
    return [(found[state], True) for state in ("warEnded", "inWar", "preparation") if state in found]


class WarLog:
    def __init__(self, store, bot, client_getter, clan_tag_getter, sender=send_with_nonce):
        self.store, self.bot = store, bot
        self.client = client_getter
        self.clan_tag = clan_tag_getter
        self.sender = sender

    def config(self) -> dict:
        cfg = self.store.load().setdefault("warlog", {})
        cfg.setdefault("channels", [])
        cfg.setdefault("wars", {})
        return cfg

    async def enable(self, channel_id: int) -> bool:
        """Add a channel. The running war counts as already posted for it."""
        cfg = self.config()
        if channel_id in cfg["channels"]:
            return False
        wars = await find_current_wars(self.client(), self.clan_tag())
        cfg["channels"].append(channel_id)
        for war, _ in wars:
            if war_id(war):
                entry = cfg["wars"].setdefault(war_id(war), {"posted": {}})
                entry["posted"][str(channel_id)] = [a["key"] for a in attacks(war)] + [f"state:{war.get('state')}"]
        self.store.save()
        return True

    def disable(self, channel_id: int) -> bool:
        cfg = self.config()
        if channel_id not in cfg["channels"]:
            return False
        cfg["channels"].remove(channel_id)
        self.store.save()
        return True

    async def tick(self) -> int:
        cfg = self.config()
        if not cfg["channels"]:
            return 0
        sent = 0
        for war, cwl in await find_current_wars(self.client(), self.clan_tag()):
            wid = war_id(war)
            if not wid:
                continue
            if war.get("state") == "warEnded" and wid not in cfg["wars"]:
                continue  # never seen live: do not post an old result
            sent += await self._post_war(cfg, war, wid, cwl, MAX_POSTS_PER_TICK - sent)
        for old in list(cfg["wars"])[:-8]:  # keep the latest wars only
            cfg["wars"].pop(old, None)
        return sent

    async def _post_war(self, cfg: dict, war: dict, wid: str, cwl: bool, budget: int) -> int:
        entry = cfg["wars"].setdefault(wid, {"posted": {}})
        items = [(f"state:{war.get('state')}", None)] + [(a["key"], a) for a in attacks(war)]
        sent = 0
        for channel_id in list(cfg["channels"]):
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                continue
            posted = entry["posted"].setdefault(str(channel_id), [])
            done = set(posted)
            for key, attack in items:
                if key in done:
                    continue
                if sent >= budget:
                    return sent
                try:
                    if attack is None:
                        await self.sender(channel, embed=render_state(war, cwl=cwl), key=f"{wid}|{key}")
                    else:
                        await self.sender(channel, content=render_attack(attack), key=f"{wid}|{key}")
                except Exception as exc:
                    log.warning("war log post failed channel=%s (%s), retry next minute", channel_id, type(exc).__name__)
                    break
                posted.append(key)
                done.add(key)
                self.store.save()
                sent += 1
        return sent
