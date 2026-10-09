import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import openai
from django.conf import settings
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracking.handlers import (
    _clean_expense,
    _looks_like_expense,
    handle_text_expense,
    handle_update,
    photo_data_url,
)
from tracking.models import Expense, ExpenseCategory, OnboardingStep, TelegramUser
from tracking.tasks import (
    format_spendings_report,
    task_daily_report_to_telegram_users,
    task_monthly_report_to_telegram_users,
    task_weekly_report_to_telegram_users,
)

SENDER = {
    "id": 42,
    "is_bot": False,
    "first_name": "Rami",
    "last_name": "Boutassghount",
    "username": "ramib_ch",
    "language_code": "en",
}


class TelegramWebhookTests(TestCase):
    def setUp(self):
        # An already-onboarded user, so the webhook exercises the expense flow
        # instead of triggering onboarding.
        TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )

    def _post(self, text):
        return self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps(
                {"message": {"chat": {"id": 42}, "from": SENDER, "text": text}}
            ),
            content_type="application/json",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET_TOKEN
            },
        )

    @patch("tracking.handlers.Bot.to_chat")
    def test_ping_replies_pong(self, to_chat):
        response = self._post("ping")

        self.assertEqual(response.status_code, 200)
        to_chat.assert_called_once_with(42, "pong")

    @patch("tracking.handlers.Bot.to_chat")
    def test_ping_is_case_insensitive(self, to_chat):
        self._post("Ping")

        to_chat.assert_called_once_with(42, "pong")

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_expense_text_is_saved_and_confirmed(self, get_response, to_chat):
        get_response.return_value = {
            "name": "Coffee",
            "date": "2026-10-01",
            "category": "dining",
            "amount": 3.5,
            "currency": "eur",
        }

        self._post("coffee 3.5 eur")

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.username, "ramib_ch")
        self.assertEqual(user.first_name, "Rami")

        expense = Expense.objects.get()
        self.assertEqual(expense.telegram_user, user)
        self.assertEqual(expense.name, "Coffee")
        self.assertEqual(expense.amount, 3.5)
        self.assertEqual(expense.currency, "EUR")
        self.assertEqual(expense.category, ExpenseCategory.DINING)
        self.assertEqual(expense.date.isoformat(), "2026-10-01")
        self.assertEqual(expense.telegram_update["message"]["text"], "coffee 3.5 eur")

        (chat_id, message), _ = to_chat.call_args
        self.assertEqual(chat_id, 42)
        self.assertIn("Coffee", message)

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt", side_effect=ValueError("bad json"))
    def test_unparseable_text_is_not_saved(self, get_response, to_chat):
        self._post("coffee 3.5")

        self.assertEqual(Expense.objects.count(), 0)
        to_chat.assert_called_once_with(42, "Sorry, I couldn't read that as an expense.")

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_invalid_amount_is_not_saved(self, get_response, to_chat):
        get_response.return_value = {
            "name": "Coffee",
            "amount": "not a number",
            "currency": "EUR",
            "category": "dining",
        }

        self._post("coffee 3.5")

        self.assertEqual(Expense.objects.count(), 0)
        to_chat.assert_called_once_with(42, "Sorry, I couldn't read that as an expense.")

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_auth_error_is_not_saved(self, get_response, to_chat):
        get_response.side_effect = openai.AuthenticationError(
            "unauthorized",
            response=MagicMock(status_code=401, request=MagicMock()),
            body=None,
        )

        self._post("45 CHF in shoes")

        self.assertEqual(Expense.objects.count(), 0)
        to_chat.assert_called_once_with(
            42,
            "I can't read expenses right now. "
            f"Contact my developer: {settings.DEVELOPER_CONTACT_URL}",
        )

    @patch("tracking.handlers.Bot.to_chat")
    def test_message_without_chat_is_ignored(self, to_chat):
        self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps({"message": {"from": SENDER, "text": "no chat here"}}),
            content_type="application/json",
        )

        to_chat.assert_not_called()


class HandleTextExpenseTests(TestCase):
    @patch("tracking.handlers.get_response_to_prompt")
    def test_parses_json_via_ai_client(self, get_response):
        get_response.return_value = {
            "name": "Bus",
            "category": "public_transport",
            "amount": 2,
            "currency": "EUR",
        }

        result = handle_text_expense("bus ticket 2 eur")

        self.assertEqual(result["name"], "Bus")
        kwargs = get_response.call_args.kwargs
        self.assertTrue(kwargs["return_json"])
        self.assertTrue(kwargs["model_id"])
        self.assertIn("bus ticket 2 eur", kwargs["prompt"])


class LooksLikeExpenseTests(TestCase):
    def test_accepts_amount_with_description(self):
        self.assertTrue(_looks_like_expense("coffee 3.5 eur"))
        self.assertTrue(_looks_like_expense("Bus ticket 2"))

    def test_rejects_text_without_a_number(self):
        self.assertFalse(_looks_like_expense("coffee"))
        self.assertFalse(_looks_like_expense("hello there"))

    def test_rejects_text_without_a_description(self):
        self.assertFalse(_looks_like_expense("3.5"))
        self.assertFalse(_looks_like_expense(""))
        self.assertFalse(_looks_like_expense("   "))


class CleanExpenseTests(TestCase):
    def test_falls_back_to_default_currency(self):
        cleaned = _clean_expense({"name": "X", "amount": 1}, default_currency="chf")

        self.assertEqual(cleaned["currency"], "CHF")

    def test_null_currency_falls_back_to_default(self):
        # The model returns null when the message states no currency.
        cleaned = _clean_expense(
            {"name": "Coffee", "amount": 6, "currency": None},
            default_currency="CHF",
        )

        self.assertEqual(cleaned["currency"], "CHF")

    def test_model_currency_wins_over_default(self):
        cleaned = _clean_expense(
            {"name": "X", "amount": 1, "currency": "usd"}, default_currency="EUR"
        )

        self.assertEqual(cleaned["currency"], "USD")

    def test_normalises_fields(self):
        cleaned = _clean_expense(
            {"name": " Coffee ", "amount": "3.5", "currency": "eur", "category": "dining"}
        )

        self.assertEqual(cleaned["name"], "Coffee")
        self.assertEqual(cleaned["amount"], 3.5)
        self.assertEqual(cleaned["currency"], "EUR")
        self.assertEqual(cleaned["category"], ExpenseCategory.DINING)
        self.assertEqual(cleaned["date"], timezone.localdate())

    def test_unknown_category_falls_back_to_other(self):
        cleaned = _clean_expense(
            {"name": "X", "amount": 1, "currency": "EUR", "category": "nonsense"}
        )

        self.assertEqual(cleaned["category"], ExpenseCategory.OTHER)

    def test_invalid_date_raises(self):
        with self.assertRaises(ValueError):
            _clean_expense(
                {
                    "name": "X",
                    "amount": 1,
                    "currency": "EUR",
                    "category": "dining",
                    "date": "not-a-date",
                }
            )

    def test_missing_amount_raises(self):
        with self.assertRaises(ValueError):
            _clean_expense({"name": "X", "currency": "EUR", "category": "dining"})


class PhotoExpenseTests(TestCase):
    def setUp(self):
        TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )

    def _post_photo(self, photos):
        return self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps(
                {"message": {"chat": {"id": 42}, "from": SENDER, "photo": photos}}
            ),
            content_type="application/json",
            headers={
                "X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET_TOKEN
            },
        )

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    @patch("tracking.handlers.Bot.download_file", return_value=b"jpeg-bytes")
    @patch(
        "tracking.handlers.Bot.get_file",
        return_value={"ok": True, "result": {"file_path": "photos/f1.jpg"}},
    )
    def test_photo_is_inlined_sent_to_vision_and_saved(
        self, get_file, download_file, get_response, to_chat
    ):
        get_response.return_value = {
            "name": "Shoes",
            "date": None,
            "category": "clothing",
            "amount": 45,
            "currency": "CHF",
        }

        response = self._post_photo([{"file_id": "small"}, {"file_id": "large"}])

        self.assertEqual(response.status_code, 200)
        # The largest size is the last entry in Telegram's photo list.
        get_file.assert_called_once_with("large")
        download_file.assert_called_once_with("photos/f1.jpg")
        kwargs = get_response.call_args.kwargs
        self.assertTrue(kwargs["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(kwargs["model_id"], settings.NEBIUS_VISION_MODEL_ID)

        expense = Expense.objects.get()
        self.assertEqual(expense.name, "Shoes")
        self.assertEqual(expense.category, ExpenseCategory.CLOTHING)
        self.assertEqual(expense.date, timezone.localdate())
        self.assertEqual(expense.telegram_user_id, 42)
        self.assertEqual(len(expense.telegram_update["message"]["photo"]), 2)

        (chat_id, message), _ = to_chat.call_args
        self.assertEqual(chat_id, 42)
        self.assertIn("Shoes", message)

    @patch("tracking.handlers.Bot.to_chat")
    @patch(
        "tracking.handlers.Bot.get_file",
        return_value={"ok": False, "description": "file not found"},
    )
    def test_getfile_failure_reports_error(self, get_file, to_chat):
        self._post_photo([{"file_id": "x"}])

        self.assertEqual(Expense.objects.count(), 0)
        to_chat.assert_called_once_with(42, "Sorry, I couldn't download that image.")


class PhotoDataUrlTests(TestCase):
    @patch("tracking.handlers.Bot.download_file", return_value=b"abc")
    @patch(
        "tracking.handlers.Bot.get_file",
        return_value={"ok": True, "result": {"file_path": "p.jpg"}},
    )
    def test_builds_data_url_from_largest_photo(self, get_file, download_file):
        url = photo_data_url({"photo": [{"file_id": "a"}, {"file_id": "b"}]})

        self.assertEqual(url, "data:image/jpeg;base64," + base64.b64encode(b"abc").decode())
        get_file.assert_called_once_with("b")

    def test_no_photo_returns_none(self):
        self.assertIsNone(photo_data_url({}))



class SpendingsReportTests(TestCase):
    def setUp(self):
        self.user = TelegramUser.objects.create(
            id=42, first_name="Rami", username="ramib_ch", daily_report=True
        )
        self.other = TelegramUser.objects.create(
            id=43, first_name="Sam", daily_report=True
        )

    def _expense(
        self,
        user,
        amount,
        currency="EUR",
        category=ExpenseCategory.DINING,
        day=None,
    ):
        return Expense.objects.create(
            telegram_user=user,
            date=day or timezone.localdate(),
            amount=amount,
            currency=currency,
            name="Expense",
            category=category,
            telegram_update={},
        )

    def _aggregated_rows(self, user):
        return (
            Expense.objects.filter(telegram_user=user)
            .values("currency", "category")
            .annotate(total=Sum("amount"))
            .order_by("currency", "-total")
        )

    def test_format_groups_by_currency_and_category(self):
        self._expense(self.user, 10, category=ExpenseCategory.DINING)
        self._expense(self.user, 5, category=ExpenseCategory.DINING)
        self._expense(self.user, 30, category=ExpenseCategory.GROCERIES)
        self._expense(self.user, 45, "CHF", category=ExpenseCategory.CLOTHING)

        text = format_spendings_report(self._aggregated_rows(self.user), "today")

        self.assertIn("Your spendings today was:", text)
        self.assertIn("    EUR", text)
        self.assertIn("    - Dining & Cafés: 15.00", text)
        self.assertIn("    - Groceries: 30.00", text)
        self.assertIn("    CHF", text)
        self.assertIn("    - Clothing & Shoes: 45.00", text)
        # Each currency header carries the total for that currency.
        self.assertIn("    EUR — 45.00", text)
        self.assertIn("    CHF — 45.00", text)
        # Currencies are alphabetical and categories go by descending total.
        self.assertLess(text.index("CHF"), text.index("EUR"))
        self.assertLess(text.index("Groceries"), text.index("Dining & Cafés"))

    @patch("tracking.tasks.Bot.to_chat")
    def test_daily_report_only_messages_users_with_spendings(self, to_chat):
        self._expense(self.user, 3.5)

        task_daily_report_to_telegram_users.call_local()

        to_chat.assert_called_once()
        (chat_id, text), _ = to_chat.call_args
        self.assertEqual(chat_id, 42)
        self.assertIn("Your spendings today was:", text)
        self.assertIn("    - Dining & Cafés: 3.50", text)

    @patch("tracking.tasks.Bot.to_chat")
    def test_weekly_report_covers_only_the_current_week(self, to_chat):
        today = timezone.localdate()
        self._expense(self.user, 1, day=today)
        self._expense(self.user, 99, day=today - timedelta(days=30))

        task_weekly_report_to_telegram_users.call_local()

        text = to_chat.call_args.args[1]
        self.assertIn("Your spendings this week was:", text)
        self.assertIn("    - Dining & Cafés: 1.00", text)
        self.assertNotIn("99.00", text)

    @patch("tracking.tasks.Bot.to_chat")
    def test_monthly_report_covers_only_the_previous_month(self, to_chat):
        today = timezone.localdate()
        last_month_end = today.replace(day=1) - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        self._expense(self.user, 7, day=last_month_start)
        self._expense(self.user, 8, day=last_month_end)
        self._expense(self.user, 99, day=today)

        task_monthly_report_to_telegram_users.call_local()

        text = to_chat.call_args.args[1]
        self.assertIn("Your spendings last month was:", text)
        self.assertIn("    - Dining & Cafés: 15.00", text)
        self.assertNotIn("99.00", text)

    @patch("tracking.tasks.Bot.to_chat")
    def test_daily_report_skips_users_who_opted_out(self, to_chat):
        self.user.daily_report = False
        self.user.save(update_fields=["daily_report"])
        self._expense(self.user, 3.5)

        task_daily_report_to_telegram_users.call_local()

        to_chat.assert_not_called()

    @patch("tracking.tasks.Bot.to_chat")
    def test_monthly_report_skips_users_who_opted_out(self, to_chat):
        last_month = (timezone.localdate().replace(day=1) - timedelta(days=1))
        self.user.monthly_report = False
        self.user.save(update_fields=["monthly_report"])
        self._expense(self.user, 7, day=last_month)

        task_monthly_report_to_telegram_users.call_local()

        to_chat.assert_not_called()

    @patch("tracking.tasks.Bot.to_chat", side_effect=[RuntimeError("boom"), None])
    def test_send_failure_does_not_stop_other_users(self, to_chat):
        self._expense(self.user, 1)
        self._expense(self.other, 2)

        task_daily_report_to_telegram_users.call_local()

        self.assertEqual(to_chat.call_count, 2)


class OnboardingTests(TestCase):
    def _update(self, text, user_id=42):
        return {
            "message": {
                "chat": {"id": user_id},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Rami",
                    "username": "ramib_ch",
                    "language_code": "en",
                },
                "text": text,
            }
        }

    @patch("tracking.handlers.Bot.to_chat")
    def test_start_asks_for_currency(self, to_chat):
        handle_update(self._update("/start"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.onboarding_step, OnboardingStep.CURRENCY)
        to_chat.assert_called_once()
        self.assertIn("currency", to_chat.call_args.args[1].lower())

    @patch("tracking.handlers.Bot.to_chat")
    def test_new_user_first_message_starts_onboarding(self, to_chat):
        # Even without /start, a brand-new user is onboarded before anything else.
        handle_update(self._update("coffee 3.5"))

        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(
            TelegramUser.objects.get(id=42).onboarding_step, OnboardingStep.CURRENCY
        )
        to_chat.assert_called_once()

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_currency_answer_saves_and_asks_reports(self, get_response, to_chat):
        TelegramUser.objects.create(id=42, onboarding_step=OnboardingStep.CURRENCY)

        handle_update(self._update("eur"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.default_currency, "EUR")
        self.assertEqual(user.onboarding_step, OnboardingStep.REPORTING)
        get_response.assert_not_called()
        self.assertIn("report", to_chat.call_args.args[1].lower())

    @patch("tracking.handlers.Bot.to_chat")
    def test_invalid_currency_reasks(self, to_chat):
        TelegramUser.objects.create(id=42, onboarding_step=OnboardingStep.CURRENCY)

        handle_update(self._update("bananas"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.default_currency, "")
        self.assertEqual(user.onboarding_step, OnboardingStep.CURRENCY)
        self.assertIn("3-letter", to_chat.call_args.args[1])

    @patch("tracking.handlers.Bot.to_chat")
    def test_report_choices_finish_onboarding(self, to_chat):
        TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.REPORTING, default_currency="EUR"
        )

        handle_update(self._update("daily"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.onboarding_step, OnboardingStep.DONE)
        self.assertTrue(user.daily_report)
        self.assertFalse(user.weekly_report)
        self.assertFalse(user.monthly_report)
        summary = to_chat.call_args.args[1]
        self.assertIn("All set", summary)
        self.assertIn("EUR", summary)

    @patch("tracking.handlers.Bot.to_chat")
    def test_report_none_disables_all(self, to_chat):
        TelegramUser.objects.create(id=42, onboarding_step=OnboardingStep.REPORTING)

        handle_update(self._update("none"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.onboarding_step, OnboardingStep.DONE)
        self.assertFalse(user.daily_report)
        self.assertFalse(user.weekly_report)
        self.assertFalse(user.monthly_report)

    @patch("tracking.handlers.Bot.to_chat")
    def test_unrecognised_report_answer_reasks(self, to_chat):
        TelegramUser.objects.create(id=42, onboarding_step=OnboardingStep.REPORTING)

        handle_update(self._update("maybe later"))

        user = TelegramUser.objects.get(id=42)
        self.assertEqual(user.onboarding_step, OnboardingStep.REPORTING)
        self.assertIn("report", to_chat.call_args.args[1].lower())

    @patch("tracking.handlers.Bot.to_chat")
    def test_start_restarts_onboarding_for_existing_user(self, to_chat):
        TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )

        handle_update(self._update("/start"))

        self.assertEqual(
            TelegramUser.objects.get(id=42).onboarding_step, OnboardingStep.CURRENCY
        )

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_onboarded_user_expense_uses_default_currency(self, get_response, to_chat):
        TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )
        get_response.return_value = {
            "name": "Coffee",
            "amount": 3.5,
            "category": "dining",
            # No currency: it should fall back to the user's default.
        }

        handle_update(self._update("coffee 3.5"))

        expense = Expense.objects.get()
        self.assertEqual(expense.currency, "EUR")
        self.assertEqual(expense.telegram_user_id, 42)

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_expense_is_not_processed_before_onboarding_done(self, get_response, to_chat):
        # A user still in onboarding: the message advances onboarding instead of
        # being parsed as an expense.
        TelegramUser.objects.create(id=42, onboarding_step=OnboardingStep.CURRENCY)

        handle_update(self._update("coffee 3.5"))

        self.assertEqual(Expense.objects.count(), 0)
        get_response.assert_not_called()

    @patch("tracking.handlers.Bot.to_chat")
    @patch("tracking.handlers.get_response_to_prompt")
    def test_expense_from_unknown_sender_is_ignored(self, get_response, to_chat):
        # No `from` (e.g. a channel post): there is no user to onboard, so the
        # message is dropped rather than parsed or answered.
        update = {"message": {"chat": {"id": 99}, "text": "coffee 3.5"}}

        handle_update(update)

        self.assertEqual(Expense.objects.count(), 0)
        get_response.assert_not_called()
        to_chat.assert_not_called()


class HomePageTests(TestCase):
    def test_home_page_renders_landing_content(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start on Telegram")
        self.assertContains(response, "Daily")
        self.assertContains(response, "Weekly")
        self.assertContains(response, "Monthly")
        self.assertContains(response, "3 seconds")


class DeleteDataCommandTests(TestCase):
    def setUp(self):
        self.user = TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )
        for amount, name in ((3.5, "Coffee"), (12.0, "Lunch")):
            Expense.objects.create(
                telegram_user=self.user,
                date=timezone.localdate(),
                amount=amount,
                currency="EUR",
                name=name,
                category=ExpenseCategory.DINING,
                telegram_update={},
            )

    def _update(self, text):
        return {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42, "is_bot": False, "first_name": "Rami"},
                "text": text,
            }
        }

    @patch("tracking.handlers.Bot.to_chat")
    def test_deletedata_lists_expenses_and_asks_to_confirm(self, to_chat):
        handle_update(self._update("/deletedata"))

        text = to_chat.call_args.args[1]
        self.assertIn("Coffee", text)
        self.assertIn("Lunch", text)
        self.assertIn("/dataconfirmdelete", text)
        # Nothing is deleted until the user confirms.
        self.assertTrue(TelegramUser.objects.get(id=42).pending_delete)
        self.assertEqual(Expense.objects.count(), 2)

    @patch("tracking.handlers.Bot.to_chat")
    def test_confirmdelete_deletes_all_data(self, to_chat):
        TelegramUser.objects.filter(id=42).update(pending_delete=True)

        handle_update(self._update("/dataconfirmdelete"))

        self.assertFalse(TelegramUser.objects.filter(id=42).exists())
        self.assertEqual(Expense.objects.count(), 0)
        self.assertIn("Deleted 2", to_chat.call_args.args[1])

    @patch("tracking.handlers.Bot.to_chat")
    def test_confirmdelete_without_request_keeps_data(self, to_chat):
        handle_update(self._update("/dataconfirmdelete"))

        self.assertTrue(TelegramUser.objects.filter(id=42).exists())
        self.assertEqual(Expense.objects.count(), 2)
        self.assertIn("Nothing to delete", to_chat.call_args.args[1])

    @patch("tracking.handlers.Bot.to_chat")
    def test_cancel_keeps_data(self, to_chat):
        TelegramUser.objects.filter(id=42).update(pending_delete=True)

        handle_update(self._update("/datadeletecancel"))

        self.assertFalse(TelegramUser.objects.get(id=42).pending_delete)
        self.assertEqual(Expense.objects.count(), 2)

    @patch("tracking.handlers.Bot.to_chat")
    def test_deletedata_with_no_expenses_does_not_arm_confirmation(self, to_chat):
        Expense.objects.all().delete()

        handle_update(self._update("/deletedata"))

        self.assertFalse(TelegramUser.objects.get(id=42).pending_delete)
        self.assertIn("no expenses", to_chat.call_args.args[1])

    @patch("tracking.handlers.Bot.to_chat")
    def test_deletelastexpense_deletes_and_informs(self, to_chat):
        handle_update(self._update("/deletelastexpense"))

        # "Lunch" (id=2) was logged last, so it is the one removed.
        self.assertEqual([e.name for e in Expense.objects.all()], ["Coffee"])
        text = to_chat.call_args.args[1]
        self.assertIn("Lunch", text)
        self.assertIn("12.00 EUR", text)

    @patch("tracking.handlers.Bot.to_chat")
    def test_deletelastexpense_with_no_expenses_informs(self, to_chat):
        Expense.objects.all().delete()

        handle_update(self._update("/deletelastexpense"))

        self.assertIn("no expenses", to_chat.call_args.args[1])


class ExpenseWindowCommandTests(TestCase):
    def setUp(self):
        self.user = TelegramUser.objects.create(
            id=42, onboarding_step=OnboardingStep.DONE, default_currency="EUR"
        )

    def _expense(self, amount, days_ago):
        Expense.objects.create(
            telegram_user=self.user,
            date=timezone.localdate() - timedelta(days=days_ago),
            amount=amount,
            currency="EUR",
            name="Expense",
            category=ExpenseCategory.DINING,
            telegram_update={},
        )

    def _update(self, text):
        return {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42, "is_bot": False, "first_name": "Rami"},
                "text": text,
            }
        }

    @patch("tracking.handlers.Bot.to_chat")
    def test_expenses7d_covers_only_the_last_7_days(self, to_chat):
        self._expense(1, days_ago=1)
        self._expense(100, days_ago=20)

        handle_update(self._update("/expenses7d"))

        text = to_chat.call_args.args[1]
        self.assertIn("Your spendings in the last 7 days was:", text)
        self.assertIn("1.00", text)
        self.assertNotIn("100.00", text)

    @patch("tracking.handlers.Bot.to_chat")
    def test_expenses30d_includes_older_expenses(self, to_chat):
        self._expense(1, days_ago=1)
        self._expense(100, days_ago=20)

        handle_update(self._update("/expenses30d"))

        self.assertIn("101.00", to_chat.call_args.args[1])

    @patch("tracking.handlers.Bot.to_chat")
    def test_expenseslast3m_uses_calendar_months(self, to_chat):
        self._expense(5, days_ago=80)  # within 3 calendar months
        self._expense(50, days_ago=200)  # outside the window

        handle_update(self._update("/expenseslast3m"))

        text = to_chat.call_args.args[1]
        self.assertIn("Your spendings in the last 3 months was:", text)
        self.assertIn("5.00", text)
        self.assertNotIn("50.00", text)

    @patch("tracking.handlers.Bot.to_chat")
    def test_window_with_no_expenses_reports_none(self, to_chat):
        handle_update(self._update("/expenses7d"))

        self.assertIn("no expenses", to_chat.call_args.args[1])
