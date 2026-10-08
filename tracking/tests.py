import base64
import json
from unittest.mock import MagicMock, patch

import openai
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tracking.handlers import (
    _clean_expense,
    get_image_url,
    handle_text_expense,
    photo_data_url,
)
from tracking.models import Expense, ExpenseCategory, TelegramUser

SENDER = {
    "id": 42,
    "is_bot": False,
    "first_name": "Rami",
    "last_name": "Boutassghount",
    "username": "ramib_ch",
    "language_code": "en",
}


class TelegramWebhookTests(TestCase):
    def _post(self, text):
        return self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps(
                {"message": {"chat": {"id": 42}, "from": SENDER, "text": text}}
            ),
            content_type="application/json",
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
        self._post("???")

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

        self._post("coffee")

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
        to_chat.assert_called_once_with(42, "I can't read expenses right now")

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


class CleanExpenseTests(TestCase):
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
    def _post_photo(self, photos):
        return self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps(
                {"message": {"chat": {"id": 42}, "from": SENDER, "photo": photos}}
            ),
            content_type="application/json",
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


class GetImageUrlTests(TestCase):
    @patch(
        "tracking.handlers.Bot.get_file",
        return_value={"ok": True, "result": {"file_path": "photos/file_0.jpg"}},
    )
    def test_returns_telegram_file_url_for_largest_photo(self, get_file):
        update = {"message": {"photo": [{"file_id": "small"}, {"file_id": "large"}]}}

        url = get_image_url(update)

        assert url is not None
        self.assertTrue(url.startswith("https://api.telegram.org/file/bot"))
        self.assertTrue(url.endswith("/photos/file_0.jpg"))
        get_file.assert_called_once_with("large")

    def test_no_photo_returns_none(self):
        self.assertIsNone(get_image_url({"message": {"text": "hi"}}))

    @patch(
        "tracking.handlers.Bot.get_file",
        return_value={"ok": False, "description": "file is temporarily unavailable"},
    )
    def test_getfile_failure_returns_none(self, get_file):
        self.assertIsNone(get_image_url({"message": {"photo": [{"file_id": "a"}]}}))
