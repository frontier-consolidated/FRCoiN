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
# ─────────────────────────────────────────────

import os
import io
import time
from typing import cast
from dotenv import load_dotenv
import openpyxl

import discord
from discord.ext import commands
from discord import app_commands

from audit_logs import audit_embed, send_audit
from db import get_config, get_dkp, get_dkp_log_entries, import_users, init_db, set_config, set_dkp
from events import active_events, add_event_attendee, create_event, delete_event, get_completed_events, get_event, get_event_attendees, get_event_in_guild, load_active_events, remove_event_attendee, take_event_attendance
from export import build_dkp_xlsx, build_event_xlsx
from helpers import get_org_role_ids, is_admin, member_has_org_role, parse_role_ids
from leaderboard import update_leaderboard
from modals.points_modal import PointsModal
from views.events_browser_view import EventsBrowserView
from logger import logger

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

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
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

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

    await update_leaderboard(bot)

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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id in active_events:
        return await interaction.response.send_message(
            "There's already an active event. Use `/cancel_event` to abort it first.", ephemeral=True
        )

    event = await create_event(guild_id, name)

    await interaction.response.send_message(
        f"✅ Event **{name}** started (ID: `{event['id']}`).\n"
        f"Use `/snapshot_event` to capture attendees, then `/end_event` to allocate DKP.",
        ephemeral=True
    )

    embed = audit_embed("🎯 Event Started", discord.Colour.teal())
    embed.add_field(name="Event", value=name, inline=True)
    embed.add_field(name="Event ID", value=str(event["id"]), inline=True)
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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event_id = active_events.get(guild_id)
    if not active_event_id:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("You must be in a voice channel to snapshot.", ephemeral=True)

    active_event = await get_event(active_event_id)
    if not active_event:
        return await interaction.response.send_message(
            f"No event with ID `{active_event_id}` found", ephemeral=True
        )

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

    await take_event_attendance(active_event_id, members)

    filter_note = f" *(org-only filter applied, {skipped} non-org member(s) skipped)*" if org_only else ""
    await interaction.response.send_message(
        f"✅ Captured **{len(members)} users** into event `{active_event["name"]}` (ID: {active_event_id}).{filter_note}", ephemeral=True
    )

    embed = audit_embed("📸 Event Snapshot Taken", discord.Colour.teal())
    embed.add_field(name="Event", value=active_event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(active_event_id), inline=True)
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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    if interaction.guild_id not in active_events:
        return await interaction.response.send_message("No active event.", ephemeral=True)

    await interaction.response.send_modal(
        PointsModal(bot, event_mode=True, guild_id=interaction.guild_id, issued_by=interaction.user)
    )


@bot.tree.command(name="cancel_event", description="Cancel the active event without awarding DKP")
async def cancel_event(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id not in active_events:
        return await interaction.response.send_message("No active event to cancel.", ephemeral=True)

    event_id = active_events.pop(guild_id)
    await delete_event(event_id)

    await interaction.response.send_message("✅ Active event cancelled. No DKP was awarded.", ephemeral=True)

    embed = audit_embed("❌ Event Cancelled", discord.Colour.red())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Cancelled By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_add", description="Manually add a member to the active event's attendance list")
@app_commands.describe(member="The member to add to the current event")
async def event_add(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event_id = active_events.get(guild_id)
    if not active_event_id:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    active_event = await get_event(active_event_id)
    if not active_event:
        return await interaction.response.send_message(
            f"No event with ID `{active_event_id}` found", ephemeral=True
        )

    if member.bot:
        return await interaction.response.send_message("You can't add a bot to an event.", ephemeral=True)

    result = await add_event_attendee(active_event_id, member)

    if result == "INSERT 0 0":
        return await interaction.response.send_message(
            f"**{member.display_name}** is already in the attendance list for event `{active_event["name"]}` (ID: {active_event_id}).", ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ Added **{member.display_name}** to event `{active_event["name"]}` (ID: {active_event_id}).", ephemeral=True
    )

    embed = audit_embed("➕ Attendee Added to Event", discord.Colour.green())
    embed.add_field(name="Event", value=active_event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(active_event_id), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Added By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_remove", description="Manually remove a member from the active event's attendance list")
@app_commands.describe(member="The member to remove from the current event")
async def event_remove(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event_id = active_events.get(guild_id)
    if not active_event_id:
        return await interaction.response.send_message("No active event. Use `/start_event` first.", ephemeral=True)

    active_event = await get_event(active_event_id)
    if not active_event:
        return await interaction.response.send_message(
            f"No event with ID `{active_event_id}` found", ephemeral=True
        )

    result = await remove_event_attendee(active_event_id, member)

    if result == "DELETE 0":
        return await interaction.response.send_message(
            f"**{member.display_name}** wasn't in the attendance list for event `{active_event["name"]}`.", ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ Removed **{member.display_name}** from event `{active_event["name"]}` (ID: {active_event_id}).", ephemeral=True
    )

    embed = audit_embed("➖ Attendee Removed from Event", discord.Colour.orange())
    embed.add_field(name="Event", value=active_event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(active_event), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Removed By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_attendance", description="Show everyone currently on the attendance list for the active event")
async def event_attendance(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    active_event_id = active_events.get(guild_id)
    if not active_event_id:
        return await interaction.response.send_message("No active event.", ephemeral=True)

    active_event = await get_event(active_event_id)
    if not active_event:
        return await interaction.response.send_message(
            f"No event with ID `{active_event_id}` found", ephemeral=True
        )

    rows = await get_event_attendees(active_event_id)

    if not rows:
        return await interaction.response.send_message(
            f"No attendees recorded yet for event `{active_event["name"]}` (ID: {active_event_id}). Use `/snapshot_event` or `/event_add`.",
            ephemeral=True
        )

    lines = []
    for row in rows:
        member = interaction.guild.get_member(row["user_id"])
        lines.append(f"• {member.mention if member else f'Unknown ({row["user_id"]})'}")

    embed = discord.Embed(
        title=f"📋 Attendance — Event `{active_event["name"]}` (ID: {active_event_id})",
        description="\n".join(lines),
        colour=discord.Colour.teal()
    )
    embed.set_footer(text=f"{len(rows)} attendee(s) • FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── NEW: Browse all saved events ─────────────────────────────────────────────

@bot.tree.command(name="events_saved", description="Browse all saved events for this server")
async def events_saved(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    events = await get_completed_events(interaction.guild_id)

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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    # Verify the event belongs to this guild
    row = await get_event_in_guild(event_id, interaction.guild_id)
    if not row:
        return await interaction.response.send_message(
            f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    buffer, event_name, count = await build_event_xlsx(bot, event_id, interaction.guild)
    safe_name = "".join(c for c in event_name if c.isalnum() or c in " _-").strip().replace(" ", "_")
    filename = f"event_{event_id}_{safe_name}-{int(time.time())}.xlsx"

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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    event = await get_event_in_guild(event_id, interaction.guild_id)
    if not event:
        return await interaction.response.send_message(
            f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.", ephemeral=True
        )

    # Guard: don't delete an event that is currently active
    if active_events.get(interaction.guild_id) == event_id:
        return await interaction.response.send_message(
            f"Event `{event_id}` is currently **active**. Use `/cancel_event` to cancel it first.", ephemeral=True
        )

    event_name = event["name"]

    await delete_event(event_id)

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
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    await interaction.response.send_modal(
        PointsModal(bot, event_mode=False, target_user=member, guild_id=interaction.guild_id, issued_by=interaction.user)
    )


@bot.tree.command(name="spend", description="Reset a member's DKP to 0 (they spent their points)")
@app_commands.describe(member="The member whose DKP should be reset to 0")
async def spend(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    await set_dkp(member.id, 0, guild_id=interaction.guild_id, reason="spent", issued_by_id=interaction.user.id)
    await update_leaderboard(bot)
    await interaction.response.send_message(f"✅ Reset **{member.display_name}**'s DKP to 0.", ephemeral=True)

    embed = audit_embed("💸 DKP Spent / Reset", discord.Colour.red())
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="New Balance", value="0", inline=True)
    embed.add_field(name="Reset By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="export_leaderboard", description="Export the full DKP leaderboard as an Excel file")
async def export_leaderboard(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    buffer, users = await build_dkp_xlsx(bot, interaction.guild)

    await interaction.response.send_message(
        file=discord.File(buffer, filename=f"leaderboard-{int(time.time())}.xlsx"), ephemeral=True
    )

    embed = audit_embed("📤 Leaderboard Exported", discord.Colour.blurple())
    embed.add_field(name="Exported By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Rows", value=str(len(users)), inline=True)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="import_leaderboard", description="Import DKP data from an Excel file")
@app_commands.describe(file="An .xlsx file exported from FRCoiN")
async def import_leaderboard(interaction: discord.Interaction, file: discord.Attachment):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
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
    
    if not ws:
        raise ValueError("Workbook has no active sheet.")

    rows_to_import = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        try:
            user_id = int(cast(str, row[2]))
            dkp = int(cast(str, row[3]))
            rows_to_import.append((user_id, dkp))
        except (TypeError, ValueError, IndexError):
            continue

    if not rows_to_import:
        return await interaction.followup.send(
            "No valid rows found. Expected columns: Rank, User, UserID, DKP.", ephemeral=True
        )

    await import_users(rows_to_import)

    await update_leaderboard(bot)
    await interaction.followup.send(f"✅ Imported **{len(rows_to_import)} users** successfully.", ephemeral=True)

    embed = audit_embed("📥 Leaderboard Imported", discord.Colour.orange())
    embed.add_field(name="Imported By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Rows Imported", value=str(len(rows_to_import)), inline=True)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="dkp", description="Check DKP balance — yours, or another member's (admin only)")
@app_commands.describe(member="Member to look up (leave blank to check your own balance)")
async def dkp(interaction: discord.Interaction, member: discord.Member | None = None):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    target = member or interaction.user

    if target != interaction.user and not await is_admin(interaction.user):
        return await interaction.response.send_message("You can only check your own DKP.", ephemeral=True)

    balance = await get_dkp(target.id)

    embed = discord.Embed(title="💰 DKP Balance", colour=discord.Colour.gold())
    embed.add_field(name="Member", value=target.mention, inline=True)
    embed.add_field(name="Balance", value=str(balance), inline=True)
    embed.set_footer(text="FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dkp_log", description="View recent DKP transactions for a member")
@app_commands.describe(member="The member whose transaction history to view")
async def dkp_log(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    entries = await get_dkp_log_entries(interaction.guild_id, member.id)

    embed = discord.Embed(title=f"📜 DKP Log — {member.display_name}", colour=discord.Colour.blurple())

    if not entries:
        embed.description = "No DKP transactions recorded for this member."
    else:
        lines = []
        for row in entries:
            amount = row["amount"]
            sign = "+" if amount >= 0 else ""
            reason = row["reason"] or "—"
            issued_by = f"<@{row['issued_by_id']}>" if row["issued_by_id"] else "system"
            ts = row["created_at"].strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"`{sign}{amount}` • {reason} • by {issued_by} • {ts}")
        embed.description = "\n".join(lines)

    embed.set_footer(text="Showing last 10 entries • FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show FRCoiN bot status and current configuration")
async def status(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    if not await is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id

    active_event_id = active_events.get(guild_id)
    if active_event_id:
        active_event = await get_event(active_event_id)
        event_str = f"`{active_event['name']}` (ID: {active_event_id})" if active_event else f"ID: {active_event_id}"
    else:
        event_str = "None"

    lb_channel_id = await get_config("leaderboard_channel")
    audit_channel_id = await get_config("audit_channel")
    admin_role_ids = await get_config("admin_roles")
    org_role_ids_raw = await get_config("org_roles")

    lb_str = f"<#{lb_channel_id}>" if lb_channel_id else "Not set"
    audit_str = f"<#{audit_channel_id}>" if audit_channel_id else "Not set"

    if admin_role_ids:
        admin_str = ", ".join(f"<@&{r}>" for r in admin_role_ids.split(",") if r.strip())
    else:
        admin_str = "Not set"

    org_str = ", ".join(f"<@&{r}>" for r in org_role_ids_raw.split(",") if r.strip()) if org_role_ids_raw else "*(all members)*"

    embed = discord.Embed(title="⚙️ FRCoiN Status", colour=discord.Colour.blurple())
    embed.add_field(name="Active Event", value=event_str, inline=False)
    embed.add_field(name="Leaderboard Channel", value=lb_str, inline=True)
    embed.add_field(name="Audit Channel", value=audit_str, inline=True)
    embed.add_field(name="Admin Roles", value=admin_str, inline=False)
    embed.add_field(name="Org Roles (filter)", value=org_str, inline=False)
    embed.set_footer(text="FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------
# READY
# ------------------------
@bot.event
async def on_ready():
    await init_db()
    await load_active_events()
    await bot.tree.sync()
    logger.info("FRCoiN logged in as %s", bot.user)

TOKEN = cast(str, os.getenv("DISCORD_TOKEN"))
bot.run(TOKEN)
