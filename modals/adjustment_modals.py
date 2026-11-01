import re
import discord
from discord.ext import commands

import actions
from modals.member_modal import resolve_member
from views.common import guild_member, reply, run_action

class NewListModal(discord.ui.Modal, title="New adjustment list"):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.name_input = discord.ui.TextInput(placeholder="e.g. Arms Fayre 08/11", max_length=100)
        self.add_item(discord.ui.Label(text="List name", description="Shown on the card, in the audit log and in members' history.",
                                       component=self.name_input))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        name = self.name_input.value.strip()
        adj_list = await run_action(interaction, lambda: actions.create_adjustment_list(self.bot, guild, actor, name))
        if adj_list:
            await reply(interaction, f"**{name}** started (ID: `{adj_list['id']}`). Its card is at the bottom of the panel channel — "
                                     "add lines there, then Apply.")

class AddLinesModal(discord.ui.Modal, title="Add lines"):
    def __init__(self, bot: commands.Bot, list_id: int):
        super().__init__()
        self.bot = bot
        self.list_id = list_id
        self.member_select = discord.ui.UserSelect(placeholder="Pick up to 25 members", min_values=1, max_values=25, required=True)
        self.points = discord.ui.TextInput(placeholder="e.g. -40 to deduct, 5 to add", max_length=9)
        self.reason = discord.ui.TextInput(placeholder="e.g. Quantum drive", style=discord.TextStyle.paragraph,
                                           required=False, max_length=200)
        self.add_item(discord.ui.Label(text="Members", description="Each member picked gets one line with these points.",
                                       component=self.member_select))
        self.add_item(discord.ui.Label(text="Points (+ or -)", component=self.points))
        self.add_item(discord.ui.Label(text="Reason (optional)", description="Shown on the card and in the member's history.",
                                       component=self.reason))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        try:
            amount = int(self.points.value)
        except ValueError:
            return await reply(interaction, "Invalid number — enter a whole number like `-40` or `5`.")
        members = [resolve_member(guild, m) for m in self.member_select.values]
        reason = self.reason.value
        message = await run_action(interaction, lambda: actions.add_adjustment_lines(self.bot, guild, actor, self.list_id, members, amount, reason))
        if message:
            await reply(interaction, message)

def parse_line_numbers(text: str) -> list[int]:
    """'3, 7' or '4-6' or '2 5' → [2, 3, ...]. Raises ValueError on anything else."""
    numbers: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip().replace("#", "")):
        if not part:
            continue
        if m := re.fullmatch(r"(\d+)-(\d+)", part):
            low, high = sorted((int(m[1]), int(m[2])))
            if high - low > 200:
                raise ValueError(part)
            numbers.update(range(low, high + 1))
        elif part.isdigit():
            numbers.add(int(part))
        else:
            raise ValueError(part)
    if not numbers:
        raise ValueError(text)
    return sorted(numbers)

class RemoveLinesModal(discord.ui.Modal, title="Remove lines"):
    def __init__(self, bot: commands.Bot, list_id: int):
        super().__init__()
        self.bot = bot
        self.list_id = list_id
        self.numbers = discord.ui.TextInput(placeholder="e.g. 3, 7 or 4-6", max_length=100)
        self.add_item(discord.ui.Label(text="Line numbers", description="The #numbers shown on the card. Removed lines stay in the record.",
                                       component=self.numbers))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        try:
            line_nos = parse_line_numbers(self.numbers.value)
        except ValueError:
            return await reply(interaction, "Enter line numbers like `3, 7` or a range like `4-6`.")
        message = await run_action(interaction, lambda: actions.remove_adjustment_lines(self.bot, guild, actor, self.list_id, line_nos))
        if message:
            await reply(interaction, message)
