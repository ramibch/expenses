"""Scheduled spendings reports sent to Telegram users.

Each report groups a user's expenses by currency and then by category, summing
the amounts, e.g.::

    Your spendings today was:

        EUR
        - Dining & Cafés: 12.50
        - Groceries: 30.00

        CHF
        - Clothing & Shoes: 45.00

Users with no spendings in the period are skipped, so nobody is messaged just
because a deadline elapsed. Each report is also opt-in per user through the
``daily_report``/``weekly_report``/``monthly_report`` flags on ``TelegramUser``.
"""

import logging
from datetime import date, timedelta

from django.db.models import Sum
from django.utils import timezone

from tracking.models import Expense, ExpenseCategory
from utils.bot import Bot

logger = logging.getLogger(__name__)


def task_daily_report_to_telegram_users():
    """Report to every opted-in user the spendings they made today."""
    today = timezone.localdate()
    report_spendings_to_telegram_users(
        start=today, end=today, period_label="today", report_field="daily_report"
    )


def task_weekly_report_to_telegram_users():
    """Report to every opted-in user the spendings they made so far this week (from Monday)."""
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    report_spendings_to_telegram_users(
        start=monday, end=today, period_label="this week", report_field="weekly_report"
    )


def task_monthly_report_to_telegram_users():
    """Report to every opted-in user the spendings they made during the previous month."""
    first_of_this_month = timezone.localdate().replace(day=1)
    last_day_of_last_month = first_of_this_month - timedelta(days=1)
    first_day_of_last_month = last_day_of_last_month.replace(day=1)
    report_spendings_to_telegram_users(
        start=first_day_of_last_month,
        end=last_day_of_last_month,
        period_label="last month",
        report_field="monthly_report",
    )


def report_spendings_to_telegram_users(
    start: date, end: date, period_label: str, report_field: str
) -> None:
    """Message each opted-in user a summary of their spendings in ``[start, end]``.

    Both bounds are inclusive. Only users whose ``TelegramUser.<report_field>`` flag
    is true receive the report, and users with no expense in the range are skipped.
    A failure to send to one user is logged and does not stop the others.
    """
    rows = (
        Expense.objects.filter(
            telegram_user__is_bot=False,
            **{f"telegram_user__{report_field}": True},
            date__gte=start,
            date__lte=end,
        )
        .values("telegram_user_id", "currency", "category")
        .annotate(total=Sum("amount"))
        .order_by("telegram_user_id", "currency", "-total")
    )

    rows_by_user: dict[int, list[dict]] = {}
    for row in rows:
        rows_by_user.setdefault(row["telegram_user_id"], []).append(row)

    for telegram_user_id, user_rows in rows_by_user.items():
        text = format_spendings_report(user_rows, period_label)
        try:
            # In a private chat the user id doubles as the chat id, which is
            # where we want reports to land rather than in a shared group chat.
            Bot.to_chat(telegram_user_id, text)
        except Exception:
            logger.exception(
                "Could not send the %s report to telegram user %s",
                period_label,
                telegram_user_id,
            )


def format_spendings_report(rows, period_label: str) -> str:
    """Build the report text from aggregated ``currency``/``category``/``total`` rows.

    Currencies are sorted alphabetically and, within each currency, categories
    are ordered by descending total so the biggest spendings come first.
    """
    by_currency: dict[str, list[dict]] = {}
    for row in rows:
        by_currency.setdefault(row["currency"], []).append(row)

    lines = [f"Your spendings {period_label} was:"]
    for currency in sorted(by_currency):
        lines.append("")
        lines.append(f"    {currency}")
        for row in by_currency[currency]:
            label = ExpenseCategory(row["category"]).label
            lines.append(f"    - {label}: {row['total']:.2f}")
    return "\n".join(lines)
