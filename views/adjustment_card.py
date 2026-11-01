import re
from typing import Any, cast
import discord
from discord.ext import commands

import actions
from helpers import is_officer
from views.common import ConfirmView, guild_member, reply, run_action

LIST_BUTTONS = {
    "add": ("Add lines", discord.ButtonStyle.primary),
    "remove": ("Remove lines", discord.ButtonStyle.secondary),
    "view": ("View all", discord.ButtonStyle.secondary),
    "export": ("Export", discord.ButtonStyle.secondary),
    "apply": ("Apply", discord.ButtonStyle.success),
    "cancel": ("Cancel", discord.ButtonStyle.danger),
}

class AdjustmentCardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"frc:al:(?P<action>[a-z]+):(?P<list_id>\d+)"):
    """An adjustment list card button. Like event cards, it lives entirely in its custom_id and the DB."""

    def __init__(self, action: str, list_id: int, row: int | None = None):
        label, style = LIST_BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"frc:al:{action}:{list_id}", row=row))
        self.action = action
        self.list_id = list_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str]):
        return cls(match["action"], int(match["list_id"]))

    async def callback(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        bot = cast(commands.Bot, interaction.client)
        list_id = self.list_id

        if not await is_officer(actor):
            return await reply(interaction, actions.NO_PERMISSION)

        if self.action in ("add", "remove"):
            # Modals must be the first response, so check the list is still open before opening one.
            if not await run_action(interaction, lambda: actions.get_open_list(guild, list_id), defer=False):
                return
            from modals.adjustment_modals import AddLinesModal, RemoveLinesModal
            modal = AddLinesModal(bot, list_id) if self.action == "add" else RemoveLinesModal(bot, list_id)
            return await interaction.response.send_modal(modal)

        if self.action in ("apply", "cancel"):
            if self.action == "apply":
                prompt = await run_action(interaction, lambda: actions.preview_adjustment_list(guild, actor, list_id))
            else:
                async def cancel_prompt():
                    adj_list = await actions.get_open_list(guild, list_id)
                    await actions.require_list_owner(actor, adj_list)
                    return f"Cancel **{adj_list['name']}**? No points will change, and its lines are kept only in the audit log's file."
                prompt = await run_action(interaction, cancel_prompt)
            if not prompt:
                return
            do = actions.apply_adjustment_list if self.action == "apply" else actions.cancel_adjustment_list

            async def confirmed(i: discord.Interaction):
                message = await run_action(i, lambda: do(bot, guild, actor, list_id))
                if message:
                    await reply(i, message)

            label = "Yes, apply it" if self.action == "apply" else "Yes, cancel list"
            return await reply(interaction, prompt, view=ConfirmView(actor, confirmed, confirm_label=label))

        if self.action == "view":
            async def show():
                return await actions.adjustment_list_embed(guild, await actions.get_open_list(guild, list_id))
            embed = await run_action(interaction, show)
            if embed:
                await reply(interaction, embed=embed)
            return

        if self.action == "export":
            result = await run_action(interaction, lambda: actions.export_adjustment_list(bot, guild, actor, list_id))
            if result:
                file, message = result
                await reply(interaction, message, file=file)

def build_list_view(adj_list: dict, has_lines: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    layout = [("add", 0), ("remove", 0), ("view", 0), ("export", 0)]
    if has_lines:
        layout.append(("apply", 1))
    layout.append(("cancel", 1))
    for action, row in layout:
        view.add_item(AdjustmentCardButton(action, adj_list["id"], row=row))
    return view
