"""Edit-in-place panels that never get posted twice.

A panel is a bot message tagged with ``[key:<name>]``. The rule that fixes the
reposting: a new message is sent ONLY when Discord has positively confirmed the
old one is gone (404 on the stored ID) AND a complete scan of recent channel
history found no other message with that key. Any other failure - rate limit,
5xx, timeout, missing permission - skips this update and the next cycle retries
the same message.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

HISTORY_SCAN_LIMIT = 200
_key_locks: dict[str, asyncio.Lock] = {}

# outcome values (also used by tests and logs)
EDITED, CREATED, SKIPPED, FORGOTTEN = "edited", "created", "skipped", "forgotten"


def marker(key: str) -> str:
    return f"[key:{key}]"


def message_has_key(msg, key: str, bot_user_id: int) -> bool:
    if getattr(msg.author, "id", None) != bot_user_id:
        return False
    tag = marker(key)
    if msg.content and tag in msg.content:
        return True
    for embed in msg.embeds or []:
        footer = embed.footer.text if embed.footer and embed.footer.text else ""
        if tag in footer:
            return True
    return False


async def find_keyed_messages(channel, key: str, bot_user_id: int, limit: int = HISTORY_SCAN_LIMIT) -> list:
    """All bot messages carrying the key, newest first. Raises on any error."""
    found = []
    async for msg in channel.history(limit=limit):
        if message_has_key(msg, key, bot_user_id):
            found.append(msg)
    return found


async def upsert(
    channel,
    key: str,
    *,
    store,
    bot_user_id: int,
    edit: Callable[[object], Awaitable[object]],
    send: Callable[[], Awaitable[object]],
    create_if_missing: bool = True,
) -> str:
    lock = _key_locks.setdefault(f"{getattr(channel, 'id', 0)}:{key}", asyncio.Lock())
    async with lock:
        state = store.load()
        messages = state["messages"]
        stored_id = messages.get(key)
        gone = stored_id is None

        if stored_id is not None:
            try:
                msg = await channel.fetch_message(int(stored_id))
            except discord.NotFound:
                gone = True
            except Exception as exc:  # 429/5xx/timeout/forbidden: retry next cycle
                log.warning("panel %s: fetch failed (%s), keeping message, retry next cycle", key, type(exc).__name__)
                return SKIPPED
            else:
                try:
                    await edit(msg)
                    return EDITED
                except discord.NotFound:
                    gone = True
                except Exception as exc:
                    log.warning("panel %s: edit failed (%s), retry next cycle", key, type(exc).__name__)
                    return SKIPPED

        # stored message is confirmed gone (or never stored): look for a keyed message
        try:
            candidates = await find_keyed_messages(channel, key, bot_user_id)
        except Exception as exc:
            log.warning("panel %s: history scan failed (%s), not posting", key, type(exc).__name__)
            return SKIPPED
        for msg in candidates:  # newest first
            try:
                await edit(msg)
            except discord.NotFound:
                continue
            except Exception as exc:
                log.warning("panel %s: edit of recovered message failed (%s)", key, type(exc).__name__)
                return SKIPPED
            messages[key] = str(msg.id)
            store.save()
            log.info("panel %s: re-attached to existing message %s", key, msg.id)
            return EDITED

        if gone and stored_id is not None:
            messages.pop(key, None)
            store.save()
        if not create_if_missing:
            return FORGOTTEN
        msg = await send()
        messages[key] = str(msg.id)
        store.save()
        log.info("panel %s: posted new message %s", key, msg.id)
        return CREATED


async def remove_duplicates(channel, key: str, *, store, bot_user_id: int) -> int:
    """Delete extra bot messages carrying the same key; the stored one stays.

    Only runs when the stored message is among the scanned ones, so a scan that
    simply missed the real panel can never delete it."""
    stored_id = store.load()["messages"].get(key)
    if stored_id is None:
        return 0
    try:
        found = await find_keyed_messages(channel, key, bot_user_id)
    except Exception:
        return 0
    if not any(str(m.id) == str(stored_id) for m in found):
        return 0
    removed = 0
    for msg in found:
        if str(msg.id) == str(stored_id):
            continue
        try:
            await msg.delete()
            removed += 1
            log.info("panel %s: removed duplicate message %s", key, msg.id)
        except discord.NotFound:
            pass
        except Exception as exc:
            log.warning("panel %s: could not remove duplicate %s (%s)", key, msg.id, type(exc).__name__)
    return removed
