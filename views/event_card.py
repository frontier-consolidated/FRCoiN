import re
from typing import Any, cast
import discord
from discord.ext import commands

import actions
from helpers import is_officer
from views.common import ConfirmView, guild_member, reply, run_action

# action -> (label, style). Labels can depend on state, see build_card_view.
CARD_BUTTONS = {
    "snapshot": ("Snapshot", discord.ButtonStyle.primary),
    "add": ("Add member", discord.ButtonStyle.secondary),
    "remove": ("Remove member", discord.ButtonStyle.secondary),
    "list": ("Attendance", discord.ButtonStyle.secondary),
    "export": ("Export list", discord.ButtonStyle.secondary),
    "scope": ("Edit scope", discord.ButtonStyle.secondary),
    "end": ("End event", discord.ButtonStyle.success),
    "close": ("Close event", discord.ButtonStyle.success),
    "editpts": ("Edit points", discord.ButtonStyle.secondary),
    "reopen": ("Reopen", discord.ButtonStyle.secondary),
    "cancel": ("Cancel", discord.ButtonStyle.danger),
}

class EventCardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"frc:ev:(?P<action>[a-z]+):(?P<event_id>\d+)"):
    """
    A card button. Everything it needs is in its custom_id and the DB, so it has no timeout
    and keeps working after restarts — registered once with bot.add_dynamic_items.
    """

    def __init__(self, action: str, event_id: int, label: str | None = None, row: int | None = None):
        default_label, style = CARD_BUTTONS[action]
        super().__init__(discord.ui.Button(
            label=label or default_label, style=style, custom_id=f"frc:ev:{action}:{event_id}", row=row
        ))
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str]):
        return cls(match["action"], int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        bot = cast(commands.Bot, interaction.client)
        event_id = self.event_id

        if not await is_officer(actor):
            return await reply(interaction, actions.NO_PERMISSION)

        if self.action in ("add", "remove", "end", "editpts", "scope"):
            # Modals must be the first response, so check the event is still open before opening one.
            event = await run_action(interaction, lambda: actions.get_open_event(guild, event_id), defer=False)
            if not event:
                return
            from modals.member_modal import MemberModal
            from modals.points_modal import PointsModal
            if self.action == "scope":
                if event["state"] != "open":
                    return await reply(interaction, "Points have already been entered for this event — press **Reopen** to change its scope.")
                from modals.event_create_modal import EditScopeModal
                current = [c for i in event["scope_channel_ids"] or [] if (c := guild.get_channel(i))]
                return await interaction.response.send_modal(EditScopeModal(bot, event_id, current))
            if self.action in ("add", "remove"):
                return await interaction.response.send_modal(MemberModal(bot, event_id, self.action))
            return await interaction.response.send_modal(
                PointsModal(bot, event_id=event_id, pending=True, default=event["pending_points"])
            )

        if self.action == "cancel":
            event = await run_action(interaction, lambda: actions.get_open_event(guild, event_id), defer=False)
            if not event:
                return

            async def confirmed(i: discord.Interaction):
                message = await run_action(i, lambda: actions.cancel_event(bot, guild, actor, event_id))
                if message:
                    await reply(i, message)

            return await reply(
                interaction, f"Cancel **{event['name']}**? Its attendance list is deleted and no DKP is awarded.",
                view=ConfirmView(actor, confirmed, confirm_label="Yes, cancel event")
            )

        if self.action == "list":
            async def show():
                return await actions.attendance_embed(guild, await actions.get_open_event(guild, event_id))
            embed = await run_action(interaction, show)
            if embed:
                await reply(interaction, embed=embed)
            return

        if self.action == "export":
            result = await run_action(interaction, lambda: actions.export_event(bot, guild, actor, event_id))
            if result:
                file, message = result
                await reply(interaction, message, file=file)
            return

        handlers = {
            "snapshot": lambda: actions.snapshot_event(bot, guild, actor, event_id),
            "close": lambda: actions.close_event(bot, guild, actor, event_id),
            "reopen": lambda: actions.reopen_event(bot, guild, actor, event_id),
        }
        message = await run_action(interaction, handlers[self.action])
        if message:
            await reply(interaction, message)

def build_card_view(event: dict, has_attendees: bool) -> discord.ui.View:
    """The buttons for an event card in its current state."""
    view = discord.ui.View(timeout=None)
    event_id = event["id"]
    snapshot_label = "Update snapshot" if event["last_snapshot_at"] else "Snapshot"

    if event["state"] == "pending_close":
        layout = [("close", None, 0), ("editpts", None, 0), ("reopen", None, 0), ("list", None, 1), ("export", None, 1)]
    else:
        layout = [
            ("snapshot", snapshot_label, 0), ("add", None, 0), ("remove", None, 0), ("list", None, 0),
            ("export", None, 1), ("scope", None, 1),
        ]
        if event["kind"] == "attendance":
            layout.append(("close", None, 1))
        elif has_attendees:
            layout.append(("end", None, 1))
        layout.append(("cancel", None, 1))

    for action, label, row in layout:
        view.add_item(EventCardButton(action, event_id, label=label, row=row))
    return view
