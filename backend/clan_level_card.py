"""Offline PNG card for a clan level-up, in the spirit of the in-game banner.

The clan badge carries a hexagon with the new level, the headline is white with a dark outline and the newly
unlocked perks are listed in green. Perks exist only up to level 10 (Clash of Clans wiki, Clan Perks, checked
2026-11-22); from level 11 on the card shows the headline alone. The font is Lilita One (SIL OFL 1.1, bundled
in assets/fonts), never Supercell's own typeface. No network access: the caller passes the badge bytes.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "LilitaOne-Regular.ttf"

# New perks per level; "Donation limit" improves troops, spells or siege machines separately.
PERKS: dict[int, tuple[str, ...]] = {
    2: ("Troop donation limit: 10", "Treasury extra storage: 10%"),
    3: ("Spell donation limit: 2", "War bonus extra loot: 10%"),
    4: ("Troop donation limit: 15", "Treasury extra storage: 20%"),
    5: ("Spell donation limit: 3", "Donation upgrade: 1 level", "War bonus extra loot: 15%"),
    6: ("Troop donation limit: 25", "Siege machine donation limit: 2", "Treasury extra storage: 30%"),
    7: ("Spell donation limit: 4", "War bonus extra loot: 20%"),
    8: ("Troop donation limit: 40", "Treasury extra storage: 40%"),
    9: ("Troop donation limit: 55", "War bonus extra loot: 25%"),
    10: ("Donation upgrade: 2 levels", "Treasury extra storage: 50%"),
}

WIDTH = 1280
BACKGROUND = (58, 56, 56)
WHITE = (255, 255, 255)
GREEN = (190, 236, 64)
OUTLINE = (24, 22, 22)
GOLD = (232, 176, 52)
HEX_FILL = (40, 34, 30)


def perks_for(level: int) -> tuple[str, ...]:
    return PERKS.get(int(level), ())


def headline(level: int) -> str:
    level = int(level)
    return f"Clan has reached level {level}! New perks unlocked:" if perks_for(level) else f"Clan has reached level {level}!"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _hexagon(center: tuple[float, float], radius: float) -> list[tuple[float, float]]:
    cx, cy = center
    return [(cx + radius * math.cos(math.radians(90 + 60 * i)), cy + radius * math.sin(math.radians(90 + 60 * i)))
            for i in range(6)]


def _sparkle(draw: ImageDraw.ImageDraw, centre: tuple[float, float], r: float) -> None:
    """The four-pointed bullet of the in-game list (the font has no matching glyph)."""
    cx, cy = centre
    points = [(cx, cy - r), (cx + r * .35, cy - r * .35), (cx + r, cy), (cx + r * .35, cy + r * .35),
              (cx, cy + r), (cx - r * .35, cy + r * .35), (cx - r, cy), (cx - r * .35, cy - r * .35)]
    draw.polygon(points, fill=GREEN, outline=OUTLINE, width=3)


def _badge(badge_png: bytes | None, size: int) -> Image.Image:
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((size, size), Image.LANCZOS)
            return badge
        except Exception:                                     # a broken download still gives a card
            pass
    shield = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shield)
    draw.rounded_rectangle((size * 0.12, size * 0.08, size * 0.88, size * 0.92), radius=size * 0.18,
                           fill=(52, 86, 160), outline=GOLD, width=max(4, size // 22))
    return shield


def render(level: int, badge_png: bytes | None = None) -> bytes:
    level = int(level)
    perks = perks_for(level)
    pad, badge_size = 56, 250
    title_font, perk_font = _font(64), _font(52)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text_x = pad + badge_size + 40
    title_lines = _wrap(probe, headline(level), title_font, WIDTH - text_x - pad)
    title_h = len(title_lines) * 74
    top = pad + 20                                             # room for the hexagon above the badge
    perks_top = max(top + badge_size, top + title_h) + 6
    height = (perks_top + len(perks) * 70 + pad - 10) if perks else max(top + badge_size, top + title_h) + pad - 16

    card = Image.new("RGBA", (WIDTH, height), BACKGROUND + (255,))
    draw = ImageDraw.Draw(card)
    badge = _badge(badge_png, badge_size)
    bx = pad + (badge_size - badge.width) // 2
    by = top + (badge_size - badge.height) // 2
    card.alpha_composite(badge, (bx, by))

    hex_r = 52
    centre = (pad + badge_size / 2, top + 30)
    draw.polygon(_hexagon(centre, hex_r + 7), fill=GOLD)
    draw.polygon(_hexagon(centre, hex_r), fill=HEX_FILL)
    number = str(level)
    hex_font = _font(58 if len(number) < 3 else 44)
    draw.text(centre, number, font=hex_font, fill=WHITE, anchor="mm", stroke_width=4, stroke_fill=OUTLINE)

    y = top + max(0, (badge_size - title_h) // 2 - 30) if not perks else top + 18
    for line in title_lines:
        draw.text((text_x, y), line, font=title_font, fill=WHITE, stroke_width=5, stroke_fill=OUTLINE)
        y += 74
    y = perks_top
    for perk in perks:
        _sparkle(draw, (pad + 38, y + 30), 13)
        draw.text((pad + 70, y), perk, font=perk_font, fill=GREEN, stroke_width=5, stroke_fill=OUTLINE)
        y += 70

    out = io.BytesIO()
    card.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
