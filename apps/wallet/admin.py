"""Django admin for the wallet ledger.

Everything here is read-only. Balances only ever change through `WalletService`, inside an
atomic transaction with the ledger entries that justify the change — hand-editing a number
in the admin would silently break that invariant, so no field here is ever writable.
"""
from django.contrib import admin
from django.db.models import Case, DecimalField, F, Sum, When

from .models import EntryType, LedgerAccount, LedgerEntry, LedgerTransaction, Wallet


class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Wallet)
class WalletAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "currency", "available_balance", "locked_balance", "total_balance_display")
    list_filter = ("currency",)
    search_fields = ("user__username", "user__display_name")
    readonly_fields = [f.name for f in Wallet._meta.fields]

    @admin.display(description="Total")
    def total_balance_display(self, obj: Wallet):
        return obj.total_balance


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    fields = ("account", "entry_type", "amount", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


_SIGNED_AMOUNT = Case(
    When(entries__entry_type=EntryType.DEBIT, then=F("entries__amount")),
    When(entries__entry_type=EntryType.CREDIT, then=-F("entries__amount")),
    output_field=DecimalField(max_digits=28, decimal_places=8),
)


@admin.register(LedgerAccount)
class LedgerAccountAdmin(ReadOnlyModelAdmin):
    list_display = ("account_type", "currency", "user", "computed_balance")
    list_filter = ("account_type", "currency")
    search_fields = ("user__username",)
    readonly_fields = [f.name for f in LedgerAccount._meta.fields]

    def get_queryset(self, request):
        # Not cached on the model (see models.py) — computed here on demand, since this
        # page is the only place anything reads a system account's balance today.
        return super().get_queryset(request).annotate(_balance=Sum(_SIGNED_AMOUNT))

    @admin.display(description="Balance", ordering="_balance")
    def computed_balance(self, obj: LedgerAccount):
        return obj._balance or 0


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(ReadOnlyModelAdmin):
    inlines = [LedgerEntryInline]
    list_display = ("id", "transaction_type", "status", "currency", "created_at", "completed_at")
    list_filter = ("transaction_type", "status", "currency")
    search_fields = ("id", "idempotency_key")
    readonly_fields = [f.name for f in LedgerTransaction._meta.fields]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyModelAdmin):
    """Registered standalone too, so ops/finance can search entries independently of any
    one transaction — e.g. every entry touching SYSTEM_HOUSE_REVENUE/TON this month."""

    list_display = ("transaction", "account", "entry_type", "amount", "created_at")
    list_filter = ("entry_type", "account__account_type", "account__currency")
    search_fields = ("transaction__id", "account__user__username")
    readonly_fields = [f.name for f in LedgerEntry._meta.fields]
