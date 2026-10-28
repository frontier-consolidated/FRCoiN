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

## 4. Upgrading from v1 to V2

The upgrade is a straight file replacement:

1. Replace the repo's files with the V2 files on `main`, push, and run the **Deploy (prod)** workflow.
2. On first start the bot migrates the database itself, re-syncs slash commands, and adds the My Points button to the leaderboard. No new environment variables or secrets are needed.
3. In Discord: give the bot's role **Manage Roles**, then run `/setup panel_channel:#frcoin-panel`.
4. Tell officers that `org_only` is gone from `/snapshot_event` — use an attendance event to record Community Members.

**Is a backup required?** No. The schema changes are additive only (new columns on `events`, new config keys). `users`, `dkp_log` and `attendance` are not changed, so balances and history carry over untouched.

**Optional precaution:** run `/export_leaderboard` before deploying and keep the `.xlsx` (restore with `/import_leaderboard`). That file covers balances only; for event history and the DKP log as well, take a database dump:
`pg_dump "$DATABASE_URL" > frcoin-backup.sql`

Best done **between events**. If a v1 event is open during the upgrade, it becomes an open DKP event and gets a card on startup. It has no stored voice scope, so its snapshots use the officer's current voice channel, like v1.

**Rollback:** redeploy the previous `main` commit. v1 ignores the new columns, so it runs fine against the migrated database.
