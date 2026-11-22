import asyncio
import json
import os
from types import SimpleNamespace

import aiohttp
import discord
import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("CLAN_TAG", "#TEST")

import panels  # noqa: E402
from coc_api import COCClient, CocApiError  # noqa: E402
from state_store import StateStore, remember_ids  # noqa: E402

BOT = 42


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "boom")


# ---------------------------------------------------------------- state store
def test_state_is_one_shared_object_and_saves_atomically(tmp_path):
    path = str(tmp_path / "state.json")
    store = StateStore(path)
    a, b = store.load(), store.load()
    assert a is b
    a["messages"]["x"] = "1"
    store.save(b)
    b["messages"]["y"] = "2"
    store.save()
    assert json.load(open(path))["messages"] == {"x": "1", "y": "2"}
    assert json.load(open(path + ".bak"))["messages"] == {"x": "1"}
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_corrupt_state_recovers_from_backup_instead_of_starting_empty(tmp_path):
    path = str(tmp_path / "state.json")
    store = StateStore(path)
    store.load()["messages"]["panel"] = "123"
    store.save()
    store.save()  # backup now holds the good copy
    open(path, "w").write('{"messages": {"pan')  # interrupted write
    again = StateStore(path)
    assert again.load()["messages"] == {"panel": "123"}
    assert again.loaded_from.endswith(".bak")


def test_missing_keys_are_filled_and_wrong_types_fixed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"messages": [], "channels": {"a": "1"}}))
    data = StateStore(str(path)).load()
    assert data["messages"] == {} and data["channels"] == {"a": "1"} and data["posted_cwl_wars"] == []


def test_remember_ids_keeps_recording_order_not_alphabetic():
    ids = remember_ids(["#Z", "#A"], ["#M", "#A"], limit=3)
    assert ids == ["#Z", "#M", "#A"]
    assert remember_ids([str(i) for i in range(10)], ["new"], limit=3) == ["8", "9", "new"]


# ---------------------------------------------------------------- panels
class Msg:
    def __init__(self, mid, key=None, author=BOT, channel=None):
        self.id = mid
        self.author = SimpleNamespace(id=author)
        self.content = f"x [key:{key}]" if key else ""
        self.embeds = []
        self.channel = channel
        self.deleted = False

    async def delete(self):
        self.deleted = True


class Channel:
    def __init__(self, messages=(), fetch_error=None, history_error=None):
        self.id = 7
        self.messages = {m.id: m for m in messages}
        self.fetch_error = fetch_error
        self.history_error = history_error
        self.sent = []

    async def fetch_message(self, mid):
        if self.fetch_error:
            raise self.fetch_error
        if mid not in self.messages:
            raise http_error(discord.NotFound, 404)
        return self.messages[mid]

    def history(self, limit=100):
        channel = self

        async def gen():
            if channel.history_error:
                raise channel.history_error
            for m in sorted(channel.messages.values(), key=lambda m: -m.id)[:limit]:
                await asyncio.sleep(0)
                yield m
        return gen()

    async def send_new(self):
        await asyncio.sleep(0.01)
        msg = Msg(1000 + len(self.sent), "k")
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg


def store_with(tmp_path, stored=None):
    store = StateStore(str(tmp_path / "s.json"))
    if stored is not None:
        store.load()["messages"]["k"] = str(stored)
    return store


async def run(channel, store, *, edit_error=None, create=True):
    edited = []

    async def edit(msg):
        if edit_error:
            raise edit_error
        edited.append(msg.id)

    outcome = await panels.upsert(channel, "k", store=store, bot_user_id=BOT, edit=edit, send=channel.send_new,
                                  create_if_missing=create)
    return outcome, edited


@pytest.mark.asyncio
async def test_stored_message_is_edited_in_place(tmp_path):
    ch = Channel([Msg(5, "k")])
    outcome, edited = await run(ch, store_with(tmp_path, 5))
    assert outcome == panels.EDITED and edited == [5] and ch.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [http_error(discord.HTTPException, 503), http_error(discord.Forbidden, 403),
                                   asyncio.TimeoutError(), aiohttp.ClientConnectionError()])
async def test_temporary_fetch_errors_never_post_a_new_message(tmp_path, error):
    ch = Channel([Msg(5, "k")], fetch_error=error)
    store = store_with(tmp_path, 5)
    outcome, _ = await run(ch, store)
    assert outcome == panels.SKIPPED and ch.sent == [] and store.load()["messages"]["k"] == "5"


@pytest.mark.asyncio
async def test_failed_edit_is_retried_later_not_reposted(tmp_path):
    ch = Channel([Msg(5, "k")])
    outcome, _ = await run(ch, store_with(tmp_path, 5), edit_error=http_error(discord.HTTPException, 429))
    assert outcome == panels.SKIPPED and ch.sent == []


@pytest.mark.asyncio
async def test_deleted_message_is_reattached_to_a_keyed_message_before_posting(tmp_path):
    ch = Channel([Msg(9, "k"), Msg(8, "other")])
    store = store_with(tmp_path, 5)
    outcome, edited = await run(ch, store)
    assert outcome == panels.EDITED and edited == [9] and ch.sent == [] and store.load()["messages"]["k"] == "9"


@pytest.mark.asyncio
async def test_post_only_when_gone_and_no_keyed_message_exists(tmp_path):
    ch = Channel([Msg(8, "other"), Msg(3, "k", author=99)])  # same key by another user does not count
    store = store_with(tmp_path, 5)
    outcome, _ = await run(ch, store)
    assert outcome == panels.CREATED and len(ch.sent) == 1 and store.load()["messages"]["k"] == str(ch.sent[0].id)


@pytest.mark.asyncio
async def test_history_failure_never_posts(tmp_path):
    ch = Channel([], history_error=http_error(discord.HTTPException, 500))
    outcome, _ = await run(ch, store_with(tmp_path, None))
    assert outcome == panels.SKIPPED and ch.sent == []


@pytest.mark.asyncio
async def test_without_create_a_deleted_panel_is_forgotten(tmp_path):
    ch = Channel([])
    store = store_with(tmp_path, 5)
    outcome, _ = await run(ch, store, create=False)
    assert outcome == panels.FORGOTTEN and ch.sent == [] and "k" not in store.load()["messages"]


@pytest.mark.asyncio
async def test_concurrent_updates_of_one_panel_post_only_once(tmp_path):
    ch = Channel([])
    store = store_with(tmp_path, None)
    results = await asyncio.gather(*(run(ch, store) for _ in range(5)))
    assert len(ch.sent) == 1
    assert sorted(r[0] for r in results) == [panels.CREATED] + [panels.EDITED] * 4


@pytest.mark.asyncio
async def test_duplicates_are_removed_but_never_the_stored_panel(tmp_path):
    keep, dup, other = Msg(5, "k"), Msg(9, "k"), Msg(7, "other")
    ch = Channel([keep, dup, other])
    store = store_with(tmp_path, 5)
    assert await panels.remove_duplicates(ch, "k", store=store, bot_user_id=BOT) == 1
    assert dup.deleted and not keep.deleted and not other.deleted
    lonely = Channel([Msg(9, "k")])  # stored message not in the scan: delete nothing
    assert await panels.remove_duplicates(lonely, "k", store=store, bot_user_id=BOT) == 0


# ---------------------------------------------------------------- COC client
class Resp:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body


class Session:
    closed = False

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        item = self.answers.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


async def no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_api_retries_temporary_errors_then_succeeds():
    s = Session([Resp(503, {"reason": "inMaintenance"}), asyncio.TimeoutError(), Resp(200, {"tag": "#A"})])
    c = COCClient("t", session=s, sleep=no_sleep)
    assert await c.get_clan("#a") == {"tag": "#A"}
    assert len(s.calls) == 3 and s.calls[0].endswith("/clans/%23A")


@pytest.mark.asyncio
async def test_api_raises_instead_of_returning_empty_data():
    c = COCClient("t", session=Session([Resp(503, {})] * 3), sleep=no_sleep)
    with pytest.raises(CocApiError):
        await c.get_clan_members("#A")
    c = COCClient("t", session=Session([Resp(401, {"reason": "accessDenied"})]), sleep=no_sleep)
    with pytest.raises(CocApiError):
        await c.get_clan("#A")


@pytest.mark.asyncio
async def test_api_not_available_keeps_old_meaning():
    c = COCClient("t", session=Session([Resp(404, {}), Resp(403, {"reason": "accessDenied"})]), sleep=no_sleep)
    assert await c.get_clan("#A") == {}
    assert await c.get_current_war("#A") is None


@pytest.mark.asyncio
async def test_identical_requests_share_one_call_and_a_short_cache():
    now = [0.0]
    s = Session([Resp(200, {"tag": "#A"}), Resp(200, {"tag": "#A", "v": 2})])
    c = COCClient("t", session=s, sleep=no_sleep, clock=lambda: now[0], cache_seconds=60)
    results = await asyncio.gather(*(c.get_clan("#A") for _ in range(5)))
    assert all(r == {"tag": "#A"} for r in results) and len(s.calls) == 1
    now[0] = 61
    assert (await c.get_clan("#A"))["v"] == 2 and len(s.calls) == 2
