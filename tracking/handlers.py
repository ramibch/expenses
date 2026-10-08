import base64
import logging
from datetime import date

import openai
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from tracking.models import Expense, ExpenseCategory, TelegramUser
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
    '  "currency": the ISO 4217 currency code, e.g. "EUR" (string),\n'
    '  "category": exactly one of the allowed values below, as a string '
    '(use "other" if none fit).\n'
    f"Allowed category values: {', '.join(ExpenseCategory.values)}.\n"
    'Example: {"name": "Coffee", "date": "2026-10-08", "amount": 3.5, '
    '"currency": "EUR", "category": "dining"}'
)


def handle_update(update: dict) -> None:
    """React to a single Telegram Update delivered to the webhook."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id:
        return

    text = (message.get("text") or "").strip()

    if text.lower() == "ping":
        Bot.to_chat(chat_id, "pong")
    elif message.get("photo"):
        reply_with_photo_expense(update)
    elif text:
        reply_with_expense(update)


def reply_with_expense(update):
    """Parse the message text and confirm the resulting expense."""
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    _reply_with_expense(update, lambda: handle_text_expense(text))


def reply_with_photo_expense(update):
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
    _reply_with_expense(update, lambda: handle_image_expense(image_url, caption))


def _reply_with_expense(update, parse_expense):
    """Parse `update` into an expense, save it, and reply with a summary."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id:
        return

    try:
        expense = _clean_expense(parse_expense())
        from_user = message.get("from") or {}
        if from_user.get("id"):
            _save_expense(expense, from_user, update)
        else:
            logger.warning("Message has no 'from' user; expense not saved: %r", message)
        summary = (
            f"{expense['name']} — {expense['amount']} {expense['currency']}"
            f" ({ExpenseCategory(expense['category']).label})"
        )
    except openai.AuthenticationError:
        logger.exception("Nebius authentication failed - check NEBIUS_API_KEY")
        Bot.to_chat(chat_id, "I can't read expenses right now")
        return
    except Exception:
        logger.exception("Could not parse an expense for chat %s", chat_id)
        Bot.to_chat(chat_id, "Sorry, I couldn't read that as an expense.")
        return

    Bot.to_chat(chat_id, summary)


def _clean_expense(data: dict) -> dict:
    """Validate the model's JSON and normalise it to Expense field values.

    Raises ValueError when a required field is missing or malformed, so only
    valid expenses are stored.
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("missing expense name")

    try:
        amount = float(data["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid expense amount") from exc

    currency = str(data.get("currency") or "").strip().upper()
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


@transaction.atomic
def _save_expense(expense: dict, from_user: dict, update: dict) -> Expense:
    """Create or update the TelegramUser, then store the expense."""
    telegram_user, _ = TelegramUser.objects.update_or_create(
        id=from_user["id"],
        defaults={
            "is_bot": from_user.get("is_bot", False),
            "first_name": from_user.get("first_name", ""),
            "last_name": from_user.get("last_name", ""),
            "username": from_user.get("username", ""),
            "language_code": from_user.get("language_code", ""),
        },
    )
    return Expense.objects.create(
        telegram_user=telegram_user, telegram_update=update, **expense
    )


def get_image_url(update: dict) -> str | None:
    """Return a browser-openable image URL for a Telegram photo update.

    Telegram sends no URL for a photo, only ``file_id``s. The largest size is
    resolved via ``getFile`` to a ``file_path`` and turned into
    ``https://api.telegram.org/file/bot<token>/<file_path>``. Returns ``None``
    when the update has no photo or the file cannot be resolved.

    Note: this URL embeds the bot token and expires (~1h). To hand an image to
    a third-party vision API, prefer :func:`photo_data_url`.
    """
    file_path = _resolve_photo_file_path(update.get("message") or {})
    if not file_path:
        return None
    return Bot.file_url + file_path


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


def handle_image_expense(image_url, text=""):
    """Parse an expense from a photo (with an optional caption) using vision."""
    prompt = text or "Extract the expense shown in this image."
    return get_response_to_prompt(
        prompt=prompt,
        model_id=settings.NEBIUS_VISION_MODEL_ID,
        image_url=image_url,
        system_prompt=EXPENSE_SYSTEM_PROMPT,
        return_json=True,
    )
