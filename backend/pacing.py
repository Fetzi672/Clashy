"""Space out message edits per channel.

Discord limits how fast messages in one channel may be edited. The panel
loops all fire at the same moment, so without spacing one edit per cycle was
rejected (HTTP 429) and retried. Edits in the same channel now run one after
another with a gap; different channels do not wait for each other.
"""
import asyncio
import os
import time

EDIT_GAP_SECONDS = float(os.getenv("DISCORD_CHANNEL_EDIT_GAP_SECONDS", "3.5") or "3.5")
_locks: dict = {}
_last: dict = {}


async def paced_edit(msg, *, gap=None, clock=time.monotonic, sleep=asyncio.sleep, **kwargs):
    """``await msg.edit(**kwargs)``, at least ``gap`` seconds after the
    previous edit in the same channel finished."""
    gap = EDIT_GAP_SECONDS if gap is None else gap
    channel_id = msg.channel.id
    lock = _locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        wait = _last.get(channel_id, float("-inf")) + gap - clock()
        if wait > 0:
            await sleep(wait)
        try:
            return await msg.edit(**kwargs)
        finally:
            _last[channel_id] = clock()
