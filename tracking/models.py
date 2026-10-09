from django.db import models


class OnboardingStep(models.TextChoices):
    """Where a user stands in the interactive onboarding conversation.

    ``DONE`` is also the field default so that users created outside the bot
    flow (admin, migrations, imports) are treated as already onboarded.
    """

    WELCOME = "welcome", "Welcome"
    CURRENCY = "currency", "Default currency"
    REPORTING = "reporting", "Report preferences"
    DONE = "done", "Done"


class TelegramUser(models.Model):
    """A Telegram user, mirroring the ``from`` object of an update.

    Example payload::

        {'id': 1777934566, 'is_bot': False, 'first_name': 'Rami',
         'last_name': 'Boutassghount', 'username': 'ramib_ch',
         'language_code': 'en'}
    """

    # Telegram's user id: globally unique and stable, so it makes a good pk.
    id = models.BigIntegerField(primary_key=True)
    is_bot = models.BooleanField(default=False)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    language_code = models.CharField(max_length=16, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    daily_report = models.BooleanField(default=False)
    weekly_report = models.BooleanField(default=True)
    monthly_report = models.BooleanField(default=True)

    # Currency used as a fallback when an expense does not state one.
    default_currency = models.CharField(max_length=3, blank=True)
    onboarding_step = models.CharField(
        max_length=16,
        choices=OnboardingStep.choices,
        default=OnboardingStep.DONE,
    )

    # Set when a user has asked to delete their data and must confirm first.
    pending_delete = models.BooleanField(default=False)    



    def __str__(self):
        if self.username:
            return f"@{self.username}"
        return f"{self.first_name} {self.last_name}".strip() or str(self.id)


class ExpenseCategory(models.TextChoices):
    # Housing & Home
    RENT_MORTGAGE = "rent_mortgage", "Rent / Mortgage"
    PROPERTY_TAX = "property_tax", "Property Tax"
    HOME_MAINTENANCE = "home_maintenance", "Home Maintenance & Repairs"
    UTILITIES = "utilities", "Utilities"
    HOUSEHOLD_SUPPLIES = "household_supplies", "Household Supplies"

    # Food & Dining
    GROCERIES = "groceries", "Groceries"
    DINING = "dining", "Dining & Cafés"
    FOOD_DELIVERY = "food_delivery", "Food Delivery"

    # Transportation
    PUBLIC_TRANSPORT = "public_transport", "Public Transport"
    FUEL = "fuel", "Fuel"
    VEHICLE_COSTS = "vehicle_costs", "Vehicle Costs"
    PARKING_TOLLS = "parking_tolls", "Parking & Tolls"

    # Shopping
    CLOTHING = "clothing", "Clothing & Shoes"
    ELECTRONICS = "electronics", "Electronics"
    SHOPPING = "shopping", "General Shopping"

    # Health & Personal Care
    HEALTHCARE = "healthcare", "Healthcare"
    FITNESS = "fitness", "Fitness & Sports"
    PERSONAL_CARE = "personal_care", "Personal Care"

    # Family & Education
    CHILDCARE = "childcare", "Childcare & Family"
    EDUCATION = "education", "Education"

    # Entertainment & Subscriptions
    ENTERTAINMENT = "entertainment", "Entertainment"
    SUBSCRIPTIONS = "subscriptions", "Subscriptions & Software"

    # Travel
    TRAVEL = "travel", "Travel"

    # Financial & Insurance
    INSURANCE = "insurance", "Insurance"
    BANK_FEES = "bank_fees", "Bank Fees & Interest"
    DEBT = "debt", "Debt & Loan Payments"
    SAVINGS_INVESTMENTS = "savings_investments", "Savings & Investments"
    TAXES = "taxes", "Taxes"
    GOVERNMENT_FEES = "government_fees", "Government Fees & Fines"

    # Work & Other
    WORK_BUSINESS = "work_business", "Work & Business"
    PETS = "pets", "Pets"
    GIFTS_DONATIONS = "gifts_donations", "Gifts & Donations"
    LEGAL_PROFESSIONAL = "legal_professional", "Legal & Professional Services"
    OTHER = "other", "Other"


class Expense(models.Model):
    telegram_user = models.ForeignKey(TelegramUser, on_delete=models.CASCADE)
    date = models.DateField()
    amount = models.FloatField()
    currency = models.CharField(max_length=3)
    name = models.CharField(max_length=256)
    category = models.CharField(max_length=64, choices=ExpenseCategory.choices)
    telegram_update = models.JSONField()
    # Telegram's update_id, globally unique per bot; makes webhook processing
    # idempotent under Telegram's redelivery/retries.
    update_id = models.BigIntegerField(unique=True, null=True, blank=True)


class RequestTrace(models.Model):
    """Timing for one processed Telegram update.

    One row per webhook update; the time spent in each step is stored on the
    related :class:`StepTiming` rows. This makes it obvious where the bot is
    slow (usually the model call, but now it's measurable rather than guessed).
    """

    class Kind(models.TextChoices):
        TEXT = "text", "Text expense"
        PHOTO = "photo", "Photo expense"
        COMMAND = "command", "Command"
        ONBOARDING = "onboarding", "Onboarding"
        IGNORED = "ignored", "Ignored / other"

    update_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    telegram_user = models.ForeignKey(
        TelegramUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="traces",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, blank=True)
    total_ms = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"#{self.pk} {self.kind or 'request'} ({self.total_ms:.0f} ms)"


class StepTiming(models.Model):
    """A single measured step inside a :class:`RequestTrace`."""

    trace = models.ForeignKey(
        RequestTrace, related_name="steps", on_delete=models.CASCADE
    )
    order = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=64)
    duration_ms = models.FloatField()

    class Meta:
        ordering = ("order", "id")

    def __str__(self):
        return f"{self.name}: {self.duration_ms:.1f} ms"