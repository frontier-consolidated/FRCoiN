# ------------------------
# HELPERS
# ------------------------

import discord
from db import get_config

async def is_admin(member: discord.Member) -> bool:
    roles = await get_config("admin_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

async def is_officer(member: discord.Member) -> bool:
    if await is_admin(member):
        return True
    roles = await get_config("officer_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

# ------------------------
# ORG ROLE FILTERING
# ------------------------
def parse_role_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of role mentions or IDs into a list of ints."""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("<@&") and part.endswith(">"):
            part = part[3:-1]
        if part.isdigit():
            ids.append(int(part))
    return ids

async def get_org_role_ids() -> list[int]:
    """Return the configured org role IDs, or [] if not set."""
    raw = await get_config("org_roles")
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]

def member_has_org_role(member: discord.Member, org_role_ids: list[int]) -> bool:
    """Return True if the member holds at least one org role."""
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(org_role_ids))
