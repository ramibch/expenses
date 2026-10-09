# Expenses Tracker

A Telegram bot that turns a chat message into a tracked expense — and sends you
**daily, weekly and monthly spending reports** automatically.

Send `coffee 3.5`, `train ticket 4.80`, or just a **photo of a receipt**. The bot
extracts the name, amount, currency, date and category with an LLM, stores it, and
keeps your books up to date. No spreadsheet, no forms, no app to install.

<!-- TODO: add a screenshot/GIF of the bot in your Telegram chat:
![Expenses Tracker](docs/screenshot.png)
-->

## Highlights

- **3-second entry** — type it the way you'd tell a friend, or snap a receipt.
- **AI parsing** — free-form text and receipt photos become structured expenses.
- **Automatic categorisation** — 30+ categories (Groceries, Dining, Rent, Travel…).
- **Any currency** — reports group spendings by currency and by category.
- **Scheduled reports** — daily / weekly / monthly summaries, opt-in per user.
- **Private** — each user's data stays with their own bot instance.
- **Open source** — MIT licensed, self-hostable end to end.

## How it works

1. **Onboarding** — on first contact the bot asks for a default currency and which
   reports the user wants (daily / weekly / monthly / none). Expenses are only
   processed once onboarding is complete.
2. **Capture** — any text with an amount, or a photo, is sent to a Nebius Token
   Factory model that returns a single JSON expense object.
3. **Store & confirm** — the validated expense is saved and confirmed in the chat.
4. **Report** — scheduled jobs aggregate the user's expenses by currency and
   category and message the summary.

## AI models: Nebius Token Factory + NVIDIA Nemotron

This project makes **runtime calls to the Nebius Token Factory inference API**
(`https://api.tokenfactory.nebius.com/v1/`, OpenAI-compatible) for every expense,
and uses an **NVIDIA open source model**:

| Purpose | Model | Config |
| --- | --- | --- |
| Text expenses | [`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`](https://huggingface.co/nvidia) (Nemotron) | `NEBIUS_TEXT_MODEL_ID` |
| Receipt photos | `openbmb/MiniCPM-V-4_5` (vision) | `NEBIUS_VISION_MODEL_ID` |

Why Nemotron Nano via Token Factory: expense extraction is a high-frequency,
low-latency task. A fast, cheap open model keeps the bot responsive and keeps API
credits low, while still producing clean structured JSON. See
[`utils/ai.py`](utils/ai.py) for the client and
[`tracking/handlers.py`](tracking/handlers.py) for the extraction prompts.

**Track:** Best Apps and Agents — a productivity app someone would actually use,
powered by Nemotron on Nebius through Token Factory.

## Architecture

```
Telegram  ──webhook──▶  Django (tracking.views.telegram_webhook)
                              │
                              ▼
                        tracking.handlers
                    ┌─────────┴──────────┐
                    ▼                    ▼
             utils.ai (Nebius)      tracking.models
             Token Factory          TelegramUser / Expense
                    │
        scheduled jobs (tracking.tasks) ──▶ reports back to Telegram
```

- `config/` — Django project settings and URLs.
- `tracking/` — models, the webhook/REST handlers, onboarding, reports, admin,
  the landing page (`templates/home.html`).
- `utils/` — `ai.py` (Token Factory client) and `bot.py` (Telegram API client).

## Tech stack

- Python 3.12, Django 6, Gunicorn, WhiteNoise
- Nebius Token Factory (OpenAI-compatible) via the `openai` SDK
- SQLite by default (swap in any Django database)
- Huey + Redis for scheduled background reports

## Getting started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Nebius Token Factory API key from [dev.nebius.com](https://dev.nebius.com/)
- Redis (for the background report consumer; not needed in immediate mode)

### 1. Install

```bash
uv sync
# or install the dependencies listed in pyproject.toml into a virtualenv with pip
```

### 2. Configure

Copy `.env-example` to `.env` and fill it in:

| Variable | Description |
| --- | --- |
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `true` for local development |
| `ALLOWED_HOSTS` | Comma-separated hosts, e.g. `localhost,example.com` |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins, e.g. `https://example.com` |
| `NEBIUS_API_KEY` | Token Factory API key |
| `TELEGRAM_BOT_API_KEY` | Telegram bot token |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Random string; Telegram echoes it in `X-Telegram-Bot-Api-Secret-Token` |
| `BASE_WEB_URL` | Public HTTPS base URL, e.g. `https://example.com` |
| `HTTPS` | `true` in production to enable HSTS / secure cookies / SSL redirect |
| `HUEY_IMMEDIATE` | `true` runs tasks synchronously in-process (dev); `false` uses Redis (default: `DEBUG`) |
| `REDIS_HOST` | Redis host for the Huey broker (default `localhost`) |
| `REDIS_PORT` | Redis port for the Huey broker (default `6379`) |
| `REDIS_DB` | Redis database index for the Huey broker (default `0`) |

### 3. Migrate and run

```bash
uv run python manage.py migrate
uv run python manage.py runserver
```

Open <http://localhost:8000/> for the landing page, or `/admin/` to browse data.

### 4. Connect Telegram

Expose the app publicly (the webhook must be HTTPS) and register the webhook:

```bash
uv run python manage.py shell -c "from utils.bot import Bot; print(Bot.set_webhook())"
```

Then message your bot: `coffee 3.5` → it should reply with the parsed expense.

## Scheduled reports

The report jobs are [Huey](https://huey.readthedocs.io/) periodic tasks defined in
`tracking/tasks.py`:

| Task | Schedule (UTC) |
| --- | --- |
| `task_daily_report_to_telegram_users` | every day at 21:00 |
| `task_weekly_report_to_telegram_users` | Sundays at 21:00 |
| `task_monthly_report_to_telegram_users` | 1st of the month at 08:00 |

Huey is configured in `config/settings.py` (`HUEY = {...}`) and uses Redis as its
broker. Start Redis, then run the consumer — it also runs the scheduler, so the
periodic tasks fire on their crontab:

```bash
python manage.py run_huey
```

For local development you can skip Redis by running in immediate mode
(`HUEY_IMMEDIATE=true`), which executes tasks synchronously in-process. Note that
immediate mode does not schedule periodic tasks.

## Deployment

The project is Gunicorn + WhiteNoise ready: collect static files, run migrations,
and serve with Gunicorn behind a reverse proxy that terminates HTTPS.

```bash
uv run python manage.py collectstatic --noinput
uv run python manage.py migrate
uv run gunicorn config.wsgi:application --bind 0.0.0.0:8000
```

Run the Huey consumer as a **separate process** so the scheduled reports fire
(see [Scheduled reports](#scheduled-reports)):

```bash
uv run python manage.py run_huey
```

Set `DEBUG=false`, a real `ALLOWED_HOSTS`, `HTTPS=true`, and `BASE_WEB_URL` to your
public URL. Remember to (re)register the webhook after deploying.

## Tests

```bash
uv run python manage.py test tracking
```

## Hackathon submission

- **Event:** Nebius x NVIDIA Global AI Hackathon
- **Track:** Best Apps and Agents
- **Demo video (≤ 3 min):** <!-- TODO: YouTube link -->
- **Live demo:** <!-- TODO: hosted URL -->
- **Builder feedback:** <!-- TODO: link/paste your Token Factory + NVIDIA feedback -->

## License

[MIT](LICENSE) © 2026 Rami
