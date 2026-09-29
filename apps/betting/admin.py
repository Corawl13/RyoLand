"""Django admin for betting.

Events, markets and selections are actively managed here (a trader opens/suspends
markets, updates odds) — that's the point of this admin, unlike the mostly read-only
admins in accounts/wallet. Bets and their legs stay read-only: a ticket is either what the
player was shown at placement or it's wrong, never something to hand-edit.
"""
from django.contrib import admin

from .models import Bet, BetSelection, Market, Selection, SportEvent


class MarketInline(admin.TabularInline):
    model = Market
    extra = 0
    fields = ("name", "market_type", "status")
    show_change_link = True


@admin.register(SportEvent)
class SportEventAdmin(admin.ModelAdmin):
    inlines = [MarketInline]
    list_display = ("name", "sport", "start_time", "status")
    list_filter = ("sport", "status")
    search_fields = ("name",)
    ordering = ("-start_time",)
    actions = ("suspend_all_markets",)

    @admin.action(description="Suspend every market for the selected events")
    def suspend_all_markets(self, request, queryset):
        from .models import MarketStatus

        updated = Market.objects.filter(event__in=queryset).update(status=MarketStatus.SUSPENDED)
        self.message_user(request, f"Suspended {updated} market(s).")


class SelectionInline(admin.TabularInline):
    model = Selection
    extra = 0
    fields = ("name", "current_odds")


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    inlines = [SelectionInline]
    list_display = ("name", "event", "market_type", "status")
    list_filter = ("status", "market_type")
    search_fields = ("name", "event__name")
    autocomplete_fields = ("event",)


@admin.register(Selection)
class SelectionAdmin(admin.ModelAdmin):
    list_display = ("name", "market", "current_odds")
    list_filter = ("market__status",)
    search_fields = ("name", "market__name")
    autocomplete_fields = ("market",)


class BetSelectionInline(admin.TabularInline):
    model = BetSelection
    extra = 0
    fields = ("selection", "event", "locked_odds", "status")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Bet)
class BetAdmin(admin.ModelAdmin):
    inlines = [BetSelectionInline]
    list_display = ("id", "user", "bet_type", "status", "currency", "stake_amount", "potential_payout", "placed_at")
    list_filter = ("bet_type", "status", "currency")
    search_fields = ("id", "idempotency_key", "user__username")
    readonly_fields = [f.name for f in Bet._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
