# Configuration

Set these in the host's environment, not in git and not in the browser. Names only are listed here. Generate each secret locally. Do not reuse a short or previously published password. After changing a Vercel environment variable, redeploy so the function picks it up. `CRON_SECRET` in particular is captured with the deployment.

## Required before the website can log in

Deploying this version without these three values makes login return "Authentication is not configured".

| Name | Rule |
|---|---|
| `DATABASE_URL` | Postgres URL. For the one-shot transfer use a direct session URL. For the Vercel function use the pooler URL with `sslmode=require`. |
| `JWT_SECRET` | At least 32 characters, no spaces. |
| `ADMIN_USERNAME` | Letters, digits, `.`, `_`, `-`. |
| `ADMIN_PASSWORD` | At least 12 characters, no line breaks. |

`DATABASE_URL` was already required. The username, password, and signing secret no longer have fallback values.

## Optional website

| Name | Effect when unset |
|---|---|
| `TEST_USERNAME` and `TEST_PASSWORD` | Demo login stays off. Both must be set, the password must be 12+ characters, and the username must differ from the admin. Demo responses are fixed fake rows, never the customer table. |
| `SETUP_TOKEN` | `POST /api/setup` stays 404. When set (16+ letters, digits, `_`, `-`), that header may create the existing `customers` table only. It does not create reminder or Telegram tables. |
| `DEFAULT_COUNTRY_CODE` | Defaults to `91`. National numbers without `+` or `00` get this code. International numbers must start with `+` or `00`. |
| `REMINDER_MESSAGE_TEMPLATE` | Uses the built-in renewal wording. Placeholders: `{name}` `{profile_name}` `{account}` `{expiry_phrase}` `{expiry_date}` `{days_left}` `{amount}`. Other `{...}` tokens are removed and are not evaluated. |

## Reminders (off until all of the send switches are set)

| Name | Effect when unset |
|---|---|
| `CRON_SECRET` | `GET /api/cron/reminders` returns 401 and does not read the database. 16+ letters, digits, `_`, `-`, no spaces. Vercel sends it as `Authorization: Bearer`. |
| `TELEGRAM_BOT_TOKEN` | Reminders stay disabled. Shape is the numeric id, a colon, then the token. |
| `TELEGRAM_CHAT_ID` | Must be a positive integer (a private user chat). Negative group or channel ids are rejected. |
| `TELEGRAM_OWNER_IDS` | If set, `TELEGRAM_CHAT_ID` must be one of these ids or reminders refuse to send. |
| `REMINDERS_DRY_RUN` | Defaults to dry-run. Live sends require the exact value `false`. |
| `REMINDER_MILESTONE_DAYS` | Defaults to `2,0`. Only days 0 through 14, at most five values. Negative days are rejected, so overdue customers are not a flood. |

A dry-run with the bot token and chat id set reads customers and returns counts only. It does not write the delivery table and does not call Telegram.

## Telegram commands (off until all three are set)

| Name | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Same bot as reminders. |
| `TELEGRAM_WEBHOOK_SECRET` | 16+ characters matching `[A-Za-z0-9_-]`. Telegram sends it as `X-Telegram-Bot-Api-Secret-Token`. |
| `TELEGRAM_OWNER_IDS` | Comma-separated positive user ids, at most five. Every message and button is ignored unless the private chat id equals that user id and the id is on this list. |
| `TELEGRAM_CONFIRM_TTL_SECONDS` | Optional. Defaults to 600. Clamped between 30 and 3600. |

There is no command to choose a different chat. The process will not send to an id outside the allowlist.

## Transfer tool

| Name | Purpose |
|---|---|
| `SOURCE_DATABASE_URL` | Read-only side. Direct connection, not the transaction pooler. |
| `TARGET_DATABASE_URL` | Side that receives a restore or a delta. |

Do not put either URL on the command line. The tool prints `host:port/database` only.

## Schema the operator applies by hand

`migrations/001_customers.sql` is the existing table. On a new database its `created_at` default is the Asia/Kolkata date. The application also writes that date on insert, so an older database whose default is still the server date does not need an alter for new rows. `002_reminder_deliveries.sql` and `003_telegram.sql` are new. All three are `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`. They do not drop, update, or delete rows. Ordinary website requests do not run them. Status and days left use Asia/Kolkata in the website, API, bot, and reminders.

Reminder rows are unique on customer id + expiry date + milestone (`days:2`, `days:0`). A failed send can be retried the same day. A successful send is not repeated for that expiry. The next cycle has a new expiry, so it can remind again. The claim stays `sending` for 10 minutes so two overlapping runs do not both send.
