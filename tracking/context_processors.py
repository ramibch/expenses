
from django.conf import settings


def site_urls(request):
    return {
        "request": request,
        "code_url": settings.REPO_CODE_URL,
        "bot_url": settings.TELEGRAM_BOT_URL,
        "contact_url": settings.DEVELOPER_CONTACT_URL,
    }