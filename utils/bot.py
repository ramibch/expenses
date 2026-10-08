import ssl

import requests
from django.conf import settings
from django.urls import reverse


class Bot:
    base_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_API_KEY}/"
    file_url = f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_API_KEY}/"

    @staticmethod
    def prepare():
        ssl._create_default_https_context = ssl._create_unverified_context

    @staticmethod
    def to_chat(chat_id: str, text: str):
        """Send text message to a chat"""
        Bot.prepare()
        params = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        response = requests.get(Bot.base_url + "sendMessage", params=params)
        return response.json()

    @staticmethod
    def get_updates(print_them=True, offset=None):
        path = "getUpdates"
        if offset:
            path += f"?offset={offset}"
        r = requests.get(Bot.base_url + path)
        if print_them:
            print(r.text)
        return r.json()

    @staticmethod
    def get_file(file_id: str):
        """Return metadata for a Telegram file, including its ``file_path``.

        https://core.telegram.org/bots/api#getfile
        """
        r = requests.get(Bot.base_url + "getFile", params={"file_id": file_id})
        return r.json()

    @staticmethod
    def download_file(file_path: str) -> bytes:
        """Download the raw bytes of a ``file_path`` returned by :meth:`get_file`."""
        r = requests.get(Bot.file_url + file_path)
        r.raise_for_status()
        return r.content

    @staticmethod
    def set_webhook():
        # https://core.telegram.org/bots/api#setwebhook
        data = {
            "url": settings.BASE_WEB_URL + reverse("telegram_webhook"),
            "secret_token": settings.TELEGRAM_WEBHOOK_SECRET_TOKEN,
        }
        r = requests.post(Bot.base_url + "setWebhook", data=data)
        return r.json()

    @staticmethod
    def delete_webhook():
        # https://core.telegram.org/bots/api#deletewebhook
        r = requests.get(Bot.base_url + "deleteWebhook")
        return r.json()
