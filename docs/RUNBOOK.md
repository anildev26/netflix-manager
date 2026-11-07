# Runbook

Do one stage at a time. Later stages are not permission to skip the checks in the earlier ones. This repository has not taken a production backup, has not opened the live database, and has not sent a Telegram or WhatsApp message.

## Stage 0. Review and test locally

This stage is the only one that is safe to do before any host or database change.

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests -q
```

Read the diff with attention to `server.py`, `netflix_manager/`, `migrations/`, and `scripts/transfer_db.py`. Confirm `vercel.json` has no `crons` entry.

## Stage 1. Deploy the code without turning on Telegram

The new code refuses to log in until `JWT_SECRET`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` are set. Set those, and the existing `DATABASE_URL`, before deploying. Do not set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `CRON_SECRET`, or `REMINDERS_DRY_RUN=false` yet.

Do not paste `deploy/cron-snippet.json` into `vercel.json` in this stage. Deploying that snippet registers a daily job.

After deploy, log in and confirm the customer count matches what you expect. Do not import or renew during the later copy window.

Rollback for this stage only: redeploy the previous git revision and keep the same `DATABASE_URL`. No rows have moved.

## Stage 2. Back up the source

Use a direct Postgres URL (Supabase session/direct, port 5432), not the transaction pooler. Poolers can reject `setval` and session state. Use `sslmode=require` when the host offers TLS.

Apply nothing to the source. The backup connection is read-only. `apply-schema` is only for the empty target in the next stage.

```bash
export SOURCE_DATABASE_URL='...'   # shell only, not git
python3 scripts/transfer_db.py backup \
  --out .local-backups/customers.snapshot.json \
  --confirm-source HOST:PORT/DATABASE
```

`--confirm-source` must equal `host:port/database` with no user or password. `.local-backups/` is gitignored. The file contains customer rows. Keep it private and delete it when you no longer need it.

If the command reports an unexpected table, stop. The live schema was not inspected when this tool was written. Either accept the gap with `--ignore-extra-tables` after you know the extra table is unrelated, or copy that table another way. A column mismatch later means the target was not created from `migrations/`. The tool will not drop or alter columns to force a fit.

`migrate_data.py` must not be used. It inserts duplicates.

## Stage 3. Restore onto Neon and validate

Create an empty Neon database yourself. Apply schema, then restore. This writes the target only after you opt in.

```bash
export TARGET_DATABASE_URL='...'
python3 scripts/transfer_db.py apply-schema \
  --confirm-target HOST:PORT/DATABASE \
  --commit \
  --write-window-acknowledged writes-frozen
python3 scripts/transfer_db.py restore \
  --file .local-backups/customers.snapshot.json \
  --confirm-target HOST:PORT/DATABASE \
  --commit \
  --write-window-acknowledged writes-frozen
python3 scripts/transfer_db.py validate \
  --file .local-backups/customers.snapshot.json \
  --confirm-target HOST:PORT/DATABASE
```

Without `--commit` and `--write-window-acknowledged writes-frozen`, restore loads the rows and rolls them back. `setval` is not undone by a Postgres rollback, so the tool puts the previous sequence values back after a dry-run.

Restore refuses a target that already has rows. It does not delete them.

Check the command output for row counts only. Do not print the snapshot file.

Validation compares row hashes, `created_at`, and sequence `last_value` / `is_called`. It does not print names or phones. Ids are preserved, so the next insert continues after the old sequence.

A practice dry-run against Neon, before `--commit`, is:

```bash
python3 scripts/transfer_db.py dry-run \
  --confirm-source SOURCE_HOST:PORT/DB \
  --confirm-target TARGET_HOST:PORT/DB
```

The source and target identities must differ. The source is only read.

## Stage 4. Cut over, and how rollback actually works

Freeze writes first: do not add, edit, import, renew, or use the bot while you switch.

1. Take a fresh backup (stage 2) after the freeze.
2. Restore that snapshot onto Neon (stage 3) and validate.
3. Change Vercel `DATABASE_URL` to the Neon **pooled** URL (`sslmode=require`) and redeploy.
4. Log in and compare the customer count with the backup count.

Rollback is not "put the old URL back" once Neon has accepted new writes.

- Before any write lands on Neon, pointing `DATABASE_URL` back at Supabase returns you to the frozen source. That is safe.
- After Neon has new or edited rows, the old database does not have those writes. `customers` has no `updated_at`, so a later backup cannot guess edits by timestamp. Use the snapshot from the freeze as the baseline:

```bash
export SOURCE_DATABASE_URL='neon-direct-url'
export TARGET_DATABASE_URL='supabase-direct-url'
python3 scripts/transfer_db.py diff \
  --file .local-backups/customers.snapshot.json \
  --confirm-target SUPABASE_HOST:PORT/DB
python3 scripts/transfer_db.py apply-delta \
  --file .local-backups/customers.snapshot.json \
  --confirm-source NEON_HOST:PORT/DB \
  --confirm-onto SUPABASE_HOST:PORT/DB
```

`diff` prints ids and the words insert, update, or delete. It does not print cell values. `apply-delta` copies changed and new rows from Neon back onto Supabase. It does **not** delete rows on Supabase unless you also pass `--propagate-deletes`. Preview it once without `--commit`; that rolls the copy back. Commit only with `--commit --write-window-acknowledged writes-frozen` while writes are still frozen. Then point `DATABASE_URL` at Supabase and redeploy.

If you cannot freeze, do not cut over.

## Stage 5. Configure Telegram privately

Create the bot and read your numeric user id yourself (BotFather, then a local `getUpdates` call). Do not paste the token into git, the website, or this chat log. The token appears in Telegram's own URL path; avoid saving that shell line in shared history.

Set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_IDS` to your positive user id
- `TELEGRAM_CHAT_ID` to that same id
- `TELEGRAM_WEBHOOK_SECRET`

Leave `REMINDERS_DRY_RUN` unset (dry-run) and do not register the webhook yet. Redeploy.

The command route stays 404 until token, webhook secret, and owner ids are all valid.

## Stage 6. Dry-run reminders

Still do not set `REMINDERS_DRY_RUN=false`.

```bash
curl -sS -H "Authorization: Bearer $CRON_SECRET" \
  https://YOUR_HOST/api/cron/reminders
```

Expect `enabled` true and `dry_run` true, with counts only. A missing delivery table is fine for a dry-run. Live sends need `migrations/002_reminder_deliveries.sql` applied first; the route then returns "Reminder storage is not initialized" instead of creating the table itself.

Registering the schedule is a separate edit: add the object in `deploy/cron-snippet.json` to `vercel.json` and redeploy. The expression `30 3 * * *` is 03:30 UTC, which is 09:00 Asia/Kolkata. There is no daylight-saving shift in India.

Vercel Hobby limits, from the current cron docs:

- The job may run only once per day. A more frequent expression fails the deploy.
- Hobby runs it sometime inside that UTC hour, so this one can fire between 03:00 and 03:59 UTC (08:30–09:29 IST). The calendar date in India does not change inside that hour, so the 2-day and expiry-day milestones stay correct. It will not be exactly 09:00.
- Cron requests hit only the production deployment, as `GET`, with user agent `vercel-cron/1.0`.
- Duration matches the project's function limit. One private message is sent per due customer, capped at 30 per run. The rest stay unsent so a same-day manual call can continue. The next automatic run is tomorrow, and tomorrow is a different milestone, so a failed "2 days before" is not retried automatically until you call the route again the same day or the expiry-day milestone arrives. Failed rows are retryable. Sent rows are not sent again for that expiry.
- Official pages: [cron jobs](https://vercel.com/docs/cron-jobs), [usage](https://vercel.com/docs/cron-jobs/usage-and-pricing), [managing and securing](https://vercel.com/docs/cron-jobs/manage-cron-jobs).

A 401 from the cron means `CRON_SECRET` was missing or did not match. Fix the variable and redeploy. The route ignores `chat_id` in the query string and in the body.

## Stage 7. One real reminder to yourself

Only after you want a message in your own Telegram chat:

1. Apply `migrations/002_reminder_deliveries.sql` on the database the app is using.
2. Set `REMINDERS_DRY_RUN=false` and redeploy.
3. Call the route once, or wait for the cron if you added it.
4. Confirm the message is in your private chat and the WhatsApp link opens a draft. Nothing is sent to the customer.

Set `REMINDERS_DRY_RUN` back to `true` if you want to stop sends without removing the token.

## Stage 8. Enable commands

Apply `migrations/003_telegram.sql` before the webhook. Then register it yourself:

```bash
curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=https://YOUR_HOST/api/telegram/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
```

Try `/help`, `/list`, and `/show` on a known id. Delete, renew, payment changes, and CSV import ask for a confirm button. The button expires (10 minutes by default) and works once. Renew shows the new start and expiry before you confirm. A second tap does not renew again.

To stop commands, delete the webhook (`deleteWebhook`) or remove `TELEGRAM_WEBHOOK_SECRET`.

## Command coverage and gaps

The bot can list and filter (all, active, expiring, expired), search name, phone, or account, show one id, add, edit one field at a time, delete, renew, change payment status without moving the date, export CSV, and import CSV.

It does not:

- charge a payment or send a WhatsApp message
- manage a Netflix account inventory (the product has no such table)
- bulk-delete
- update existing rows from a CSV (import only appends, skips duplicates, and caps the file at 200 rows and 200 KB)
- offer the website's demo login

CSV export prefixes cells that start with `=`, `+`, `-`, `@`, or a tab so a spreadsheet does not treat them as formulas. The website export and import use the same rules.

## Clock

Reminders and the bot use Asia/Kolkata. The website still uses the server's local date, which is UTC on Vercel. Between 00:00 and 05:30 IST the badge and a bot list can disagree by one day. The 09:00 reminder window is on the same India date as UTC. Renewal math does not use "today" at all.

## Monitoring

After a live reminder or a command, check the function logs for status codes only. A 401 is an auth mismatch. A 503 on reminders or the webhook means the new table was not created. A 500 after a bad chat id means the destination was not a positive owner id. Logs are written as exception class names, not SQL or phone numbers. Do not enable Flask debug in production.
