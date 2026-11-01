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
#   Manage Roles — only used to lock the panel channel to Officers+;
#                  the bot never changes anyone's roles
#
# Required privileged intent: Server Members
# ─────────────────────────────────────────────

import os
from typing import Union
from dotenv import load_dotenv

import discord
from discord.ext import commands
from discord import app_commands

import actions
import panel
from db import get_config, get_dkp, get_dkp_log_entries, init_db
from events import get_completed_events, get_open_events
from helpers import get_org_role_ids, is_admin, is_officer, member_has_org_role, parse_role_ids, slash_allowed
from leaderboard import update_leaderboard
from modals.points_modal import PointsModal
from views.adjust_panel import AdjustPanel
from views.adjustment_card import AdjustmentCardButton
from views.admin_panel import AdminPanel
from views.common import reply, run_action
from views.event_card import EventCardButton
from views.events_browser_view import EventsBrowserView
from views.dkp_log_browser_view import DkpLogBrowserView
from views.leaderboard_view import LeaderboardView
from views.lobby_view import LobbyView
from logger import logger

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# ------------------------
# SLASH COMMAND HELPERS
# ------------------------
async def officer_context(interaction: discord.Interaction) -> Union[tuple[discord.Guild, discord.Member], None]:
    """Guild + member for an officer-tier slash command, or None after telling the user why not."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await reply(interaction, "This command can only be used in a server.")
        return None
    if not await slash_allowed(interaction.user):
        if await is_officer(interaction.user):
            panel_channel = await get_config("panel_channel")
            where = f" in <#{panel_channel}>" if panel_channel else ""
            await reply(interaction, f"This is now done from the panel{where}.")
        else:
            await reply(interaction, actions.NO_PERMISSION)
        return None
    return interaction.guild, interaction.user

def guild_context(interaction: discord.Interaction) -> Union[tuple[discord.Guild, discord.Member], None]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return None
    return interaction.guild, interaction.user

async def open_event_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    if not interaction.guild_id:
        return []
    choices = []
    for e in await get_open_events(interaction.guild_id):
        label = f"[{'DKP' if e['kind'] == 'dkp' else 'ATT'}] {e['name']} (ID {e['id']})"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=e["id"]))
    return choices[:25]

async def run_and_reply(interaction: discord.Interaction, action):
    message = await run_action(interaction, action)
    if message:
        await reply(interaction, message)

EVENT_OPTION = "The open event (only needed when more than one is open)"

# ------------------------
# SLASH COMMANDS
# ------------------------

@bot.tree.command(name="setup", description="Configure FRCoiN — update only the options you pass, everything else stays as-is")
@app_commands.describe(
    leaderboard_channel="Channel where the leaderboard message will live (leave blank to keep current)",
    admin_roles="Role mentions or role IDs separated by commas, full bot admins (leave blank to keep current)",
    audit_channel="Channel where all bot actions will be logged (leave blank to keep current)",
    org_roles="Role mentions or IDs for org members — only they can earn DKP (leave blank to keep current; 'none' to clear)",
    officer_roles="Role mentions or IDs for officers, day-to-day ops below admin (leave blank to keep current; 'none' to clear)",
    panel_channel="Officers+ only channel for the button panel — the bot locks it down (leave blank to keep current)",
    slash_access="Who can use the officer slash commands (leave blank to keep current)"
)
@app_commands.choices(slash_access=[
    app_commands.Choice(name="Officer+ (default)", value="officer"),
    app_commands.Choice(name="Admin-only (officers use the panel)", value="admin"),
])
async def setup(
    interaction: discord.Interaction,
    leaderboard_channel: discord.TextChannel | None = None,
    admin_roles: str | None = None,
    audit_channel: discord.TextChannel | None = None,
    org_roles: str | None = None,
    officer_roles: str | None = None,
    panel_channel: discord.TextChannel | None = None,
    slash_access: app_commands.Choice[str] | None = None
):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    guild, actor = ctx

    changes: dict = {}
    if leaderboard_channel is not None:
        changes["leaderboard_channel"] = leaderboard_channel
    if audit_channel is not None:
        changes["audit_channel"] = audit_channel
    if panel_channel is not None:
        changes["panel_channel"] = panel_channel
    if slash_access is not None:
        changes["slash_access"] = slash_access.value

    for key, raw, clearable in (("admin_roles", admin_roles, False), ("org_roles", org_roles, True), ("officer_roles", officer_roles, True)):
        if raw is None:
            continue
        if clearable and raw.strip().lower() in ("", "none", "clear"):
            changes[key] = []
            continue
        role_ids = parse_role_ids(raw)
        if not role_ids:
            hint = " Pass `none` to clear it." if clearable else ""
            return await reply(interaction, f"No valid role mentions or IDs found in `{key}`. Separate multiple roles with commas.{hint}")
        changes[key] = role_ids

    if not changes:
        return await reply(
            interaction,
            "You didn't pass any options to change. Provide at least one of: `leaderboard_channel`, `admin_roles`, "
            "`audit_channel`, `org_roles`, `officer_roles`, `panel_channel`, `slash_access`."
        )

    await run_and_reply(interaction, lambda: actions.apply_setup(bot, guild, actor, changes))


@bot.tree.command(name="start_event", description="Start a new DKP or attendance event")
@app_commands.describe(
    name="Name of the event",
    kind="DKP event (org members only, earns points) or attendance only (everyone, no points)",
    scope="Category or voice channel (pick several via the panel). Blank = your voice channel at each snapshot"
)
@app_commands.rename(kind="type")
@app_commands.choices(kind=[
    app_commands.Choice(name="DKP event", value="dkp"),
    app_commands.Choice(name="Attendance only", value="attendance"),
])
async def start_event(
    interaction: discord.Interaction,
    name: app_commands.Range[str, 1, 100],
    kind: app_commands.Choice[str] | None = None,
    scope: discord.CategoryChannel | discord.VoiceChannel | discord.StageChannel | None = None
):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx
    kind_value = kind.value if kind else "dkp"

    async def start():
        event, overlap_warning = await actions.start_event(bot, guild, actor, name, kind_value, [scope] if scope else None)
        return (f"Event **{name}** started (ID: `{event['id']}`).\n"
                f"Use `/snapshot_event` (or the card in the panel) to capture attendees, then `/end_event`."
                + (f"\n{overlap_warning}" if overlap_warning else ""))

    await run_and_reply(interaction, start)


@bot.tree.command(name="snapshot_event", description="Capture everyone in the event's voice scope into its attendance list")
@app_commands.describe(event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def snapshot_event(interaction: discord.Interaction, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    async def snapshot():
        resolved = await actions.resolve_event(guild, event)
        return await actions.snapshot_event(bot, guild, actor, resolved["id"])

    await run_and_reply(interaction, snapshot)


@bot.tree.command(name="end_event", description="End an event and allocate DKP to all attendees immediately")
@app_commands.describe(event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def end_event(interaction: discord.Interaction, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    resolved = await run_action(interaction, lambda: actions.resolve_event(guild, event), defer=False)
    if not resolved:
        return
    if resolved["kind"] == "attendance":
        return await run_and_reply(interaction, lambda: actions.close_event(bot, guild, actor, resolved["id"]))
    await interaction.response.send_modal(PointsModal(bot, event_id=resolved["id"], pending=False))


@bot.tree.command(name="cancel_event", description="Cancel an event without awarding DKP")
@app_commands.describe(event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def cancel_event(interaction: discord.Interaction, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    async def cancel():
        resolved = await actions.resolve_event(guild, event)
        return await actions.cancel_event(bot, guild, actor, resolved["id"])

    await run_and_reply(interaction, cancel)


@bot.tree.command(name="event_add", description="Manually add a member to an event's attendance list")
@app_commands.describe(member="The member to add", event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def event_add(interaction: discord.Interaction, member: discord.Member, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    async def add():
        resolved = await actions.resolve_event(guild, event)
        return await actions.add_attendee(bot, guild, actor, resolved["id"], member)

    await run_and_reply(interaction, add)


@bot.tree.command(name="event_remove", description="Manually remove a member from an event's attendance list")
@app_commands.describe(member="The member to remove", event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def event_remove(interaction: discord.Interaction, member: discord.Member, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    async def remove():
        resolved = await actions.resolve_event(guild, event)
        return await actions.remove_attendee(bot, guild, actor, resolved["id"], member)

    await run_and_reply(interaction, remove)


@bot.tree.command(name="event_attendance", description="Show everyone currently on an event's attendance list")
@app_commands.describe(event=EVENT_OPTION)
@app_commands.autocomplete(event=open_event_autocomplete)
async def event_attendance(interaction: discord.Interaction, event: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, _ = ctx

    async def show():
        return await actions.attendance_embed(guild, await actions.resolve_event(guild, event))

    embed = await run_action(interaction, show)
    if embed:
        await reply(interaction, embed=embed)


@bot.tree.command(name="events_saved", description="Browse all saved events for this server")
async def events_saved(interaction: discord.Interaction):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    events = await get_completed_events(guild.id)
    if not events:
        return await reply(interaction, "No events have been saved for this server yet.")

    view = EventsBrowserView(events=events, guild=guild, invoker=actor)
    await reply(interaction, embed=view.build_embed(), view=view)


@bot.tree.command(name="event_export", description="Export the attendance list for a specific saved event as an Excel file")
@app_commands.describe(event_id="The event ID (find it with /events_saved)")
async def event_export(interaction: discord.Interaction, event_id: int):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx

    result = await run_action(interaction, lambda: actions.export_event(bot, guild, actor, event_id))
    if result:
        file, message = result
        await reply(interaction, message, file=file)


@bot.tree.command(name="event_delete", description="Permanently delete a saved event and its attendance records")
@app_commands.describe(event_id="The event ID to delete (find it with /events_saved)")
async def event_delete(interaction: discord.Interaction, event_id: int):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    guild, actor = ctx
    await run_and_reply(interaction, lambda: actions.delete_saved_event(bot, guild, actor, event_id))


# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="adjust", description="Manually adjust DKP for a specific member")
@app_commands.describe(member="The member to adjust DKP for")
async def adjust(interaction: discord.Interaction, member: discord.Member):
    if not await officer_context(interaction):
        return
    await interaction.response.send_modal(PointsModal(bot, target_user=member))


@bot.tree.command(name="spend", description="Deduct DKP a member has spent (leave amount blank to reset to 0)")
@app_commands.describe(
    member="The member whose DKP should be spent",
    reason="Why this spend is happening (shown in the DKP log and audit channel)",
    amount="Amount to deduct (leave blank to reset the member's balance to 0)"
)
async def spend(interaction: discord.Interaction, member: discord.Member, reason: str, amount: int | None = None):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    guild, actor = ctx
    await run_and_reply(interaction, lambda: actions.spend(bot, guild, actor, member, reason, amount))


@bot.tree.command(name="export_leaderboard", description="Export the full DKP leaderboard as an Excel file")
async def export_leaderboard(interaction: discord.Interaction):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    guild, actor = ctx
    file = await run_action(interaction, lambda: actions.export_leaderboard(bot, guild, actor))
    if file:
        await reply(interaction, file=file)


@bot.tree.command(name="import_leaderboard", description="Import DKP data from an Excel file")
@app_commands.describe(file="An .xlsx file exported from FRCoiN")
async def import_leaderboard(interaction: discord.Interaction, file: discord.Attachment):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    guild, actor = ctx

    async def do_import():
        return await actions.import_leaderboard(bot, guild, actor, file.filename, await file.read())

    await run_and_reply(interaction, do_import)


async def member_lookup_allowed(interaction: discord.Interaction, target: discord.Member, officer_tier: bool) -> bool:
    """Shared /dkp and /dkp_log access rules. Replies with the refusal and returns False if not allowed."""
    user = interaction.user
    assert isinstance(user, discord.Member)
    if await get_config("slash_access") == "admin" and not await is_admin(user):
        lb_channel = await get_config("leaderboard_channel")
        where = f" in <#{lb_channel}>" if lb_channel else ""
        await reply(interaction, f"Use the **My Points** button under the leaderboard{where} to see your balance and history.")
        return False
    if target != user:
        org_role_ids = await get_org_role_ids()
        is_org_member = bool(org_role_ids) and member_has_org_role(user, org_role_ids)
        allowed = await is_officer(user) if officer_tier else await is_admin(user)
        if not allowed and not is_org_member:
            await reply(interaction, "You can only check your own DKP." if not officer_tier else "You can only view your own DKP log.")
            return False
    return True


@bot.tree.command(name="dkp", description="Check DKP balance — yours, or another member's (admin only)")
@app_commands.describe(member="Member to look up (leave blank to check your own balance)")
async def dkp(interaction: discord.Interaction, member: discord.Member | None = None):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    target = member or ctx[1]
    if not await member_lookup_allowed(interaction, target, officer_tier=False):
        return

    balance = await get_dkp(target.id)

    embed = discord.Embed(title="DKP Balance", colour=discord.Colour.gold())
    embed.add_field(name="Member", value=target.mention, inline=True)
    embed.add_field(name="Balance", value=str(balance), inline=True)
    embed.set_footer(text="FRCoiN")
    await reply(interaction, embed=embed)


@bot.tree.command(name="dkp_log", description="View DKP transaction history — yours, or another member's (officer/admin/org-role only)")
@app_commands.describe(member="Member to look up (leave blank to view your own history)")
async def dkp_log(interaction: discord.Interaction, member: discord.Member | None = None):
    ctx = guild_context(interaction)
    if not ctx:
        return await reply(interaction, "This command can only be used in a server.")
    guild, user = ctx
    target = member or user
    if not await member_lookup_allowed(interaction, target, officer_tier=True):
        return

    entries = await get_dkp_log_entries(guild.id, target.id)

    if not entries:
        embed = discord.Embed(title=f"DKP Log — {target.display_name}", colour=discord.Colour.blurple())
        embed.description = "No DKP transactions recorded for this member."
        return await reply(interaction, embed=embed)

    view = DkpLogBrowserView(entries=entries, target=target, invoker=user)
    await reply(interaction, embed=view.build_embed(), view=view)


@bot.tree.command(name="status", description="Show FRCoiN bot status and current configuration")
async def status(interaction: discord.Interaction):
    ctx = await officer_context(interaction)
    if not ctx:
        return
    await reply(interaction, embed=await actions.status_embed(ctx[0]))


@bot.tree.command(name="adjustment_mode", description="Open the Adjustments panel (spend, add/remove points)")
async def adjustment_mode(interaction: discord.Interaction):
    await AdjustPanel.send(bot, interaction)


@bot.tree.command(name="admin_mode", description="Open the Admin panel (setup, exports, imports, saved events)")
async def admin_mode(interaction: discord.Interaction):
    await AdminPanel.send(bot, interaction)


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
# STARTUP
# ------------------------
async def setup_hook():
    await init_db()
    # Persistent components: the lobby, the leaderboard's My Points button, and every event / adjustment list card's buttons.
    bot.add_view(LobbyView(bot))
    bot.add_view(LeaderboardView())
    bot.add_dynamic_items(EventCardButton, AdjustmentCardButton)

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    await bot.tree.sync()
    await update_leaderboard(bot)  # attaches the My Points button after an upgrade
    await panel.restore_panel(bot)
    logger.info("FRCoiN logged in as %s", bot.user)

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your bot token.")
    bot.run(TOKEN)
