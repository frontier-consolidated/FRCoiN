# ------------------------
# DATABASE
# ------------------------

from typing import Union
import asyncpg
import os
from logger import logger

# Global connection pool
db_pool: Union[asyncpg.Pool, None] = None

async def get_pool() -> asyncpg.Pool:
    global db_pool

    DATABASE_URL = os.getenv("DATABASE_URL")

    if DATABASE_URL is None:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in your PostgreSQL connection string."
        )

    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return db_pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                dkp     INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id         SERIAL PRIMARY KEY,
                name       TEXT    NOT NULL,
                guild_id   BIGINT  NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended      BOOLEAN NOT NULL DEFAULT FALSE,
                dkp_awarded INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                user_id  BIGINT  NOT NULL,
                PRIMARY KEY (event_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dkp_log (
                id             SERIAL PRIMARY KEY,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                guild_id       BIGINT,
                target_user_id BIGINT,
                amount         INTEGER,
                reason         TEXT,
                issued_by_id   BIGINT
            )
        """)
        # Migration: add new columns to events if upgrading from older schema
        await conn.execute("""
            ALTER TABLE events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """)
        await conn.execute("""
            ALTER TABLE events ADD COLUMN IF NOT EXISTS ended BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE events ADD COLUMN IF NOT EXISTS dkp_awarded INTEGER
        """)
        # V2 migration: panel state. Additive only, so v1 still runs against a migrated DB.
        await conn.execute("""
            ALTER TABLE events
                ADD COLUMN IF NOT EXISTS state                   TEXT NOT NULL DEFAULT 'open',
                ADD COLUMN IF NOT EXISTS kind                    TEXT NOT NULL DEFAULT 'dkp',
                ADD COLUMN IF NOT EXISTS created_by_id           BIGINT,
                ADD COLUMN IF NOT EXISTS scope_channel_ids       BIGINT[],
                ADD COLUMN IF NOT EXISTS pending_points          INTEGER,
                ADD COLUMN IF NOT EXISTS card_channel_id         BIGINT,
                ADD COLUMN IF NOT EXISTS card_message_id         BIGINT,
                ADD COLUMN IF NOT EXISTS last_snapshot_at        TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS last_snapshot_breakdown JSONB
        """)
        # Pre-release demo builds stored a single scope_channel_id; fold it into the list. No-op on v1 databases.
        await conn.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'events' AND column_name = 'scope_channel_id') THEN
                    UPDATE events SET scope_channel_ids = ARRAY[scope_channel_id]
                        WHERE scope_channel_id IS NOT NULL AND scope_channel_ids IS NULL;
                    ALTER TABLE events DROP COLUMN scope_channel_id;
                END IF;
            END $$
        """)
        await conn.execute("UPDATE events SET state = 'closed' WHERE ended AND state <> 'closed'")

    logger.info("Database initialised.")

async def add_dkp(user_id: int, amount: int, guild_id: Union[int, None] = None, reason: Union[str, None] = None, issued_by_id: Union[int, None] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, dkp) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET dkp = users.dkp + $2
        """, user_id, amount)
        if guild_id is not None:
            await conn.execute("""
                INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id)
                VALUES ($1, $2, $3, $4, $5)
            """, guild_id, user_id, amount, reason, issued_by_id)

async def set_dkp(user_id: int, amount: int, guild_id: Union[int, None] = None, reason: Union[str, None] = None, issued_by_id: Union[int, None] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, dkp) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET dkp = $2
        """, user_id, amount)
        if guild_id is not None:
            await conn.execute("""
                INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id)
                VALUES ($1, $2, $3, $4, $5)
            """, guild_id, user_id, amount, reason, issued_by_id)

async def get_all_users():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, dkp FROM users ORDER BY dkp DESC")

async def import_users(rows_to_import: list[tuple[int, int]]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM users")
            await conn.executemany(
                "INSERT INTO users (user_id, dkp) VALUES ($1, $2)", rows_to_import
            )

async def get_dkp(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT dkp FROM users WHERE user_id = $1", user_id)
    return row["dkp"] if row else 0

async def get_dkp_log_entries(guild_id: int, user_id: int, limit: int = 500):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT amount, reason, issued_by_id, created_at
            FROM dkp_log
            WHERE guild_id = $1 AND target_user_id = $2
            ORDER BY created_at DESC
            LIMIT $3
        """, guild_id, user_id, limit)
    return rows

async def get_config(key: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM config WHERE key = $1", key)
        return row["value"] if row else None

async def set_config(key: str, value):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO config (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, str(value))
