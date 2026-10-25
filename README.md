# FRCoiN

Frontier Consolidated "DKP" Bot

## Setup

1. Create python virtual environment: `python -m venv .venv`
2. Install dependencies: `./.venv/Scripts/pip install .`
3. Copy `.env.example` to `.env` and fill in your bot token and database connection url
4. Run the bot: `./.venv/Scripts/python main.py`

## Features

- Event-based DKP: start/snapshot/end/cancel events, with voice-channel attendance capture and manual add/remove.
- `/snapshot_event` only captures members holding a configured org role by default — pass `org_only:False` to include non-org guests for open/alliance events.
- Manual DKP adjustment (`/adjust`) and spend-down (`/spend`, with an optional partial amount), both with a required reason — every change is written to the `dkp_log` table and the audit channel.
- Self-service `/dkp` and `/dkp_log` so members can check their own balance/history (paginated); officers and org-role holders can look up others.
- Full leaderboard export/import via `.xlsx`, with an automatic pre-import backup posted to the audit channel — the import aborts if the backup can't be sent.
- Two-tier permissions: **Admin** (full control, via `admin_roles`) and **Officer** (day-to-day event/adjustment operations, via `officer_roles`, optional) — see the command table below.
- A global error handler logs unhandled command/modal errors and replies with a friendly message instead of a silent failure.

## Commands

| Command | Tier | Notes |
|---|---|---|
| `/setup` | Admin | Config: leaderboard/audit channels, admin/officer/org roles — every option is independent, only the ones you pass are changed |
| `/start_event`, `/snapshot_event`, `/end_event`, `/cancel_event` | Officer+ | Event lifecycle |
| `/event_add`, `/event_remove`, `/event_attendance` | Officer+ | Manual attendance editing |
| `/events_saved`, `/event_export` | Officer+ | Browse/export past events |
| `/event_delete` | Admin | Permanently delete a saved event |
| `/adjust` | Officer+ | Manual DKP +/- with required reason |
| `/spend` | Admin | Deduct DKP (optional amount, or reset to 0), required reason |
| `/export_leaderboard`, `/import_leaderboard` | Admin | Full leaderboard backup/restore (auto-backs-up before import) |
| `/dkp`, `/dkp_log` | Everyone (self) / Officer+ or org role (others) | Self-service balance + paginated history |
| `/status` | Officer+ | Current config dump |

"Officer+" means the command works for anyone holding an `officer_roles` role, or any admin. If `officer_roles` is left unset via `/setup`, only admins can run those commands.

`/setup` updates only the options you pass — leave the rest blank to keep their current values. Pass `none` for `org_roles`/`officer_roles` to explicitly clear them.
