from typing import Union
import discord
from discord.ext import commands
import actions
from logger import logger
from views.common import guild_member, reply, run_action

class PointsModal(discord.ui.Modal, title="Enter DKP Points"):
    """
    Event mode (event_id set): pending=True stores the points on the event (card's End event / Edit points),
    pending=False awards immediately (/end_event). Adjust mode (target_user set): a manual ± with a reason.
    """
    points = discord.ui.TextInput(label="Points (+ or -)", placeholder="e.g. 5 or -3")

    def __init__(self, bot: commands.Bot, event_id: Union[int, None] = None, pending: bool = False,
                 target_user: Union[discord.Member, None] = None, default: Union[int, None] = None):
        super().__init__()
        self.bot = bot
        self.event_id = event_id
        self.pending = pending
        self.target_user = target_user
        if default is not None:
            self.points.default = str(default)

        if self.target_user:
            self.reason = discord.ui.TextInput(
                label="Reason",
                placeholder="e.g. Correcting missed attendance",
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=200,
            )
            self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            await interaction.response.send_message(
                "Invalid number — please enter a whole number like `5` or `-3`.", ephemeral=True
            )
            return

        ctx = guild_member(interaction)
        if not ctx:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        guild, actor = ctx

        if self.event_id is not None:
            event_id = self.event_id
            if self.pending:
                message = await run_action(interaction, lambda: actions.set_points_pending(self.bot, guild, actor, event_id, value))
            else:
                message = await run_action(interaction, lambda: actions.close_event(self.bot, guild, actor, event_id, value))
        elif self.target_user:
            target, reason = self.target_user, self.reason.value.strip()
            message = await run_action(interaction, lambda: actions.adjust(self.bot, guild, actor, target, value, reason))
        else:
            return

        if message:
            await reply(interaction, message)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("Unhandled error in PointsModal (event_id=%s): %s", self.event_id, error, exc_info=error)
        message = "Something went wrong processing that submission. The issue has been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException as e:
            logger.warning("Could not notify user of modal error: %s", e)
