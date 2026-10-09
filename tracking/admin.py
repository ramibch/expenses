import json

from django.contrib import admin
from django.utils.html import format_html

from tracking.models import Expense, TelegramUser


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "username",
        "first_name",
        "last_name",
        "is_bot",
        "language_code",
        "default_currency",
        "daily_report",
        "weekly_report",
        "monthly_report",
        "onboarding_step",
        "created_at",
    )
    list_editable = ("daily_report", "weekly_report", "monthly_report")
    list_filter = (
        "is_bot",
        "language_code",
        "default_currency",
        "onboarding_step",
        "daily_report",
        "weekly_report",
        "monthly_report",
    )
    search_fields = ("id", "username", "first_name", "last_name")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "currency", "category", "date", "telegram_user")
    list_editable = ("category",)
    list_filter = ("category", "currency", ("date", admin.DateFieldListFilter))
    search_fields = (
        "name",
        "telegram_user__username",
        "telegram_user__first_name",
        "telegram_user__last_name",
    )
    date_hierarchy = "date"
    ordering = ("-date", "-id")
    list_select_related = ("telegram_user",)
    autocomplete_fields = ("telegram_user",)
    readonly_fields = ("telegram_update_json",)

    @admin.display(description="Telegram update")
    def telegram_update_json(self, obj):
        if obj.pk is None:
            return "-"
        return format_html(
            "<pre>{}</pre>",
            json.dumps(obj.telegram_update, indent=2, ensure_ascii=False),
        )
