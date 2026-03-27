import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import openpyxl
import io
import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# Per-guild active event state (supports multi-server)
active_events: dict[int, int] = {}

# Global connection pool
db_pool: asyncpg.Pool = None

# ------------------------
# DATABASE
# ------------------------
async def get_pool() -> asyncpg.Pool:
    global db_pool
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
    logger.info("Database initialised.")

# ------------------------
# HELPERS
# ------------------------
async def add_dkp(user_id: int, amount: int, guild_id: int = None, reason: str = None, issued_by_id: int = None):
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

async def set_dkp(user_id: int, amount: int, guild_id: int = None, reason: str = None, issued_by_id: int = None):
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

async def is_admin(member: discord.Member) -> bool:
    roles = await get_config("admin_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

# ------------------------
# AUDIT LOGGING
# ------------------------
async def send_audit(guild: discord.Guild, embed: discord.Embed):
    channel_id = await get_config("audit_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning("Could not send audit log: %s", e)

def audit_embed(title: str, colour: discord.Colour = discord.Colour.blurple()) -> discord.Embed:
    return discord.Embed(title=title, colour=colour, timestamp=datetime.now(timezone.utc))

# ------------------------
# LEADERBOARD
# ------------------------
async def update_leaderboard():
    channel_id = await get_config("leaderboard_channel")
    message_id = await get_config("leaderboard_message")

    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if not channel:
        logger.warning("Leaderboard channel not found (id=%s).", channel_id)
        return

    data = await get_all_users()
    embed = discord.Embed(title="🏆 FRCoiN DKP Leaderboard", colour=discord.Colour.gold())
    lines = []
    guild = channel.guild
    for i, row in enumerate(data[:50], 1):
        uid, dkp = row["user_id"], row["dkp"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except discord.NotFound:
                name = f"Unknown ({uid})"
            except discord.HTTPException as e:
                logger.warning("Could not fetch user %s: %s", uid, e)
                name = f"Unknown ({uid})"
        lines.append(f"`{i:>2}.` **{name}** — {dkp}")

    embed.description = "\n".join(lines) if lines else "*No entries yet.*"
    embed.set_footer(text=f"Showing top {min(len(data), 50)} of {len(data)} members • FRCoiN")

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=None, embed=embed)
            return
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("Could not edit leaderboard message: %s", e)

    message = await channel.send(embed=embed)
    await set_config("leaderboard_message", message.id)

# ------------------------
# HELPERS — ORG ROLE FILTERING
# ------------------------
def parse_role_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of role mentions or IDs into a list of ints."""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("<@&") and part.endswith(">"):
            part = part[3:-1]
        if part.isdigit():
            ids.append(int(part))
    return ids

async def get_org_role_ids() -> list[int]:
    """Return the configured org role IDs, or [] if not set."""
    raw = await get_config("org_roles")
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]

def member_has_org_role(member: discord.Member, org_role_ids: list[int]) -> bool:
    """Return True if the member holds at least one org role."""
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(org_role_ids))

# ------------------------
# HELPERS — EVENT EXPORT
# ------------------------
async def build_event_xlsx(event_id: int, guild: discord.Guild) -> tuple[io.BytesIO, str, int]:
    """
    Build an xlsx for a single saved event.
    Returns (buffer, event_name, attendee_count).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        event_row = await conn.fetchrow("SELECT name, created_at, dkp_awarded FROM events WHERE id = $1", event_id)
        attendees = await conn.fetch("SELECT user_id FROM attendance WHERE event_id = $1", event_id)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(["#", "Display Name", "UserID", "DKP Awarded"])

    dkp_val = event_row["dkp_awarded"] if event_row["dkp_awarded"] is not None else "N/A"

    for i, row in enumerate(attendees, 1):
        uid = row["user_id"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except (discord.NotFound, discord.HTTPException):
                name = f"Unknown ({uid})"
        ws.append([i, name, uid, dkp_val])

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Event ID", event_id])
    ws2.append(["Event Name", event_row["name"]])
    ws2.append(["Created At", str(event_row["created_at"])])
    ws2.append(["DKP Awarded", dkp_val])
    ws2.append(["Attendee Count", len(attendees)])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, event_row["name"], len(attendees)

# ------------------------
# VIEWS — SAVED EVENTS BROWSER
# ------------------------
EVENTS_PER_PAGE = 8

class EventsBrowserView(discord.ui.View):
    """Paginated view of all saved events for the guild, with export and delete buttons."""

    def __init__(self, events: list, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=120)
        self.events = events          # list of asyncpg Records
        self.guild = guild
        self.invoker = invoker
        self.page = 0
        self.total_pages = max(1, (len(events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self._update_buttons()

    def _current_page_events(self):
        start = self.page * EVENTS_PER_PAGE
        return self.events[start:start + EVENTS_PER_PAGE]

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📁 Saved Events",
            colour=discord.Colour.teal(),
            description="Select an event ID with `/event_export <id>` or `/event_delete <id>`."
        )
        page_events = self._current_page_events()
        for ev in page_events:
            status = "✅ Ended" if ev["ended"] else "🔴 Active / Cancelled"
            dkp_str = f"{ev['dkp_awarded']} DKP" if ev["dkp_awarded"] is not None else "—"
            ts = ev["created_at"].strftime("%Y-%m-%d %H:%M UTC") if ev["created_at"] else "Unknown"
            embed.add_field(
                name=f"`ID {ev['id']}` — {ev['name']}",
                value=f"{status} • Awarded: {dkp_str} • {ts}",
                inline=False
            )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages} • {len(self.events)} total events • FRCoiN")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("Only the person who ran this command can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

# ------------------------
# MODALS
# ------------------------
class PointsModal(discord.ui.Modal, title="Enter DKP Points"):
    points = discord.ui.TextInput(label="Points (+ or -)", placeholder="e.g. 5 or -3")

    def __init__(self, event_mode=True, target_user: discord.Member = None,
                 guild_id: int = None, issued_by: discord.Member = None):
        super().__init__()
        self.event_mode = event_mode
        self.target_user = target_user
        self.guild_id = guild_id
        self.issued_by = issued_by

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            return await interaction.response.send_message(
                "Invalid number — please enter a whole number like `5` or `-3`.", ephemeral=True
            )

        guild_id = interaction.guild_id
        issued_by_id = interaction.user.id

        if self.event_mode:
            active_event = active_events.get(guild_id)
            if not active_event:
                return await interaction.response.send_message("No active event.", ephemeral=True)

            pool = await get_pool()
            async with pool.acquire() as conn:
                attendees = await conn.fetch("SELECT user_id FROM attendance WHERE event_id = $1", active_event)

            if not attendees:
                return await interaction.response.send_message(
                    "No attendees recorded for this event. Use `/snapshot_event` first.", ephemeral=True
                )

            for row in attendees:
                await add_dkp(row["user_id"], value, guild_id=guild_id,
                               reason=f"event:{active_event}", issued_by_id=issued_by_id)

            # Mark the event as ended and record the DKP awarded
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE events SET ended = TRUE, dkp_awarded = $1 WHERE id = $2",
                    value, active_event
                )

            await update_leaderboard()
            active_events.pop(guild_id, None)

            await interaction.response.send_message(
                f"✅ Awarded **{value} DKP** to **{len(attendees)} users**.", ephemeral=True
            )

            embed = audit_embed("📋 Event DKP Awarded", discord.Colour.green())
            embed.add_field(name="Event ID", value=str(active_event), inline=True)
            embed.add_field(name="Points", value=f"{value:+}", inline=True)
            embed.add_field(name="Recipients", value=str(len(attendees)), inline=True)
            embed.add_field(name="Issued By", value=interaction.user.mention, inline=False)
            await send_audit(interaction.guild, embed)

        else:
            await add_dkp(self.target_user.id, value, guild_id=guild_id,
                           reason="manual_adjust", issued_by_id=issued_by_id)
            await update_leaderboard()
            await interaction.response.send_message(
                f"✅ Updated **{self.target_user.display_name}** by **{value:+} DKP**.", ephemeral=True
            )

            embed = audit_embed("✏️ DKP Manually Adjusted", discord.Colour.orange())
            embed.add_field(name="Member", value=self.target_user.mention, inline=True)
            embed.add_field(name="Adjustment", value=f"{value:+}", inline=True)
            embed.add_field(name="Issued By", value=interaction.user.mention, inline=False)
            await send_audit(interaction.guild, embed)

# ------------------------
# SLASH COMMANDS
# ------------------------

@bot.tree.command(name="setup", description="Configure FRCoiN — admin roles, leaderboard channel, audit channel, and org roles")
@app_commands.describe(
    leaderboard_channel="Channel where the leaderboard message will live",
    admin_roles="Role mentions or role IDs separated by commas (bot admins)",
    audit_channel="Channel where all bot actions will be logged",
    org_roles="Role mentions or IDs for org members (used to filter snapshots). Leave blank to allow all."
)
async def setup(
    interaction: discord.Interaction,
    leaderboard_channel: discord.TextChannel,
    admin_roles: str,
    audit_channel: discord.TextChannel,
    org_roles: str = ""
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need Administrator permission to run setup.", ephemeral=True
        )

    role_ids = parse_role_ids(admin_roles)
    if not role_ids:
        return await interaction.response.send_message(
            "No valid admin role mentions or IDs found. Separate multiple roles with commas.", ephemeral=True
        )

    await set_config("admin_roles", ",".join(str(r) for r in role_ids))
    await set_config("leaderboard_channel", leaderboard_channel.id)
    await set_config("audit_channel", audit_channel.id)

    org_summary = "*(none — all members allowed)*"
    if org_roles.strip():
        org_ids = parse_role_ids(org_roles)
        if org_ids:
            await set_config("org_roles", ",".join(str(r) for r in org_ids))
            org_summary = ", ".join(f"<@&{r}>" for r in org_ids)
        else:
            return await interaction.response.send_message(
                "No valid org role mentions or IDs found in `org_roles`.", ephemeral=True
            )
    else:
        # Clear any previously set org_roles
        await set_config("org_roles", "")

    await update_leaderboard()

    await interaction.response.send_message(
        f"✅ **FRCoiN** setup complete.\n"
        f"📊 Leaderboard channel: {leaderboard_channel.mention}\n"
        f"🔍 Audit channel: {audit_channel.mention}\n"
        f"🛡️ Org roles (snapshot filter): {org_summary}",
        ephemeral=True
    )

    embed = audit_embed("⚙️ FRCoiN Setup Run", discord.Colour.blue())
    embed.add_field(name="Run By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Leaderboard Channel", value=leaderboard_channel.mention, inline=True)
    embed.add_field(name="Audit Channel", value=audit_channel.mention, inline=True)
    embed.add_field(name="Admin Role IDs", value=", ".join(str(r) for r in role_ids), inline=False)
    embed.add_field(name="Org Roles", value=org_summary, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="start_event", description="Start a new DKP event")
@app_commands.describe(name="Name of the event")
async def start_event(interaction: discord.Interaction, name: str):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id in active_events:
        return await interaction.response.send_message(
            "There's already an active event. Use `/cancel_event` to abort it first.", ephemeral=True
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (name, guild_id) VALUES ($1, $2) RETURNING id", name, guild_id
        )
        active_events[guild_id] = row["id"]

    await interaction.response.send_message(
        f"✅ Event **{name}** started (ID: `{row['id']}`).\n"
        f"Use `/snapshot_event` to capture attendees, then `/end_event` to allocate DKP.",
        ephemeral=True
    )

    embed = audit_embed("🎯 Event Started", discord.Colour.teal())
    embed.add_field(name="Event", value=name, inline=True)
    embed.add_field(name="Event ID", value=str(row["id"]), inline=True)
    embed.add_field(name="Started By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(
    name="snapshot_event",
    description="Capture voice channel members into the active event, with optional org-role filtering"
)
@app_commands.describe(
    org_only="If True, only capture members with a configured org role (ignores non-org attendees)"
)
async def snapshot_event(interaction: discord.Interaction, org_only: bool = False):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event = active_events.get(guild_id)
    if not active_event:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("You must be in a voice channel to snapshot.", ephemeral=True)

    all_members = [m for m in interaction.user.voice.channel.members if not m.bot]

    if org_only:
        org_role_ids = await get_org_role_ids()
        if not org_role_ids:
            return await interaction.response.send_message(
                "No org roles are configured. Set them with `/setup` first, or use `/snapshot_event org_only:False`.",
                ephemeral=True
            )
        members = [m for m in all_members if member_has_org_role(m, org_role_ids)]
        skipped = len(all_members) - len(members)
    else:
        members = all_members
        skipped = 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        for m in members:
            await conn.execute(
                "INSERT INTO attendance (event_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                active_event, m.id
            )

    filter_note = f" *(org-only filter applied, {skipped} non-org member(s) skipped)*" if org_only else ""
    await interaction.response.send_message(
        f"✅ Captured **{len(members)} users** into event `{active_event}`.{filter_note}", ephemeral=True
    )

    embed = audit_embed("📸 Event Snapshot Taken", discord.Colour.teal())
    embed.add_field(name="Event ID", value=str(active_event), inline=True)
    embed.add_field(name="Captured", value=f"{len(members)} users", inline=True)
    embed.add_field(name="Org-Only Filter", value="Yes" if org_only else "No", inline=True)
    if org_only:
        embed.add_field(name="Skipped (non-org)", value=str(skipped), inline=True)
    embed.add_field(name="By", value=interaction.user.mention, inline=False)
    names = ", ".join(m.display_name for m in members[:20])
    if len(members) > 20:
        names += f" … +{len(members) - 20} more"
    embed.add_field(name="Members", value=names or "None", inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="end_event", description="End the active event and allocate DKP to all attendees")
async def end_event(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    if interaction.guild_id not in active_events:
        return await interaction.response.send_message("No active event.", ephemeral=True)

    await interaction.response.send_modal(
        PointsModal(event_mode=True, guild_id=interaction.guild_id, issued_by=interaction.user)
    )


@bot.tree.command(name="cancel_event", description="Cancel the active event without awarding DKP")
async def cancel_event(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id not in active_events:
        return await interaction.response.send_message("No active event to cancel.", ephemeral=True)

    event_id = active_events.pop(guild_id)
    await interaction.response.send_message("✅ Active event cancelled. No DKP was awarded.", ephemeral=True)

    embed = audit_embed("❌ Event Cancelled", discord.Colour.red())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Cancelled By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_add", description="Manually add a member to the active event's attendance list")
@app_commands.describe(member="The member to add to the current event")
async def event_add(interaction: discord.Interaction, member: discord.Member):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event = active_events.get(guild_id)
    if not active_event:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    if member.bot:
        return await interaction.response.send_message("You can't add a bot to an event.", ephemeral=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO attendance (event_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            active_event, member.id
        )

    if result == "INSERT 0 0":
        return await interaction.response.send_message(
            f"**{member.display_name}** is already in the attendance list for event `{active_event}`.", ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ Added **{member.display_name}** to event `{active_event}`.", ephemeral=True
    )

    embed = audit_embed("➕ Attendee Added to Event", discord.Colour.green())
    embed.add_field(name="Event ID", value=str(active_event), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Added By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_remove", description="Manually remove a member from the active event's attendance list")
@app_commands.describe(member="The member to remove from the current event")
async def event_remove(interaction: discord.Interaction, member: discord.Member):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event = active_events.get(guild_id)
    if not active_event:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM attendance WHERE event_id = $1 AND user_id = $2",
            active_event, member.id
        )

    if result == "DELETE 0":
        return await interaction.response.send_message(
            f"**{member.display_name}** wasn't in the attendance list for event `{active_event}`.", ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ Removed **{member.display_name}** from event `{active_event}`.", ephemeral=True
    )

    embed = audit_embed("➖ Attendee Removed from Event", discord.Colour.orange())
    embed.add_field(name="Event ID", value=str(active_event), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Removed By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_list", description="Show everyone currently on the attendance list for the active event")
async def event_list(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event = active_events.get(guild_id)
    if not active_event:
        return await interaction.response.send_message("No active event.", ephemeral=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM attendance WHERE event_id = $1", active_event)

    if not rows:
        return await interaction.response.send_message(
            f"No attendees recorded yet for event `{active_event}`. Use `/snapshot_event` or `/event_add`.",
            ephemeral=True
        )

    lines = []
    for row in rows:
        member = interaction.guild.get_member(row["user_id"])
        lines.append(f"• {member.mention if member else f'Unknown ({row[\"user_id\"]})'}")

    embed = discord.Embed(
        title=f"📋 Attendance — Event `{active_event}`",
        description="\n".join(lines),
        colour=discord.Colour.teal()
    )
    embed.set_footer(text=f"{len(rows)} attendee(s) • FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── NEW: Browse all saved events ─────────────────────────────────────────────

@bot.tree.command(name="events_saved", description="Browse all saved events for this server")
async def events_saved(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, name, created_at, ended, dkp_awarded FROM events WHERE guild_id = $1 ORDER BY id DESC",
            interaction.guild_id
        )

    if not events:
        return await interaction.response.send_message(
            "No events have been saved for this server yet.", ephemeral=True
        )

    view = EventsBrowserView(events=events, guild=interaction.guild, invoker=interaction.user)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


# ── NEW: Export a specific saved event ───────────────────────────────────────

@bot.tree.command(name="event_export", description="Export the attendance list for a specific saved event as an Excel file")
@app_commands.describe(event_id="The event ID (find it with /events_saved)")
async def event_export(interaction: discord.Interaction, event_id: int):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    # Verify the event belongs to this guild
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM events WHERE id = $1 AND guild_id = $2", event_id, interaction.guild_id)

    if not row:
        return await interaction.response.send_message(
            f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    buffer, event_name, count = await build_event_xlsx(event_id, interaction.guild)
    safe_name = "".join(c for c in event_name if c.isalnum() or c in " _-").strip().replace(" ", "_")
    filename = f"event_{event_id}_{safe_name}.xlsx"

    await interaction.followup.send(
        f"✅ Export for **{event_name}** (ID: `{event_id}`) — {count} attendee(s).",
        file=discord.File(buffer, filename=filename),
        ephemeral=True
    )

    embed = audit_embed("📤 Event Exported", discord.Colour.blurple())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Event Name", value=event_name, inline=True)
    embed.add_field(name="Attendees", value=str(count), inline=True)
    embed.add_field(name="Exported By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


# ── NEW: Delete a specific saved event ───────────────────────────────────────

@bot.tree.command(name="event_delete", description="Permanently delete a saved event and its attendance records")
@app_commands.describe(event_id="The event ID to delete (find it with /events_saved)")
async def event_delete(interaction: discord.Interaction, event_id: int):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM events WHERE id = $1 AND guild_id = $2", event_id, interaction.guild_id
        )

    if not row:
        return await interaction.response.send_message(
            f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.", ephemeral=True
        )

    # Guard: don't delete an event that is currently active
    if active_events.get(interaction.guild_id) == event_id:
        return await interaction.response.send_message(
            f"Event `{event_id}` is currently **active**. Use `/cancel_event` to cancel it first.", ephemeral=True
        )

    event_name = row["name"]

    async with pool.acquire() as conn:
        # CASCADE on attendance means attendee rows are deleted automatically
        await conn.execute("DELETE FROM events WHERE id = $1", event_id)

    await interaction.response.send_message(
        f"🗑️ Event **{event_name}** (ID: `{event_id}`) and all its attendance records have been deleted.",
        ephemeral=True
    )

    embed = audit_embed("🗑️ Event Deleted", discord.Colour.red())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Event Name", value=event_name, inline=True)
    embed.add_field(name="Deleted By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="adjust", description="Manually adjust DKP for a specific member")
@app_commands.describe(member="The member to adjust DKP for")
async def adjust(interaction: discord.Interaction, member: discord.Member):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    await interaction.response.send_modal(
        PointsModal(event_mode=False, target_user=member, guild_id=interaction.guild_id, issued_by=interaction.user)
    )


@bot.tree.command(name="spend", description="Reset a member's DKP to 0 (they spent their points)")
@app_commands.describe(member="The member whose DKP should be reset to 0")
async def spend(interaction: discord.Interaction, member: discord.Member):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    await set_dkp(member.id, 0, guild_id=interaction.guild_id, reason="spent", issued_by_id=interaction.user.id)
    await update_leaderboard()
    await interaction.response.send_message(f"✅ Reset **{member.display_name}**'s DKP to 0.", ephemeral=True)

    embed = audit_embed("💸 DKP Spent / Reset", discord.Colour.red())
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="New Balance", value="0", inline=True)
    embed.add_field(name="Reset By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="export", description="Export the full DKP leaderboard as an Excel file")
async def export(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    data = await get_all_users()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Rank", "User", "UserID", "DKP"])

    guild = interaction.guild
    for i, row in enumerate(data, 1):
        uid, dkp = row["user_id"], row["dkp"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except discord.NotFound:
                name = "Unknown"
            except discord.HTTPException as e:
                logger.warning("Could not fetch user %s during export: %s", uid, e)
                name = "Unknown"
        ws.append([i, name, uid, dkp])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    await interaction.response.send_message(
        file=discord.File(buffer, filename="leaderboard.xlsx"), ephemeral=True
    )

    embed = audit_embed("📤 Leaderboard Exported", discord.Colour.blurple())
    embed.add_field(name="Exported By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Rows", value=str(len(data)), inline=True)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="import_leaderboard", description="Import DKP data from an Excel file")
@app_commands.describe(file="An .xlsx file exported from FRCoiN")
async def import_leaderboard(interaction: discord.Interaction, file: discord.Attachment):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    if not file.filename.endswith(".xlsx"):
        return await interaction.response.send_message("Please upload a valid `.xlsx` file.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    file_bytes = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws = wb.active
    except Exception as e:
        logger.error("Failed to parse uploaded workbook: %s", e)
        return await interaction.followup.send(
            "Could not read the file — make sure it's a valid `.xlsx` export.", ephemeral=True
        )

    rows_to_import = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        try:
            user_id = int(row[2])
            dkp = int(row[3])
            rows_to_import.append((user_id, dkp))
        except (TypeError, ValueError, IndexError):
            continue

    if not rows_to_import:
        return await interaction.followup.send(
            "No valid rows found. Expected columns: Rank, User, UserID, DKP.", ephemeral=True
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM users")
            await conn.executemany(
                "INSERT INTO users (user_id, dkp) VALUES ($1, $2)", rows_to_import
            )

    await update_leaderboard()
    await interaction.followup.send(f"✅ Imported **{len(rows_to_import)} users** successfully.", ephemeral=True)

    embed = audit_embed("📥 Leaderboard Imported", discord.Colour.orange())
    embed.add_field(name="Imported By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Rows Imported", value=str(len(rows_to_import)), inline=True)
    await send_audit(interaction.guild, embed)


# ------------------------
# READY
# ------------------------
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    logger.info("FRCoiN logged in as %s", bot.user)


bot.run(TOKEN)

# ─────────────────────────────────────────────
# FRCoiN — Required .env variables:
#   DISCORD_TOKEN   — your bot token
#   DATABASE_URL    — PostgreSQL DSN, e.g.:
#                     postgresql://user:password@localhost:5432/dkp
#
# Required pip packages:
#   discord.py  asyncpg  openpyxl  python-dotenv
#
# Required Discord bot permissions:
#   View Channels · Send Messages · Embed Links
#   Read Message History · Attach Files
#   Use Application Commands · Connect (voice)
#
# New commands added:
#   /events_saved           — browse all saved events (paginated)
#   /event_export <id>      — export a specific event's attendance as xlsx
#   /event_delete <id>      — permanently delete a saved event
#   /snapshot_event org_only:True/False — filter by org roles on capture
#   /setup org_roles        — new optional param to configure org roles
# ─────────────────────────────────────────────
