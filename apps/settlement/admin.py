"""Django admin for settlement.

Two complementary entry points for "submit result and settle", since a full per-selection
result (which selections won, which voided) doesn't fit a bulk multi-select admin action:

1. `EventResultAdmin`'s add form IS the "submit the official result" step — enter the
   event, the winning/voided selection ids, or check "cancelled", and save. Settlement
   runs automatically right after the row is created (see `save_model`).
2. The added `trigger_settlement` action on `SportEventAdmin`/`MarketAdmin` (extending
   apps.betting's own admin classes rather than duplicating them) is "trigger it again" —
   for retrying a FAILED batch, or an event whose result was already submitted via the API
   and just needs (re-)settling from here. It's a no-op with a message if the selected
   event has no `EventResult` on file yet.

`EventResult` rows are add-and-view only, never edited — see the model docstring for why.
"""
from django.contrib import admin, messages

from apps.betting.admin import MarketAdmin as BettingMarketAdmin
from apps.betting.admin import SportEventAdmin as BettingSportEventAdmin
from apps.betting.models import Market, SportEvent

from .exceptions import ResultAlreadySubmittedError
from .models import EventResult, SettlementBatch
from .services import EventSettlementService


def _trigger_settlement_for_events(modeladmin, request, events) -> None:
    settled, skipped = 0, 0
    for event in events:
        result = EventResult.objects.filter(event=event).first()
        if result is None:
            skipped += 1
            continue
        EventSettlementService.settle_event(
            event=event,
            winning_selection_ids=result.winning_selection_ids,
            voided_selection_ids=result.voided_selection_ids,
            is_cancelled=result.is_cancelled,
            scores=result.scores,
            submitted_by=request.user,
        )
        settled += 1
    if settled:
        modeladmin.message_user(request, f"Settled {settled} event(s).")
    if skipped:
        modeladmin.message_user(
            request, f"{skipped} event(s) have no submitted result yet — nothing to settle.", level=messages.WARNING
        )


@admin.action(description="Trigger settlement (using the result already on file)")
def trigger_settlement_for_selected_events(modeladmin, request, queryset):
    _trigger_settlement_for_events(modeladmin, request, queryset)


@admin.action(description="Trigger settlement for the parent event (using the result on file)")
def trigger_settlement_for_selected_markets(modeladmin, request, queryset):
    events = SportEvent.objects.filter(pk__in=queryset.values_list("event_id", flat=True).distinct())
    _trigger_settlement_for_events(modeladmin, request, events)


admin.site.unregister(SportEvent)


@admin.register(SportEvent)
class SportEventAdmin(BettingSportEventAdmin):
    actions = (*BettingSportEventAdmin.actions, trigger_settlement_for_selected_events)


admin.site.unregister(Market)


@admin.register(Market)
class MarketAdmin(BettingMarketAdmin):
    actions = trigger_settlement_for_selected_markets,


@admin.register(EventResult)
class EventResultAdmin(admin.ModelAdmin):
    list_display = ("event", "is_cancelled", "settled_at", "created_at")
    search_fields = ("event__name",)
    autocomplete_fields = ("event",)
    readonly_fields = ("settled_at", "created_at", "updated_at", "submitted_by")

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def save_model(self, request, obj, form, change):
        obj.submitted_by = request.user
        super().save_model(request, obj, form, change)  # creates the EventResult row itself
        try:
            EventSettlementService.settle_event(
                event=obj.event,
                winning_selection_ids=obj.winning_selection_ids,
                voided_selection_ids=obj.voided_selection_ids,
                is_cancelled=obj.is_cancelled,
                scores=obj.scores,
                submitted_by=request.user,
            )
        except ResultAlreadySubmittedError:
            self.message_user(
                request,
                "A different result was already on file for this event — the new result "
                "was saved but settlement was NOT re-run. Resolve the conflict manually.",
                level=messages.ERROR,
            )
        else:
            self.message_user(request, "Result submitted and settlement completed.")


@admin.register(SettlementBatch)
class SettlementBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "status", "total_bets_processed", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("event__name",)
    readonly_fields = [f.name for f in SettlementBatch._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
