import os
import io
import asyncio
import logging
import json
import glob
import re
import aiohttp
from collections import defaultdict
from datetime import datetime, time as dtime
from typing import Optional, Dict, Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from pacing import paced_edit
import panels
import emojis as E
from coc_api import COCClient, CocApiError
from state_store import StateStore, remember_ids
from warlog import WarLog
import clan_logs as CL
from datetime import timezone

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment
load_dotenv()

# Environment variables
COC_TOKEN = os.getenv('COC_API_TOKEN')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GUILD_ID = int(os.getenv('ALLOWED_GUILD_ID', 0))
CLAN_TAG = os.getenv('CLAN_TAG', '')

# Channel IDs from environment
CHANNEL_CLAN_STATISTIKEN = int(os.getenv('CHANNEL_CLAN_STATISTIKEN', 0))
CHANNEL_KRIEGSPROTOKOLL = int(os.getenv('CHANNEL_KRIEGSPROTOKOLL', 0))
CHANNEL_CWL_TABELLE = int(os.getenv('CHANNEL_CWL_TABELLE', 0))
CHANNEL_CLAN_SPIELE = int(os.getenv('CHANNEL_CLAN_SPIELE', 0))

STATE_PATH = "/data/state.json"
CACHE_DIR = "/data/cache"
CARDS_DIR = "/data/cards"
WAR_SNAPSHOTS_DIR = "/data/war_snapshots"

LEGEND_SOURCE_URL = "https://clashofclans-layouts.com/de/plans/th_18/legend_league/"
ANTI_DRAGON_SOURCE_URL = "https://clashofclans-layouts.com/de/plans/th_18/anti_air_dragon_electro/"
TH17_LEGEND_SOURCE_URL = "https://clashofclans-layouts.com/de/plans/th_17/legend_league/"
TH17_ANTI_DRAGON_SOURCE_URL = "https://clashofclans-layouts.com/de/plans/th_17/anti_air_dragon_electro/"
LEGEND_SITE_BASE = "https://clashofclans-layouts.com"
LEGEND_TOP_N = 4


STORE = StateStore(STATE_PATH)


def load_state() -> Dict[str, Any]:
    """The one shared state dict (see state_store.py)."""
    return STORE.load()


def save_state(state: Optional[Dict[str, Any]] = None) -> None:
    STORE.save()


def set_channel(key: str, channel_id: int) -> None:
    state = load_state()
    channels = state.get("channels", {})
    channels[key] = str(channel_id)
    state["channels"] = channels
    save_state(state)


def set_channel_clan_tag(key: str, clan_tag: str) -> None:
    state = load_state()
    tags = state.get("channel_clan_tags", {})
    tags[key] = clan_tag
    state["channel_clan_tags"] = tags
    save_state(state)


def get_channel_clan_tag(key: str) -> Optional[str]:
    state = load_state()
    tags = state.get("channel_clan_tags", {})
    return tags.get(key)


def get_channel_id(key: str) -> Optional[int]:
    state = load_state()
    channels = state.get("channels", {})
    val = channels.get(key)
    if val:
        try:
            return int(val)
        except Exception:
            return None
    return None


def set_updates_paused(paused: bool) -> None:
    state = load_state()
    state["updates_paused"] = paused
    save_state(state)


def get_war_id(war: dict) -> Optional[str]:
    if not war:
        return None
    return (
        war.get("endTime")
        or war.get("startTime")
        or war.get("preparationStartTime")
        or war.get("opponent", {}).get("tag")
    )


async def war_marker_exists(channel: discord.abc.Messageable, war_id: str, limit: int = 120) -> bool:
    marker = f"[war:{war_id}]"
    try:
        async for msg in channel.history(limit=limit):
            if not msg.author.bot or msg.author.id != bot.user.id:
                continue
            if not msg.embeds:
                continue
            ft = (msg.embeds[0].footer.text or "") if msg.embeds[0].footer else ""
            if marker in ft:
                return True
    except Exception:
        return False
    return False


def updates_paused() -> bool:
    state = load_state()
    return bool(state.get("updates_paused", False))


def write_cache(name: str, data: Any) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_cache(name: str) -> Optional[Any]:
    path = os.path.join(CACHE_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_cache_dir() -> int:
    if not os.path.exists(CACHE_DIR):
        return 0
    removed = 0
    for p in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            os.remove(p)
            removed += 1
        except Exception:
            continue
    return removed


def clear_registered_channels() -> None:
    state = load_state()
    state["channels"] = {}
    save_state(state)


async def fetch_clan_with_members() -> dict:
    """Fresh clan + member list, or an exception. Never returns empty data that
    would overwrite a good panel with "Unknown / 0 members"."""
    clan_data = await bot.coc_client.get_clan(CLAN_TAG)
    if not clan_data or not clan_data.get("tag"):
        raise CocApiError("clan data unavailable")
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan_data["memberList"] = members
    if int(clan_data.get("members") or 0) > 0 and not clan_data.get("memberList"):
        raise CocApiError("member list unavailable")
    write_cache("clan", clan_data)
    write_cache("members", members)
    return clan_data


def save_war_snapshot(war: dict) -> None:
    if not war:
        return
    state = war.get("state")
    if state in (None, "notInWar"):
        return

    war_id = war.get("endTime") or war.get("startTime") or datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_id = str(war_id).replace(":", "-").replace("/", "_")

    os.makedirs(WAR_SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(WAR_SNAPSHOTS_DIR, f"war_{safe_id}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(war, f)
    except Exception:
        pass


def read_latest_war_snapshot() -> Optional[dict]:
    try:
        files = glob.glob(os.path.join(WAR_SNAPSHOTS_DIR, "war_*.json"))
        if not files:
            return None
        latest = max(files, key=os.path.getmtime)
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


async def fetch_pil_image(url: str, size: tuple[int, int]) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        session = await bot.coc_client.get_session()
        async with session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.read()
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        return im.resize(size)
    except Exception:
        return None


def _draw_glow_circle(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]):
    for i in range(6, 0, -1):
        alpha = 18 * i
        draw.ellipse((cx - r - i * 3, cy - r - i * 3, cx + r + i * 3, cy + r + i * 3), fill=(*color, alpha))


def _draw_progress_bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, pct: float):
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(35, 47, 71, 180))
    fill_w = max(8, int(w * max(0.0, min(1.0, pct))))
    draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=h // 2, fill=(34, 211, 238, 220))


def _draw_circular_progress(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, pct: float):
    pct = max(0.0, min(1.0, pct))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(51, 65, 85, 220), width=14)
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=-90, end=-90 + int(360 * pct), fill=(56, 189, 248, 255), width=14)


def render_stat_card(
    title: str,
    subtitle: str,
    lines: list[str],
    filename: str,
    kpis: Optional[list[tuple[str, str]]] = None,
    badge: Optional[Image.Image] = None,
    accent: tuple[int, int, int] = (56, 189, 248),
) -> str:
    os.makedirs(CARDS_DIR, exist_ok=True)
    path = os.path.join(CARDS_DIR, filename)

    w, h = 1400, 840
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    d = ImageDraw.Draw(img, "RGBA")

    # dark slate gradient (statbot-like)
    top = (22, 24, 32)
    bottom = (26, 28, 38)
    for y in range(h):
        t = y / h
        c = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
            255,
        )
        d.line((0, y, w, y), fill=c)

    # subtle glow accents
    d.ellipse((w - 320, -80, w + 80, 280), fill=(82, 194, 252, 45))
    d.ellipse((-120, h - 240, 220, h + 120), fill=(147, 112, 255, 40))

    # main card (slate)
    card_fill = (36, 38, 48, 240)
    card_outline = (68, 70, 82, 180)
    d.rounded_rectangle((32, 32, w - 32, h - 32), radius=22, fill=card_fill, outline=card_outline, width=2)

    # header bar (glass)
    d.rounded_rectangle((58, 56, w - 58, 196), radius=18, fill=(255, 255, 255, 20), outline=(120, 126, 142, 80), width=1)
    d.rectangle((58, 56, 70, 196), fill=(*accent, 200))

    d.text((90, 78), title, font=_font(58), fill=(235, 240, 245, 255))
    d.text((92, 136), subtitle, font=_font(26), fill=(180, 188, 200, 240))

    if badge:
        bg_badge = badge.resize((220, 220)).copy()
        alpha = bg_badge.split()[-1].point(lambda p: int(p * 0.40))
        bg_badge.putalpha(alpha)
        img.paste(bg_badge, (w - 300, 40), bg_badge)

    # KPI grid (stat cards)
    kpis = kpis or []
    icons = ["👥", "🏆", "🏛️", "🛡️", "🔥", "📈"]
    tile_w, tile_h = 300, 120
    sx, sy, gap = 70, 228, 22
    tile_fill = (46, 48, 60, 240)
    tile_outline = (82, 86, 104, 150)
    for i, (k, v) in enumerate(kpis[:6]):
        x = sx + (i % 3) * (tile_w + gap)
        y = sy + (i // 3) * (tile_h + 16)
        d.rounded_rectangle((x, y, x + tile_w, y + tile_h), radius=16, fill=tile_fill, outline=tile_outline, width=1)
        d.text((x + 14, y + 12), f"{icons[i % len(icons)]} {k}", font=_font(22), fill=(170, 176, 190, 240))
        d.text((x + 14, y + 50), v, font=_font(42), fill=(235, 240, 245, 255))

    # description panel (glass)
    d.rounded_rectangle((70, 518, 900, h - 92), radius=18, fill=(255, 255, 255, 14), outline=(82, 86, 104, 120), width=1)
    d.text((92, 540), "📝 Clan Beschreibung & Details", font=_font(26), fill=(206, 212, 223, 255))
    y = 586
    for line in lines[:3]:
        d.text((96, y), line, font=_font(26), fill=(226, 230, 236, 240))
        y += 40

    # War widget (glass + circular winrate + member bar)
    wx1, wy1, wx2, wy2 = 930, 518, w - 70, h - 92
    d.rounded_rectangle((wx1, wy1, wx2, wy2), radius=18, fill=(255, 255, 255, 14), outline=(82, 86, 104, 120), width=1)
    d.text((wx1 + 24, wy1 + 22), "⚔️ War Performance", font=_font(26), fill=(206, 212, 223, 255))

    members = None
    winrate_pct = 0.0
    for k, v in kpis:
        if "Mitglieder" in k and "/" in v:
            members = v
    for line in lines:
        if "Winrate" in line or "Win Rate" in line:
            try:
                num = float(line.split(":")[-1].replace("%", "").strip())
                winrate_pct = num / 100.0
            except Exception:
                pass

    d.text((wx1 + 28, wy1 + 68), "Siege / Niederlagen", font=_font(21), fill=(170, 176, 190, 240))
    d.text((wx1 + 30, wy1 + 102), "W", font=_font(34), fill=(46, 204, 113, 255))
    d.text((wx1 + 72, wy1 + 104), "vs", font=_font(20), fill=(170, 176, 190, 220))
    d.text((wx1 + 120, wy1 + 102), "L", font=_font(34), fill=(235, 87, 87, 255))

    _draw_circular_progress(d, wx1 + 210, wy1 + 154, 60, winrate_pct)
    d.text((wx1 + 166, wy1 + 220), f"Winrate {int(winrate_pct*100)}%", font=_font(20), fill=(170, 176, 190, 240))

    if members:
        d.text((wx1 + 30, wy1 + 260), f"Mitglieder {members}", font=_font(21), fill=(170, 176, 190, 240))
        try:
            a, b = members.split("/")
            _draw_progress_bar(d, wx1 + 30, wy1 + 296, 250, 16, int(a) / max(1, int(b)))
        except Exception:
            _draw_progress_bar(d, wx1 + 30, wy1 + 296, 250, 16, 0)

    # footer
    d.text((72, h - 64), f"Updated: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}", font=_font(20), fill=(170, 176, 190, 230))
    d.ellipse((w - 170 - 10, h - 60 - 10, w - 170 + 10, h - 60 + 10), fill=(52, 211, 153, 240))
    d.text((w - 148, h - 66), "API Online", font=_font(20), fill=(190, 245, 220, 240))

    img.convert("RGB").save(path, "PNG")
    return path


async def upsert_png_message(channel: discord.TextChannel, key: str, file_path: str, title_text: str = ""):
    content = f"{title_text} [key:{key}]".strip()

    async def edit(msg):
        file = discord.File(file_path, filename=os.path.basename(file_path))
        await paced_edit(msg, content=content, attachments=[file], embed=None)

    async def send():
        file = discord.File(file_path, filename=os.path.basename(file_path))
        return await channel.send(content=content, file=file, allowed_mentions=discord.AllowedMentions.none())

    return await panels.upsert(channel, key, store=STORE, bot_user_id=bot.user.id, edit=edit, send=send)


class ClashBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.coc_client: Optional[COCClient] = None
        self.clan_data: Optional[dict] = None

    async def setup_hook(self):
        # Initialize COC client
        self.coc_client = COCClient(COC_TOKEN)
        
        # Get clan data
        if CLAN_TAG:
            try:
                self.clan_data = await self.coc_client.get_clan(CLAN_TAG)
                logger.info(f"Connected to clan: {self.clan_data.get('name', 'Unknown')}")
            except Exception as e:
                logger.error(f"Failed to connect to clan: {e}")
        
        # Start background tasks (each waits for the gateway cache before its first run)
        for loop in ALL_LOOPS:
            loop.before_loop(_wait_until_ready)
        for loop in ALL_LOOPS:
            loop.start()
        cleanup_duplicate_panels.start()

        # Sync commands
        await self.tree.sync(guild=discord.Object(id=GUILD_ID))
        logger.info("Commands synced")

    async def close(self):
        if self.coc_client:
            await self.coc_client.close()
        await super().close()


# Initialize bot
bot = ClashBot()


def add_timestamp(embed: discord.Embed) -> discord.Embed:
    embed.timestamp = datetime.now(timezone.utc)
    embed.set_footer(text="Letztes Update • COC Clan Bot")
    return embed


def role_label(role: str) -> str:
    mapping = {
        "leader": "👑 Anführer",
        "coLeader": "🛡️ Co-Leader",
        "elder": "⭐ Ältester",
        "member": "👤 Member",
    }
    return mapping.get(role, "👤 Member")


async def upsert_embed_message(channel: discord.TextChannel, key: str, embeds: list[discord.Embed], *, create_if_missing: bool = True):
    """Edit the keyed panel in place. A new message is only posted when the old one is
    confirmed deleted; temporary Discord errors skip this cycle (see panels.py)."""
    tag = f"[key:{key}]"
    for e in embeds:
        footer_text = (e.footer.text or "").strip() if e.footer else ""
        if tag not in footer_text:
            e.set_footer(text=f"{footer_text} {tag}".strip())

    async def edit(msg):
        await paced_edit(msg, embeds=embeds, content=None)

    async def send():
        return await channel.send(embeds=embeds, allowed_mentions=discord.AllowedMentions.none())

    return await panels.upsert(channel, key, store=STORE, bot_user_id=bot.user.id, edit=edit, send=send,
                               create_if_missing=create_if_missing)


async def upsert_command_embed(interaction: discord.Interaction, key: str, embed: discord.Embed, success_text: str):
    await upsert_embed_message(interaction.channel, key, [embed])
    await reply(interaction, success_text)


def _extract_legend_items(listing_html: str, limit: int = LEGEND_TOP_N) -> list[dict]:
    items: list[dict] = []
    seen = set()
    pattern = re.compile(
        r'<a[^>]*title="(?P<title>[^"]+)"[^>]*href="(?P<href>/de/plans/th_\d+/[^"]+\.html)"[^>]*>(?P<body>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for m in pattern.finditer(listing_html):
        href = (m.group("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)

        body = m.group("body") or ""
        img_match = re.search(r'<img[^>]*class="[^"]*base_grid_img[^"]*"[^>]*(?:src|data-src)="([^"]+)"', body, flags=re.IGNORECASE | re.DOTALL)
        if not img_match:
            img_match = re.search(r'<img[^>]*(?:src|data-src)="([^"]*?/pics/[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', body, flags=re.IGNORECASE | re.DOTALL)
        image_url = urljoin(LEGEND_SITE_BASE, img_match.group(1).strip()) if img_match else None

        items.append(
            {
                "title": (m.group("title") or "Legend Base").strip(),
                "detail_url": urljoin(LEGEND_SITE_BASE, href),
                "image_url": image_url,
            }
        )
        if len(items) >= limit:
            break

    return items


async def _fetch_detail_html(detail_url: str) -> Optional[str]:
    session = await bot.coc_client.get_session()
    async with session.get(detail_url) as resp:
        if resp.status != 200:
            return None
        return await resp.text()


def _extract_openlayout_from_html(html: str) -> Optional[str]:
    m = re.search(r'https://link\.clashofclans\.com/[^"\'\s<>]+', html, flags=re.IGNORECASE)
    return m.group(0) if m else None


def _extract_image_from_html(html: str) -> Optional[str]:
    m = re.search(r'(?:src|data-src)="([^"]*?/pics/[^"]+/(?:original|preview)/[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html, flags=re.IGNORECASE)
    if not m:
        return None
    return urljoin(LEGEND_SITE_BASE, m.group(1).strip())


async def fetch_top_bases_from_listing(source_url: str, limit: int = LEGEND_TOP_N, label: str = "Layout") -> list[dict]:
    if not bot.coc_client:
        return []

    session = await bot.coc_client.get_session()
    async with session.get(source_url) as resp:
        if resp.status != 200:
            logger.error("%s listing fetch failed: %s", label, resp.status)
            return []
        listing_html = await resp.text()

    items = _extract_legend_items(listing_html, limit=limit)
    if not items:
        return []

    for item in items:
        try:
            html = await _fetch_detail_html(item["detail_url"])
            if not html:
                item["openlayout_url"] = None
                continue
            item["openlayout_url"] = _extract_openlayout_from_html(html)
            if not item.get("image_url"):
                item["image_url"] = _extract_image_from_html(html)
        except Exception as e:
            logger.warning("%s detail fetch failed (%s): %s", label, item.get("detail_url"), e)
            item["openlayout_url"] = None

    return items


def build_legend_embeds(items: list[dict], panel_title: str = "TH18 Legend Base") -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    for idx, item in enumerate(items, start=1):
        embed = discord.Embed(
            title=f"🏆 {panel_title} #{idx}",
            description=item.get("title", "Base"),
            color=discord.Color.orange(),
            url=item.get("detail_url"),
        )

        openlayout = item.get("openlayout_url")
        if openlayout:
            embed.add_field(name="🔗 Link", value=f"[Base in Clash of Clans öffnen]({openlayout})", inline=False)
        else:
            embed.add_field(name="🔗 Link", value="Nicht gefunden (Seite prüfen).", inline=False)

        if item.get("detail_url"):
            embed.add_field(name="🌐 Quelle", value=f"[Layout-Seite öffnen]({item['detail_url']})", inline=False)

        if item.get("image_url"):
            embed.set_image(url=item["image_url"])

        embeds.append(add_timestamp(embed))

    return embeds


async def update_base_legend_panel_once() -> bool:
    channel_id = get_channel_id("base_legend")
    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if not channel:
        return False

    items = await fetch_top_bases_from_listing(LEGEND_SOURCE_URL, limit=LEGEND_TOP_N, label="Legend")
    if not items:
        return False

    embeds = build_legend_embeds(items, panel_title="TH18 Legend Base")
    await upsert_embed_message(channel, "base_legend_panel", embeds)
    return True


async def update_base_antidragon_panel_once() -> bool:
    channel_id = get_channel_id("base_antidragon")
    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if not channel:
        return False

    items = await fetch_top_bases_from_listing(ANTI_DRAGON_SOURCE_URL, limit=LEGEND_TOP_N, label="Anti-Dragon")
    if not items:
        return False

    embeds = build_legend_embeds(items, panel_title="TH18 Anti Dragon Base")
    await upsert_embed_message(channel, "base_antidragon_panel", embeds)
    return True


async def update_base_legend17_panel_once() -> bool:
    channel_id = get_channel_id("base_legend17")
    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if not channel:
        return False

    items = await fetch_top_bases_from_listing(TH17_LEGEND_SOURCE_URL, limit=LEGEND_TOP_N, label="TH17 Legend")
    if not items:
        return False

    embeds = build_legend_embeds(items, panel_title="TH17 Legend Base")
    await upsert_embed_message(channel, "base_legend17_panel", embeds)
    return True


async def update_base_antidragon17_panel_once() -> bool:
    channel_id = get_channel_id("base_antidragon17")
    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if not channel:
        return False

    items = await fetch_top_bases_from_listing(TH17_ANTI_DRAGON_SOURCE_URL, limit=LEGEND_TOP_N, label="TH17 Anti-Dragon")
    if not items:
        return False

    embeds = build_legend_embeds(items, panel_title="TH17 Anti Dragon Base")
    await upsert_embed_message(channel, "base_antidragon17_panel", embeds)
    return True


def format_clan_statistics(clan_data: dict) -> discord.Embed:
    """Format clan statistics for display."""
    name = clan_data.get("name", "Unknown")
    tag = clan_data.get("tag", "")

    members = clan_data.get("memberList", [])
    member_count = len(members)

    location = clan_data.get("location", {})
    location_name = location.get("name", "N/A")

    created = clan_data.get("createdDate", "N/A")

    wars_won = clan_data.get("warWins", 0)
    wars_lost = clan_data.get("warLosses", 0)
    wars_tied = clan_data.get("warTies", 0)
    total_wars = wars_won + wars_lost + wars_tied
    win_rate = (wars_won / total_wars * 100) if total_wars > 0 else 0

    war_league = clan_data.get("warLeague", {})
    war_league_name = war_league.get("name", "N/A") if war_league else "N/A"

    points = clan_data.get("clanPoints", 0)
    required_trophies = clan_data.get("requiredTrophies", 0)
    capital_points = clan_data.get("clanCapitalPoints", 0)
    war_win_streak = clan_data.get("warWinStreak", 0)

    badge = clan_data.get("badgeUrls", {})
    labels = clan_data.get("labels", [])
    label_text = ", ".join([l.get("name", "") for l in labels[:4]]) or "Keine"

    embed = discord.Embed(
        title=f"📊 {name}",
        description=f"**Clan-Tag:** `{tag}`",
        color=discord.Color.blue(),
    )
    if badge.get("medium"):
        embed.set_thumbnail(url=badge.get("medium"))

    embed.add_field(
        name="🏰 Allgemein",
        value=(
            f"👥 Mitglieder: **{member_count}/50**\n"
            f"🌍 Ort: **{location_name}**\n"
            f"🗓️ Erstellt: **{created}**\n"
            f"🏷️ Labels: **{label_text}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ Krieg",
        value=(
            f"✅ Siege: **{wars_won}**\n"
            f"❌ Niederlagen: **{wars_lost}**\n"
            f"➖ Unentschieden: **{wars_tied}**\n"
            f"📈 Win Rate: **{win_rate:.1f}%**"
        ),
        inline=True,
    )

    embed.add_field(
        name="🏆 Fortschritt",
        value=(
            f"🏅 Clan-Punkte: **{points}**\n"
            f"🏛️ Capital-Punkte: **{capital_points}**\n"
            f"🔥 Win-Streak: **{war_win_streak}**\n"
            f"🎯 Benötigte Trophäen: **{required_trophies}**"
        ),
        inline=True,
    )

    embed.add_field(name="🛡️ Kriegsliga", value=f"**{war_league_name}**", inline=False)
    return add_timestamp(embed)


def format_top_members(clan_data: dict) -> discord.Embed:
    """Format top members."""
    name = clan_data.get("name", "Unknown")
    members = clan_data.get("memberList", [])

    sorted_members = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)[:10]

    embed = discord.Embed(
        title=f"👥 Top 10 Mitglieder • {name}",
        color=discord.Color.gold()
    )

    lines = []
    for i, member in enumerate(sorted_members, 1):
        trophies = member.get("trophies", 0)
        attack_wins = member.get("attackWins", 0)
        member_name = member.get("name", "Unknown")
        role = role_label(member.get("role", "member")).split(" ")[0]
        lines.append(f"`{i:>2}.` {E.th(member.get('townHallLevel'))} {role} **{member_name}** • 🏆 {trophies} • {E.SWORD()} {attack_wins}")

    embed.description = "\n".join(lines) if lines else "Keine Daten verfügbar."
    return add_timestamp(embed)


def role_sort_key(member: dict) -> tuple:
    role_order = {"leader": 0, "coLeader": 1, "elder": 2, "member": 3}
    return (role_order.get(member.get("role", "member"), 9), -member.get("trophies", 0))


def format_memberlist_embeds(clan_data: dict) -> list[discord.Embed]:
    name = clan_data.get("name", "Unknown")
    members = clan_data.get("memberList", [])
    members_sorted = sorted(members, key=role_sort_key)
    chunks = [members_sorted[i:i + 25] for i in range(0, 50, 25)]

    embeds = []
    for idx, chunk in enumerate(chunks, 1):
        embed = discord.Embed(
            title=f"👥 Memberliste • {name}",
            description=f"Teil **{idx}/2** • Sortierung: 👑 Anführer → 🛡️ Co-Leader → ⭐ Ältester → 👤 Member",
            color=discord.Color.blurple(),
        )
        lines = []
        for i, m in enumerate(chunk, 1 + (idx - 1) * 25):
            icon_role = role_label(m.get("role", "member")).split(" ")[0]
            lines.append(
                f"`{i:>2}.` {E.th(m.get('townHallLevel'))} {icon_role} **{m.get('name', 'Unknown')}** • 🏆 {m.get('trophies', 0)}"
            )
        embed.description += "\n\n" + ("\n".join(lines) if lines else "Keine Daten.")
        add_timestamp(embed)
        embeds.append(embed)
    return embeds


def format_clan_games_progress(clan_data: dict, raid_data: Optional[dict] = None) -> discord.Embed:
    """Format Clan Games / Progress."""
    name = clan_data.get("name", "Unknown")
    members = clan_data.get("memberList", [])
    points = clan_data.get("clanPoints", 0)

    embed = discord.Embed(
        title=f"🎮 Fortschritt • {name}",
        color=discord.Color.green()
    )

    embed.add_field(
        name="📊 Übersicht",
        value=(
            f"👥 Mitglieder: **{len(members)}/50**\n"
            f"🏆 Clan-Punkte: **{points}**\n"
            f"🎯 Ziel: **50.000**"
        ),
        inline=False
    )

    sorted_members = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)[:5]
    top_list = "\n".join([f"`{i}.` {E.th(m.get('townHallLevel'))} **{m.get('name', 'Unknown')}** • 🏆 {m.get('trophies', 0)}" for i, m in enumerate(sorted_members, 1)])
    embed.add_field(
        name="🏅 Top 5 Trophäensammler",
        value=top_list or "Keine Daten.",
        inline=False
    )

    if raid_data:
        total_loot = raid_data.get("capitalTotalLoot", 0)
        attacks = raid_data.get("totalAttacks", 0)
        districts = raid_data.get("enemyDistrictsDestroyed", 0)
        embed.add_field(
            name="🏛️ Capital-Raid (aktuell)",
            value=(
                f"💰 Loot: **{total_loot}**\n"
                f"⚔️ Angriffe: **{attacks}**\n"
                f"🏚️ Zerstörte Distrikte: **{districts}**"
            ),
            inline=False
        )

    return add_timestamp(embed)


def format_cwl_table(clan_data: dict) -> discord.Embed:
    """Format CWL table."""
    name = clan_data.get("name", "Unknown")
    tag = clan_data.get("tag", "")
    members = clan_data.get("memberList", [])

    war_league = clan_data.get("warLeague", {})
    war_league_name = war_league.get("name", "N/A") if war_league else "N/A"

    embed = discord.Embed(
        title=f"⚔️ CWL Übersicht • {name}",
        color=discord.Color.red()
    )

    embed.add_field(
        name="📋 Info",
        value=(
            f"🏷️ Clan-Tag: `{tag}`\n"
            f"🛡️ CWL-Liga: **{war_league_name}**\n"
            f"👥 Mitglieder: **{len(members)}/50**"
        ),
        inline=False
    )

    sorted_members = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)[:15]
    member_list = "\n".join(
        [f"`{i+1:>2}.` {E.th(m.get('townHallLevel'))} **{m.get('name', 'Unknown')}** • 🏆 {m.get('trophies', 0)}" for i, m in enumerate(sorted_members)]
    )
    embed.add_field(
        name="🎯 CWL Roster (Top 15)",
        value=member_list or "Keine Daten.",
        inline=False
    )

    return add_timestamp(embed)


async def send_war_log(war_data: dict, channel: Optional[discord.abc.Messageable] = None, title: str = "⚔️ Kriegsprotokoll", war_id: Optional[str] = None):
    """Send war notification to a channel."""
    if not war_data:
        return
    
    if channel is None:
        channel = bot.get_channel(CHANNEL_KRIEGSPROTOKOLL)
    if not channel:
        return
    
    clan = war_data.get("clan", {})
    opponent = war_data.get("opponent", {})
    result = war_data.get("result")

    if not result and clan and opponent:
        clan_stars_tmp = clan.get("stars", 0)
        opponent_stars_tmp = opponent.get("stars", 0)
        if clan_stars_tmp > opponent_stars_tmp:
            result = "win"
        elif clan_stars_tmp < opponent_stars_tmp:
            result = "lose"
        else:
            result = "tie"
    if not result:
        result = "unknown"
    
    result_icon = {"win": "✅", "lose": "❌", "tie": "➖"}.get(result, "❔")
    embed = discord.Embed(
        title=title,
        color=discord.Color.red() if result == "lose" else discord.Color.green()
    )

    clan_stars = clan.get("stars", war_data.get("stars", 0))
    opponent_stars = opponent.get("stars", 0)
    max_stars = war_data.get("maxStars", 0) or (len(clan.get("members", [])) * 3 if clan else 0)

    embed.add_field(
        name="📊 Ergebnis",
        value=(
            f"{result_icon} Ergebnis: **{result.upper()}**\n"
            f"{E.STAR()} Sterne: **{clan_stars} - {opponent_stars}** (Max: {max_stars})\n"
            f"🧪 Clan-EXP: **{war_data.get('expEarned', 0)}**\n"
            f"🛡️ Gegner: **{opponent.get('name', 'N/A') if opponent else 'N/A'}**"
        ),
        inline=False,
    )

    add_timestamp(embed)
    if war_id:
        ft = (embed.footer.text or "").strip() if embed.footer else ""
        embed.set_footer(text=f"{ft} [war:{war_id}]".strip())
    await channel.send(embed=embed)


# Background tasks
LOOP_TIMEOUT_SECONDS = 240


def guarded(func):
    """No exception ends a loop (discord.ext.tasks stops a loop for good when one
    escapes), and no hung call blocks a loop longer than LOOP_TIMEOUT_SECONDS."""
    import functools

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            await asyncio.wait_for(func(*args, **kwargs), timeout=LOOP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("%s: cycle exceeded %ss, retrying next cycle", func.__name__, LOOP_TIMEOUT_SECONDS)
        except CocApiError as exc:
            logger.warning("%s: Clash API unavailable (%s), panel kept as is", func.__name__, exc)
        except Exception:
            logger.exception("%s: cycle failed, retrying next cycle", func.__name__)
    return wrapper


@tasks.loop(minutes=5)
@guarded
async def update_clan_statistics():
    """Update clan statistics message every 5 minutes."""
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return

    channel_id = get_channel_id("clan_statistiken")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        clan_data = await fetch_clan_with_members()
        bot.clan_data = clan_data
        cached = read_cache("clan") or clan_data
        embed = format_clan_statistics(cached)
        embed2 = format_top_members(cached)
        member_embeds = format_memberlist_embeds(cached)

        await upsert_embed_message(channel, "clan_statistiken", [embed, embed2])
        await upsert_embed_message(channel, "memberliste_1", [member_embeds[0]])
        await upsert_embed_message(channel, "memberliste_2", [member_embeds[1]])
        logger.info("Clan statistics updated")
    except Exception as e:
        logger.warning(f"Clan statistics not updated: {e}")


@tasks.loop(minutes=5)
@guarded
async def update_clan_spiele():
    """Update clan games progress every 5 minutes."""
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return

    channel_id = get_channel_id("clan_spiele")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        clan_data = await fetch_clan_with_members()
        raid = await bot.coc_client.get_capital_raid_seasons(CLAN_TAG, limit=1)
        write_cache("capital_raid", raid)

        cached = read_cache("clan") or clan_data
        raid_cached = read_cache("capital_raid") or raid
        raid_latest = raid_cached[0] if raid_cached else None
        embed = format_clan_games_progress(cached, raid_latest)

        await upsert_embed_message(channel, "clan_spiele", [embed])
        logger.info("Clan games updated")
    except Exception as e:
        logger.warning(f"Clan games not updated: {e}")


@tasks.loop(minutes=5)
@guarded
async def update_cwl_tabelle():
    """Update CWL table message every 5 minutes."""
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return

    channel_id = get_channel_id("cwl_tabelle")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        clan_data = await fetch_clan_with_members()
        cached = read_cache("clan") or clan_data
        embed = format_cwl_table(cached)

        await upsert_embed_message(channel, "cwl_tabelle", [embed])
        logger.info("CWL table updated")
    except Exception as e:
        logger.warning(f"CWL table not updated: {e}")


@tasks.loop(minutes=5)
@guarded
async def check_war_log():
    """Check for new wars and send to war log."""
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return

    channel_id = get_channel_id("kriegsprotokoll")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        live_war = await bot.coc_client.get_current_war(CLAN_TAG)
        if live_war:
            save_war_snapshot(live_war)

        wars = await bot.coc_client.get_clan_war_log(CLAN_TAG)
        write_cache("warlog", wars)

        # Fallback: if warlog is private/unavailable, use current war endpoint
        if not wars:
            current_war = await bot.coc_client.get_current_war(CLAN_TAG)
            if current_war:
                write_cache("current_war", current_war)
                save_war_snapshot(current_war)
                wars = [current_war]

        cached_wars = wars or (read_cache("warlog") or [])
        state = load_state()
        posted = {str(x) for x in state.get("posted_wars", [])}

        # Nur den neuesten, noch nicht geposteten, beendeten Krieg senden
        newest_new_war = None
        newest_new_war_id = None

        for war in cached_wars:
            war_id = get_war_id(war)
            if not war.get("result") or not war_id or war_id in posted:
                continue

            if newest_new_war_id is None or str(war_id) > str(newest_new_war_id):
                newest_new_war = war
                newest_new_war_id = war_id

        if newest_new_war and newest_new_war_id:
            if await war_marker_exists(channel, str(newest_new_war_id)):
                state["posted_wars"] = remember_ids(state["posted_wars"], [newest_new_war_id])
                save_state(state)
                return

            await send_war_log(newest_new_war, channel=channel, war_id=str(newest_new_war_id))
            state["posted_wars"] = remember_ids(state["posted_wars"], [newest_new_war_id])
            save_state(state)
    except Exception as e:
        logger.error(f"Error checking war log: {e}")


@tasks.loop(minutes=5)
@guarded
async def check_cwl_wars():
    """Check for finished CWL wars and post to CWL channel."""
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return

    channel_id = get_channel_id("cwl_tabelle")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        group = await bot.coc_client.get_cwl_league_group(CLAN_TAG)
        if not group or "rounds" not in group:
            return
        write_cache("cwl_group", group)

        cached_group = read_cache("cwl_group") or group
        state = load_state()
        posted = {str(x) for x in state.get("posted_cwl_wars", [])}

        for rnd in cached_group.get("rounds", []):
            for war_tag in rnd.get("warTags", []):
                if war_tag == "#0":
                    continue
                if war_tag in posted:
                    continue
                war = await bot.coc_client.get_cwl_war(war_tag)
                if war:
                    write_cache(f"cwl_war_{war_tag.replace('#','')}", war)
                cached_war = read_cache(f"cwl_war_{war_tag.replace('#','')}") or war
                if cached_war and cached_war.get("state") == "warEnded":
                    if not await war_marker_exists(channel, str(war_tag)):
                        await send_war_log(cached_war, channel=channel, title="🏆 CWL Kriegsprotokoll", war_id=str(war_tag))
                    posted.add(war_tag)
                    # saved right away: a crash later in this loop must not post it again
                    state["posted_cwl_wars"] = remember_ids(state["posted_cwl_wars"], [war_tag])
                    save_state(state)
    except Exception as e:
        logger.error(f"Error checking CWL wars: {e}")


async def build_war_missed_embed() -> Optional[discord.Embed]:
    war = await bot.coc_client.get_current_war(CLAN_TAG)
    source = "live"

    if not war or war.get("state") in (None, "notInWar"):
        war = read_latest_war_snapshot()
        source = "snapshot"
        if not war:
            return None
    else:
        save_war_snapshot(war)

    attacks_per_member = war.get("attacksPerMember", 2)
    members = war.get("clan", {}).get("members", [])
    missing = []
    total_possible = len(members) * attacks_per_member
    total_used = 0

    for m in members:
        used = len(m.get("attacks", []))
        total_used += used
        left = max(attacks_per_member - used, 0)
        if left > 0:
            missing.append((f"{E.th(m.get('townhallLevel') or m.get('townHallLevel'))} {m.get('name', 'Unknown')}", left))

    total_missing = max(total_possible - total_used, 0)
    miss_pct = (total_missing / total_possible * 100) if total_possible > 0 else 0.0

    title = "🚨 Fehlende Kriegsangriffe"
    if source == "snapshot":
        title += " (Letzter Snapshot)"

    embed = discord.Embed(title=title, color=discord.Color.orange())
    embed.add_field(
        name="📊 Miss-Quote",
        value=(
            f"⚔️ Genutzt: **{total_used}/{total_possible}**\n"
            f"❌ Offen: **{total_missing}**\n"
            f"📉 Miss-Rate: **{miss_pct:.1f}%**"
        ),
        inline=False,
    )

    embed.add_field(
        name="👥 Offene Angriffe pro Spieler",
        value="\n".join([f"• **{n}**: {l} offen" for n, l in missing[:25]]) if missing else "Alle Angriffe wurden genutzt. ✅",
        inline=False,
    )

    return add_timestamp(embed)


async def build_war_performance_embed() -> Optional[discord.Embed]:
    war = await bot.coc_client.get_current_war(CLAN_TAG)
    if not war or war.get("state") in (None, "notInWar"):
        return None

    rows = []
    for m in war.get("clan", {}).get("members", []):
        attacks = m.get("attacks", [])
        stars = sum(a.get("stars", 0) for a in attacks)
        destr = sum(a.get("destructionPercentage", 0) for a in attacks) / len(attacks) if attacks else 0
        rows.append((m.get("name", "Unknown"), stars, destr, len(attacks), m.get("townhallLevel") or m.get("townHallLevel")))

    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
    embed = discord.Embed(title="📊 Kriegsperformance", color=discord.Color.blue())
    embed.description = "\n".join([f"`{i+1:>2}.` {E.th(r[4])} **{r[0]}** • {E.STAR()} {r[1]} • 💥 {r[2]:.1f}% • {E.SWORD()} {r[3]}" for i, r in enumerate(rows[:15])]) or "Keine Daten."
    return add_timestamp(embed)


async def build_cwl_performance_embed() -> Optional[discord.Embed]:
    group = await bot.coc_client.get_cwl_league_group(CLAN_TAG)
    if not group or "rounds" not in group:
        return None

    agg = defaultdict(lambda: {"stars": 0, "destr": 0.0, "attacks": 0})
    clan_tag_raw = CLAN_TAG.upper()
    for rnd in group.get("rounds", []):
        for war_tag in rnd.get("warTags", []):
            if war_tag == "#0":
                continue
            war = await bot.coc_client.get_cwl_war(war_tag)
            if not war or war.get("state") != "warEnded":
                continue
            side = war.get("clan", {}) if (war.get("clan", {}).get("tag", "").upper() == clan_tag_raw) else war.get("opponent", {})
            for m in side.get("members", []):
                for a in m.get("attacks", []):
                    rec = agg[m.get("name", "Unknown")]
                    rec["stars"] += a.get("stars", 0)
                    rec["destr"] += a.get("destructionPercentage", 0)
                    rec["attacks"] += 1

    rows = []
    for name, rec in agg.items():
        avg = rec["destr"] / rec["attacks"] if rec["attacks"] else 0
        rows.append((name, rec["stars"], avg, rec["attacks"]))
    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)

    embed = discord.Embed(title="🏆 CWL Performance", color=discord.Color.red())
    embed.description = "\n".join([f"`{i+1:>2}.` **{r[0]}** • {E.STAR()} {r[1]} • 💥 {r[2]:.1f}% • {E.SWORD()} {r[3]}" for i, r in enumerate(rows[:15])]) or "Keine Daten."
    return add_timestamp(embed)


async def build_cwl_missed_embed() -> Optional[discord.Embed]:
    group = await bot.coc_client.get_cwl_league_group(CLAN_TAG)
    if not group or "rounds" not in group:
        return None

    clan_tag_raw = CLAN_TAG.upper()
    agg = defaultdict(lambda: {"used": 0, "possible": 0, "missed": 0})
    total_used = 0
    total_possible = 0

    for rnd in group.get("rounds", []):
        for war_tag in rnd.get("warTags", []):
            if war_tag == "#0":
                continue
            war = await bot.coc_client.get_cwl_war(war_tag)
            if not war or war.get("state") != "warEnded":
                continue

            attacks_per_member = war.get("attacksPerMember", 1)
            side = war.get("clan", {}) if (war.get("clan", {}).get("tag", "").upper() == clan_tag_raw) else war.get("opponent", {})
            members = side.get("members", [])

            for m in members:
                name = m.get("name", "Unknown")
                used = len(m.get("attacks", []))
                possible = attacks_per_member
                missed = max(possible - used, 0)

                agg[name]["used"] += used
                agg[name]["possible"] += possible
                agg[name]["missed"] += missed

                total_used += used
                total_possible += possible

    if total_possible == 0:
        return None

    total_missed = max(total_possible - total_used, 0)
    total_miss_pct = (total_missed / total_possible * 100)

    rows = []
    for name, rec in agg.items():
        miss_pct = (rec["missed"] / rec["possible"] * 100) if rec["possible"] else 0.0
        rows.append((name, rec["missed"], rec["used"], rec["possible"], miss_pct))

    rows.sort(key=lambda x: (x[1], x[4], x[0]), reverse=True)

    embed = discord.Embed(title="🚨 CWL Missed Attacks", color=discord.Color.orange())
    embed.add_field(
        name="📊 Team-Miss-Quote",
        value=(
            f"⚔️ Genutzt: **{total_used}/{total_possible}**\n"
            f"❌ Verpasst: **{total_missed}**\n"
            f"📉 Miss-Rate: **{total_miss_pct:.1f}%**"
        ),
        inline=False,
    )

    lines = []
    for i, r in enumerate(rows[:20], 1):
        lines.append(f"`{i:>2}.` **{r[0]}** • ❌ {r[1]} • {E.SWORD()} {r[2]}/{r[3]} • 📉 {r[4]:.1f}%")

    embed.add_field(name="👥 Spieler (höchste Misses)", value="\n".join(lines) if lines else "Keine Daten.", inline=False)
    return add_timestamp(embed)


async def build_activity_donations_embed(clan_tag: Optional[str] = None) -> Optional[discord.Embed]:
    tag = clan_tag or CLAN_TAG
    if not tag:
        return None
    members = await bot.coc_client.get_clan_members(tag)
    if not members:
        return None

    top_d = sorted(members, key=lambda m: m.get("donations", 0), reverse=True)[:10]
    top_r = sorted(members, key=lambda m: m.get("donationsReceived", 0), reverse=True)[:10]

    embed = discord.Embed(title="📦 Donation Activity", color=discord.Color.green())
    embed.add_field(
        name="📤 Top Donors",
        value="\n".join([f"{E.th(m.get('townHallLevel'))} **{m.get('name','Unknown')}**: {E.DONATED()} {m.get('donations',0)}" for m in top_d]) or "No data.",
        inline=True,
    )
    embed.add_field(
        name="📥 Top Recipients",
        value="\n".join([f"{E.th(m.get('townHallLevel'))} **{m.get('name','Unknown')}**: {E.RECEIVED()} {m.get('donationsReceived',0)}" for m in top_r]) or "No data.",
        inline=True,
    )
    return add_timestamp(embed)


async def build_clan_overview_card() -> Optional[str]:
    clan = await bot.coc_client.get_clan(CLAN_TAG)
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan["memberList"] = members
    if not clan:
        return None

    badge_url = (clan.get("badgeUrls") or {}).get("large") or (clan.get("badgeUrls") or {}).get("medium")
    badge = await fetch_pil_image(badge_url, (120, 120)) if badge_url else None

    ww = clan.get('warWins', 0)
    wl = clan.get('warLosses', 0)
    wr = (ww / (ww + wl) * 100) if (ww + wl) > 0 else 0

    lines = [
        f"{clan.get('name', 'Unknown')} ({clan.get('tag', '-')})",
        f"War Liga: {(clan.get('warLeague') or {}).get('name', 'N/A')}  •  Winrate: {wr:.1f}%",
        f"War Siege/Niederlagen: {ww}/{wl}  •  Warlog: {'Public' if clan.get('isWarLogPublic', False) else 'Private'}",
        f"{(clan.get('description') or '-')[0:120]}",
    ]
    kpis = [
        ("Mitglieder", f"{len(clan.get('memberList', []))}/50"),
        ("Clan Level", str(clan.get("clanLevel", "-"))),
        ("Clan Punkte", str(clan.get("clanPoints", 0))),
        ("Capital Punkte", str(clan.get("clanCapitalPoints", 0))),
        ("Siege", str(ww)),
        ("Niederlagen", str(wl)),
    ]
    return render_stat_card(
        "COC • Clan Overview",
        "Auto-Update alle 5 Minuten",
        lines,
        "clan_overview.png",
        kpis=kpis,
        badge=badge,
        accent=(34, 197, 94),
    )


async def build_war_performance_card() -> Optional[str]:
    war = await bot.coc_client.get_current_war(CLAN_TAG)
    if not war or war.get("state") in (None, "notInWar"):
        return None

    rows = []
    for m in war.get("clan", {}).get("members", []):
        attacks = m.get("attacks", [])
        stars = sum(a.get("stars", 0) for a in attacks)
        destr = sum(a.get("destructionPercentage", 0) for a in attacks) / len(attacks) if attacks else 0
        rows.append((m.get("name", "Unknown"), stars, destr, len(attacks)))
    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)

    clan_stars = war.get('clan', {}).get('stars', 0)
    opp_stars = war.get('opponent', {}).get('stars', 0)
    attacks_per_member = war.get('attacksPerMember', 2)
    missed = 0
    for m in war.get("clan", {}).get("members", []):
        missed += max(attacks_per_member - len(m.get("attacks", [])), 0)

    badge_url = (war.get('opponent', {}).get('badgeUrls') or {}).get('large') or (war.get('opponent', {}).get('badgeUrls') or {}).get('medium')
    badge = await fetch_pil_image(badge_url, (120, 120)) if badge_url else None

    lines = [
        f"Gegner: {(war.get('opponent') or {}).get('name', 'N/A')}",
        f"Status: {war.get('state', 'N/A')}",
        f"Sterne: {clan_stars} - {opp_stars}",
    ]
    for i, r in enumerate(rows[:8], 1):
        lines.append(f"{i:>2}. {r[0]} | ⭐{r[1]} | 💥{r[2]:.1f}% | ⚔️{r[3]}")

    kpis = [
        ("Status", str(war.get('state', 'N/A'))),
        ("Sterne", f"{clan_stars} - {opp_stars}"),
        ("Angriffe offen", str(missed)),
        ("Teilnehmer", str(len(war.get('clan', {}).get('members', [])))),
    ]

    return render_stat_card(
        "COC • War Performance",
        "Auto-Update alle 5 Minuten",
        lines,
        "war_performance.png",
        kpis=kpis,
        badge=badge,
        accent=(239, 68, 68),
    )


@tasks.loop(minutes=5)
@guarded
async def update_war_missed_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("war_missed")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    embed = await build_war_missed_embed()
    if embed:
        await upsert_embed_message(channel, "war_missed_panel", [embed])


@tasks.loop(minutes=5)
@guarded
async def update_war_performance_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("war_performance")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    embed = await build_war_performance_embed()
    if embed:
        await upsert_embed_message(channel, "war_performance_panel", [embed])


@tasks.loop(minutes=5)
@guarded
async def update_cwl_performance_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("cwl_performance")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    embed = await build_cwl_performance_embed()
    if embed:
        await upsert_embed_message(channel, "cwl_performance_panel", [embed])


@tasks.loop(minutes=5)
@guarded
async def update_cwl_missed_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("cwl_missed")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    embed = await build_cwl_missed_embed()
    if embed:
        await upsert_embed_message(channel, "cwl_missed_panel", [embed])


@tasks.loop(minutes=5)
@guarded
async def update_activity_donations_panel():
    if updates_paused() or not bot.coc_client:
        return
    channel_id = get_channel_id("activity_donations")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    clan_tag = get_channel_clan_tag("activity_donations") or CLAN_TAG
    if not clan_tag:
        return
    embed = await build_activity_donations_embed(clan_tag)
    if embed:
        await upsert_embed_message(channel, "activity_donations_panel", [embed], create_if_missing=False)


@tasks.loop(minutes=5)
@guarded
async def update_clan_overview_card_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("card_clan_overview")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    path = await build_clan_overview_card()
    if path:
        await upsert_png_message(channel, "card_clan_overview", path, "🖼️ Clan Overview Card")


@tasks.loop(minutes=5)
@guarded
async def update_war_performance_card_panel():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    channel_id = get_channel_id("card_war_performance")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    path = await build_war_performance_card()
    if path:
        await upsert_png_message(channel, "card_war_performance", path, "🖼️ War Performance Card")


@tasks.loop(time=dtime(hour=12, minute=0, tzinfo=ZoneInfo("Europe/Berlin")))
@guarded
async def update_base_legend_panel_daily():
    if updates_paused() or not bot.coc_client:
        return
    try:
        ok = await update_base_legend_panel_once()
        if ok:
            logger.info("Legend bases panel updated (daily schedule)")
        else:
            logger.warning("Legend bases panel skipped (no channel/data)")
    except Exception as e:
        logger.error(f"Error updating legend bases panel: {e}")


@tasks.loop(time=dtime(hour=12, minute=0, tzinfo=ZoneInfo("Europe/Berlin")))
@guarded
async def update_base_antidragon_panel_daily():
    if updates_paused() or not bot.coc_client:
        return
    try:
        ok = await update_base_antidragon_panel_once()
        if ok:
            logger.info("Anti-dragon bases panel updated (daily schedule)")
        else:
            logger.warning("Anti-dragon bases panel skipped (no channel/data)")
    except Exception as e:
        logger.error(f"Error updating anti-dragon bases panel: {e}")


@tasks.loop(time=dtime(hour=12, minute=0, tzinfo=ZoneInfo("Europe/Berlin")))
@guarded
async def update_base_legend17_panel_daily():
    if updates_paused() or not bot.coc_client:
        return
    try:
        ok = await update_base_legend17_panel_once()
        if ok:
            logger.info("TH17 legend bases panel updated (daily schedule)")
        else:
            logger.warning("TH17 legend bases panel skipped (no channel/data)")
    except Exception as e:
        logger.error(f"Error updating TH17 legend bases panel: {e}")


@tasks.loop(time=dtime(hour=12, minute=0, tzinfo=ZoneInfo("Europe/Berlin")))
@guarded
async def update_base_antidragon17_panel_daily():
    if updates_paused() or not bot.coc_client:
        return
    try:
        ok = await update_base_antidragon17_panel_once()
        if ok:
            logger.info("TH17 anti-dragon bases panel updated (daily schedule)")
        else:
            logger.warning("TH17 anti-dragon bases panel skipped (no channel/data)")
    except Exception as e:
        logger.error(f"Error updating TH17 anti-dragon bases panel: {e}")


async def _wait_until_ready():
    await bot.wait_until_ready()


WAR_LOG = WarLog(STORE, bot, lambda: bot.coc_client, lambda: CLAN_TAG)
CLAN_LOGS = CL.ClanLogs(StateStore(os.path.join(os.path.dirname(STATE_PATH), "clan_logs.json")),
                        bot, lambda: bot.coc_client, lambda: CLAN_TAG)


@tasks.loop(minutes=1)
@guarded
async def update_clan_logs():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    sent = await CLAN_LOGS.tick()
    if sent:
        logger.info("Clan logs: %s new message(s)", sent)


@tasks.loop(minutes=1)
@guarded
async def update_war_log():
    if updates_paused() or not bot.coc_client or not CLAN_TAG:
        return
    sent = await WAR_LOG.tick()
    if sent:
        logger.info("War log: %s new message(s)", sent)


@tasks.loop(seconds=30)
@guarded
async def check_maintenance():
    if not bot.coc_client:
        return
    event = await CLAN_LOGS.check_maintenance()
    if event:
        logger.info("Supercell maintenance %s", event["kind"])


ALL_LOOPS = [
    update_clan_statistics, update_clan_spiele, update_cwl_tabelle, check_war_log, check_cwl_wars,
    update_war_missed_panel, update_war_performance_panel, update_cwl_performance_panel,
    update_cwl_missed_panel, update_activity_donations_panel, update_clan_overview_card_panel,
    update_war_performance_card_panel, update_base_legend_panel_daily, update_base_antidragon_panel_daily,
    update_base_legend17_panel_daily, update_base_antidragon17_panel_daily, update_war_log, update_clan_logs, check_maintenance,
]


@tasks.loop(hours=1)
@guarded
async def cleanup_duplicate_panels():
    """Remove extra copies of a panel left behind by the old reposting bug."""
    state = load_state()
    for key, message_id in list(state["messages"].items()):
        channel = None
        # the message lives in exactly one registered channel; find it by fetching
        for ch_id in {int(v) for v in state["channels"].values()}:
            ch = bot.get_channel(ch_id)
            if ch is None:
                continue
            try:
                await ch.fetch_message(int(message_id))
            except Exception:
                continue
            channel = ch
            break
        if channel is not None:
            await panels.remove_duplicates(channel, key, store=STORE, bot_user_id=bot.user.id)


@cleanup_duplicate_panels.before_loop
async def _cleanup_wait():
    await bot.wait_until_ready()
    await asyncio.sleep(120)


async def reply(interaction: discord.Interaction, text: str, **kwargs):
    """Ephemeral answer whether or not the interaction was deferred."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, CocApiError):
        text = "⚠️ Die Clash-of-Clans-API ist gerade nicht erreichbar (z. B. Wartung). Bitte gleich nochmal versuchen."
    else:
        logger.exception("command %s failed", getattr(interaction.command, "name", "?"), exc_info=original)
        text = "❌ Befehl fehlgeschlagen. Bitte nochmal versuchen."
    try:
        await reply(interaction, text)
    except Exception:
        pass


# Commands
AUTHORIZED_DISCORD_ID = int(os.getenv("OWNER_DISCORD_ID", "0") or 0)  # bot owner (Discord user ID)


def is_authorized(interaction: discord.Interaction) -> bool:
    return interaction.user and interaction.user.id == AUTHORIZED_DISCORD_ID


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="clan-statistiken")
async def clan_statistiken(interaction: discord.Interaction):
    """Zeigt aktuelle Clan-Statistiken an (Embed)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("clan_statistiken", interaction.channel_id)

    clan_data = await bot.coc_client.get_clan(CLAN_TAG)
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan_data["memberList"] = members
    write_cache("clan", clan_data)
    write_cache("members", members)
    cached = read_cache("clan") or clan_data
    embed = format_clan_statistics(cached)
    embed2 = format_top_members(cached)
    await upsert_embed_message(interaction.channel, "clan_statistiken", [embed, embed2])
    await reply(interaction, "✅ Clan-Statistiken aktualisiert.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="kriegsprotokoll")
async def kriegsprotokoll(interaction: discord.Interaction):
    """Zeigt das letzte Kriegsprotokoll (Embed)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("kriegsprotokoll", interaction.channel_id)

    wars = await bot.coc_client.get_clan_war_log(CLAN_TAG)
    if not wars:
        await reply(interaction, "❌ Kein Kriegsprotokoll verfügbar!", ephemeral=True)
        return

    await send_war_log(wars[0], channel=interaction.channel)
    await reply(interaction, "✅ Kriegsprotokoll gesendet!", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="cwl-tabelle")
async def cwl_tabelle(interaction: discord.Interaction):
    """Zeigt die CWL-Tabelle (Embed)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("cwl_tabelle", interaction.channel_id)

    clan_data = await bot.coc_client.get_clan(CLAN_TAG)
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan_data["memberList"] = members
    write_cache("clan", clan_data)
    write_cache("members", members)
    cached = read_cache("clan") or clan_data
    embed = format_cwl_table(cached)
    await upsert_embed_message(interaction.channel, "cwl_tabelle", [embed])
    await reply(interaction, "✅ CWL-Tabelle aktualisiert.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="clan-spiele")
async def clan_spiele(interaction: discord.Interaction):
    """Zeigt den Clan-Spiele Fortschritt (Embed)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("clan_spiele", interaction.channel_id)

    clan_data = await bot.coc_client.get_clan(CLAN_TAG)
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan_data["memberList"] = members
    raid = await bot.coc_client.get_capital_raid_seasons(CLAN_TAG, limit=1)
    write_cache("clan", clan_data)
    write_cache("members", members)
    write_cache("capital_raid", raid)
    cached = read_cache("clan") or clan_data
    raid_cached = read_cache("capital_raid") or raid
    raid_latest = raid_cached[0] if raid_cached else None
    embed = format_clan_games_progress(cached, raid_latest)
    await upsert_embed_message(interaction.channel, "clan_spiele", [embed])
    await reply(interaction, "✅ Clan-Spiele aktualisiert.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="memberliste")
async def memberliste(interaction: discord.Interaction):
    """Erstellt/aktualisiert beide Memberlisten (2 Embeds)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("clan_statistiken", interaction.channel_id)

    clan_data = await bot.coc_client.get_clan(CLAN_TAG)
    members = await bot.coc_client.get_clan_members(CLAN_TAG)
    if members:
        clan_data["memberList"] = members
    write_cache("clan", clan_data)
    write_cache("members", members)
    cached = read_cache("clan") or clan_data

    member_embeds = format_memberlist_embeds(cached)
    await upsert_embed_message(interaction.channel, "memberliste_1", [member_embeds[0]])
    await upsert_embed_message(interaction.channel, "memberliste_2", [member_embeds[1]])

    await reply(interaction, "✅ Memberlisten aktualisiert.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="standorte")
async def standorte(interaction: discord.Interaction):
    """Listet Standorte (Locations)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    locations = await bot.coc_client.get_locations()
    write_cache("locations", locations)
    cached = read_cache("locations") or locations

    embed = discord.Embed(title="🌍 Standorte", color=discord.Color.teal())
    lines = [f"{loc.get('id')} — {loc.get('name')}" for loc in cached[:20]]
    embed.description = "\n".join(lines) if lines else "Keine Daten."
    add_timestamp(embed)
    await upsert_command_embed(interaction, "standorte", embed, "✅ Standorte aktualisiert.")


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="ranking-clans")
@app_commands.describe(location_id="Location ID", limit="Anzahl (max 20)")
async def ranking_clans(interaction: discord.Interaction, location_id: int, limit: int = 10):
    """Zeigt Clan-Rankings für einen Standort."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    limit = max(1, min(20, limit))
    rankings = await bot.coc_client.get_location_clan_rankings(location_id, limit=limit)
    write_cache(f"ranking_clans_{location_id}_{limit}", rankings)
    cached = read_cache(f"ranking_clans_{location_id}_{limit}") or rankings

    embed = discord.Embed(title=f"🏆 Clan-Rankings (Location {location_id})", color=discord.Color.gold())
    lines = []
    for r in cached:
        lines.append(f"#{r.get('rank')} {r.get('name')} — 🏆{r.get('clanPoints', 0)}")
    embed.description = "\n".join(lines) if lines else "Keine Daten."
    add_timestamp(embed)
    await upsert_command_embed(interaction, f"ranking_clans_{location_id}_{limit}", embed, "✅ Clan-Ranking aktualisiert.")


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="ranking-spieler")
@app_commands.describe(location_id="Location ID", limit="Anzahl (max 20)")
async def ranking_spieler(interaction: discord.Interaction, location_id: int, limit: int = 10):
    """Zeigt Spieler-Rankings für einen Standort."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    limit = max(1, min(20, limit))
    rankings = await bot.coc_client.get_location_player_rankings(location_id, limit=limit)
    write_cache(f"ranking_players_{location_id}_{limit}", rankings)
    cached = read_cache(f"ranking_players_{location_id}_{limit}") or rankings

    embed = discord.Embed(title=f"🏅 Spieler-Rankings (Location {location_id})", color=discord.Color.purple())
    lines = []
    for r in cached:
        lines.append(f"#{r.get('rank')} {r.get('name')} — 🏆{r.get('trophies', 0)}")
    embed.description = "\n".join(lines) if lines else "Keine Daten."
    add_timestamp(embed)
    await upsert_command_embed(interaction, f"ranking_spieler_{location_id}_{limit}", embed, "✅ Spieler-Ranking aktualisiert.")


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="goldpass")
async def goldpass(interaction: discord.Interaction):
    """Zeigt aktuelle Goldpass-Season."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    data = await bot.coc_client.get_goldpass_current()
    if not data:
        await reply(interaction, "❌ Keine Goldpass-Daten verfügbar.", ephemeral=True)
        return

    write_cache("goldpass_current", data)
    cached = read_cache("goldpass_current") or data

    embed = discord.Embed(title="✨ Goldpass (aktuell)", color=discord.Color.yellow())
    embed.add_field(name="Start", value=cached.get("startTime", "-"), inline=True)
    embed.add_field(name="Ende", value=cached.get("endTime", "-"), inline=True)
    add_timestamp(embed)
    await upsert_command_embed(interaction, "goldpass_current", embed, "✅ Goldpass aktualisiert.")


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="war-missed")
async def war_missed(interaction: discord.Interaction):
    """Zeigt fehlende Angriffe im aktuellen Krieg."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("war_missed", interaction.channel_id)
    embed = await build_war_missed_embed()
    if not embed:
        await reply(interaction, "⚠️ Kein aktueller Krieg verfügbar (API/Privatsphäre).", ephemeral=True)
        return

    await upsert_embed_message(interaction.channel, "war_missed_panel", [embed])
    await reply(interaction, "✅ War-Missed Panel aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="war-performance")
async def war_performance(interaction: discord.Interaction):
    """Zeigt Performance im aktuellen Krieg."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("war_performance", interaction.channel_id)
    embed = await build_war_performance_embed()
    if not embed:
        await reply(interaction, "⚠️ Kein aktueller Krieg verfügbar (API/Privatsphäre).", ephemeral=True)
        return

    await upsert_embed_message(interaction.channel, "war_performance_panel", [embed])
    await reply(interaction, "✅ War-Performance Panel aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="cwl-performance")
async def cwl_performance(interaction: discord.Interaction):
    """Zeigt CWL-Performance über beendete CWL-Kriege."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("cwl_performance", interaction.channel_id)
    embed = await build_cwl_performance_embed()
    if not embed:
        await reply(interaction, "⚠️ Keine CWL-Daten verfügbar.", ephemeral=True)
        return

    await upsert_embed_message(interaction.channel, "cwl_performance_panel", [embed])
    await reply(interaction, "✅ CWL-Performance Panel aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="cwl-missed")
async def cwl_missed(interaction: discord.Interaction):
    """Zeigt verpasste Angriffe und Miss-Quote in der CWL."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("cwl_missed", interaction.channel_id)
    embed = await build_cwl_missed_embed()
    if not embed:
        await reply(interaction, "⚠️ Keine CWL-Daten verfügbar.", ephemeral=True)
        return

    await upsert_embed_message(interaction.channel, "cwl_missed_panel", [embed])
    await reply(interaction, "✅ CWL-Missed Panel aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="activity-donations")
@app_commands.describe(clan_tag="Clash of Clans Clan-Tag (z.B. #ABC123)")
async def activity_donations(interaction: discord.Interaction, clan_tag: Optional[str] = None):
    """Zeigt Donation-Aktivität der Mitglieder. Optional für ein anderes Clan-Tag."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return
    tag = clan_tag.strip() if clan_tag else None
    if not tag and not CLAN_TAG:
        await reply(interaction, "❌ Kein Clan-Tag verfügbar. Gib eines an oder setze CLAN_TAG in der .env.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("activity_donations", interaction.channel_id)
    if tag and tag != CLAN_TAG:
        set_channel_clan_tag("activity_donations", tag)
    elif not tag and CLAN_TAG:
        set_channel_clan_tag("activity_donations", CLAN_TAG)
    embed = await build_activity_donations_embed(tag)
    if not embed:
        await reply(interaction, "⚠️ Keine Memberdaten verfügbar.", ephemeral=True)
        return

    await upsert_embed_message(interaction.channel, "activity_donations_panel", [embed], create_if_missing=False)
    await reply(interaction, "✅ Donation-Panel aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="card-clan-overview")
async def card_clan_overview(interaction: discord.Interaction):
    """Erstellt/aktualisiert die Clan Overview PNG Card."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("card_clan_overview", interaction.channel_id)
    path = await build_clan_overview_card()
    if not path:
        await reply(interaction, "⚠️ Keine Clandaten verfügbar.", ephemeral=True)
        return
    await upsert_png_message(interaction.channel, "card_clan_overview", path, "🖼️ Clan Overview Card")
    await reply(interaction, "✅ Clan Overview Card aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="card-war-performance")
async def card_war_performance(interaction: discord.Interaction):
    """Erstellt/aktualisiert die War Performance PNG Card."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("card_war_performance", interaction.channel_id)
    path = await build_war_performance_card()
    if not path:
        await reply(interaction, "⚠️ Kein aktueller Krieg verfügbar (API/Privatsphäre).", ephemeral=True)
        return
    await upsert_png_message(interaction.channel, "card_war_performance", path, "🖼️ War Performance Card")
    await reply(interaction, "✅ War Performance Card aktualisiert (Auto-Update: 5 Min).", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="baselegend")
async def baselegend(interaction: discord.Interaction):
    """Zeigt die Top 4 TH18 Legend League Bases (mit Bild + OpenLayout-Link)."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("base_legend", interaction.channel_id)
    try:
        ok = await update_base_legend_panel_once()
        if not ok:
            await reply(interaction, "⚠️ Konnte keine Legend-Bases laden.", ephemeral=True)
            return
        await reply(interaction, 
            "✅ Top 4 Legend-Bases gepostet. Auto-Update läuft täglich um 12:00 (Europe/Berlin).",
            ephemeral=True,
        )
    except Exception as e:
        await reply(interaction, f"❌ Fehler beim Laden der Legend-Bases: {e}", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="baseantidragon")
async def baseantidragon(interaction: discord.Interaction):
    """Zeigt die Top 4 TH18 Anti Air Dragon/E-Dragon Bases."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("base_antidragon", interaction.channel_id)
    try:
        ok = await update_base_antidragon_panel_once()
        if not ok:
            await reply(interaction, "⚠️ Konnte keine Anti-Dragon-Bases laden.", ephemeral=True)
            return
        await reply(interaction, 
            "✅ Top 4 Anti-Dragon-Bases gepostet. Auto-Update läuft täglich um 12:00 (Europe/Berlin).",
            ephemeral=True,
        )
    except Exception as e:
        await reply(interaction, f"❌ Fehler beim Laden der Anti-Dragon-Bases: {e}", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="baselegend17")
async def baselegend17(interaction: discord.Interaction):
    """Zeigt die Top 4 TH17 Legend League Bases."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("base_legend17", interaction.channel_id)
    try:
        ok = await update_base_legend17_panel_once()
        if not ok:
            await reply(interaction, "⚠️ Konnte keine TH17 Legend-Bases laden.", ephemeral=True)
            return
        await reply(interaction, 
            "✅ Top 4 TH17 Legend-Bases gepostet. Auto-Update läuft täglich um 12:00 (Europe/Berlin).",
            ephemeral=True,
        )
    except Exception as e:
        await reply(interaction, f"❌ Fehler beim Laden der TH17 Legend-Bases: {e}", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="baseantidragon17")
async def baseantidragon17(interaction: discord.Interaction):
    """Zeigt die Top 4 TH17 Anti Air Dragon/E-Dragon Bases."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    if not bot.coc_client:
        await reply(interaction, "❌ Bot nicht bereit.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    set_channel("base_antidragon17", interaction.channel_id)
    try:
        ok = await update_base_antidragon17_panel_once()
        if not ok:
            await reply(interaction, "⚠️ Konnte keine TH17 Anti-Dragon-Bases laden.", ephemeral=True)
            return
        await reply(interaction, 
            "✅ Top 4 TH17 Anti-Dragon-Bases gepostet. Auto-Update läuft täglich um 12:00 (Europe/Berlin).",
            ephemeral=True,
        )
    except Exception as e:
        await reply(interaction, f"❌ Fehler beim Laden der TH17 Anti-Dragon-Bases: {e}", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="warlog")
@app_commands.describe(channel="Channel für den Live-Kriegslog (Standard: dieser Channel)", aus="Log in diesem Channel ausschalten")
async def warlog_command(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, aus: bool = False):
    """Live-Kriegslog: jeder Angriff und jede Verteidigung, Matchup, Start und Ende."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.")
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!")
        return
    target = channel or interaction.channel
    if aus:
        removed = WAR_LOG.disable(target.id)
        await reply(interaction, f"⏹️ Kriegslog in {target.mention} ausgeschaltet." if removed else f"In {target.mention} ist kein Kriegslog aktiv.")
        return
    perms = target.permissions_for(target.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.use_external_emojis):
        await reply(interaction, f"❌ Mir fehlen Rechte in {target.mention} (Nachrichten senden, Links einbetten, externe Emojis).")
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    added = await WAR_LOG.enable(target.id)
    if not added:
        await reply(interaction, f"ℹ️ Kriegslog läuft in {target.mention} bereits.")
        return
    await reply(interaction, f"✅ Kriegslog in {target.mention} aktiv. Ab jetzt kommt jeder neue Angriff live (Prüfung jede Minute); "
                             "laufende Angriffe werden nicht nachgepostet.")


def _clanlog_overview() -> str:
    channels = {cid: list(types) for cid, types in CLAN_LOGS.overview().items()}
    for cid in WAR_LOG.config()["channels"]:
        channels.setdefault(int(cid), []).insert(0, "war")
    if not channels:
        return "Noch keine Logs aktiv."
    lines = []
    for cid, types in sorted(channels.items()):
        names = ", ".join(CL.LABELS.get(t, t) for t in types)
        lines.append(f"<#{cid}>: {names}")
    return "\n".join(lines)


class ClanLogSelect(discord.ui.Select):
    def __init__(self, channel: discord.TextChannel, owner_id: int):
        active = set(CLAN_LOGS.channel_types(channel.id))
        if channel.id in WAR_LOG.config()["channels"]:
            active.add("war")
        options = [discord.SelectOption(label=CL.LABELS[t], value=t, default=t in active)
                   for t in ["war"] + [t for t in CL.LOG_TYPES if t != "war"]]
        super().__init__(placeholder="Logs für diesen Channel auswählen", min_values=0,
                         max_values=len(options), options=options)
        self.channel, self.owner_id = channel, owner_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id or not is_authorized(interaction):
            await reply(interaction, "❌ Du bist nicht berechtigt.")
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        chosen = set(self.values)
        CLAN_LOGS.set_channel_types(self.channel.id, sorted(chosen - {"war"}))
        if "war" in chosen:
            await WAR_LOG.enable(self.channel.id)
        else:
            WAR_LOG.disable(self.channel.id)
        order = ["war"] + [t for t in CL.LOG_TYPES if t != "war"]
        picked = ", ".join(CL.LABELS[t] for t in order if t in chosen) or "keine"
        await interaction.edit_original_response(
            content=f"✅ {self.channel.mention}: **{picked}**\nNeue Ereignisse kommen ab jetzt (Upgrades und Namen werden alle 5 Minuten geprüft, "
                    f"der Rest jede Minute). Alte Ereignisse werden nicht nachgepostet.\n\n**Alle aktiven Logs**\n{_clanlog_overview()}",
            view=None)


class ClanLogView(discord.ui.View):
    def __init__(self, channel: discord.TextChannel, owner_id: int):
        super().__init__(timeout=300)
        self.add_item(ClanLogSelect(channel, owner_id))


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="clanlog")
@app_commands.describe(channel="Channel, für den du die Logs einstellst (Standard: dieser Channel)")
async def clanlog_command(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    """Clan-Logs pro Channel: Krieg, Beitritte, Spenden, Upgrades, Namen, Rollen, Liga, Wartung."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.")
        return
    if not bot.coc_client or not CLAN_TAG:
        await reply(interaction, "❌ Clan nicht konfiguriert!")
        return
    target = channel or interaction.channel
    perms = target.permissions_for(target.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.use_external_emojis):
        await reply(interaction, f"❌ Mir fehlen Rechte in {target.mention} (Nachrichten senden, Links einbetten, externe Emojis).")
        return
    await reply(interaction, f"Welche Logs sollen in {target.mention} laufen? Mehrfachauswahl möglich, alles abwählen schaltet den Channel aus."
                             f"\n\n**Aktuell aktiv**\n{_clanlog_overview()}",
                view=ClanLogView(target, interaction.user.id))


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="pause-updates")
async def pause_updates(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    set_updates_paused(True)
    await reply(interaction, "⏸️ Auto-Updates pausiert.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="resume-updates")
async def resume_updates(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    set_updates_paused(False)
    await reply(interaction, "▶️ Auto-Updates fortgesetzt.", ephemeral=True)


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="status")
async def bot_status(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    st = load_state()
    channels = st.get("channels", {})
    embed = discord.Embed(title="🤖 Bot-Status", color=discord.Color.blurple())
    embed.add_field(name="Updates", value="Pausiert" if updates_paused() else "Aktiv", inline=True)
    embed.add_field(name="Gespeicherte Channels", value=str(len(channels)), inline=True)
    embed.add_field(name="War-Posts", value=str(len(st.get("posted_wars", []))), inline=True)
    embed.add_field(name="CWL-Posts", value=str(len(st.get("posted_cwl_wars", []))), inline=True)
    add_timestamp(embed)
    await upsert_command_embed(interaction, "bot_status", embed, "✅ Status aktualisiert.")


@bot.tree.command(guild=discord.Object(id=GUILD_ID), name="reset-runtime")
async def reset_runtime(interaction: discord.Interaction):
    """Leert Cache-Dateien und entfernt alle registrierten Channel-Zuordnungen."""
    if not is_authorized(interaction):
        await reply(interaction, "❌ Du bist nicht berechtigt.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    removed_cache = clear_cache_dir()
    clear_registered_channels()
    await reply(interaction, 
        f"🧹 Runtime zurückgesetzt: {removed_cache} Cache-Dateien gelöscht, Channel-Registrierungen entfernt.",
        ephemeral=True,
    )


# Run bot
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
