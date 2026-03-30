# ------------------------
# AUDIT LOGGING
# ------------------------

import discord
from datetime import datetime, timezone
from db import get_config
from logger import logger

async def send_audit(guild: discord.Guild, embed: discord.Embed):
    channel_id = await get_config("audit_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel)):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning("Could not send audit log: %s", e)

def audit_embed(title: str, colour: discord.Colour = discord.Colour.blurple()) -> discord.Embed:
    return discord.Embed(title=title, colour=colour, timestamp=datetime.now(timezone.utc))
