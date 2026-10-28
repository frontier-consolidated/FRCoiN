# ------------------------
# ACTIONS — shared by slash commands and panel buttons
# ------------------------
# Each action checks permissions, does the work, writes the audit embed and refreshes the panel.
# User-facing refusals are raised as ActionError; the caller shows the message ephemerally.

import io
import time
from typing import Union, cast
import openpyxl

import discord
from discord.ext import commands

import panel
from audit_logs import audit_embed, send_audit, send_audit_with_file
from db import add_dkp, get_config, get_dkp, import_users, set_config, set_dkp
from events import (
    OPEN_STATES, add_event_attendee, award_and_close_event, close_attendance_event, create_event, delete_event,
    event_lock, get_event_attendees, get_event_in_guild, get_open_events, mark_snapshot, remove_event_attendee,
    set_event_state, take_event_attendance,
)
from export import build_dkp_xlsx, build_event_xlsx
from helpers import collect_scope_members, get_org_role_ids, is_admin, is_officer, member_has_org_role, scope_voice_channels
from leaderboard import update_leaderboard
from logger import logger

NO_PERMISSION = "You don't have permission to do this."
STALE = "This event has already moved on — refresh the card or pick it again from the lobby."
NO_ORG_ROLES = "No org roles are configured, so nobody can earn DKP yet. Run `/setup org_roles:…` (or Admin → Setup) first."

KIND_LABELS = {"dkp": "DKP event", "attendance": "Attendance only"}

class ActionError(Exception):
    """A user-facing refusal. The message is shown ephemerally to whoever clicked or ran the command."""

async def require_officer(actor: discord.Member):
    if not await is_officer(actor):
        raise ActionError(NO_PERMISSION)

async def require_admin(actor: discord.Member):
    if not await is_admin(actor):
        raise ActionError(NO_PERMISSION)

def name_list(names: list[str], limit: int = 20) -> str:
    text = ", ".join(names[:limit])
    if len(names) > limit:
        text += f" … +{len(names) - limit} more"
    return text or "None"

def display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"Unknown ({user_id})"

def scope_label(guild: discord.Guild, scope_channel_ids: Union[list[int], None]) -> str:
    if not scope_channel_ids:
        return "Officer's current voice channel"
    labels = []
    for channel_id in scope_channel_ids:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.CategoryChannel):
            labels.append(f"{channel.name} (category — every voice channel in it)")
        elif channel:
            labels.append(channel.mention)
        else:
            labels.append(f"Deleted channel ({channel_id})")
    return ", ".join(labels)

# ------------------------
# EVENT LOOKUP
# ------------------------
async def get_open_event(guild: discord.Guild, event_id: int) -> dict:
    event = await get_event_in_guild(event_id, guild.id)
    if not event:
        raise ActionError(f"No event with ID `{event_id}` found for this server.")
    if event["state"] not in OPEN_STATES:
        raise ActionError(STALE)
    return event

async def resolve_event(guild: discord.Guild, event_id: Union[int, None]) -> dict:
    """For slash commands: the given event, or the only open one, or ask the user to pick."""
    if event_id is not None:
        return await get_open_event(guild, event_id)
    open_events = await get_open_events(guild.id)
    if not open_events:
        raise ActionError("No active event. Use `/start_event` or the panel's **Create Event** first.")
    if len(open_events) > 1:
        raise ActionError("Several events are open — pick one with the `event` option.")
    return open_events[0]

# ------------------------
# EVENT LIFECYCLE
# ------------------------
async def start_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, name: str,
                      kind: str = "dkp", scope: Union[list[discord.abc.GuildChannel], None] = None) -> tuple[dict, str]:
    """Returns the new event and a warning (empty if none) naming other open events whose scope shares channels with it."""
    await require_officer(actor)
    if kind == "dkp" and not await get_org_role_ids():
        raise ActionError(NO_ORG_ROLES)
    if any(not isinstance(c, (discord.CategoryChannel, discord.VoiceChannel, discord.StageChannel)) for c in scope or []):
        raise ActionError("The scope must be categories or voice channels.")
    scope_ids = list(dict.fromkeys(c.id for c in scope or []))

    # Someone in a shared channel would be captured by both events, so tell the officer up front.
    overlap_warning = ""
    if scope_ids:
        new_channels = {c.id for c in scope_voice_channels(guild, scope_ids)}
        shared = []
        for other in await get_open_events(guild.id):
            common = new_channels & {c.id for c in scope_voice_channels(guild, other["scope_channel_ids"])}
            if common:
                shared.append(f"**{other['name']}** ({', '.join(f'<#{i}>' for i in common)})")
        if shared:
            overlap_warning = ("⚠️ This event shares voice channels with " + "; ".join(shared)
                               + ". Anyone in a shared channel will be captured by more than one event.")

    event = await create_event(guild.id, name, kind, actor.id, scope_ids)

    embed = audit_embed("Event Started", discord.Colour.teal())
    embed.add_field(name="Event", value=name, inline=True)
    embed.add_field(name="Event ID", value=str(event["id"]), inline=True)
    embed.add_field(name="Type", value=KIND_LABELS[kind], inline=True)
    embed.add_field(name="Scope", value=scope_label(guild, event["scope_channel_ids"]), inline=False)
    embed.add_field(name="Started By", value=actor.mention, inline=False)
    await send_audit(guild, embed)

    await panel.upsert_card(bot, guild, event["id"])
    await panel.refresh_lobby(bot, guild)
    return event, overlap_warning

async def snapshot_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int) -> str:
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        if event["state"] != "open":
            raise ActionError("Points have already been entered for this event — press **Reopen** to snapshot again.")

        collected = collect_scope_members(guild, event["scope_channel_ids"], actor)
        if collected is None:
            if event["scope_channel_ids"]:
                raise ActionError("This event's voice scope no longer exists. Join a voice channel and use `/snapshot_event`, or start a new event.")
            raise ActionError("This event has no voice scope, so you must be in a voice channel to snapshot.")
        in_voice, breakdown = collected

        if event["kind"] == "dkp":
            org_role_ids = await get_org_role_ids()
            if not org_role_ids:
                raise ActionError(NO_ORG_ROLES)
            captured = [m for m in in_voice if member_has_org_role(m, org_role_ids)]
        else:
            captured = in_voice

        before = {r["user_id"] for r in await get_event_attendees(event_id)}
        await take_event_attendance(event_id, captured)

        # Additive: nobody is removed. Officers see who is new and who has left voice, and remove by hand.
        new = [m for m in captured if m.id not in before]
        voice_ids = {m.id for m in in_voice}
        left = sorted(uid for uid in before if uid not in voice_ids)
        skipped = len(in_voice) - len(captured)
        await mark_snapshot(event_id, {
            "channels": breakdown, "new": [m.id for m in new], "left": left, "skipped": skipped,
        })

    await panel.upsert_card(bot, guild, event_id)

    breakdown_str = " · ".join(f"{name} {count}" for name, count in breakdown.items()) or "no voice channels"
    embed = audit_embed("Event Snapshot Taken", discord.Colour.teal())
    embed.add_field(name="Event", value=event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Captured", value=f"{len(captured)} users ({len(new)} new)", inline=True)
    embed.add_field(name="Channels", value=breakdown_str, inline=False)
    if event["kind"] == "dkp":
        embed.add_field(name="Skipped (non-org)", value=str(skipped), inline=True)
    embed.add_field(name="By", value=actor.mention, inline=False)
    embed.add_field(name="Members", value=name_list([m.display_name for m in captured]), inline=False)
    await send_audit(guild, embed)

    skipped_note = f" Skipped **{skipped}** non-org member(s)." if event["kind"] == "dkp" and skipped else ""
    return (f"Captured **{len(captured)}** from voice ({breakdown_str}) into **{event['name']}** — "
            f"**{len(new)} new**, {len(left)} on the list no longer in voice.{skipped_note}")

async def add_attendee(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int, member: discord.Member) -> str:
    await require_officer(actor)
    if member.bot:
        raise ActionError("You can't add a bot to an event.")
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        if event["state"] != "open":
            raise ActionError("Points have already been entered for this event — press **Reopen** to change attendees.")
        if event["kind"] == "dkp":
            org_role_ids = await get_org_role_ids()
            if not org_role_ids:
                raise ActionError(NO_ORG_ROLES)
            if not member_has_org_role(member, org_role_ids):
                raise ActionError(
                    f"**{member.display_name}** isn't an org member, and only org members can earn DKP. "
                    "Use an attendance event to record Community Members."
                )
        result = await add_event_attendee(event_id, member)

    if result == "INSERT 0 0":
        raise ActionError(f"**{member.display_name}** is already on the list for **{event['name']}**.")

    await panel.upsert_card(bot, guild, event_id)

    embed = audit_embed("Attendee Added to Event", discord.Colour.green())
    embed.add_field(name="Event", value=event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Added By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"Added **{member.display_name}** to **{event['name']}** (ID: {event_id})."

async def remove_attendee(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int,
                          member: Union[discord.Member, discord.User]) -> str:
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        if event["state"] != "open":
            raise ActionError("Points have already been entered for this event — press **Reopen** to change attendees.")
        result = await remove_event_attendee(event_id, cast(discord.Member, member))

    if result == "DELETE 0":
        raise ActionError(f"**{member.display_name}** wasn't on the list for **{event['name']}**.")

    await panel.upsert_card(bot, guild, event_id)

    embed = audit_embed("Attendee Removed from Event", discord.Colour.orange())
    embed.add_field(name="Event", value=event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Removed By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"Removed **{member.display_name}** from **{event['name']}** (ID: {event_id})."

async def cancel_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int) -> str:
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        await panel.delete_card(bot, event)
        await delete_event(event_id)

    await panel.refresh_lobby(bot, guild)

    embed = audit_embed("Event Cancelled", discord.Colour.red())
    embed.add_field(name="Event", value=event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Cancelled By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"**{event['name']}** cancelled. No DKP was awarded."

async def set_points_pending(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int, points: int) -> str:
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        if event["kind"] != "dkp":
            raise ActionError("Attendance events don't award points — just press **Close**.")
        if not await get_event_attendees(event_id):
            raise ActionError("No attendees recorded for this event. Take a snapshot or add members first.")
        await set_event_state(event_id, "pending_close", points)

    await panel.upsert_card(bot, guild, event_id)
    await panel.refresh_lobby(bot, guild)
    return f"**{points:+} DKP** is pending for **{event['name']}**. Nothing is awarded until someone presses **Close event**."

async def reopen_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int) -> str:
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        if event["state"] != "pending_close":
            raise ActionError(STALE)
        await set_event_state(event_id, "open", None)

    await panel.upsert_card(bot, guild, event_id)
    await panel.refresh_lobby(bot, guild)
    return f"**{event['name']}** reopened. Pending points were cleared."

async def close_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int,
                      points: Union[int, None] = None) -> str:
    """
    DKP events: award `points` (or the pending points) to attendees who are *still* org members, in one transaction.
    Attendance events: archive only. Either way: audit, DM the creator, delete the card.
    """
    await require_officer(actor)
    async with event_lock(event_id):
        event = await get_open_event(guild, event_id)
        attendee_ids = [r["user_id"] for r in await get_event_attendees(event_id)]
        skipped_ids: list[int] = []

        if event["kind"] == "attendance":
            if not await close_attendance_event(event_id):
                raise ActionError(STALE)
        else:
            points = points if points is not None else event["pending_points"]
            if points is None:
                raise ActionError("No points entered yet — press **End event** first.")
            if not attendee_ids:
                raise ActionError("No attendees recorded for this event. Take a snapshot or add members first.")
            org_role_ids = await get_org_role_ids()
            if not org_role_ids:
                raise ActionError(NO_ORG_ROLES)

            recipients = []
            for uid in attendee_ids:
                member = guild.get_member(uid)
                if member and member_has_org_role(member, org_role_ids):
                    recipients.append(uid)
                else:
                    skipped_ids.append(uid)

            if not await award_and_close_event(event_id, points, guild.id, actor.id, recipients):
                raise ActionError(STALE)

    if event["kind"] == "dkp":
        await update_leaderboard(bot)
        embed = audit_embed("Event DKP Awarded", discord.Colour.green())
    else:
        embed = audit_embed("Attendance Event Closed", discord.Colour.blue())
    embed.add_field(name="Event", value=event["name"], inline=True)
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Started", value=discord.utils.format_dt(event["created_at"], "f"), inline=True)
    if event["kind"] == "dkp":
        embed.add_field(name="Points", value=f"{points:+}", inline=True)
        embed.add_field(name="Recipients", value=str(len(attendee_ids) - len(skipped_ids)), inline=True)
        if skipped_ids:
            embed.add_field(name="Skipped (no longer org members)",
                            value=name_list([display_name(guild, uid) for uid in skipped_ids]), inline=False)
    else:
        org_role_ids = await get_org_role_ids()
        members = sum(1 for uid in attendee_ids if (m := guild.get_member(uid)) and member_has_org_role(m, org_role_ids))
        embed.add_field(name="Attendees", value=f"{len(attendee_ids)} ({members} members · {panel.community_count(len(attendee_ids) - members)})", inline=True)
    embed.add_field(name="Attendee List", value=name_list([display_name(guild, uid) for uid in attendee_ids], limit=40)[:1024], inline=False)
    embed.add_field(name="Closed By", value=actor.mention, inline=False)

    if not await dm_creator(bot, event, embed):
        embed.add_field(name="Note", value="DM to creator failed", inline=False)
    await send_audit(guild, embed)

    await panel.delete_card(bot, event)
    await panel.refresh_lobby(bot, guild)

    if event["kind"] == "dkp":
        skipped_note = f" Skipped **{len(skipped_ids)}** who are no longer org members." if skipped_ids else ""
        return f"Awarded **{points:+} DKP** to **{len(attendee_ids) - len(skipped_ids)} users** for **{event['name']}**.{skipped_note}"
    return f"**{event['name']}** closed with **{len(attendee_ids)}** attendees recorded."

async def dm_creator(bot: commands.Bot, event: dict, embed: discord.Embed) -> bool:
    """DM the event's creator a copy of the close summary. True if sent (or there's no creator to DM)."""
    if not event["created_by_id"]:
        return True
    try:
        user = bot.get_user(event["created_by_id"]) or await bot.fetch_user(event["created_by_id"])
        await user.send(content=f"Your event **{event['name']}** has been closed.", embed=embed)
        return True
    except discord.HTTPException as e:
        logger.warning("Could not DM event creator %s: %s", event["created_by_id"], e)
        return False

async def attendance_embed(guild: discord.Guild, event: dict) -> discord.Embed:
    rows = await get_event_attendees(event["id"])
    org_role_ids = await get_org_role_ids()
    lines = []
    for row in rows:
        member = guild.get_member(row["user_id"])
        mention = member.mention if member else f"Unknown ({row['user_id']})"
        if event["kind"] == "attendance":
            tag = "Member" if member and member_has_org_role(member, org_role_ids) else "Community Member"
            lines.append(f"• {mention} — {tag}")
        else:
            lines.append(f"• {mention}")

    embed = discord.Embed(
        title=f"Attendance — {event['name']} (ID: {event['id']})",
        description="\n".join(lines)[:4096] if lines else "*No attendees yet. Take a snapshot or add members.*",
        colour=discord.Colour.teal()
    )
    embed.set_footer(text=f"{len(rows)} attendee(s) • FRCoiN")
    return embed

# ------------------------
# DKP ADJUSTMENTS
# ------------------------
async def adjust(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, member: discord.Member,
                 amount: int, reason: str) -> str:
    await require_officer(actor)
    if amount > 0:
        org_role_ids = await get_org_role_ids()
        if not org_role_ids:
            raise ActionError(NO_ORG_ROLES)
        if not member_has_org_role(member, org_role_ids):
            raise ActionError(f"**{member.display_name}** isn't an org member, so they can't be given DKP. Negative adjustments and spends still work.")

    await add_dkp(member.id, amount, guild_id=guild.id, reason=f"manual_adjust: {reason}", issued_by_id=actor.id)
    await update_leaderboard(bot)

    embed = audit_embed("DKP Manually Adjusted", discord.Colour.orange())
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Adjustment", value=f"{amount:+}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Issued By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"Updated **{member.display_name}** by **{amount:+} DKP**."

async def spend(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, member: Union[discord.Member, discord.User],
                reason: str, amount: Union[int, None] = None) -> str:
    await require_officer(actor)
    if amount is not None and amount < 0:
        raise ActionError("Amount must be a positive number to deduct — use Add/Remove points (or `/adjust`) to add DKP.")

    current = await get_dkp(member.id)
    new_balance = 0 if amount is None else max(0, current - amount)
    deducted = current - new_balance
    clamped_note = ""
    if amount is not None and amount > current:
        clamped_note = f" (requested {amount}, but **{member.display_name}** only had {current} — balance reset to 0)"

    await set_dkp(member.id, new_balance, guild_id=guild.id, reason=reason, issued_by_id=actor.id)
    await update_leaderboard(bot)

    embed = audit_embed("DKP Spent", discord.Colour.red())
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Previous Balance", value=str(current), inline=True)
    embed.add_field(name="New Balance", value=str(new_balance), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Spent By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"**{member.display_name}**'s DKP reduced by **{deducted}** → new balance **{new_balance}**.{clamped_note}"

# ------------------------
# SAVED EVENTS & LEADERBOARD FILES
# ------------------------
async def export_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int) -> tuple[discord.File, str]:
    await require_officer(actor)
    if not await get_event_in_guild(event_id, guild.id):
        raise ActionError(f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.")

    buffer, event_name, count = await build_event_xlsx(bot, event_id, guild)
    safe_name = "".join(c for c in event_name if c.isalnum() or c in " _-").strip().replace(" ", "_")
    filename = f"event_{event_id}_{safe_name}-{int(time.time())}.xlsx"

    embed = audit_embed("Event Exported", discord.Colour.blurple())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Event Name", value=event_name, inline=True)
    embed.add_field(name="Attendees", value=str(count), inline=True)
    embed.add_field(name="Exported By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return discord.File(buffer, filename=filename), f"Export for **{event_name}** (ID: `{event_id}`) — {count} attendee(s)."

async def delete_saved_event(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, event_id: int) -> str:
    await require_admin(actor)
    event = await get_event_in_guild(event_id, guild.id)
    if not event:
        raise ActionError(f"No event with ID `{event_id}` found for this server. Use `/events_saved` to browse.")
    if event["state"] in OPEN_STATES:
        raise ActionError(f"Event `{event_id}` is still **open**. Cancel it from its card (or `/cancel_event`) first.")

    await delete_event(event_id)

    embed = audit_embed("Event Deleted", discord.Colour.red())
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Event Name", value=event["name"], inline=True)
    embed.add_field(name="Deleted By", value=actor.mention, inline=False)
    await send_audit(guild, embed)
    return f"Event **{event['name']}** (ID: `{event_id}`) and all its attendance records have been deleted."

async def export_leaderboard(bot: commands.Bot, guild: discord.Guild, actor: discord.Member) -> discord.File:
    await require_admin(actor)
    buffer, users = await build_dkp_xlsx(bot, guild)

    embed = audit_embed("Leaderboard Exported", discord.Colour.blurple())
    embed.add_field(name="Exported By", value=actor.mention, inline=False)
    embed.add_field(name="Rows", value=str(len(users)), inline=True)
    await send_audit(guild, embed)
    return discord.File(buffer, filename=f"leaderboard-{int(time.time())}.xlsx")

async def import_leaderboard(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, filename: str, file_bytes: bytes) -> str:
    await require_admin(actor)
    if not filename.endswith(".xlsx"):
        raise ActionError("Please upload a valid `.xlsx` file.")

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws = wb.active
    except Exception as e:
        logger.error("Failed to parse uploaded workbook: %s", e)
        raise ActionError("Could not read the file — make sure it's a valid `.xlsx` export.")
    if not ws:
        raise ActionError("That workbook has no active sheet.")

    rows_to_import = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        try:
            rows_to_import.append((int(cast(str, row[2])), int(cast(str, row[3]))))
        except (TypeError, ValueError, IndexError):
            continue
    if not rows_to_import:
        raise ActionError("No valid rows found. Expected columns: Rank, User, UserID, DKP.")

    backup_buffer, backup_users = await build_dkp_xlsx(bot, guild)
    backup_embed = audit_embed("Pre-Import Backup", discord.Colour.dark_grey())
    backup_embed.description = "The leaderboard below is about to be **wiped and replaced** by an import. This is the pre-import snapshot."
    backup_embed.add_field(name="Triggered By", value=f"{actor.mention} running an import", inline=False)
    backup_embed.add_field(name="Rows Backed Up", value=str(len(backup_users)), inline=True)
    backup_sent = await send_audit_with_file(
        guild, backup_embed, discord.File(backup_buffer, filename=f"pre-import-backup-{int(time.time())}.xlsx")
    )
    if not backup_sent:
        raise ActionError(
            "Import aborted — couldn't post the automatic pre-import backup (no audit channel configured, "
            "or the bot can't post there). Set an audit channel first, then try again."
        )

    await import_users(rows_to_import)
    await update_leaderboard(bot)

    embed = audit_embed("Leaderboard Imported", discord.Colour.orange())
    embed.add_field(name="Imported By", value=actor.mention, inline=False)
    embed.add_field(name="Rows Imported", value=str(len(rows_to_import)), inline=True)
    await send_audit(guild, embed)
    return f"Imported **{len(rows_to_import)} users** successfully."

# ------------------------
# SETUP
# ------------------------
SETUP_LABELS = {
    "leaderboard_channel": "Leaderboard Channel",
    "audit_channel": "Audit Channel",
    "panel_channel": "Panel Channel",
    "admin_roles": "Admin Roles",
    "officer_roles": "Officer Roles",
    "org_roles": "Org Roles",
    "slash_access": "Slash Commands",
}

async def apply_setup(bot: commands.Bot, guild: discord.Guild, actor: discord.Member, changes: dict) -> str:
    """
    `changes` holds only the settings to change: channels as channel objects, roles as lists of role IDs
    ([] clears officer/org roles), slash_access as 'officer' or 'admin'. Needs the Discord Administrator permission.
    """
    if not actor.guild_permissions.administrator:
        raise ActionError("You need Administrator permission to run setup.")
    if not changes:
        raise ActionError("Nothing to change.")
    if "admin_roles" in changes and not changes["admin_roles"]:
        raise ActionError("Admin roles can't be empty — at least one role must be able to administer FRCoiN.")

    old_panel_channel = await get_config("panel_channel")
    summary = []
    for key, value in changes.items():
        if key.endswith("_channel"):
            await set_config(key, value.id)
            shown = value.mention
        elif key.endswith("_roles"):
            await set_config(key, ",".join(str(r) for r in value))
            if value:
                shown = ", ".join(f"<@&{r}>" for r in value)
            elif key == "officer_roles":
                shown = "*(cleared — officer tier disabled, admins only)*"
            else:
                shown = "*(cleared — nobody can earn DKP until org roles are set)*"
        else:
            await set_config(key, value)
            shown = "Admin-only" if value == "admin" else "Officer+"
        summary.append((SETUP_LABELS[key], shown))

    await update_leaderboard(bot)

    warning = None
    if "panel_channel" in changes and str(changes["panel_channel"].id) != old_panel_channel:
        await panel.move_panel(bot, guild, old_panel_channel)
    if {"panel_channel", "admin_roles", "officer_roles"} & changes.keys():
        warning = await panel.sync_panel_permissions(guild)

    embed = audit_embed("FRCoiN Setup Run", discord.Colour.blue())
    embed.add_field(name="Run By", value=actor.mention, inline=False)
    for label, value in summary:
        embed.add_field(name=label, value=value, inline=False)
    if warning:
        embed.add_field(name="Warning", value=warning, inline=False)
    await send_audit(guild, embed)

    text = "**FRCoiN** setup updated.\n" + "\n".join(f"{label}: {value}" for label, value in summary)
    if warning:
        text += f"\n⚠️ {warning}"
    return text

async def status_embed(guild: discord.Guild) -> discord.Embed:
    open_events = await get_open_events(guild.id)
    if open_events:
        events_str = "\n".join(
            f"`{e['id']}` [{'DKP' if e['kind'] == 'dkp' else 'ATT'}] **{e['name']}**"
            f"{' — points pending' if e['state'] == 'pending_close' else ''}"
            for e in open_events[:15]
        )
    else:
        events_str = "None"

    def channel_str(value):
        return f"<#{value}>" if value else "Not set"

    def roles_str(value, empty):
        return ", ".join(f"<@&{r}>" for r in value.split(",") if r.strip()) if value else empty

    embed = discord.Embed(title="FRCoiN Status", colour=discord.Colour.blurple())
    embed.add_field(name=f"Open Events ({len(open_events)})", value=events_str, inline=False)
    embed.add_field(name="Panel Channel", value=channel_str(await get_config("panel_channel")), inline=True)
    embed.add_field(name="Leaderboard Channel", value=channel_str(await get_config("leaderboard_channel")), inline=True)
    embed.add_field(name="Audit Channel", value=channel_str(await get_config("audit_channel")), inline=True)
    embed.add_field(name="Admin Roles", value=roles_str(await get_config("admin_roles"), "Not set"), inline=False)
    embed.add_field(name="Officer Roles", value=roles_str(await get_config("officer_roles"), "*(disabled — admins only)*"), inline=False)
    embed.add_field(name="Org Roles (can earn DKP)", value=roles_str(await get_config("org_roles"), "*(not set — DKP events are blocked)*"), inline=False)
    embed.add_field(name="Slash Commands", value="Admin-only" if await get_config("slash_access") == "admin" else "Officer+", inline=True)
    problems = await panel.lockdown_problems(guild)
    if problems:
        embed.add_field(name="⚠️ Panel channel not locked down", value="\n".join(f"• {p}" for p in problems)[:1024], inline=False)
    embed.set_footer(text="FRCoiN")
    return embed
