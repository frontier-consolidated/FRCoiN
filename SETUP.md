# FRCoiN Setup

## 1. Running the bot

**Prerequisites:** Python 3.14 (see `.python-version`) or Docker, and PostgreSQL.

1. Start the database: `docker compose up -d db`. It listens on host port **6767**, so
   `DATABASE_URL=postgresql://postgres:postgres@localhost:6767/frcoin`. Change the default password for anything real.
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `DATABASE_URL`.
3. Install and run:

   | Linux / macOS | Windows |
   |---|---|
   | `python -m venv .venv` | `python -m venv .venv` |
   | `.venv/bin/pip install .` | `.venv\Scripts\pip install .` |
   | `.venv/bin/python -m main` | `.venv\Scripts\python -m main` |

   Or with Docker: `docker build -t frcoin . && docker run --env-file .env frcoin`.

**Production** is deployed by the GitHub Actions **Deploy (prod)** workflow: Actions → Deploy (prod) → *Run workflow* on `main`. Pushing alone does not deploy.

**Testing / demos:** use a separate Discord application (e.g. "FRCoiN Dev") with its own token and its own database. Never run a test copy with prod's token — it would knock the live bot offline and overwrite its slash commands.

## 2. Discord application and invite

1. Create an application and bot at <https://discord.com/developers/applications>. Under **Bot**, turn on the **Server Members Intent**.
2. Invite it with scopes `bot` and `applications.commands`, and these permissions:
   View Channels · Send Messages · Embed Links · Read Message History · Attach Files · Use Application Commands · Connect · **Manage Roles**.
   Manage Roles is only used to lock the panel channel to Officers+ (editing that channel's permission overwrites). The bot never changes anyone's roles.

## 3. Server configuration (first run)

1. Create a text channel for the panel, e.g. `#frcoin-panel`.
2. Run `/setup` (needs the Discord **Administrator** permission):
   `leaderboard_channel`, `audit_channel`, `admin_roles`, `officer_roles`, `org_roles`, `panel_channel`.
   Alternatively set `panel_channel` first, then use **Admin → Setup: Channels / Roles** with pickers.
3. Check the panel channel is locked to Officers+: `/status` shows a warning if not. To fix by hand: deny *View Channel* to @everyone, allow it to the admin and officer roles, and deny them *Send Messages*.
4. **Org roles:** only members holding an org role can earn DKP. Everyone else is a *Community Member* — recorded by attendance events, never awarded DKP.
5. Smoke test: run an attendance event, then a DKP event awarding 1 point to a test member, then check the leaderboard's **My Points**.

## 4. Upgrading from 1.x to 2.0.0

The upgrade is a straight file replacement. Allow about 10 minutes, at a quiet time between events.

### Discord prerequisites

Do these in Discord before the deploy. None of them affect 1.x, so they can be done days ahead.

**1. Bot permission.** Give the bot's role **Manage Roles** (Server Settings → Roles). The bot only uses it to set the panel channel's permissions. It never changes anyone's roles.

**2. Org roles must be set.** In 1.x they were an optional snapshot filter. In 2.0.0 **only org members can earn DKP**. If no org roles are set, DKP events and positive adjustments are blocked with a "run setup first" message. Check `/status` in 1.x: if *Org Roles* shows *(all members)*, run `/setup org_roles:@Member` (comma-separate several roles) before upgrading.

**3. Channels**

| Channel | New? | Who should see it | Suggested permissions |
|---|---|---|---|
| **Panel**, e.g. `#frcoin-panel` (text) | New, required | Admin + officer roles only | @everyone: View Channel ✗. Admin/officer roles: View Channel ✓, Read Message History ✓, Send Messages ✗. Bot: View Channel, Send Messages, Embed Links, Read Message History. **The bot sets all of this itself** when you run `/setup panel_channel`, provided it has Manage Roles. Just create the channel. |
| **Leaderboard** (existing) | No | Everyone who can earn DKP | Members: View Channel, Read Message History. Send Messages can stay off: the new **My Points** button works without it. Bot: Send Messages, Embed Links. |
| **Audit** (existing) | No | Admins/officers | Unchanged. Bot: Send Messages, Embed Links, Attach Files (for the pre-import backup). |
| **Event voice category**, e.g. `EVENTS` holding Alpha, Bravo, Comms | Recommended | As now | Put squad voice channels under one category so a single snapshot covers all of them. The bot needs **View Channel** on each voice channel it should count, so check any private ones. Officers no longer need to be in voice to snapshot. |

**4. Settings: what needs running after the update**

| Setting | Needed? | How |
|---|---|---|
| Panel channel | **Required, new** | `/setup panel_channel:#frcoin-panel` |
| Org roles | Required if not already set (see 2) | `/setup org_roles:…` |
| Leaderboard, audit channels; admin, officer roles | No, they carry over | Change later only if wanted |
| Slash command access | No, defaults to Officer+ (same as 1.x) | Switch to Admin-only later, in rollout phase 2 |

After the panel channel is set, everything else can also be changed from **Admin → Setup: Channels / Roles** using pickers. `/setup` and the Setup buttons need the Discord **Administrator** permission, as in 1.x.

**Before** (with the Discord prerequisites above done):
1. Finish or cancel any open event (`/status` shows it). An open 1.x event does carry over, but it's simpler not to have one.
2. Tell officers there's a short freeze: no FRCoiN commands until the panel appears in `#frcoin-panel`. While the deploy swaps the old bot for the new one, both can briefly be online at the same time and answer the same command twice, and any form someone has open in the old bot is lost.
3. Optional: run `/export_leaderboard` and keep the file (see below).

**Deploy:**
4. Keep 1.x as a branch, then put 2.0.0 on `main` (the deploy workflow only runs from `main`):
   - create a branch `v1` and a tag `v1-final` from the current `main` and push both;
   - replace the files on `main` with the 2.0.0 files, push, and run the **Deploy (prod)** workflow.
5. On first start the bot migrates the database itself, re-syncs slash commands, and adds the My Points button to the leaderboard. No new environment variables or secrets are needed.

**After:**
6. Run `/setup panel_channel:#frcoin-panel`. The lobby should appear and the channel should become hidden from regular members. New commands (`/adjustment_mode`, `/admin_mode`) and options can take a minute to show up. Restart Discord (Ctrl+R) if they don't.
7. Run `/status`: it should show no lockdown warning. If it does (e.g. Manage Roles was granted afterwards), grant the permission and run the same `/setup panel_channel:#frcoin-panel` again, which re-applies the lockdown. Check the leaderboard has a **My Points** button.
8. Smoke test: **Take Attendance** → Snapshot → Close. Nobody's points change, and the audit channel shows the close.
9. Lift the freeze. Tell officers that `org_only` is gone from `/snapshot_event` — use an attendance event to record Community Members.

**Is a backup required?** No. The schema changes are additive only (new columns on `events`, new config keys). `users`, `dkp_log` and `attendance` are not changed, so balances and history carry over untouched.

**Optional precaution:** run `/export_leaderboard` before deploying and keep the `.xlsx` (restore with `/import_leaderboard`). That file covers balances only; for event history and the DKP log as well, take a database dump:
`pg_dump "$DATABASE_URL" > frcoin-backup.sql`

Best done **between events**. If a 1.x event is open during the upgrade, it becomes an open DKP event and gets a card on startup. It has no stored voice scope, so its snapshots use the officer's current voice channel, like 1.x.

**Rollback:** point `main` back at the `v1` branch (`git reset --hard v1-final`, force-push `main`) and run the **Deploy (prod)** workflow again. A copy of 1.x is also kept on the `main` branch of the personal backup repo. 1.x ignores the new columns, so it runs fine against the migrated database. First close or cancel any open 2.0.0 events: 1.x can only track one open event, so it would only see the newest. If you later upgrade again, events that 1.x ended are recognised as closed, and they're never awarded twice.
