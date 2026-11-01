# ------------------------
# ADJUSTMENT LISTS
# ------------------------
# A list collects many +/- point lines (e.g. an Arms Fayre) and applies them all at once.

import asyncio
from collections import defaultdict
from typing import Union
from db import get_pool

# One lock per list, so two officers can't apply (or edit while applying) the same list at once.
_list_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def list_lock(list_id: int) -> asyncio.Lock:
    return _list_locks[list_id]

LIST_COLUMNS = "id, guild_id, name, state, created_by_id, created_at, closed_by_id, closed_at, card_channel_id, card_message_id"
LINE_COLUMNS = "id, list_id, line_no, user_id, amount, reason, added_by_id, added_at, removed_by_id, removed_at, outcome"

def line_reason(list_id: int, list_name: str, reason: Union[str, None]) -> str:
    """The DKP log reason for a line, as members see it in My Points."""
    return f"adjlist:{list_id} {list_name}" + (f": {reason}" if reason else "")

def member_nets(lines: list[dict]) -> dict[int, int]:
    """Each member's total change across the given lines."""
    nets: dict[int, int] = {}
    for line in lines:
        nets[line["user_id"]] = nets.get(line["user_id"], 0) + line["amount"]
    return nets

async def create_list(guild_id: int, name: str, created_by_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"INSERT INTO adjustment_lists (guild_id, name, created_by_id) VALUES ($1, $2, $3) RETURNING {LIST_COLUMNS}",
            guild_id, name, created_by_id
        )
    return dict(row)

async def get_list(list_id: int) -> Union[dict, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {LIST_COLUMNS} FROM adjustment_lists WHERE id = $1", list_id)
    return dict(row) if row else None

async def get_list_in_guild(list_id: int, guild_id: int) -> Union[dict, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {LIST_COLUMNS} FROM adjustment_lists WHERE id = $1 AND guild_id = $2", list_id, guild_id)
    return dict(row) if row else None

async def get_open_lists(guild_id: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {LIST_COLUMNS} FROM adjustment_lists WHERE guild_id = $1 AND state = 'open' ORDER BY id DESC", guild_id
        )
    return [dict(r) for r in rows]

async def get_all_open_lists() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {LIST_COLUMNS} FROM adjustment_lists WHERE state = 'open' ORDER BY id")
    return [dict(r) for r in rows]

async def set_list_card(list_id: int, channel_id: Union[int, None], message_id: Union[int, None]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE adjustment_lists SET card_channel_id = $1, card_message_id = $2 WHERE id = $3",
            channel_id, message_id, list_id
        )

async def get_lines(list_id: int, include_removed: bool = False) -> list[dict]:
    pool = await get_pool()
    where = "" if include_removed else " AND removed_at IS NULL"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {LINE_COLUMNS} FROM adjustment_lines WHERE list_id = $1{where} ORDER BY line_no", list_id)
    return [dict(r) for r in rows]

async def add_lines(list_id: int, entries: list[tuple[int, int, Union[str, None]]], added_by_id: int) -> Union[list[int], None]:
    """
    Add (user_id, amount, reason) lines. Line numbers continue from the list's highest, and are never reused.
    Returns the new line numbers, or None if the list is no longer open.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchrow("SELECT id FROM adjustment_lists WHERE id = $1 AND state = 'open' FOR UPDATE", list_id):
                return None
            start = await conn.fetchval("SELECT COALESCE(MAX(line_no), 0) FROM adjustment_lines WHERE list_id = $1", list_id)
            numbers = list(range(start + 1, start + 1 + len(entries)))
            await conn.executemany(
                """INSERT INTO adjustment_lines (list_id, line_no, user_id, amount, reason, added_by_id)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                [(list_id, n, uid, amount, reason, added_by_id) for n, (uid, amount, reason) in zip(numbers, entries)]
            )
    return numbers

async def remove_lines(list_id: int, line_nos: list[int], removed_by_id: int) -> list[int]:
    """Mark lines as removed (they stay in the record). Returns the line numbers actually removed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """UPDATE adjustment_lines SET removed_by_id = $3, removed_at = NOW()
               WHERE list_id = $1 AND line_no = ANY($2::int[]) AND removed_at IS NULL
               RETURNING line_no""",
            list_id, line_nos, removed_by_id
        )
    return sorted(r["line_no"] for r in rows)

async def cancel_list(list_id: int, closed_by_id: int) -> bool:
    """Returns False if the list was no longer open."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE adjustment_lists SET state = 'cancelled', closed_by_id = $2, closed_at = NOW()
               WHERE id = $1 AND state = 'open' RETURNING id""",
            list_id, closed_by_id
        )
    return row is not None

async def apply_list(list_id: int, closed_by_id: int, guild_id: int, eligible_gainers: set[int]) -> Union[dict, None]:
    """
    Apply every active line in one transaction. Each member's lines are applied together:
    a net deduction only if their balance covers it, a net gain only if they're in `eligible_gainers`
    (current org members). Anyone else is skipped. Returns None if the list was no longer open, else
    {"applied": {user_id: (net, new_balance)}, "skipped": {user_id: (net, balance)}}.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE adjustment_lists SET state = 'applied', closed_by_id = $2, closed_at = NOW()
                   WHERE id = $1 AND state = 'open' RETURNING name""",
                list_id, closed_by_id
            )
            if row is None:
                return None
            lines = [dict(r) for r in await conn.fetch(
                f"SELECT {LINE_COLUMNS} FROM adjustment_lines WHERE list_id = $1 AND removed_at IS NULL ORDER BY line_no", list_id
            )]

            applied: dict[int, tuple[int, int]] = {}
            skipped: dict[int, tuple[int, int]] = {}
            for uid, net in member_nets(lines).items():
                if net > 0 and uid not in eligible_gainers:
                    new_balance = None
                elif net >= 0:
                    new_balance = await conn.fetchval("""
                        INSERT INTO users (user_id, dkp) VALUES ($1, $2)
                        ON CONFLICT (user_id) DO UPDATE SET dkp = users.dkp + $2 RETURNING dkp
                    """, uid, net)
                else:
                    new_balance = await conn.fetchval(
                        "UPDATE users SET dkp = dkp + $2 WHERE user_id = $1 AND dkp + $2 >= 0 RETURNING dkp", uid, net
                    )
                if new_balance is None:
                    skipped[uid] = (net, await conn.fetchval("SELECT dkp FROM users WHERE user_id = $1", uid) or 0)
                else:
                    applied[uid] = (net, new_balance)

            await conn.executemany(
                """INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id)
                   VALUES ($1, $2, $3, $4, $5)""",
                [(guild_id, line["user_id"], line["amount"], line_reason(list_id, row["name"], line["reason"]), line["added_by_id"])
                 for line in lines if line["user_id"] in applied]
            )
            await conn.execute(
                """UPDATE adjustment_lines SET outcome = CASE WHEN user_id = ANY($2::bigint[]) THEN 'applied' ELSE 'skipped' END
                   WHERE list_id = $1 AND removed_at IS NULL""",
                list_id, list(applied)
            )
    return {"applied": applied, "skipped": skipped}
