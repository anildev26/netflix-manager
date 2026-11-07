# Netflix Manager

Small Flask admin for Netflix subscription customers. The site and the owner Telegram bot share one Postgres database and the same renewal rules: one clamped calendar month, and a late payment continues the old cycle.

Nothing in this repository turns itself on. Reminders, the Telegram webhook, and database transfer stay idle until the settings in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) are present. The ordered steps are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## What is implemented

- Website login, customer add/edit/delete, mark-paid renewal, search, filters, and CSV import/export. Status and new dates use Asia/Kolkata, the same clock as the bot and reminders.
- Owner-only Telegram commands for those same actions, with confirmation buttons.
- Daily reminder code for 2 days before expiry and the expiry day, India time, with a WhatsApp draft link. It does not send WhatsApp messages.
- Scripts to back up, restore, validate, and copy post-cutover changes between Postgres databases.

## What is not active

- No cron is registered in `vercel.json`. The schedule to add later is `deploy/cron-snippet.json`.
- The Telegram webhook is not registered with Telegram.
- `scripts/transfer_db.py` does not run unless you point it at databases and pass the confirmation flags.
- `migrate_data.py` is deprecated and refuses to run.

## Local checks

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests -q
```

The tests use synthetic rows and, when local Postgres is available, disposable databases on `127.0.0.1` only. They do not need production credentials.
