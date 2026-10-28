# ------------------------
# PANEL — lobby message, event cards and channel lockdown
# ------------------------

from typing import Union
import discord
from discord.ext import commands

from db import get_config, set_config
from events import get_all_open_events, get_event, get_event_attendees, get_open_events, set_event_card
from helpers import get_org_role_ids, member_has_org_role
from logger import logger

CARD_COLOURS = {
    ("dkp", "open"): discord.Colour.teal(),
    ("dkp", "pending_close"): discord.Colour.gold(),
    ("attendance", "open"): discord.Colour.blue(),
}

async def get_panel_channel(guild: discord.Guild) -> Union[discord.TextChannel, None]:
    channel_id = await get_config("panel_channel")
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None

# ------------------------
# CHANNEL LOCKDOWN (Officers+ only)
# ------------------------
async def _staff_roles(guild: discord.Guild) -> list[discord.Role]:
    ids = set()
    for key in ("admin_roles", "officer_roles"):
        raw = await get_config(key)
        if raw:
            ids.update(int(r) for r in raw.split(",") if r.strip().isdigit())
    return [r for r in (guild.get_role(i) for i in ids) if r]

async def sync_panel_permissions(guild: discord.Guild) -> Union[str, None]:
    """Hide the panel channel from @everyone, show it read-only to admin/officer roles. Returns a warning on failure."""
    channel = await get_panel_channel(guild)
    if not channel:
        return None
    try:
        await channel.set_permissions(guild.default_role, view_channel=False, reason="FRCoiN panel: Officers+ only")
        for role in await _staff_roles(guild):
            await channel.set_permissions(
                role, view_channel=True, read_message_history=True, send_messages=False, reason="FRCoiN panel: Officers+ only"
            )
        await channel.set_permissions(
            guild.me, view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
            reason="FRCoiN panel: bot access"
        )
    except discord.Forbidden:
        return (f"The bot couldn't lock down {channel.mention} — it needs **Manage Roles** (Manage Permissions) in that channel. "
                "Until then, set it by hand: hide it from @everyone, allow admin/officer roles to view it.")
    return None

async def lockdown_problems(guild: discord.Guild) -> list[str]:
    channel = await get_panel_channel(guild)
    if not channel:
        return ["No panel channel set — run `/setup panel_channel:#…`."]
    problems = []
    if channel.permissions_for(guild.default_role).view_channel:
        problems.append(f"@everyone can see {channel.mention}.")
    for role in await _staff_roles(guild):
        if not channel.overwrites_for(role).view_channel and not channel.permissions_for(role).view_channel:
            problems.append(f"{role.mention} can't see {channel.mention}.")
    me = channel.permissions_for(guild.me)
    if not (me.send_messages and me.embed_links):
        problems.append(f"The bot can't post embeds in {channel.mention}.")
    if not me.manage_roles:
        problems.append("The bot lacks **Manage Roles** there, so it can't fix this automatically.")
    return problems

# ------------------------
# EVENT CARDS
# ------------------------
def community_count(n: int) -> str:
    return f"{n} Community Member{'' if n == 1 else 's'}"

def _breakdown_str(channels: dict) -> str:
    return " · ".join(f"{name} **{count}**" for name, count in channels.items()) or "*no voice channels in scope*"

def _names(guild: discord.Guild, ids: list[int], limit: int = 10) -> str:
    names = [(m.display_name if (m := guild.get_member(i)) else f"Unknown ({i})") for i in ids[:limit]]
    if len(ids) > limit:
        names.append(f"+{len(ids) - limit} more")
    return ", ".join(names)

async def render_card(guild: discord.Guild, event: dict) -> tuple[discord.Embed, discord.ui.View]:
    # actions and the views import panel, so import them lazily
    from actions import scope_label
    from views.event_card import build_card_view

    attendee_ids = [r["user_id"] for r in await get_event_attendees(event["id"])]
    kind, state = event["kind"], event["state"]
    snapshot = event["last_snapshot_breakdown"]

    title = f"{'⚔️ DKP Event' if kind == 'dkp' else '📋 Attendance'} — {event['name']}"
    if state == "pending_close":
        title += " · points pending"
    lines = [
        f"**ID** `{event['id']}` · Created {discord.utils.format_dt(event['created_at'], 'D')}",
        f"Started {discord.utils.format_dt(event['created_at'], 'R')}"
        + (f" by <@{event['created_by_id']}>" if event["created_by_id"] else ""),
        f"**Scope:** {scope_label(guild, event['scope_channel_ids'])}",
        f"**Last snapshot:** {discord.utils.format_dt(event['last_snapshot_at'], 'R')}" if event["last_snapshot_at"] else "**Last snapshot:** *none yet*",
    ]
    embed = discord.Embed(title=title, description="\n".join(lines), colour=CARD_COLOURS.get((kind, state), discord.Colour.teal()))

    if kind == "attendance":
        org_role_ids = await get_org_role_ids()
        members = sum(1 for uid in attendee_ids if (m := guild.get_member(uid)) and member_has_org_role(m, org_role_ids))
        attendees = f"**{len(attendee_ids)}** ({members} members · {community_count(len(attendee_ids) - members)})"
    else:
        attendees = f"**{len(attendee_ids)}** org members"
    embed.add_field(name="Attendees", value=attendees, inline=True)

    if snapshot:
        embed.add_field(name="In voice at last snapshot", value=_breakdown_str(snapshot["channels"]), inline=False)
        changes = [f"**+{len(snapshot['new'])}** new"]
        if snapshot["left"]:
            changes.append(f"**{len(snapshot['left'])}** left voice: {_names(guild, snapshot['left'])}")
        embed.add_field(name="Since previous snapshot", value=" · ".join(changes), inline=False)

    if state == "pending_close":
        embed.add_field(
            name="Pending points",
            value=f"**{event['pending_points']:+} DKP** to each attendee who is still an org member.\n"
                  "Nothing is awarded until someone presses **Close event**.",
            inline=False
        )
    embed.set_footer(text="Any officer can operate this card • FRCoiN")
    return embed, build_card_view(event, has_attendees=bool(attendee_ids))

async def upsert_card(bot: commands.Bot, guild: discord.Guild, event_id: int, repost: bool = False):
    """Edit the event's card in place, or (re)post it at the bottom of the panel channel."""
    event = await get_event(event_id)
    channel = await get_panel_channel(guild)
    if not event or not channel:
        return
    embed, view = await render_card(guild, event)

    if event["card_message_id"]:
        if not repost and event["card_channel_id"] == channel.id:
            try:
                await channel.get_partial_message(event["card_message_id"]).edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.warning("Could not edit card for event %s: %s", event_id, e)
                return
        else:
            await delete_card(bot, event)

    message = await channel.send(embed=embed, view=view)
    await set_event_card(event_id, channel.id, message.id)

async def delete_card(bot: commands.Bot, event: dict):
    if not event["card_message_id"]:
        return
    channel = bot.get_channel(event["card_channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.get_partial_message(event["card_message_id"]).delete()
        except discord.HTTPException:
            pass
    await set_event_card(event["id"], None, None)

# ------------------------
# LOBBY
# ------------------------
def lobby_embed(open_events: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="FRCoiN Panel",
        description=(
            "**Create Event** — a DKP event; only org members are captured and earn points.\n"
            "**Take Attendance** — records everyone in voice, members and Community Members. No DKP.\n"
            "Each open event has its own card below. Any officer can operate any card.\n"
            "Use the dropdown to bring an event's card back to the bottom of the channel."
        ),
        colour=discord.Colour.blurple()
    )
    if open_events:
        lines = []
        for e in open_events[:10]:
            tag = "DKP" if e["kind"] == "dkp" else "ATT"
            pending = " · points pending" if e["state"] == "pending_close" else ""
            link = f"https://discord.com/channels/{e['guild_id']}/{e['card_channel_id']}/{e['card_message_id']}" if e["card_message_id"] else None
            name = f"[{e['name']}]({link})" if link else e["name"]
            lines.append(f"`{e['id']}` [{tag}] {name}{pending}")
        if len(open_events) > 10:
            lines.append(f"… and {len(open_events) - 10} more in the dropdown")
        embed.add_field(name=f"Open events ({len(open_events)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Open events", value="*None right now.*", inline=False)
    embed.set_footer(text="Officers+ only • FRCoiN")
    return embed

async def refresh_lobby(bot: commands.Bot, guild: discord.Guild):
    channel = await get_panel_channel(guild)
    if not channel:
        return
    from views.lobby_view import LobbyView
    open_events = await get_open_events(guild.id)
    embed, view = lobby_embed(open_events), LobbyView(bot, open_events)

    message_id = await get_config("panel_message")
    if message_id:
        try:
            await channel.get_partial_message(int(message_id)).edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning("Could not edit panel lobby: %s", e)
            return

    message = await channel.send(embed=embed, view=view)
    await set_config("panel_message", message.id)

async def move_panel(bot: commands.Bot, guild: discord.Guild, old_channel_id: Union[str, None]):
    """The panel channel changed: remove the old lobby, then post the lobby and every open card in the new one."""
    message_id = await get_config("panel_message")
    old_channel = bot.get_channel(int(old_channel_id)) if old_channel_id else None
    if message_id and isinstance(old_channel, discord.TextChannel):
        try:
            await old_channel.get_partial_message(int(message_id)).delete()
        except discord.HTTPException:
            pass
    await set_config("panel_message", "")
    await refresh_lobby(bot, guild)
    for event in await get_open_events(guild.id):
        await upsert_card(bot, guild, event["id"], repost=True)

async def restore_panel(bot: commands.Bot):
    """On startup: re-render the lobby and every open card, re-posting any that were deleted by hand."""
    channel_id = await get_config("panel_channel")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        logger.warning("Panel channel not found (id=%s).", channel_id)
        return
    for event in await get_all_open_events():
        if event["guild_id"] == channel.guild.id:
            await upsert_card(bot, channel.guild, event["id"])
    await refresh_lobby(bot, channel.guild)
    logger.info("Panel restored in #%s.", channel.name)
