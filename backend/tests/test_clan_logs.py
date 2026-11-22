import copy
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")

import clan_logs as CL  # noqa: E402
import clan_log_render as R  # noqa: E402
from state_store import StateStore  # noqa: E402

CLAN = "#2L28VGUR2"


def member(tag, name="P", role="member", th=17, don=0, rec=0):
    return {"tag": tag, "name": name, "role": role, "townHallLevel": th, "donations": don, "donationsReceived": rec}


def player(tag, name="P", th=17, barb=10, league="Legend League III", role="member", extra=()):
    return {"tag": tag, "name": name, "townHallLevel": th, "role": role, "leagueTier": {"name": league},
            "troops": [{"name": "Barbarian", "level": barb, "village": "home"}, *extra],
            "heroes": [{"name": "Barbarian King", "level": 90, "village": "home"}],
            "spells": [], "heroEquipment": [{"name": "Giant Gauntlet", "level": 20, "village": "home"}]}


class Client:
    def __init__(self):
        self.members = [member("#A", "Alice", don=10), member("#B", "Bob")]
        self.players = {"#A": player("#A", "Alice"), "#B": player("#B", "Bob")}
        self.fail_players = set()
        self.clan_down = False

    async def get_clan(self, tag):
        if self.clan_down:
            raise RuntimeError("503")
        return {"tag": CLAN, "name": "Clankrieger 2.0", "members": len(self.members),
                "memberList": copy.deepcopy(self.members), "badgeUrls": {"medium": "https://x/b.png"}}

    async def get_player(self, tag):
        if tag in self.fail_players:
            raise RuntimeError("timeout")
        return copy.deepcopy(self.players.get(tag, {}))


def make(tmp_path, client):
    now = [1000.0]
    sent = []
    fail = set()

    async def sender(channel, rendered, key):
        if channel.id in fail:
            raise RuntimeError("discord down")
        sent.append((channel.id, rendered, key))

    bot = SimpleNamespace(get_channel=lambda cid: SimpleNamespace(id=cid))
    logs = CL.ClanLogs(StateStore(str(tmp_path / "cl.json")), bot, lambda: client, lambda: CLAN,
                       sender=sender, clock=lambda: now[0])
    return logs, sent, now, fail


def texts(sent):
    out = []
    for _, r, _ in sent:
        if "content" in r:
            out.append(r["content"])
        else:
            e = r["embed"]
            out.append(" ".join([e.title or "", e.description or ""] + [f.name + " " + f.value for f in e.fields]))
    return out


@pytest.mark.asyncio
async def test_baseline_posts_nothing_then_each_kind_of_event_posts_once(tmp_path):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, list(CL.LOG_TYPES))
    assert await logs.tick() == 0 and sent == []
    # one minute later: donation, join, leave
    now[0] += 60
    c.members = [member("#A", "Alice", don=25, rec=3), member("#C", "Cara", role="admin", th=19)]
    c.players["#C"] = player("#C", "Cara", th=19)
    assert await logs.tick() == 3
    t = texts(sent)
    assert any("Member Joined" in x and "<:th19:" in x and "Elder" in x for x in t)
    assert any("Member Left" in x and "Bob" in x for x in t)
    assert any("Donated" in x and "`15`" in x and "<:donation_up:" in x for x in t)
    # five minutes later: upgrades + changes (Cara is new -> baseline only)
    now[0] += 300
    c.members = [member("#A", "Alice2", don=25, rec=3), member("#C", "Cara", role="admin", th=19)]
    c.players["#A"] = player("#A", "Alice2", th=18, barb=11, league="Legend League II", role="coLeader",
                             extra=[{"name": "Druid", "level": 1, "village": "home"}])
    sent.clear()
    await logs.tick()
    t = texts(sent)
    assert any("upgraded to <:th18:" in x and "Townhall 18" in x for x in t)
    assert any("leveled up <:tr_barbarian:" in x and "to lv11" in x for x in t)
    assert any("unlocked" in x and "Druid" in x for x in t)
    assert any("Player Name Changed" in x and "**Alice** → **Alice2**" in x for x in t)
    assert any("Player Role Changed" in x and "**Member** → **Co-Leader**" in x for x in t)
    assert any("Player League Changed" in x and "Legend League II" in x for x in t)
    assert not any("Cara" in x for x in t)
    n = len(sent)
    now[0] += 60
    assert await logs.tick() == 0 and len(sent) == n


@pytest.mark.asyncio
async def test_channels_only_get_the_types_they_selected(tmp_path):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, ["donations"])
    logs.set_channel_types(6, ["member_join", "war"])  # war is handled by /warlog, ignored here
    assert logs.channel_types(6) == ["member_join"]
    await logs.tick()
    now[0] += 60
    c.members = [member("#A", "Alice", don=11), member("#B", "Bob"), member("#D", "Dan")]
    await logs.tick()
    assert sorted((cid, R.route_event({"event_type": "x"}) or "") for cid, _, _ in sent) and \
        {(cid, "Donated" in texts([s])[0]) for s in sent for cid in [s[0]]} == {(5, True), (6, False)}
    logs.set_channel_types(5, [])
    assert 5 not in logs.overview()


@pytest.mark.asyncio
async def test_api_failures_never_create_fake_events(tmp_path):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, list(CL.LOG_TYPES))
    await logs.tick()
    now[0] += 300
    c.clan_down = True
    assert await logs.tick() == 0
    c.clan_down = False
    c.fail_players = {"#A"}
    c.players["#A"] = player("#A", "Alice", barb=12)
    assert await logs.tick() == 0  # failed player keeps its old snapshot
    c.fail_players = set()
    now[0] += 300
    await logs.tick()
    assert [x for x in texts(sent) if "to lv12" in x]
    # an empty member list for a clan with members is ignored, not "everyone left"
    async def empty(tag):
        return {"tag": CLAN, "members": 2, "memberList": []}
    c.get_clan = empty
    sent.clear()
    now[0] += 60
    assert await logs.tick() == 0 and sent == []


@pytest.mark.asyncio
async def test_failed_send_is_retried_and_expires_after_two_hours(tmp_path):
    c = Client()
    logs, sent, now, fail = make(tmp_path, c)
    logs.set_channel_types(5, ["member_join"])
    logs.set_channel_types(6, ["member_join"])
    await logs.tick()
    fail.add(6)
    now[0] += 60
    c.members.append(member("#E", "Eve"))
    assert await logs.tick() == 1 and sent[0][0] == 5
    fail.clear()
    now[0] += 60
    assert await logs.tick() == 1 and sent[1][0] == 6 and sent[0][2] == sent[1][2]
    fail.add(5)
    now[0] += 60
    c.members.append(member("#F", "Fay"))
    await logs.tick()
    now[0] += CL.EVENT_TTL + 1
    fail.clear()
    before = len(sent)
    await logs.tick()
    assert len(sent) == before and logs.data()["outbox"] == []


@pytest.mark.asyncio
async def test_state_survives_restart_without_reposting(tmp_path):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, list(CL.LOG_TYPES))
    await logs.tick()
    now[0] += 60
    c.members.append(member("#G", "Gus"))
    await logs.tick()
    again, sent2, now2, _ = make(tmp_path, c)
    now2[0] = now[0] + 60
    assert await again.tick() == 0 and sent2 == []


@pytest.mark.asyncio
async def test_disabling_everything_resets_the_baseline(tmp_path):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, ["member_join"])
    await logs.tick()
    logs.set_channel_types(5, [])
    await logs.tick()
    c.members.append(member("#H", "Hal"))
    logs.set_channel_types(5, ["member_join"])
    now[0] += 60
    assert await logs.tick() == 0  # fresh baseline, no backlog


def test_rendering_escapes_names_and_never_pings():
    e = {"event_type": "player_change", "player_tag": "#A",
         "payload": {"player": {"tag": "#A", "name": "@everyone *x*"}, "field": "name",
                     "old_value": "a", "new_value": "b"}}
    out = R.render_event(e)["embed"].description
    assert "@everyone" not in out.replace("@\u200beveryone", "") and "\\*x\\*" in out


import maintenance as M  # noqa: E402


def test_maintenance_rules_match_the_data_service():
    st = {"in_maintenance": False, "started_at": None}
    m = M.MaintenanceMonitor(lambda: st, lambda: None)
    assert m.observe(M.MAINTENANCE, 100) is None
    assert m.observe(M.UNKNOWN, 130) is None          # timeout resets the streak
    assert m.observe(M.MAINTENANCE, 160) is None
    ev = m.observe(M.MAINTENANCE, 190)
    assert ev["kind"] == "started" and ev["started_at"] == 160
    assert m.observe(M.MAINTENANCE, 220) is None      # no repeat
    assert m.observe(M.OK, 2200) is None
    ev = m.observe(M.OK, 2230)
    assert ev["kind"] == "ended" and ev["duration_seconds"] == 2040
    assert M.render("ended", 2040) == "✅ **The maintenance break is ending soon!** (Started 34m ago)"
    other = M.MaintenanceMonitor(lambda: {"in_maintenance": False}, lambda: None)
    assert [other.observe(M.UNAVAILABLE, t) for t in (1, 2, 3)] == [None] * 3 and other.observe(M.UNAVAILABLE, 4)


@pytest.mark.asyncio
async def test_maintenance_posts_only_to_maintenance_channels_once(tmp_path, monkeypatch):
    c = Client()
    logs, sent, now, _ = make(tmp_path, c)
    logs.set_channel_types(5, ["maintenance"])
    logs.set_channel_types(6, ["donations"])
    results = iter([M.MAINTENANCE, M.MAINTENANCE, M.MAINTENANCE, M.OK, M.OK])

    async def probe(client, timeout=10.0):
        return next(results)

    monkeypatch.setattr(M, "probe", probe)
    for _ in range(5):
        now[0] += 30
        await logs.check_maintenance()
    assert [cid for cid, _, _ in sent] == [5, 5]
    assert "has started" in sent[0][1]["content"] and "ending soon" in sent[1][1]["content"]
    # restart during maintenance keeps the start (state is on disk)
    assert logs.data()["maintenance"]["in_maintenance"] is False



class LevelClient(Client):
    def __init__(self, level):
        super().__init__()
        self.level = level

    async def get_clan(self, tag):
        data = await super().get_clan(tag)
        data["clanLevel"] = self.level
        data["badgeUrls"] = {"large": "https://api-assets.clashofclans.com/badges/512/AbC_d-1.png"}
        return data


def level_logs(tmp_path, level):
    c = LevelClient(level)
    logs, sent, now, fail = make(tmp_path, c)
    logs.fetched = []

    async def badge(url):
        logs.fetched.append(url)
        return None
    logs.badge_fetcher = badge
    return c, logs, sent, now


@pytest.mark.asyncio
async def test_clan_level_up_posts_one_card_per_level_and_a_dip_never_reposts(tmp_path):
    c, logs, sent, now = level_logs(tmp_path, 9)
    logs.set_channel_types(7, ["clan_level"])
    await logs.tick()
    assert sent == []                                                    # first look is the baseline
    for level in (10, 9, 10, 10, 12):
        now[0] += 60
        c.level = level
        await logs.tick()
    assert [r["file"].filename for _, r, _ in sent] == ["clan-level-10.png", "clan-level-12.png"]
    assert [k for _, _, k in sent] == [f"clan_level|{CLAN}|10", f"clan_level|{CLAN}|12"]
    _, first, _ = sent[0]
    assert first["content"].startswith("## Clankrieger 2.0 Reaches Clan Level 10- ")
    assert "<@" not in first["content"]
    assert logs.fetched == ["https://api-assets.clashofclans.com/badges/512/AbC_d-1.png"] * 2


@pytest.mark.asyncio
async def test_clan_level_only_for_channels_that_chose_it_and_baseline_resets_when_disabled(tmp_path):
    c, logs, sent, now = level_logs(tmp_path, 5)
    logs.set_channel_types(7, ["member_join"])
    await logs.tick()
    now[0] += 60
    c.level = 6
    await logs.tick()
    assert sent == []
    logs.set_channel_types(7, [])
    await logs.tick()
    assert logs.data()["clan_level"] is None
    c.level = 8
    logs.set_channel_types(7, ["clan_level"])
    await logs.tick()
    assert sent == []                                                    # re-enabling never posts an old jump


@pytest.mark.asyncio
async def test_badge_only_from_the_supercell_cdn(monkeypatch):
    import aiohttp
    opened = []

    class Session:
        def __init__(self, *a, **k):
            opened.append(1)
            raise RuntimeError("no network in tests")
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    for url in ("https://evil.example/badges/512/a.png", "http://api-assets.clashofclans.com/badges/512/a.png",
                "https://api-assets.clashofclans.com/badges/999/a.png", ""):
        assert await CL.fetch_badge(url) is None
    assert opened == []                                                  # refused before any request
    assert await CL.fetch_badge("https://api-assets.clashofclans.com/badges/512/AbC_d-1.png") is None
    assert opened == [1]


def test_clan_level_type_is_selectable_with_a_label():
    assert "clan_level" in CL.LOG_TYPES and CL.LABELS["clan_level"]


@pytest.mark.asyncio
async def test_send_rendered_uploads_the_png_with_nonce_and_no_mentions():
    import json
    seen = {}

    class HTTP:
        async def send_message(self, channel_id, *, params):
            seen["body"] = json.loads(params.multipart[0]["value"])
            seen["files"] = [part["filename"] for part in params.multipart[1:]]
            return {"id": "1"}

    state = SimpleNamespace(http=HTTP(), allowed_mentions=None, create_message=lambda channel, data: data)
    channel = SimpleNamespace(id=9, _state=state)
    rendered = await CL.render_level({"payload": {"clan": {"name": "X", "tag": CLAN}, "level": {"old": 4, "new": 5}},
                                      "created": 1792929600.0}, badge_fetcher=lambda url: _none())
    await CL.send_rendered(channel, rendered, "clan_level|x|5")
    assert seen["files"] == ["clan-level-5.png"]
    assert seen["body"]["enforce_nonce"] is True and seen["body"]["allowed_mentions"]["parse"] == []
    assert seen["body"]["content"] == "## X Reaches Clan Level 5- 10/25/2026"


async def _none():
    return None
