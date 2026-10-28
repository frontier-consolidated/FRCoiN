# ------------------------
# EVENTS
# ------------------------

import asyncio
import json
from collections import defaultdict
from typing import Union
import discord
from db import get_pool

# Event state lives in the DB (events.state), so cards keep working across restarts.
OPEN_STATES = ("open", "pending_close")

# One lock per event, so two officers clicking at once can't both snapshot/close.
_event_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def event_lock(event_id: int) -> asyncio.Lock:
    return _event_locks[event_id]

EVENT_COLUMNS = """
    id, name, guild_id, created_at, ended, dkp_awarded, state, kind, created_by_id,
    scope_channel_ids, pending_points, card_channel_id, card_message_id,
    last_snapshot_at, last_snapshot_breakdown
"""

def _decode(row) -> Union[dict, None]:
    if row is None:
        return None
    event = dict(row)
    if event["ended"]:  # v1 ends events without knowing about `state`, so `ended` wins
        event["state"] = "closed"
    breakdown = event.get("last_snapshot_breakdown")
    event["last_snapshot_breakdown"] = json.loads(breakdown) if breakdown else None
    return event

async def get_open_events(guild_id: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE guild_id = $1 AND state = ANY($2::text[]) AND NOT ended ORDER BY id DESC",
            guild_id, list(OPEN_STATES)
        )
    return [_decode(r) for r in rows]

async def get_all_open_events() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE state = ANY($1::text[]) AND NOT ended ORDER BY id",
            list(OPEN_STATES)
        )
    return [_decode(r) for r in rows]

async def get_completed_events(guild_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, name, created_at, ended, dkp_awarded, kind, state FROM events WHERE guild_id = $1 ORDER BY id DESC",
            guild_id
        )

    return events

async def get_event(event_id: int) -> Union[dict, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {EVENT_COLUMNS} FROM events WHERE id = $1", event_id)

    return _decode(row)

async def get_event_in_guild(event_id: int, guild_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {EVENT_COLUMNS} FROM events WHERE id = $1 AND guild_id = $2", event_id, guild_id)

    return _decode(row)

async def create_event(guild_id: int, name: str, kind: str = "dkp",
                       created_by_id: Union[int, None] = None, scope_channel_ids: Union[list[int], None] = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO events (name, guild_id, kind, created_by_id, scope_channel_ids)
                VALUES ($1, $2, $3, $4, $5) RETURNING {EVENT_COLUMNS}""",
            name, guild_id, kind, created_by_id, scope_channel_ids or None
        )

    return _decode(row)

async def set_event_state(event_id: int, state: str, pending_points: Union[int, None] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET state = $1, pending_points = $2 WHERE id = $3",
            state, pending_points, event_id
        )

async def set_event_card(event_id: int, channel_id: Union[int, None], message_id: Union[int, None]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET card_channel_id = $1, card_message_id = $2 WHERE id = $3",
            channel_id, message_id, event_id
        )

async def mark_snapshot(event_id: int, breakdown: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET last_snapshot_at = NOW(), last_snapshot_breakdown = $1::jsonb WHERE id = $2",
            json.dumps(breakdown), event_id
        )

async def end_event(event_id: int, dkp_awarded: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET ended = TRUE, state = 'closed', dkp_awarded = $1 WHERE id = $2",
            dkp_awarded, event_id
        )

async def close_attendance_event(event_id: int) -> bool:
    """Archive an attendance event. `ended` is set too, so v1 never treats it as active. Returns False if already closed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE events SET state = 'closed', ended = TRUE, pending_points = NULL WHERE id = $1 AND state <> 'closed' AND NOT ended RETURNING id",
            event_id
        )
    return row is not None

async def award_and_close_event(event_id: int, points: int, guild_id: int, issued_by_id: int, member_ids: list[int]) -> bool:
    """
    Award `points` to every id in `member_ids` and close the event, in one transaction.
    Returns False (and awards nothing) if the event was already closed.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE events SET state = 'closed', ended = TRUE, dkp_awarded = $1, pending_points = NULL
                   WHERE id = $2 AND state <> 'closed' AND NOT ended RETURNING id""",
                points, event_id
            )
            if row is None:
                return False
            await conn.executemany("""
                INSERT INTO users (user_id, dkp) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET dkp = users.dkp + $2
            """, [(uid, points) for uid in member_ids])
            await conn.executemany("""
                INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id)
                VALUES ($1, $2, $3, $4, $5)
            """, [(guild_id, uid, points, f"event:{event_id}", issued_by_id) for uid in member_ids])
    return True

async def delete_event(event_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # CASCADE on attendance means attendee rows are deleted automatically
        await conn.execute("DELETE FROM events WHERE id = $1", event_id)


async def get_event_attendees(event_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        attendees = await conn.fetch("SELECT user_id FROM attendance WHERE event_id = $1", event_id)

    return attendees

async def add_event_attendee(event_id: int, member: discord.Member):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO attendance (event_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            event_id, member.id
        )

    return result

async def remove_event_attendee(event_id: int, member: discord.Member):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM attendance WHERE event_id = $1 AND user_id = $2",
            event_id, member.id
        )

    return result

async def take_event_attendance(event_id: int, members: list[discord.Member]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO attendance (event_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [(event_id, m.id) for m in members]
        )
