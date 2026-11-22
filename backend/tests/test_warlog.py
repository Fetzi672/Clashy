import copy
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")

import warlog  # noqa: E402
from state_store import StateStore  # noqa: E402

OWN = "#OWN"


def member(tag, name, th, pos, attacks=()):
    return {"tag": tag, "name": name, "townhallLevel": th, "mapPosition": pos, "attacks": list(attacks)}


def attack(order, a, d, stars, pct):
    return {"order": order, "attackerTag": a, "defenderTag": d, "stars": stars, "destructionPercentage": pct}


def war(state="inWar", ours=(), theirs=(), prep="20261120T100000.000Z"):
    return {"state": state, "teamSize": 2, "attacksPerMember": 2, "preparationStartTime": prep,
            "clan": {"tag": OWN, "name": "Us", "stars": 3, "destructionPercentage": 80,
                     "members": [member("#A", "Alice", 19, 1, ours), member("#B", "Bob", 17, 2)]},
            "opponent": {"tag": "#OPP", "name": "Them", "stars": 1, "destructionPercentage": 40,
                         "members": [member("#X", "Xeno", 18, 1, theirs), member("#Y", "Yan", 16, 2)]}}


class Client:
    def __init__(self, current=None, group=None, cwl=None):
        self.current, self.group, self.cwl = current, group, cwl or {}

    async def get_current_war(self, tag):
        return copy.deepcopy(self.current)

    async def get_cwl_league_group(self, tag):
        return self.group

    async def get_cwl_war(self, tag):
        return copy.deepcopy(self.cwl.get(tag))


def setup(tmp_path, client, *, fail=None):
    store = StateStore(str(tmp_path / "s.json"))
    sent = []

    async def sender(channel, *, content=None, embed=None, key):
        if fail and fail(channel, key):
            raise RuntimeError("discord down")
        sent.append((channel.id, content, embed.title if embed else None, key))

    bot = SimpleNamespace(get_channel=lambda cid: SimpleNamespace(id=cid))
    log = warlog.WarLog(store, bot, lambda: client, lambda: OWN, sender=sender)
    return log, sent, store


def test_attack_line_matches_the_sbj_protocol_format():
    w = war(ours=[attack(1, "#A", "#Y", 2, 87.4)])
    line = warlog.render_attack(warlog.attacks(w)[0])
    assert line.startswith("⚔️ <:th19:") and "**Alice¹**" in line and "**87%**" in line
    assert line.count("war_star") == 2 and line.count("no_star") == 1 and line.endswith("²")
    d = warlog.render_attack(warlog.attacks(war(theirs=[attack(2, "#X", "#A", 3, 100)]))[0])
    assert d.startswith("🛡️ <:th18:") and "**Xeno¹**" in d


@pytest.mark.asyncio
async def test_enable_does_not_dump_the_running_war_then_new_attacks_post_once(tmp_path):
    client = Client(current=war(ours=[attack(1, "#A", "#Y", 3, 100)]))
    log, sent, _ = setup(tmp_path, client)
    assert await log.enable(5) is True and await log.enable(5) is False
    assert await log.tick() == 0 and sent == []
    client.current = war(ours=[attack(1, "#A", "#Y", 3, 100)], theirs=[attack(2, "#X", "#A", 1, 55)])
    assert await log.tick() == 1 and sent[0][1].startswith("🛡️")
    assert await log.tick() == 0


@pytest.mark.asyncio
async def test_full_war_lifecycle_and_multiple_channels(tmp_path):
    client = Client(current={"state": "notInWar"})
    log, sent, store = setup(tmp_path, client)
    await log.enable(5)
    await log.enable(6)
    client.current = war("preparation")
    assert await log.tick() == 2 and {s[2] for s in sent} == {"War Matchup"}
    client.current = war("inWar", ours=[attack(1, "#A", "#Y", 2, 70)])
    assert await log.tick() == 4  # start embed + attack, in both channels
    client.current = war("warEnded", ours=[attack(1, "#A", "#Y", 2, 70)])
    assert await log.tick() == 2 and sent[-1][2] == "War Ended"
    assert await log.tick() == 0
    # restart: state on disk prevents reposting
    log2, sent2, _ = setup(tmp_path, client)
    log2.store = StateStore(store.path)
    assert await log2.tick() == 0


@pytest.mark.asyncio
async def test_failed_post_is_retried_and_others_continue(tmp_path):
    client = Client(current=war("inWar"))
    down = {6}
    log, sent, _ = setup(tmp_path, client, fail=lambda ch, key: ch.id in down)
    await log.enable(5)
    await log.enable(6)
    client.current = war("inWar", ours=[attack(1, "#A", "#Y", 1, 30)])
    assert await log.tick() == 1 and sent[0][0] == 5
    down.clear()
    assert await log.tick() == 1 and sent[1][0] == 6


@pytest.mark.asyncio
async def test_cwl_uses_the_round_in_progress_and_swaps_sides(tmp_path):
    fought = war("inWar", ours=[attack(1, "#A", "#Y", 3, 100)], prep="P1")
    fought["clan"], fought["opponent"] = fought["opponent"], fought["clan"]  # we are listed as opponent
    group = {"state": "inWar", "rounds": [{"warTags": ["#W1", "#W2"]}, {"warTags": ["#W3"]}, {"warTags": ["#0"]}]}
    client = Client(current={"state": "notInWar"}, group=group,
                    cwl={"#W1": {"clan": {"tag": "#Z"}, "opponent": {"tag": "#Q"}, "state": "inWar"},
                         "#W2": fought, "#W3": war("preparation", prep="P2")})
    found = await warlog.find_current_wars(client, OWN)
    assert [(w["state"], cwl) for w, cwl in found] == [("inWar", True), ("preparation", True)]
    assert found[0][0]["clan"]["tag"] == OWN
    log, sent, _ = setup(tmp_path, client)
    await log.enable(5)
    assert await log.tick() == 0


@pytest.mark.asyncio
async def test_unseen_ended_war_is_not_posted(tmp_path):
    client = Client(current=war("warEnded", prep="OLD"))
    log, sent, _ = setup(tmp_path, client)
    log.config()["channels"].append(5)
    assert await log.tick() == 0 and sent == []


@pytest.mark.asyncio
async def test_no_channels_means_no_api_calls(tmp_path):
    class Boom(Client):
        async def get_current_war(self, tag):
            raise AssertionError("should not be called")

    log, _, _ = setup(tmp_path, Boom())
    assert await log.tick() == 0
