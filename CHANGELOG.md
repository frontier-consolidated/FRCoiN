# Changelog

All notable changes to FRCoiN are documented here.

## 2026-10-25

### Added

- New "officer" permission tier (`officer_roles`, configurable via `/setup`), with an `is_officer()` helper that lets officers run the day-to-day event/adjustment commands without full admin access.
- `/dkp_log` is now self-service: anyone can view their own DKP history (leave `member` blank); checking someone else's history still requires officer/admin or an org role. Results are paginated via a new `DkpLogBrowserView` instead of being truncated to the last 10 entries.
- `/import_leaderboard` now automatically backs up the current leaderboard to the audit channel before wiping and replacing it. If the backup can't be sent (e.g. no audit channel configured), the import is aborted instead of proceeding with no safety net.
- A global error handler (`bot.tree.error` and `PointsModal.on_error`) now catches unhandled command/modal exceptions, logs them, and replies with a friendly message instead of a silent "application did not respond" failure.
- Startup now fails fast with a clear message if `DISCORD_TOKEN` or `DATABASE_URL` is missing, instead of an opaque error deep in `discord.py`/`asyncpg`.
- `/setup` can now update a single option at a time — every parameter is optional, and only the options you pass are changed. Pass `none` for `org_roles`/`officer_roles` to explicitly clear them.

### Changed

- `/spend` now accepts an optional `amount` (deduct a specific amount, clamped at 0) and a required `reason`, instead of unconditionally resetting a member's DKP to 0 with a hardcoded reason.
- `/snapshot_event`'s `org_only` option now defaults to `True` (org members only) instead of `False`, so officers no longer need to remember to enable it for routine events. Pass `org_only:False` to capture everyone, including non-org guests.
- `/start_event`, `/snapshot_event`, `/end_event`, `/cancel_event`, `/event_add`, `/event_remove`, `/event_attendance`, `/events_saved`, `/event_export`, `/adjust`, `/dkp_log` (viewing others), and `/status` now accept the Officer tier, not just Admin.
- `get_dkp_log_entries`'s default fetch limit increased from 10 to 500 to support pagination.

### Fixed

- `/event_remove`'s audit embed logged the raw event database record instead of the event ID.

### Docs

- `README.md` gained a Features/Commands overview.
- `Officer_Guide.docx` corrected stale command names (`/event_list` → `/event_attendance`, `/export` → `/export_leaderboard`), added a Permission Tiers section and per-command tier annotations, documented the redesigned `/spend` and per-option `/setup`, added `/dkp`/`/dkp_log`/`/status` to the command reference, and updated the `/snapshot_event` walkthrough for the new `org_only` default.
- Removed all emoji from bot messages, embeds, and source comments.
