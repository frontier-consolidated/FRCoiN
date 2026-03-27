import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import openpyxl
import io
import os
import logging
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB = "dkp.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild active event state (supports multi-server)
active_events: dict[int, int] = {}

# ------------------------
# DATABASE
# ------------------------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, dkp INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS attendance (event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id,user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, guild_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dkp_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                guild_id INTEGER,
                target_user_id INTEGER,
                amount INTEGER,
                reason TEXT,
                issued_by_id INTEGER
            )
        """)
        await db.commit()

# ------------------------
# HELPERS
# ------------------------
async def add_dkp(user_id: int, amount: int, guild_id: int = None, reason: str = None, issued_by_id: int = None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO users (user_id, dkp) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET dkp = dkp + ?",
            (user_id, amount, amount)
        )
        if guild_id is not None:
            await db.execute(
                "INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id) VALUES (?,?,?,?,?)",
                (guild_id, user_id, amount, reason, issued_by_id)
            )
        await db.commit()

async def set_dkp(user_id: int, amount: int, guild_id: int = None, reason: str = None, issued_by_id: int = None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO users (user_id, dkp) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET dkp = ?",
            (user_id, amount, amount)
        )
        if guild_id is not None:
            await db.execute(
                "INSERT INTO dkp_log (guild_id, target_user_id, amount, reason, issued_by_id) VALUES (?,?,?,?,?)",
                (guild_id, user_id, amount, reason, issued_by_id)
            )
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, dkp FROM users ORDER BY dkp DESC")
        return await cur.fetchall()

async def get_config(key: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM config WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_config(key: str, value):
    async with aiosqlite.connect(DB) as db:
        await db.execute("REPLACE INTO config (key,value) VALUES (?,?)", (key, str(value)))
        await db.commit()

async def is_admin(member: discord.Member) -> bool:
    roles = await get_config("admin_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

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
        logger.warning("Leaderboard channel not found (id=%s). Bot may lack access.", channel_id)
        return

    data = await get_all_users()

    # Build embed (avoids 2000-char message limit; supports up to 6000 chars)
    embed = discord.Embed(title="🏆 DKP Leaderboard", colour=discord.Colour.gold())

    lines = []
    # Cache member lookups to avoid redundant API calls
    guild = channel.guild
    for i, (uid, dkp) in enumerate(data[:50], 1):  # cap at 50 to stay within embed limits
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
    embed.set_footer(text=f"Showing top {min(len(data), 50)} of {len(data)} members")

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
# MODALS
# ------------------------
class PointsModal(discord.ui.Modal, title="Enter DKP Points"):
    points = discord.ui.TextInput(label="Points (+ or -)", placeholder="e.g. 5 or -3")

    def __init__(self, event_mode=True, target_user: discord.Member = None, guild_id: int = None, issued_by: discord.Member = None):
        super().__init__()
        self.event_mode = event_mode
        self.target_user = target_user
        self.guild_id = guild_id
        self.issued_by = issued_by

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            return await interaction.response.send_message("Invalid number — please enter a whole number like `5` or `-3`.", ephemeral=True)

        guild_id = interaction.guild_id
        issued_by_id = interaction.user.id

        if self.event_mode:
            active_event = active_events.get(guild_id)
            if not active_event:
                return await interaction.response.send_message("No active event.", ephemeral=True)

            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT user_id FROM attendance WHERE event_id=?", (active_event,))
                attendees = await cur.fetchall()

            if not attendees:
                return await interaction.response.send_message("No attendees recorded for this event. Use `/snapshot_event` first.", ephemeral=True)

            for (uid,) in attendees:
                await add_dkp(uid, value, guild_id=guild_id, reason=f"event:{active_event}", issued_by_id=issued_by_id)

            await update_leaderboard()
            active_events.pop(guild_id, None)  # clear event only after success

            await interaction.response.send_message(
                f"✅ Awarded **{value} DKP** to **{len(attendees)} users**.",
                ephemeral=True
            )

        else:
            await add_dkp(self.target_user.id, value, guild_id=guild_id, reason="manual_adjust", issued_by_id=issued_by_id)
            await update_leaderboard()
            await interaction.response.send_message(
                f"✅ Updated **{self.target_user.display_name}** by **{value:+} DKP**.",
                ephemeral=True
            )

# ------------------------
# COMMANDS
# ------------------------
@bot.tree.command(name="setup", description="Configure admin roles and the leaderboard channel")
@app_commands.describe(
    leaderboard_channel="Channel where the leaderboard message should live",
    admin_roles="Role mentions or role IDs separated by commas"
)
async def setup(
    interaction: discord.Interaction,
    leaderboard_channel: discord.TextChannel,
    admin_roles: str
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("You need Administrator permission to run setup.", ephemeral=True)

    role_ids = []
    for raw in admin_roles.split(","):
        raw = raw.strip()
        if raw.startswith("<@&") and raw.endswith(">"):
            raw = raw[3:-1]
        if raw.isdigit():
            role_ids.append(raw)

    if not role_ids:
        return await interaction.response.send_message(
            "No valid role mentions or IDs found. Separate multiple roles with commas.",
            ephemeral=True
        )

    await set_config("admin_roles", ",".join(role_ids))
    await set_config("leaderboard_channel", leaderboard_channel.id)
    await update_leaderboard()

    await interaction.response.send_message(
        f"✅ Setup complete. Leaderboard channel set to {leaderboard_channel.mention}.",
        ephemeral=True
    )

@bot.tree.command(name="start_event", description="Start a new DKP event")
@app_commands.describe(name="Name of the event")
async def start_event(interaction: discord.Interaction, name: str):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id in active_events:
        return await interaction.response.send_message(
            "There's already an active event. Use `/cancel_event` to abort it first.",
            ephemeral=True
        )

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("INSERT INTO events (name, guild_id) VALUES (?,?)", (name, guild_id))
        await db.commit()
        active_events[guild_id] = cur.lastrowid

    await interaction.response.send_message(
        f"✅ Event **{name}** started.\nUse `/snapshot_event` to capture attendees, then `/end_event` to allocate DKP.",
        ephemeral=True
    )

@bot.tree.command(name="snapshot_event", description="Capture everyone in your current voice channel into the active event")
async def snapshot_event(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event = active_events.get(guild_id)
    if not active_event:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("You must be in a voice channel to snapshot.", ephemeral=True)

    members = [m for m in interaction.user.voice.channel.members if not m.bot]

    async with aiosqlite.connect(DB) as db:
        for m in members:
            await db.execute(
                "INSERT OR IGNORE INTO attendance VALUES (?,?)",
                (active_event, m.id)
            )
        await db.commit()

    await interaction.response.send_message(f"✅ Captured **{len(members)} users** into the active event.", ephemeral=True)

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

    active_events.pop(guild_id)
    await interaction.response.send_message("✅ Active event cancelled. No DKP was awarded.", ephemeral=True)

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

@bot.tree.command(name="export", description="Export the DKP leaderboard as an Excel file")
async def export(interaction: discord.Interaction):
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    data = await get_all_users()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Rank", "User", "UserID", "DKP"])

    guild = interaction.guild
    for i, (uid, dkp) in enumerate(data, 1):
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

    # Write to memory — no temp file on disk
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    await interaction.response.send_message(
        file=discord.File(buffer, filename="leaderboard.xlsx"),
        ephemeral=True
    )

@bot.tree.command(name="import_leaderboard", description="Import DKP data from an Excel file")
@app_commands.describe(file="An .xlsx file exported from this bot")
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
        return await interaction.followup.send("Could not read the file — make sure it's a valid `.xlsx` export.", ephemeral=True)

    # Validate rows before touching the database
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
            "No valid rows found in the file. Expected columns: Rank, User, UserID, DKP.",
            ephemeral=True
        )

    # Only write to DB after successful validation
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM users")
        for user_id, dkp in rows_to_import:
            await db.execute(
                "INSERT INTO users (user_id, dkp) VALUES (?, ?)",
                (user_id, dkp)
            )
        await db.commit()

    await update_leaderboard()
    await interaction.followup.send(f"✅ Imported **{len(rows_to_import)} users** successfully.", ephemeral=True)

# ------------------------
# READY
# ------------------------
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    logger.info("Logged in as %s", bot.user)

bot.run(TOKEN)

# Required permisions on Discord - 
# View Channels
# Send Messages
# Embed Links
# Read Message History
# Attach Files
# Use Application Commands