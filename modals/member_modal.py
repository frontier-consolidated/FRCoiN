from typing import Union
import discord
from discord.ext import commands

import actions
from views.common import guild_member, reply, run_action

class MemberModal(discord.ui.Modal):
    """Pick a member to add to / remove from an event's attendance list."""

    def __init__(self, bot: commands.Bot, event_id: int, mode: str):
        super().__init__(title="Add member" if mode == "add" else "Remove member")
        self.bot = bot
        self.event_id = event_id
        self.mode = mode
        self.member_select = discord.ui.UserSelect(placeholder="Pick a member", required=True)
        self.add_item(discord.ui.Label(text="Member", component=self.member_select))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        member = resolve_member(guild, self.member_select.values[0])

        if self.mode == "add":
            if not isinstance(member, discord.Member):
                return await reply(interaction, "That user isn't in this server.")
            message = await run_action(interaction, lambda: actions.add_attendee(self.bot, guild, actor, self.event_id, member))
        else:
            message = await run_action(interaction, lambda: actions.remove_attendee(self.bot, guild, actor, self.event_id, member))
        if message:
            await reply(interaction, message)

def resolve_member(guild: discord.Guild, user: Union[discord.Member, discord.User]) -> Union[discord.Member, discord.User]:
    """Prefer the cached guild Member (it carries roles) over the plain User a select may resolve to."""
    return guild.get_member(user.id) or user
