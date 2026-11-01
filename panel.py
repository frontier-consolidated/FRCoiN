# ------------------------
# PANEL — lobby message, event cards and channel lockdown
# ------------------------

from typing import Awaitable, Callable, Union
import discord
from discord.ext import commands

from adjustment_lists import get_all_open_lists, get_lines, get_list, get_open_lists, member_nets, set_list_card
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

async def _publish(bot: commands.Bot, channel: discord.TextChannel, record: dict, embed: discord.Embed, view: discord.ui.View,
                   repost: bool, save: Callable[[Union[int, None], Union[int, None]], Awaitable[None]], what: str):
    """Edit a card in place, or (re)post it at the bottom of the panel channel and save its new ids."""
    if record["card_message_id"]:
        if not repost and record["card_channel_id"] == channel.id:
            try:
                await channel.get_partial_message(record["card_message_id"]).edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.warning("Could not edit card for %s: %s", what, e)
                return
        else:
            await _delete_message(bot, record)
            await save(None, None)

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:  # e.g. the bot lost Send Messages there; /status reports it
        logger.warning("Could not post card for %s: %s", what, e)
        return
    await save(channel.id, message.id)

async def _delete_message(bot: commands.Bot, record: dict):
    channel = bot.get_channel(record["card_channel_id"])
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.get_partial_message(record["card_message_id"]).delete()
        except discord.HTTPException:
            pass

async def upsert_card(bot: commands.Bot, guild: discord.Guild, event_id: int, repost: bool = False):
    """Edit the event's card in place, or (re)post it at the bottom of the panel channel."""
    event = await get_event(event_id)
    channel = await get_panel_channel(guild)
    if not event or not channel:
        return
    embed, view = await render_card(guild, event)
    await _publish(bot, channel, event, embed, view, repost, lambda c, m: set_event_card(event_id, c, m), f"event {event_id}")

async def delete_card(bot: commands.Bot, event: dict):
    if not event["card_message_id"]:
        return
    await _delete_message(bot, event)
    await set_event_card(event["id"], None, None)

# ------------------------
# ADJUSTMENT LIST CARDS
# ------------------------
LIST_CARD_LINES = 20

async def render_list_card(guild: discord.Guild, adj_list: dict) -> tuple[discord.Embed, discord.ui.View]:
    from actions import _skip_lines, list_shortfalls  # actions imports panel, so import lazily
    from views.adjustment_card import build_list_view

    lines = await get_lines(adj_list["id"])
    nets = member_nets(lines)
    head = [
        f"**ID** `{adj_list['id']}` · Started {discord.utils.format_dt(adj_list['created_at'], 'R')} by <@{adj_list['created_by_id']}>",
        f"Any officer can add or remove lines. Only <@{adj_list['created_by_id']}> or an admin can apply or cancel.",
        "",
    ]
    rows = [f"`#{l['line_no']}` <@{l['user_id']}> **{l['amount']:+}**" + (f" — {l['reason']}" if l["reason"] else "")
            for l in lines[:LIST_CARD_LINES]]
    if len(lines) > LIST_CARD_LINES:
        rows.append(f"… and {len(lines) - LIST_CARD_LINES} more — **View all**")
    description = "\n".join(head + (rows or ["*No lines yet — click **Add lines**.*"]))
    embed = discord.Embed(title=f"🧾 Adjustment list — {adj_list['name']}"[:256], description=description[:4096],
                          colour=discord.Colour.orange())

    if lines:
        added = sum(l["amount"] for l in lines if l["amount"] > 0)
        removed = -sum(l["amount"] for l in lines if l["amount"] < 0)
        embed.add_field(name="Totals", value=f"**{len(lines)}** lines · **{len(nets)}** members · **-{removed}** / **+{added}** points",
                        inline=False)
        short, not_org = await list_shortfalls(guild, lines)
        problems = _skip_lines(short, not_org)
        embed.add_field(name="Balance check",
                        value=("\n".join(f"⚠️ {p}" for p in problems)[:1024] + "\n*They'll be skipped unless fixed before Apply.*")
                        if problems else "✅ Everyone on the list can cover their lines.", inline=False)
    embed.set_footer(text="Nothing changes until the list is applied • FRCoiN")
    return embed, build_list_view(adj_list, has_lines=bool(lines))

async def upsert_list_card(bot: commands.Bot, guild: discord.Guild, list_id: int, repost: bool = False):
    adj_list = await get_list(list_id)
    channel = await get_panel_channel(guild)
    if not adj_list or not channel:
        return
    embed, view = await render_list_card(guild, adj_list)
    await _publish(bot, channel, adj_list, embed, view, repost, lambda c, m: set_list_card(list_id, c, m), f"adjustment list {list_id}")

async def delete_list_card(bot: commands.Bot, adj_list: dict):
    if not adj_list["card_message_id"]:
        return
    await _delete_message(bot, adj_list)
    await set_list_card(adj_list["id"], None, None)

# ------------------------
# LOBBY
# ------------------------
def lobby_embed(open_events: list[dict], open_lists: Union[list[dict], None] = None) -> discord.Embed:
    embed = discord.Embed(
        title="FRCoiN Panel",
        description=(
            "**Create Event** — a DKP event; only org members are captured and earn points.\n"
            "**Take Attendance** — records everyone in voice, members and Community Members. No DKP.\n"
            "Each open event has its own card below. Any officer can operate any card.\n"
            "**Adjustments** — spends and corrections, or an **adjustment list** for many members at once.\n"
            "Use the dropdown to bring a card back to the bottom of the channel."
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
    if open_lists:
        lines = []
        for a in open_lists[:10]:
            link = f"https://discord.com/channels/{a['guild_id']}/{a['card_channel_id']}/{a['card_message_id']}" if a["card_message_id"] else None
            lines.append(f"`{a['id']}` [ADJ] " + (f"[{a['name']}]({link})" if link else a["name"]))
        if len(open_lists) > 10:
            lines.append(f"… and {len(open_lists) - 10} more in the dropdown")
        embed.add_field(name=f"Open adjustment lists ({len(open_lists)})", value="\n".join(lines), inline=False)
    embed.set_footer(text="Officers+ only • FRCoiN")
    return embed

async def refresh_lobby(bot: commands.Bot, guild: discord.Guild):
    channel = await get_panel_channel(guild)
    if not channel:
        return
    from views.lobby_view import LobbyView
    open_events, open_lists = await get_open_events(guild.id), await get_open_lists(guild.id)
    embed, view = lobby_embed(open_events, open_lists), LobbyView(bot, open_events, open_lists)

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

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.warning("Could not post panel lobby: %s", e)
        return
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
    for adj_list in await get_open_lists(guild.id):
        await upsert_list_card(bot, guild, adj_list["id"], repost=True)

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
    for adj_list in await get_all_open_lists():
        if adj_list["guild_id"] == channel.guild.id:
            await upsert_list_card(bot, channel.guild, adj_list["id"])
    await refresh_lobby(bot, channel.guild)
    logger.info("Panel restored in #%s.", channel.name)
