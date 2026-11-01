from typing import cast
import discord
from discord.ext import commands

import actions
from helpers import is_admin, is_officer
from views.common import guild_member, reply, run_action

class LobbyView(discord.ui.View):
    """
    The panel's lobby message. Persistent (fixed custom_ids, no timeout): one instance is registered with
    bot.add_view at startup, and refresh_lobby edits the message with a fresh instance carrying the current options.
    """

    def __init__(self, bot: commands.Bot, open_events: list[dict] | None = None, open_lists: list[dict] | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"[{'DKP' if e['kind'] == 'dkp' else 'ATT'}] {e['name']}"[:100],
                value=f"e:{e['id']}",
                description=f"ID {e['id']}" + (" · points pending" if e["state"] == "pending_close" else ""),
            )
            for e in open_events or []
        ] + [
            discord.SelectOption(label=f"[ADJ] {a['name']}"[:100], value=f"a:{a['id']}", description=f"Adjustment list · ID {a['id']}")
            for a in open_lists or []
        ]
        if options:
            self.events_select.options = options[:25]  # Discord caps a select at 25 options
            self.events_select.disabled = False
        else:
            # Discord requires at least one option, even on a disabled select.
            self.events_select.options = [discord.SelectOption(label="No open events", value="none")]
            self.events_select.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not await is_officer(interaction.user):
            await interaction.response.send_message(actions.NO_PERMISSION, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="+ Create Event", style=discord.ButtonStyle.success, custom_id="frc:lobby:create", row=0)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from modals.event_create_modal import EventCreateModal
        await interaction.response.send_modal(EventCreateModal(self.bot, "dkp"))

    @discord.ui.button(label="Take Attendance", style=discord.ButtonStyle.primary, custom_id="frc:lobby:attendance", row=0)
    async def attendance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from modals.event_create_modal import EventCreateModal
        await interaction.response.send_modal(EventCreateModal(self.bot, "attendance"))

    @discord.ui.button(label="Adjustments", style=discord.ButtonStyle.secondary, custom_id="frc:lobby:adjust", row=0)
    async def adjust_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from views.adjust_panel import AdjustPanel
        await AdjustPanel.send(self.bot, interaction)

    @discord.ui.button(label="Admin", style=discord.ButtonStyle.secondary, custom_id="frc:lobby:admin", row=0)
    async def admin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_admin(cast(discord.Member, interaction.user)):
            return await reply(interaction, actions.NO_PERMISSION)
        from views.admin_panel import AdminPanel
        await AdminPanel.send(self.bot, interaction)

    @discord.ui.select(placeholder="Open events — pick one to bring its card to the bottom", custom_id="frc:lobby:events", row=1,
                       options=[discord.SelectOption(label="No open events", value="none")])
    async def events_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        ctx = guild_member(interaction)
        if not ctx or select.values[0] == "none":
            return
        guild, _ = ctx
        # "e:<id>" = event, "a:<id>" = adjustment list. A bare number comes from a lobby posted before 2.1.0.
        kind, _, raw_id = select.values[0].rpartition(":")
        item_id = int(raw_id)

        async def repost():
            from panel import refresh_lobby, upsert_card, upsert_list_card
            if kind == "a":
                item = await actions.get_open_list(guild, item_id)
                await upsert_list_card(self.bot, guild, item_id, repost=True)
            else:
                item = await actions.get_open_event(guild, item_id)
                await upsert_card(self.bot, guild, item_id, repost=True)
            await refresh_lobby(self.bot, guild)  # also resets the dropdown's selection
            return item

        item = await run_action(interaction, repost)
        if item:
            await reply(interaction, f"Moved **{item['name']}**'s card to the bottom of the channel.")
