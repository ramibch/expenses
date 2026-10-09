import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .handlers import handle_update


def home(request):
    """Marketing landing page that funnels visitors into the Telegram bot."""
    return render(request, "home.html")


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Handle updates delivered by Telegram via the configured webhook."""
    # Verify the secret token we set via setWebhook, if one is configured.
    secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET_TOKEN", None)
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return HttpResponseForbidden()

    try:
        update = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)
    
    handle_update(update)

    # Telegram only needs a 2xx to consider the update delivered.
    return HttpResponse("ok")
