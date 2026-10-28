from typing import Any, Awaitable, Callable, Union
import discord
from logger import logger

GENERIC_ERROR = "Something went wrong processing that. The issue has been logged."

async def reply(interaction: discord.Interaction, content: Union[str, None] = None, **kwargs):
    """Ephemeral reply that works whether or not the interaction has already been responded to / deferred."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)

async def run_action(interaction: discord.Interaction, action: Callable[[], Awaitable[Any]], defer: bool = True) -> Any:
    """
    Defer (ephemeral), run an actions.* call, and turn refusals into an ephemeral message.
    Returns the action's result, or None if it was refused or failed (the user has already been told).
    """
    from actions import ActionError  # actions imports panel, which imports the views

    if defer and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        return await action()
    except ActionError as e:
        await reply(interaction, str(e))
    except Exception as e:
        logger.error("Unhandled error in panel action: %s", e, exc_info=e)
        try:
            await reply(interaction, GENERIC_ERROR)
        except discord.HTTPException:
            pass
    return None

def guild_member(interaction: discord.Interaction) -> Union[tuple[discord.Guild, discord.Member], None]:
    if interaction.guild and isinstance(interaction.user, discord.Member):
        return interaction.guild, interaction.user
    return None

class ConfirmView(discord.ui.View):
    """Ephemeral Yes/No confirmation for destructive actions. Only the person who asked can answer."""

    def __init__(self, invoker: discord.abc.User, on_confirm: Callable[[discord.Interaction], Awaitable[None]],
                 confirm_label: str = "Yes, do it"):
        super().__init__(timeout=120)
        self.invoker = invoker
        self.on_confirm = on_confirm
        self.confirm_btn.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("Only the person who asked can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Working on it…", view=None)
        await self.on_confirm(interaction)

    @discord.ui.button(label="No, keep it", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Nothing changed.", view=None)
