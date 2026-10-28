from typing import Union
import discord
from discord.ext import commands

import actions
from helpers import is_officer
from modals.member_modal import resolve_member
from views.common import guild_member, reply, run_action

PANEL_TIMEOUT = 14 * 60  # just under the 15-minute interaction token, so on_timeout can still edit the message

class ExpiringPanel(discord.ui.View):
    """Ephemeral panel for whoever opened it. On timeout it tells them to re-open it rather than failing silently."""

    def __init__(self, bot: commands.Bot, invoker: discord.abc.User):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.invoker = invoker
        self.opened_with: Union[discord.Interaction, None] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("Open your own panel to use these buttons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.opened_with:
            try:
                await self.opened_with.edit_original_response(content="This panel has expired — open it again.", embed=None, view=None)
            except discord.HTTPException:
                pass

class AdjustPanel(ExpiringPanel):
    @classmethod
    async def send(cls, bot: commands.Bot, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not await is_officer(interaction.user):
            return await reply(interaction, actions.NO_PERMISSION)
        view = cls(bot, interaction.user)
        embed = discord.Embed(
            title="Adjustments",
            description="**Spend** — deduct DKP a member has spent (blank amount resets them to 0).\n"
                        "**Add / Remove points** — a manual ± with a reason. Only org members can be given DKP.",
            colour=discord.Colour.orange()
        )
        await reply(interaction, embed=embed, view=view)
        view.opened_with = interaction

    @discord.ui.button(label="Spend", style=discord.ButtonStyle.danger)
    async def spend_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SpendModal(self.bot))

    @discord.ui.button(label="Add / Remove points", style=discord.ButtonStyle.primary)
    async def adjust_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdjustModal(self.bot))

class SpendModal(discord.ui.Modal, title="Spend DKP"):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.member_select = discord.ui.UserSelect(placeholder="Pick a member", required=True)
        self.amount = discord.ui.TextInput(placeholder="Blank = reset balance to 0", required=False, max_length=9)
        self.reason = discord.ui.TextInput(placeholder="e.g. Won the ship raffle", style=discord.TextStyle.paragraph, max_length=200)
        self.add_item(discord.ui.Label(text="Member", component=self.member_select))
        self.add_item(discord.ui.Label(text="Amount to deduct", component=self.amount))
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        raw = self.amount.value.strip()
        try:
            amount = int(raw) if raw else None
        except ValueError:
            return await reply(interaction, "Invalid amount — enter a whole number, or leave it blank to reset to 0.")
        member = resolve_member(guild, self.member_select.values[0])
        reason = self.reason.value.strip()
        message = await run_action(interaction, lambda: actions.spend(self.bot, guild, actor, member, reason, amount))
        if message:
            await reply(interaction, message)

class AdjustModal(discord.ui.Modal, title="Add / Remove DKP"):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.member_select = discord.ui.UserSelect(placeholder="Pick a member", required=True)
        self.points = discord.ui.TextInput(placeholder="e.g. 5 or -3", max_length=9)
        self.reason = discord.ui.TextInput(placeholder="e.g. Correcting missed attendance", style=discord.TextStyle.paragraph, max_length=200)
        self.add_item(discord.ui.Label(text="Member", component=self.member_select))
        self.add_item(discord.ui.Label(text="Points (+ or -)", component=self.points))
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        try:
            amount = int(self.points.value)
        except ValueError:
            return await reply(interaction, "Invalid number — please enter a whole number like `5` or `-3`.")
        member = resolve_member(guild, self.member_select.values[0])
        if not isinstance(member, discord.Member):
            return await reply(interaction, "That user isn't in this server.")
        reason = self.reason.value.strip()
        message = await run_action(interaction, lambda: actions.adjust(self.bot, guild, actor, member, amount, reason))
        if message:
            await reply(interaction, message)
