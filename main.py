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

from audit_logs import audit_embed, send_audit, send_audit_with_file
from db import get_config, get_dkp, get_dkp_log_entries, import_users, init_db, set_config, set_dkp
from events import active_events, add_event_attendee, create_event, delete_event, get_completed_events, get_event, get_event_attendees, get_event_in_guild, load_active_events, remove_event_attendee, take_event_attendance
from export import build_dkp_xlsx, build_event_xlsx
from helpers import get_org_role_ids, is_admin, is_officer, member_has_org_role, parse_role_ids
from leaderboard import update_leaderboard
from modals.points_modal import PointsModal
from views.events_browser_view import EventsBrowserView
from views.dkp_log_browser_view import DkpLogBrowserView
from logger import logger

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# ------------------------
# SLASH COMMANDS
# ------------------------

@bot.tree.command(name="setup", description="Configure FRCoiN — update only the options you pass, everything else stays as-is")
@app_commands.describe(
    leaderboard_channel="Channel where the leaderboard message will live (leave blank to keep current)",
    admin_roles="Role mentions or role IDs separated by commas, full bot admins (leave blank to keep current)",
    audit_channel="Channel where all bot actions will be logged (leave blank to keep current)",
    org_roles="Role mentions or IDs for org members, used to filter snapshots (leave blank to keep current; 'none' to clear)",
    officer_roles="Role mentions or IDs for officers, day-to-day ops below admin (leave blank to keep current; 'none' to clear)"
)
async def setup(
    interaction: discord.Interaction,
    leaderboard_channel: discord.TextChannel | None = None,
    admin_roles: str | None = None,
    audit_channel: discord.TextChannel | None = None,
    org_roles: str | None = None,
    officer_roles: str | None = None
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need Administrator permission to run setup.", ephemeral=True
        )

    if leaderboard_channel is None and admin_roles is None and audit_channel is None and org_roles is None and officer_roles is None:
        return await interaction.response.send_message(
            "You didn't pass any options to change. Provide at least one of: "
            "`leaderboard_channel`, `admin_roles`, `audit_channel`, `org_roles`, `officer_roles`.",
            ephemeral=True
        )

    changes = []  # (label, display value) pairs, for the confirmation + audit embed

    if leaderboard_channel is not None:
        await set_config("leaderboard_channel", leaderboard_channel.id)
        changes.append(("Leaderboard Channel", leaderboard_channel.mention))

    if admin_roles is not None:
        role_ids = parse_role_ids(admin_roles)
        if not role_ids:
            return await interaction.response.send_message(
                "No valid admin role mentions or IDs found in `admin_roles`. Separate multiple roles with commas.",
                ephemeral=True
            )
        await set_config("admin_roles", ",".join(str(r) for r in role_ids))
        changes.append(("Admin Roles", ", ".join(f"<@&{r}>" for r in role_ids)))

    if audit_channel is not None:
        await set_config("audit_channel", audit_channel.id)
        changes.append(("Audit Channel", audit_channel.mention))

    if org_roles is not None:
        if org_roles.strip().lower() in ("", "none", "clear"):
            await set_config("org_roles", "")
            changes.append(("Org Roles", "*(cleared — all members allowed)*"))
        else:
            org_ids = parse_role_ids(org_roles)
            if not org_ids:
                return await interaction.response.send_message(
                    "No valid org role mentions or IDs found in `org_roles`. Pass `none` to clear it.",
                    ephemeral=True
                )
            await set_config("org_roles", ",".join(str(r) for r in org_ids))
            changes.append(("Org Roles", ", ".join(f"<@&{r}>" for r in org_ids)))

    if officer_roles is not None:
        if officer_roles.strip().lower() in ("", "none", "clear"):
            await set_config("officer_roles", "")
            changes.append(("Officer Roles", "*(cleared — officer tier disabled, admins only)*"))
        else:
            officer_ids = parse_role_ids(officer_roles)
            if not officer_ids:
                return await interaction.response.send_message(
                    "No valid officer role mentions or IDs found in `officer_roles`. Pass `none` to clear it.",
                    ephemeral=True
                )
            await set_config("officer_roles", ",".join(str(r) for r in officer_ids))
            changes.append(("Officer Roles", ", ".join(f"<@&{r}>" for r in officer_ids)))

    await update_leaderboard(bot)

    summary = "\n".join(f"{label}: {value}" for label, value in changes)
    await interaction.response.send_message(
        f"**FRCoiN** setup updated.\n{summary}",
        ephemeral=True
    )

    embed = audit_embed("FRCoiN Setup Run", discord.Colour.blue())
    embed.add_field(name="Run By", value=interaction.user.mention, inline=False)
    for label, value in changes:
        embed.add_field(name=label, value=value, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="start_event", description="Start a new DKP event")
@app_commands.describe(name="Name of the event")
async def start_event(interaction: discord.Interaction, name: str):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_officer(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id in active_events:
        return await interaction.response.send_message(
            "There's already an active event. Use `/cancel_event` to abort it first.", ephemeral=True
        )

    event = await create_event(guild_id, name)

    await interaction.response.send_message(
        f"Event **{name}** started (ID: `{event['id']}`).\n"
        f"Use `/snapshot_event` to capture attendees, then `/end_event` to allocate DKP.",
        ephemeral=True
    )

    embed = audit_embed("Event Started", discord.Colour.teal())
    embed.add_field(name="Event", value=name, inline=True)
    embed.add_field(name="Event ID", value=str(event["id"]), inline=True)
    embed.add_field(name="Started By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(
    name="snapshot_event",
    description="Capture voice channel members into the active event, with optional org-role filtering"
)
@app_commands.describe(
    org_only="If False, capture every voice member including non-org guests (default: True — org members only)"
)
async def snapshot_event(interaction: discord.Interaction, org_only: bool = True):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_officer(interaction.user):
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
    org_role_ids = await get_org_role_ids()

    if org_only:
        if not org_role_ids:
            return await interaction.response.send_message(
                "No org roles are configured. Set them with `/setup` first, or use `/snapshot_event org_only:False`.",
                ephemeral=True
            )
        members = [m for m in all_members if member_has_org_role(m, org_role_ids)]
        skipped = len(all_members) - len(members)
    else:
        members = all_members
        skipped = len([m for m in all_members if org_role_ids and not member_has_org_role(m, org_role_ids)])

    await take_event_attendance(active_event_id, members)

    filter_note = f" *(org_only disabled — capturing everyone, including {skipped} non-org member(s))*" if not org_only else ""
    await interaction.response.send_message(
        f"Captured **{len(members)} users** into event `{active_event["name"]}` (ID: {active_event_id}).{filter_note}", ephemeral=True
    )

    embed = audit_embed("Event Snapshot Taken", discord.Colour.teal())
    embed.add_field(name="Event", value=active_event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(active_event_id), inline=True)
    embed.add_field(name="Captured", value=f"{len(members)} users", inline=True)
    embed.add_field(name="Org-Only Filter", value="Yes (default)" if org_only else "No — everyone captured", inline=True)
    if org_only:
        embed.add_field(name="Skipped (non-org)", value=str(skipped), inline=True)
    else:
        embed.add_field(name="Non-Org Captured", value=str(skipped), inline=True)
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
    
    if not await is_officer(interaction.user):
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
    
    if not await is_officer(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id not in active_events:
        return await interaction.response.send_message("No active event to cancel.", ephemeral=True)

    event_id = active_events.pop(guild_id)
    await delete_event(event_id)

    await interaction.response.send_message("Active event cancelled. No DKP was awarded.", ephemeral=True)

    embed = audit_embed("Event Cancelled", discord.Colour.red())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Cancelled By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_add", description="Manually add a member to the active event's attendance list")
@app_commands.describe(member="The member to add to the current event")
async def event_add(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_officer(interaction.user):
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
        f"Added **{member.display_name}** to event `{active_event["name"]}` (ID: {active_event_id}).", ephemeral=True
    )

    embed = audit_embed("Attendee Added to Event", discord.Colour.green())
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
    
    if not await is_officer(interaction.user):
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
        f"Removed **{member.display_name}** from event `{active_event["name"]}` (ID: {active_event_id}).", ephemeral=True
    )

    embed = audit_embed("Attendee Removed from Event", discord.Colour.orange())
    embed.add_field(name="Event", value=active_event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(active_event_id), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Removed By", value=interaction.user.mention, inline=False)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="event_attendance", description="Show everyone currently on the attendance list for the active event")
async def event_attendance(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
    if not await is_officer(interaction.user):
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
        title=f"Attendance — Event `{active_event["name"]}` (ID: {active_event_id})",
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
    
    if not await is_officer(interaction.user):
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
        
    if not await is_officer(interaction.user):
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
        f"Export for **{event_name}** (ID: `{event_id}`) — {count} attendee(s).",
        file=discord.File(buffer, filename=filename),
        ephemeral=True
    )

    embed = audit_embed("Event Exported", discord.Colour.blurple())
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
        f"Event **{event_name}** (ID: `{event_id}`) and all its attendance records have been deleted.",
        ephemeral=True
    )

    embed = audit_embed("Event Deleted", discord.Colour.red())
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
    
    if not await is_officer(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    await interaction.response.send_modal(
        PointsModal(bot, event_mode=False, target_user=member, guild_id=interaction.guild_id, issued_by=interaction.user)
    )


@bot.tree.command(name="spend", description="Deduct DKP a member has spent (leave amount blank to reset to 0)")
@app_commands.describe(
    member="The member whose DKP should be spent",
    reason="Why this spend is happening (shown in the DKP log and audit channel)",
    amount="Amount to deduct (leave blank to reset the member's balance to 0)"
)
async def spend(interaction: discord.Interaction, member: discord.Member, reason: str, amount: int | None = None):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    if not await is_officer(interaction.user):
        return await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)

    if amount is not None and amount < 0:
        return await interaction.response.send_message(
            "Amount must be a positive number to deduct — use `/adjust` to add DKP.", ephemeral=True
        )

    current = await get_dkp(member.id)

    if amount is None:
        new_balance = 0
    else:
        new_balance = max(0, current - amount)

    deducted = current - new_balance
    clamped_note = ""
    if amount is not None and amount > current:
        clamped_note = f" (requested {amount}, but **{member.display_name}** only had {current} — balance reset to 0)"

    await set_dkp(member.id, new_balance, guild_id=interaction.guild_id, reason=reason, issued_by_id=interaction.user.id)
    await update_leaderboard(bot)
    await interaction.response.send_message(
        f"**{member.display_name}**'s DKP reduced by **{deducted}** → new balance **{new_balance}**.{clamped_note}",
        ephemeral=True
    )

    embed = audit_embed("DKP Spent", discord.Colour.red())
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Previous Balance", value=str(current), inline=True)
    embed.add_field(name="New Balance", value=str(new_balance), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Spent By", value=interaction.user.mention, inline=False)
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

    embed = audit_embed("Leaderboard Exported", discord.Colour.blurple())
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

    backup_buffer, backup_users = await build_dkp_xlsx(bot, interaction.guild)
    backup_embed = audit_embed("Pre-Import Backup", discord.Colour.dark_grey())
    backup_embed.description = "The leaderboard below is about to be **wiped and replaced** by an import. This is the pre-import snapshot."
    backup_embed.add_field(name="Triggered By", value=f"{interaction.user.mention} running /import_leaderboard", inline=False)
    backup_embed.add_field(name="Rows Backed Up", value=str(len(backup_users)), inline=True)
    backup_sent = await send_audit_with_file(
        interaction.guild, backup_embed,
        discord.File(backup_buffer, filename=f"pre-import-backup-{int(time.time())}.xlsx")
    )

    if not backup_sent:
        return await interaction.followup.send(
            "Import aborted — couldn't post the automatic pre-import backup (no audit channel configured, "
            "or the bot can't post there). Run `/setup` to set an audit channel first, then try again.",
            ephemeral=True
        )

    await import_users(rows_to_import)

    await update_leaderboard(bot)
    await interaction.followup.send(f"Imported **{len(rows_to_import)} users** successfully.", ephemeral=True)

    embed = audit_embed("Leaderboard Imported", discord.Colour.orange())
    embed.add_field(name="Imported By", value=interaction.user.mention, inline=False)
    embed.add_field(name="Rows Imported", value=str(len(rows_to_import)), inline=True)
    await send_audit(interaction.guild, embed)


@bot.tree.command(name="dkp", description="Check DKP balance — yours, or another member's (admin only)")
@app_commands.describe(member="Member to look up (leave blank to check your own balance)")
async def dkp(interaction: discord.Interaction, member: discord.Member | None = None):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    target = member or interaction.user

    if target != interaction.user:
        org_role_ids = await get_org_role_ids()
        is_org_member = bool(org_role_ids) and member_has_org_role(interaction.user, org_role_ids)
        if not await is_admin(interaction.user) and not is_org_member:
            return await interaction.response.send_message("You can only check your own DKP.", ephemeral=True)

    balance = await get_dkp(target.id)

    embed = discord.Embed(title="DKP Balance", colour=discord.Colour.gold())
    embed.add_field(name="Member", value=target.mention, inline=True)
    embed.add_field(name="Balance", value=str(balance), inline=True)
    embed.set_footer(text="FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dkp_log", description="View DKP transaction history — yours, or another member's (officer/admin/org-role only)")
@app_commands.describe(member="Member to look up (leave blank to view your own history)")
async def dkp_log(interaction: discord.Interaction, member: discord.Member | None = None):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    target = member or interaction.user

    if target != interaction.user:
        org_role_ids = await get_org_role_ids()
        is_org_member = bool(org_role_ids) and member_has_org_role(interaction.user, org_role_ids)
        if not await is_officer(interaction.user) and not is_org_member:
            return await interaction.response.send_message("You can only view your own DKP log.", ephemeral=True)

    entries = await get_dkp_log_entries(interaction.guild_id, target.id)

    if not entries:
        embed = discord.Embed(title=f"DKP Log — {target.display_name}", colour=discord.Colour.blurple())
        embed.description = "No DKP transactions recorded for this member."
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    view = DkpLogBrowserView(entries=entries, target=target, invoker=interaction.user)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="status", description="Show FRCoiN bot status and current configuration")
async def status(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

    if not await is_officer(interaction.user):
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
    officer_role_ids_raw = await get_config("officer_roles")
    org_role_ids_raw = await get_config("org_roles")

    lb_str = f"<#{lb_channel_id}>" if lb_channel_id else "Not set"
    audit_str = f"<#{audit_channel_id}>" if audit_channel_id else "Not set"

    if admin_role_ids:
        admin_str = ", ".join(f"<@&{r}>" for r in admin_role_ids.split(",") if r.strip())
    else:
        admin_str = "Not set"

    officer_str = ", ".join(f"<@&{r}>" for r in officer_role_ids_raw.split(",") if r.strip()) if officer_role_ids_raw else "*(disabled — admins only)*"

    org_str = ", ".join(f"<@&{r}>" for r in org_role_ids_raw.split(",") if r.strip()) if org_role_ids_raw else "*(all members)*"

    embed = discord.Embed(title="FRCoiN Status", colour=discord.Colour.blurple())
    embed.add_field(name="Active Event", value=event_str, inline=False)
    embed.add_field(name="Leaderboard Channel", value=lb_str, inline=True)
    embed.add_field(name="Audit Channel", value=audit_str, inline=True)
    embed.add_field(name="Admin Roles", value=admin_str, inline=False)
    embed.add_field(name="Officer Roles", value=officer_str, inline=False)
    embed.add_field(name="Org Roles (filter)", value=org_str, inline=False)
    embed.set_footer(text="FRCoiN")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------
# ERROR HANDLING
# ------------------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(
        "Unhandled app command error in /%s: %s",
        interaction.command.name if interaction.command else "?", error, exc_info=error
    )
    message = "Something went wrong running that command. The issue has been logged."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as e:
        logger.warning("Could not notify user of command error: %s", e)


# ------------------------
# READY
# ------------------------
@bot.event
async def on_ready():
    await init_db()
    await load_active_events()
    await bot.tree.sync()
    logger.info("FRCoiN logged in as %s", bot.user)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your bot token.")
bot.run(TOKEN)
