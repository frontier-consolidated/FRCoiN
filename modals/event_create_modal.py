from typing import cast
import discord
from discord.ext import commands

import actions
from views.common import guild_member, reply, run_action

class EventCreateModal(discord.ui.Modal):
    """Create a DKP or attendance event: a name plus an optional voice scope (any mix of voice channels and categories)."""

    def __init__(self, bot: commands.Bot, kind: str):
        super().__init__(title="Create DKP Event" if kind == "dkp" else "Take Attendance")
        self.bot = bot
        self.kind = kind
        self.name_input = discord.ui.TextInput(placeholder="e.g. Saturday Fleet Op", max_length=100)
        self.scope_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.category, discord.ChannelType.voice, discord.ChannelType.stage_voice],
            placeholder="Voice channels, or a whole category",
            required=False,
            max_values=25,
        )
        self.add_item(discord.ui.Label(text="Event name", component=self.name_input))
        self.add_item(discord.ui.Label(
            text="Voice scope",
            description="Pick one or more channels, or a category. Blank = your own voice channel each time.",
            component=self.scope_select,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        scope = [c for v in self.scope_select.values if (c := guild.get_channel(v.id))]
        name = self.name_input.value.strip()

        result = await run_action(
            interaction, lambda: actions.start_event(self.bot, guild, actor, name, self.kind, cast(list[discord.abc.GuildChannel], scope))
        )
        if result:
            event, overlap_warning = result
            await reply(interaction, f"**{name}** started (ID: `{event['id']}`). Its card is at the bottom of the panel channel."
                                     + (f"\n{overlap_warning}" if overlap_warning else ""))
