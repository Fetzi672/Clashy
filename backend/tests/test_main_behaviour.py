import asyncio
import os
import re

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("CLAN_TAG", "#TEST")
os.environ.setdefault("ALLOWED_GUILD_ID", "1")

import emojis as E  # noqa: E402
import main  # noqa: E402  (does not start the bot on import)


def test_emoji_database_covers_town_halls_units_and_symbols():
    count = E.load()
    assert count >= 180
    for level in range(1, 20):
        assert re.fullmatch(r"<:th%d:\d+>" % level, E.th(level))
    for name in ("Barbarian", "Electro Dragon", "Revenge Deck", "Heroic Spell", "Barbarian King", "Minion Squad"):
        assert re.fullmatch(r"<:\w+:\d+>", E.unit(name)), name
    assert E.STAR().startswith("<:war_star:") and E.SWORD().startswith("<:war_sword:")
    assert E.th(99) == "TH99" and E.th(None) == "🏰" and E.unit("Nope", "-") == "-"
    assert E.stars(2).count("war_star") == 2 and E.stars(2).count("no_star") == 1


def test_owner_scoped_registry_emojis_are_not_copied():
    assert E.emoji("war_attack") == "" and E.emoji("war_defense") == ""


def test_memberlist_shows_town_hall_emojis():
    clan = {"name": "C", "memberList": [{"name": "A", "role": "leader", "trophies": 5, "townHallLevel": 19}]}
    embeds = main.format_memberlist_embeds(clan)
    assert "<:th19:" in embeds[0].description


@pytest.mark.asyncio
async def test_guarded_loop_body_never_raises():
    calls = []

    @main.guarded
    async def boom():
        calls.append(1)
        raise RuntimeError("x")

    @main.guarded
    async def api_down():
        raise main.CocApiError("maintenance")

    await boom()
    await api_down()
    assert calls == [1]


@pytest.mark.asyncio
async def test_guarded_loop_body_is_time_limited(monkeypatch):
    monkeypatch.setattr(main, "LOOP_TIMEOUT_SECONDS", 0.05)

    @main.guarded
    async def hang():
        await asyncio.Event().wait()

    await asyncio.wait_for(hang(), timeout=1)


def test_every_background_loop_is_guarded_and_registered():
    assert len(main.ALL_LOOPS) == 19
    for loop in main.ALL_LOOPS:
        assert getattr(loop.coro, "__wrapped__", None) is not None, loop.coro.__name__


@pytest.mark.asyncio
async def test_empty_clan_data_raises_instead_of_rendering_unknown(monkeypatch):
    class Client:
        async def get_clan(self, tag):
            return {}

        async def get_clan_members(self, tag):
            return []

    monkeypatch.setattr(main.bot, "coc_client", Client())
    with pytest.raises(main.CocApiError):
        await main.fetch_clan_with_members()

    class NoMembers(Client):
        async def get_clan(self, tag):
            return {"tag": "#A", "members": 30}

    monkeypatch.setattr(main.bot, "coc_client", NoMembers())
    with pytest.raises(main.CocApiError):
        await main.fetch_clan_with_members()
