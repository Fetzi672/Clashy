"""Log rendering copied from SBJ Protocol (member-registry-bot
services/registry_log_service.py), so Clashy's logs look exactly the same.
Only the emoji lookup is Clashy's own (emojis.py) and the registry DB badge
fallback is gone (Clashy has the clan badge itself)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import discord

import emojis as E


def normalize_tag(tag: str) -> str:
    tag = str(tag or "").strip().upper().replace("O", "0")
    return tag if tag.startswith("#") else "#" + tag


LOG_TYPES = (
    "member_join",
    "member_leave",
    "donations",
    "war",
    "townhall_upgrade",
    "troop_upgrade",
    "hero_upgrade",
    "spell_upgrade",
    "hero_equipment_upgrade",
    "name_change",
    "role_change",
    "league_change",
)

_UPGRADE_TYPES = {
    "townhall": "townhall_upgrade",
    "town_hall": "townhall_upgrade",
    "townhall_level": "townhall_upgrade",
    "troop": "troop_upgrade",
    "troops": "troop_upgrade",
    "hero": "hero_upgrade",
    "heroes": "hero_upgrade",
    "spell": "spell_upgrade",
    "spells": "spell_upgrade",
    "hero_equipment": "hero_equipment_upgrade",
    "equipment": "hero_equipment_upgrade",
    "hero_equipment_upgrade": "hero_equipment_upgrade",
}
_CHANGE_TYPES = {
    "name": "name_change",
    "player_name": "name_change",
    "role": "role_change",
    "ingame_role": "role_change",
    "league": "league_change",
    "league_id": "league_change",
}
_SUPERSCRIPT_TRANSLATION = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
_CLASH_TAG_PATTERN = re.compile(r"^#[A-Z0-9]{1,19}$")
_DISCORD_CONTENT_LIMIT = 2000


def _payload(event: dict) -> dict:
    value = event.get("payload")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _first(data: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_text(value: Any, default: str = "Unknown", limit: int = 200) -> str:
    text = str(value if value not in (None, "") else default)[:limit]
    text = re.sub(r"(?i)https?://", lambda match: match.group(0).replace(":", "∶"), text)
    text = discord.utils.escape_mentions(text)
    text = text.replace("\\", "\\\\")
    for character in "*_`~|[]()<>":
        text = text.replace(character, f"\\{character}")
    return text


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _display_value(value: Any, default: str = "Unknown") -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("id") or default
    return _safe_text(value, default)


def _superscript(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(value)).translate(_SUPERSCRIPT_TRANSLATION)
    except (TypeError, ValueError, OverflowError):
        return ""


def _safe_level(value: Any) -> str:
    try:
        level = int(value)
    except (TypeError, ValueError, OverflowError):
        return "?"
    return str(level) if 0 <= level <= 9999 else "?"


def _upgrade_log_type(payload: dict) -> str | None:
    raw = str(_first(payload, "category", "upgrade_type", "type", "field", default=""))
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return _UPGRADE_TYPES.get(normalized)


def _canonical_upgrade_type(payload: dict) -> str:
    log_type = _upgrade_log_type(payload)
    return log_type.removesuffix("_upgrade") if log_type else ""


def _safe_clash_tag(tag: Any) -> str | None:
    if tag in (None, ""):
        return None
    normalized = normalize_tag(str(tag))
    return normalized if _CLASH_TAG_PATTERN.fullmatch(normalized) else None


def _discord_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate_discord(text: str, max_units: int) -> str:
    if _discord_units(text) <= max_units:
        return text
    if max_units <= 1:
        return "…"[:max_units]
    units = 0
    result: list[str] = []
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > max_units - 1:
            break
        result.append(character)
        units += width
    return "".join(result) + "…"


def player_link(tag: str | None) -> str | None:
    normalized = _safe_clash_tag(tag)
    if not normalized:
        return None
    return f"https://link.clashofclans.com/en?action=OpenPlayerProfile&tag={quote(normalized, safe='')}"


def clan_link(tag: str | None) -> str | None:
    normalized = _safe_clash_tag(tag)
    if not normalized:
        return None
    return f"https://link.clashofclans.com/en?action=OpenClanProfile&tag={quote(normalized, safe='')}"



class _Renderer:
    emoji = staticmethod(E.emoji)

    def _townhall_icon(self, level: Any) -> str:
        parsed = _number(level)
        return self.emoji(f"th{parsed}", f"TH{parsed}") if parsed > 0 else "🏰"

    def route_event(self, event: dict) -> str | None:
        event_type = str(event.get("event_type") or "").strip().lower()
        if event_type == "clan_member_join":
            return "member_join"
        if event_type == "clan_member_leave":
            return "member_leave"
        if event_type == "clan_donation":
            return "donations"
        if event_type.startswith("war_"):
            return "war"
        payload = _payload(event)
        if event_type == "player_upgrade":
            return _upgrade_log_type(payload)
        if event_type == "player_change":
            field = str(_first(payload, "field", "field_name", "change_type", "type", default="")).lower()
            return _CHANGE_TYPES.get(field)
        return None


    def _player_description(self, event: dict, payload: dict) -> str:
        identity = _mapping(payload.get("player")) or _mapping(payload.get("member"))
        tag = event.get("player_tag") or identity.get("tag") or payload.get("player_tag")
        name = _safe_text(
            identity.get("name")
            or _first(payload, "player_name", "name", default=tag),
            default=str(tag or "Unknown player"),
        )
        link = player_link(str(tag)) if tag else None
        return f"[{name}]({link})" if link else name


    def render_event(self, event: dict) -> dict[str, Any]:
        event_type = str(event.get("event_type") or "").lower()
        payload = _payload(event)
        player = self._player_description(event, payload)
        clan_data = _mapping(payload.get("clan"))
        clan = _safe_text(
            clan_data.get("name") or payload.get("clan_name") or event.get("clan_tag"),
            "Clan",
        )

        if event_type in {"clan_member_join", "clan_member_leave"}:
            joined = event_type.endswith("join")
            member = _mapping(payload.get("member"))
            th = _first(member, "townHallLevel", "townhall_level")
            icon = "✅" if joined else "🚪"
            description = f"{icon} {player} {'joined' if joined else 'left'} **{clan}**."
            embed = discord.Embed(
                title="Member Joined" if joined else "Member Left",
                description=description,
                color=discord.Color.green() if joined else discord.Color.red(),
            )
            if th is not None:
                level = _number(th)
                embed.add_field(
                    name="Town Hall",
                    value=f"{self.emoji(f'th{level}', '🏰')} TH{level}",
                    inline=True,
                )
            role = member.get("role")
            if role:
                embed.add_field(name="Role", value=_display_value(role).replace("_", " ").title(), inline=True)
            embed.set_footer(text=f"Clan: {event.get('clan_tag') or 'Unknown'}")
            return {"embed": embed}

        if event_type == "clan_donation":
            members = [item for item in payload.get("members") or [] if isinstance(item, dict)]

            def donation_lines(delta_key: str, emoji_name: str, fallback: str) -> str:
                changed = sorted(
                    (item for item in members if _number(item.get(delta_key)) > 0),
                    key=lambda item: (
                        -_number(item.get(delta_key)),
                        str(item.get("player_name") or "").casefold(),
                        str(item.get("player_tag") or ""),
                    ),
                )
                lines: list[str] = []
                for item in changed:
                    if len(lines) >= 10:
                        break
                    amount = _number(item.get(delta_key))
                    member_tag = item.get("player_tag")
                    member_name = _safe_text(item.get("player_name") or member_tag, "Unknown")
                    link = player_link(str(member_tag)) if member_tag else None
                    label = f"[{member_name}]({link})" if link else member_name
                    line = f"{self.emoji(emoji_name, fallback)} `{amount:,}` {label}"
                    if len("\n".join((*lines, line))) > 900:
                        break
                    lines.append(line)
                omitted = len(changed) - len(lines)
                if omitted:
                    lines.append(f"… and {omitted} more players")
                return "\n".join(lines) or "—"

            clan_tag = clan_data.get("tag") or event.get("clan_tag")
            badge_urls = _mapping(clan_data.get("badgeUrls") or clan_data.get("badge_urls"))
            badge_url = _first(badge_urls, "large", "medium", "small") or clan_data.get("badge_url")

            embed = discord.Embed(
                title=clan,
                url=clan_link(str(clan_tag)) if clan_tag else None,
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Donated",
                value=donation_lines("donated_delta", "donation_up", "⬆️"),
                inline=False,
            )
            embed.add_field(
                name="Received",
                value=donation_lines("received_delta", "donation_down", "⬇️"),
                inline=False,
            )
            if isinstance(badge_url, str) and badge_url.startswith(("https://", "http://")):
                embed.set_thumbnail(url=badge_url)
            return {"embed": embed}

        if event_type == "player_upgrade":
            unit_raw = str(_first(payload, "name", "unit_name", "item_name", default="Upgrade"))
            unit = _safe_text(unit_raw)
            old_level = _first(payload, "old_level", "from_level")
            new_level = _safe_level(_first(payload, "new_level", "to_level", "level"))
            upgrade_type = _canonical_upgrade_type(payload)
            village = payload.get("village")
            player_data = _mapping(payload.get("player"))
            th = _first(player_data, "townHallLevel", "townhall_level")
            if upgrade_type == "townhall":
                icon = self._townhall_icon(new_level)
                return {"content": f"{player} upgraded to {icon}Townhall {new_level}"}

            families = {
                "troop": "troops",
                "hero": "heroes",
                "spell": "spells",
                "hero_equipment": "heroEquipment",
            }
            family = families.get(upgrade_type)
            emoji_key = f"{family}:{unit_raw}:{village}" if family and village else unit_raw
            icon = self.emoji(emoji_key)
            prefix = f"{self._townhall_icon(th)}{player}"
            if old_level is None:
                content = f"{prefix} unlocked {icon}{unit}"
            else:
                content = f"{prefix} leveled up {icon}{unit} to lv{new_level}"
            return {"content": content}

        if event_type == "player_change":
            log_type = self.route_event(event) or "player_change"
            label = log_type.removesuffix("_change").replace("_", " ").title()
            before = _display_value(_first(payload, "old_value", "before", "old", default="Unknown"))
            after = _display_value(_first(payload, "new_value", "after", "new", default="Unknown"))
            embed = discord.Embed(
                title=f"Player {label} Changed",
                description=f"{player}\n**{before}** → **{after}**",
                color=discord.Color.teal(),
            )
            return {"embed": embed}


        return {"content": f"Clan event `{_safe_text(event_type, 'unknown')}`"}


_R = _Renderer()
route_event = _R.route_event
render_event = _R.render_event
