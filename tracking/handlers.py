import base64
import calendar
import logging
import re
from datetime import date, timedelta

import openai
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from django.db import IntegrityError

from tracking.models import Expense, ExpenseCategory, OnboardingStep, TelegramUser
from tracking.tasks import format_spendings_report
from utils.ai import get_response_to_prompt
from utils.bot import Bot

logger = logging.getLogger(__name__)


EXPENSE_SYSTEM_PROMPT = (
    "You extract a single expense from the user's message.\n"
    "Reply with one JSON object and nothing else: no markdown, no code fences, "
    "no explanation.\n"
    "Use exactly these keys:\n"
    '  "name": a short description of the expense (string),\n'
    '  "date": the date of the expense as "YYYY-MM-DD" (ISO 8601), '
    "or null when it is not stated,\n"
    '  "amount": the amount as a number, without a currency symbol (number),\n'
    '  "currency": the ISO 4217 code of the currency stated in the message '
    '(string), or null when the message does not state a currency. Never guess '
    "or infer a currency,\n"
    '  "category": exactly one of the allowed values below, as a string '
    '(use "other" if none fit).\n'
    f"Today date is {date.today().isoformat()} (ISO 8601, YYYY-MM-DD).\n"
    f"Allowed category values: {', '.join(ExpenseCategory.values)}.\n"
    'Example: {"name": "Coffee", "date": "2026-10-08", "amount": 3.5, '
    '"currency": "EUR", "category": "dining"}\n'
    'Example with no stated currency: {"name": "Coffee", "date": null, '
    '"amount": 6, "currency": null, "category": "dining"}'
)


def handle_update(update: dict) -> None:
    """React to a single Telegram Update delivered to the webhook."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id:
        return

    telegram_user, _ = _upsert_telegram_user(message)
    text = (message.get("text") or "").strip()

    if text.startswith("/"):
        handle_user_command(text, chat_id, message, telegram_user)
        return

    # Messages are only actionable once a user has finished onboarding. Unknown
    # senders (no `from`, or a bot) are ignored, and a user still in onboarding is
    # walked through it first. Either way, expenses are only processed for users
    # who completed onboarding.
    if telegram_user is None or telegram_user.onboarding_step != OnboardingStep.DONE:
        if telegram_user is not None:
            answer_onboarding(telegram_user, chat_id, message)
        return

    if text.lower() == "ping":
        Bot.to_chat(chat_id, "pong")
    elif message.get("photo"):
        reply_with_photo_expense(update, telegram_user)
    elif _looks_like_expense(text):
        reply_with_expense(update, telegram_user)


def _upsert_telegram_user(message: dict) -> tuple[TelegramUser | None, bool]:
    """Create or refresh the TelegramUser for `message`'s sender.

    Returns ``(user, created)``; ``user`` is ``None`` for updates whose sender
    is missing or a bot (e.g. channel posts). A user is created at the
    ``WELCOME`` onboarding step so the conversation starts right away.
    """
    from_user = message.get("from") or {}
    if not from_user.get("id") or from_user.get("is_bot"):
        return None, False

    telegram_user, created = TelegramUser.objects.update_or_create(
        id=from_user["id"],
        defaults={
            "is_bot": from_user.get("is_bot", False),
            "first_name": from_user.get("first_name", ""),
            "last_name": from_user.get("last_name", ""),
            "username": from_user.get("username", ""),
            "language_code": from_user.get("language_code", ""),
        },
    )
    if created:
        telegram_user.onboarding_step = OnboardingStep.WELCOME
        telegram_user.save(update_fields=["onboarding_step", "updated_at"])
    return telegram_user, created


def _looks_like_expense(text: str) -> bool:
    """Cheaply decide whether `text` could describe an expense.

    An expense needs an amount and a description, so we require a digit and at
    least one whitespace-separated word. This runs on every text message to
    skip the model call for chit-chat that cannot be an expense.
    """
    return any(char.isdigit() for char in text) and len(text.split()) >= 2


def handle_user_command(cmd, chat_id, message, telegram_user=None):
    match cmd.split()[0]:
        case "/start":
            start_onboarding(telegram_user, chat_id)
        case "/deletedata":
            ask_delete_confirmation(telegram_user, chat_id)
        case "/dataconfirmdelete":
            confirm_data_delete_all(telegram_user, chat_id)
        case "/datadeletecancel":
            cancel_data_delete(telegram_user, chat_id)
        case "/deletelastexpense":
            delete_last_expense(telegram_user, chat_id)

        case "/expenseslast1y":
            report_recent_expenses(
                telegram_user, chat_id, "in the last year", months=12
            )
        case "/expenseslast6m":
            report_recent_expenses(
                telegram_user, chat_id, "in the last 6 months", months=6
            )
        case "/expenseslast3m":
            report_recent_expenses(
                telegram_user, chat_id, "in the last 3 months", months=3
            )
        case "/expenses30d":
            report_recent_expenses(
                telegram_user, chat_id, "in the last 30 days", days=30
            )
        case "/expenses7d":
            report_recent_expenses(
                telegram_user, chat_id, "in the last 7 days", days=7
            )


# Cap the number of expenses listed so the message stays within Telegram's limit.
MAX_LISTED_EXPENSES = 50

DELETE_CONFIRMATION_PROMPT = (
    "⚠️ This permanently deletes all your expenses and settings.\n"
    "Reply /dataconfirmdelete to delete everything, or /datadeletecancel to abort."
)


def ask_delete_confirmation(telegram_user, chat_id):
    """List the user's expenses and ask them to confirm deleting everything."""
    if telegram_user is None:
        Bot.to_chat(chat_id, "I don't have any data for you yet.")
        return

    expenses = Expense.objects.filter(telegram_user=telegram_user).order_by(
        "date", "id"
    )
    count = expenses.count()
    if not count:
        Bot.to_chat(chat_id, "You have no expenses to delete.")
        return

    telegram_user.pending_delete = True
    telegram_user.save(update_fields=["pending_delete", "updated_at"])

    Bot.to_chat(
        chat_id,
        f"Here are all your expenses ({count}):\n\n"
        f"{format_expense_list(expenses)}\n\n"
        f"{DELETE_CONFIRMATION_PROMPT}",
    )


def confirm_data_delete_all(telegram_user, chat_id):
    """Delete all of the user's data, only if a deletion was requested first."""
    if telegram_user is None or not telegram_user.pending_delete:
        Bot.to_chat(chat_id, "Nothing to delete. Send /deletedata first.")
        return

    count = Expense.objects.filter(telegram_user=telegram_user).count()
    # Deleting the user cascades to their expenses and clears the pending flag.
    telegram_user.delete()
    Bot.to_chat(
        chat_id,
        f"🗑️ Deleted {count} expenses and all your settings. "
        "Send /start to begin again.",
    )


def cancel_data_delete(telegram_user, chat_id):
    """Cancel a pending data deletion."""
    if telegram_user is None or not telegram_user.pending_delete:
        Bot.to_chat(chat_id, "Nothing to cancel.")
        return

    telegram_user.pending_delete = False
    telegram_user.save(update_fields=["pending_delete", "updated_at"])
    Bot.to_chat(chat_id, "Cancelled — your data is safe.")


def format_expense_list(expenses) -> str:
    """Format expenses as a numbered list, capped for Telegram's message limit."""
    expenses = list(expenses)
    shown = expenses[:MAX_LISTED_EXPENSES]
    lines = [
        f"{i}. {expense.date} — {expense.name}: {expense.amount:.2f} "
        f"{expense.currency} ({ExpenseCategory(expense.category).label})"
        for i, expense in enumerate(shown, start=1)
    ]
    remaining = len(expenses) - len(shown)
    if remaining:
        lines.append(f"… and {remaining} more")
    return "\n".join(lines)


def delete_last_expense(telegram_user, chat_id):
    """Delete the user's most recently logged expense and confirm it."""
    if telegram_user is None:
        Bot.to_chat(chat_id, "I don't have any data for you yet.")
        return

    expense = Expense.objects.filter(telegram_user=telegram_user).order_by("id").last()
    
    if expense is None:
        Bot.to_chat(chat_id, "You have no expenses to delete.")
        return

    summary = (
        f"{expense.name} — {expense.amount:.2f} {expense.currency} "
        f"({ExpenseCategory(expense.category).label})"
    )
    expense.delete()
    Bot.to_chat(chat_id, f"🗑️ Deleted your last expense:\n{summary}")


def report_recent_expenses(
    telegram_user, chat_id, period_label, *, months: int = 0, days: int = 0
):
    """Report the user's spendings over the window ending today.

    The window starts ``months`` calendar months (or ``days`` days) before today
    and runs to today, inclusive.
    """
    if telegram_user is None:
        Bot.to_chat(chat_id, "I don't have any data for you yet.")
        return

    today = timezone.localdate()
    start = _start_of_window(today, months=months, days=days)
    rows = list(
        Expense.objects.filter(
            telegram_user=telegram_user, date__gte=start, date__lte=today
        )
        .values("currency", "category")
        .annotate(total=Sum("amount"))
        .order_by("currency", "-total")
    )
    if not rows:
        Bot.to_chat(chat_id, f"You have no expenses {period_label}.")
        return

    Bot.to_chat(chat_id, format_spendings_report(rows, period_label))


def _start_of_window(today: date, *, months: int = 0, days: int = 0) -> date:
    """Return the inclusive start date of a window ending on ``today``."""
    if months:
        year, month = today.year, today.month - months
        while month < 1:
            month += 12
            year -= 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    return today - timedelta(days=days)


ONBOARDING_GREETING = (
    "👋 Welcome! Let's set up your expense tracker.\n\n"
    "Which currency do you mostly spend in? Send a 3-letter code, e.g. EUR."
)

REPORTING_PROMPT = (
    "Got it. How often would you like a spending report?\n\n"
    'Reply with any of: daily, weekly, monthly — or "none".'
)

REPORT_FIELDS = ("daily_report", "weekly_report", "monthly_report")


def start_onboarding(telegram_user: TelegramUser | None, chat_id) -> None:
    """Begin (or restart) onboarding by asking for the default currency."""
    if telegram_user is None:
        Bot.to_chat(chat_id, "Welcome")
        return
    _ask_default_currency(telegram_user, chat_id)


def answer_onboarding(telegram_user: TelegramUser, chat_id, message: dict) -> None:
    """Advance the onboarding flow with the user's latest message."""
    text = (message.get("text") or "").strip()
    match telegram_user.onboarding_step:
        case OnboardingStep.WELCOME:
            _ask_default_currency(telegram_user, chat_id)
        case OnboardingStep.CURRENCY:
            _store_default_currency(telegram_user, chat_id, text)
        case OnboardingStep.REPORTING:
            _store_report_choices(telegram_user, chat_id, text)
        case _:
            # Unknown step: restart so the user is never stuck.
            _ask_default_currency(telegram_user, chat_id)


def _ask_default_currency(telegram_user: TelegramUser, chat_id) -> None:
    telegram_user.onboarding_step = OnboardingStep.CURRENCY
    telegram_user.save(update_fields=["onboarding_step", "updated_at"])
    Bot.to_chat(chat_id, ONBOARDING_GREETING)


def _store_default_currency(telegram_user: TelegramUser, chat_id, text: str) -> None:
    currency = text.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        Bot.to_chat(chat_id, "Please send a 3-letter ISO currency code, e.g. EUR.")
        return

    telegram_user.default_currency = currency
    telegram_user.onboarding_step = OnboardingStep.REPORTING
    telegram_user.save(
        update_fields=["default_currency", "onboarding_step", "updated_at"]
    )
    Bot.to_chat(chat_id, REPORTING_PROMPT)


def _store_report_choices(telegram_user: TelegramUser, chat_id, text: str) -> None:
    choices = _parse_report_choices(text)
    if choices is None:
        Bot.to_chat(chat_id, REPORTING_PROMPT)
        return

    for field, enabled in choices.items():
        setattr(telegram_user, field, enabled)
    telegram_user.onboarding_step = OnboardingStep.DONE
    telegram_user.save(update_fields=[*REPORT_FIELDS, "onboarding_step", "updated_at"])
    Bot.to_chat(chat_id, _onboarding_summary(telegram_user))


def _parse_report_choices(text: str) -> dict[str, bool] | None:
    """Parse report choices from a free-form answer.

    Accepts any of "daily"/"weekly"/"monthly"; a negating word such as "none"
    or "never" turns every report off. Returns ``None`` when nothing is
    recognised so the caller can re-ask instead of guessing.
    """
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & {"none", "never", "off", "skip", "no"}:
        return {field: False for field in REPORT_FIELDS}

    choices = {
        "daily_report": "daily" in words,
        "weekly_report": "weekly" in words,
        "monthly_report": "monthly" in words,
    }
    if not any(choices.values()):
        return None
    return choices


def _onboarding_summary(telegram_user: TelegramUser) -> str:
    enabled = [
        period
        for field, period in (
            ("daily_report", "daily"),
            ("weekly_report", "weekly"),
            ("monthly_report", "monthly"),
        )
        if getattr(telegram_user, field)
    ]
    reports = "Reports: " + (", ".join(enabled) if enabled else "none") + "."
    return (
        f"✅ All set! Default currency: {telegram_user.default_currency}. {reports}\n\n"
        'Send me an expense any time, e.g. "coffee 3.5".'
        'You can also just send a photo of your ticket!'
    )


def reply_with_expense(update, telegram_user=None):
    """Parse the message text and confirm the resulting expense."""
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    _reply_with_expense(update, telegram_user, handle_text_expense, text)


def reply_with_photo_expense(update, telegram_user=None):
    """Parse the photo in the message and confirm the resulting expense."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    image_url = photo_data_url(message)
    if not image_url:
        Bot.to_chat(chat_id, "Sorry, I couldn't download that image.")
        return

    caption = (message.get("caption") or "").strip()
    _reply_with_expense(update, telegram_user, handle_image_expense, image_url, caption)


def _reply_with_expense(update, telegram_user, parse_expense, *args):
    """Parse `update` into an expense, save it, and reply with a summary.

    `parse_expense` is called with `args` inside the `try` below, so a parsing
    failure is reported to the user instead of crashing the webhook.
    """
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id:
        return

    # Fast path: a retried delivery is already in the DB — skip the expensive
    # AI call and don't re-send a confirmation.
    update_id = update.get("update_id")
    if update_id is not None and Expense.objects.filter(update_id=update_id).exists():
        return

    expense_summary = None

    try:
        default_currency = telegram_user.default_currency if telegram_user else None
        expense = _clean_expense(
            parse_expense(*args), default_currency=default_currency
        )
        if telegram_user is not None:
            _save_expense(expense, telegram_user, update)
            expense_summary = (
                f"✅ {expense['name']} — {expense['amount']} {expense['currency']}"
                f" ({ExpenseCategory(expense['category']).label})"
            )
        else:
            logger.warning("Message has no 'from' user; expense not saved: %r", message)
        
    except openai.AuthenticationError:
        logger.exception("Nebius authentication failed - check NEBIUS_API_KEY")
        Bot.to_chat(chat_id, f"I can't read expenses right now. Contact my developer: {settings.DEVELOPER_CONTACT_URL}")
        return
    except Exception:
        logger.exception("Could not parse an expense for chat %s", chat_id)
        Bot.to_chat(chat_id, "Sorry, I couldn't read that as an expense.")
        return

    if expense_summary:
        Bot.to_chat(chat_id, expense_summary)
    else:
        Bot.to_chat(chat_id, "I couldn't recognise that as expense")


def _clean_expense(data: dict, default_currency: str | None = None) -> dict:
    """Validate the model's JSON and normalise it to Expense field values.

    ``default_currency`` is used when the model does not produce a usable
    currency code. Raises ValueError when a required field is missing or
    malformed, so only valid expenses are stored.
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("missing expense name")

    try:
        amount = float(data["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid expense amount") from exc

    currency = str(data.get("currency") or "").strip().upper()
    if (len(currency) != 3 or not currency.isalpha()) and default_currency:
        currency = default_currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("invalid currency code")

    category = data.get("category")
    if category not in ExpenseCategory.values:
        category = ExpenseCategory.OTHER

    return {
        "name": name[:256],
        "date": _parse_expense_date(data.get("date")),
        "amount": amount,
        "currency": currency,
        "category": category,
    }


def _parse_expense_date(value) -> date:
    """Return an ISO date, defaulting to today when none is given."""
    if not value:
        return timezone.localdate()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r}") from exc


def _save_expense(expense, telegram_user, update):
    """Store the expense against an already-resolved TelegramUser.

    Returns the saved Expense, or ``None`` if this update was already
    processed (a duplicate delivery from Telegram).
    """
    try:
        return Expense.objects.create(
            telegram_user=telegram_user,
            telegram_update=update,
            update_id=update.get("update_id"),
            **expense,
        )
    except IntegrityError:
        logger.info("Ignoring duplicate Telegram update %s", update.get("update_id"))
        return None


def photo_data_url(message: dict) -> str | None:
    """Build a ``data:`` URL for a Telegram photo message.

    Telegram sends no URL for a photo, only ``file_id``s. The bytes are inlined
    as a base64 data URL so the vision API never sees the bot token that
    Telegram embeds in its own file URLs.
    """
    file_path = _resolve_photo_file_path(message)
    if not file_path:
        return None

    # Telegram photos are always JPEG.
    data = Bot.download_file(file_path)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _resolve_photo_file_path(message: dict) -> str | None:
    """Resolve the largest photo in `message` to a Telegram ``file_path``.

    The ``photo`` list holds several sizes of the same image ordered ascending,
    so the last entry is the largest.
    """
    photos = message.get("photo") or []
    if not photos:
        return None

    file_info = Bot.get_file(photos[-1]["file_id"])
    if not file_info.get("ok"):
        logger.warning("getFile failed: %s", file_info)
        return None

    return file_info["result"]["file_path"]


def handle_text_expense(text):
    """Parse free-form `text` into an expense dict using the text model."""
    prompt = f"Extract the expense from this text:\n{text}"
    return get_response_to_prompt(
        prompt=prompt,
        model_id=settings.NEBIUS_TEXT_MODEL_ID,
        system_prompt=EXPENSE_SYSTEM_PROMPT,
        return_json=True,
    )


def handle_image_expense(image_url:str, text:str|None=None):
    """Parse an expense from a photo (with an optional caption) using vision."""
    prompt = "Extract the expense shown in this image."
    if text:
        prompt += f"\nADDIONAL CONTEXT:\n{text}"
    return get_response_to_prompt(
        prompt=prompt,
        model_id=settings.NEBIUS_VISION_MODEL_ID,
        image_url=image_url,
        system_prompt=EXPENSE_SYSTEM_PROMPT,
        return_json=True,
    )
