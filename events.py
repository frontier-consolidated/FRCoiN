# ------------------------
# EVENTS
# ------------------------

import discord 
from db import get_pool

# Per-guild active event state (supports multi-server)
active_events: dict[int, int] = {}

async def load_active_events():
    pool = await get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, guild_id FROM events WHERE ended = false ORDER BY id DESC"
        )

    for event in events:
        active_events[event["guild_id"]] = event["id"]

async def get_completed_events(guild_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, name, created_at, ended, dkp_awarded FROM events WHERE guild_id = $1 ORDER BY id DESC",
            guild_id
        )

    return events

async def get_event(event_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, name FROM events WHERE id = $1", event_id)
    
    return row

async def get_event_in_guild(event_id: int, guild_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM events WHERE id = $1 AND guild_id = $2", event_id, guild_id)
    
    return row

async def create_event(guild_id: int, name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (name, guild_id) VALUES ($1, $2) RETURNING id", name, guild_id
        )
        active_events[guild_id] = row["id"]

    return row

async def end_event(event_id: int, dkp_awarded: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET ended = TRUE, dkp_awarded = $1 WHERE id = $2",
            dkp_awarded, event_id
        )

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
        for m in members:
            await conn.execute(
                "INSERT INTO attendance (event_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                event_id, m.id
            )
