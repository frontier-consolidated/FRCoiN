from typing import Any, cast
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
        # The picker lists every channel the officer can see; one the bot doesn't know about can't be snapshotted.
        unknown = [v.name for v in self.scope_select.values if not guild.get_channel(v.id)]
        if unknown:
            return await reply(interaction, f"The bot can't see {', '.join(f'**{n}**' for n in unknown)}, so it couldn't tell who is there. "
                                            "Ask an admin to give the bot **View Channel** on it, or pick different channels.")
        scope = [c for v in self.scope_select.values if (c := guild.get_channel(v.id))]
        name = self.name_input.value.strip()

        result = await run_action(
            interaction, lambda: actions.start_event(self.bot, guild, actor, name, self.kind, cast(list[discord.abc.GuildChannel], scope))
        )
        if result:
            event, overlap_warning = result
            await reply(interaction, f"**{name}** started (ID: `{event['id']}`). Its card is at the bottom of the panel channel."
                                     + (f"\n{overlap_warning}" if overlap_warning else ""))

class EditScopeModal(discord.ui.Modal, title="Edit scope"):
    """Change an open event's voice scope. The picker starts with the event's current channels."""

    def __init__(self, bot: commands.Bot, event_id: int, current: list[Any]):
        super().__init__()
        self.bot = bot
        self.event_id = event_id
        self.scope_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.category, discord.ChannelType.voice, discord.ChannelType.stage_voice],
            placeholder="Voice channels, or a whole category",
            required=False,
            max_values=25,
            default_values=current[:25],
        )
        self.add_item(discord.ui.Label(
            text="Voice scope",
            description="Future snapshots look here. Nobody already on the list is removed. Blank = your own voice channel.",
            component=self.scope_select,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        unknown = [v.name for v in self.scope_select.values if not guild.get_channel(v.id)]
        if unknown:
            return await reply(interaction, f"The bot can't see {', '.join(f'**{n}**' for n in unknown)}, so it couldn't tell who is there. "
                                            "Ask an admin to give the bot **View Channel** on it, or pick different channels.")
        scope = [c for v in self.scope_select.values if (c := guild.get_channel(v.id))]
        event_id = self.event_id
        message = await run_action(
            interaction, lambda: actions.edit_event_scope(self.bot, guild, actor, event_id, cast(list[discord.abc.GuildChannel], scope))
        )
        if message:
            await reply(interaction, message)
