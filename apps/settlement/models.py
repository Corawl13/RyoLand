"""Official results and settlement batch bookkeeping. The actual "did this bet win"
computation lives in services.py; these models are the audit trail and the run log."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.betting.models import SportEvent
from apps.core.models import BaseModel


class SettlementStatus(models.TextChoices):
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class EventResult(BaseModel):
    """The official outcome submitted for one event. One per event — a correction after
    the fact isn't supported here (rare enough, and consequential enough, that it belongs
    to a deliberate future workflow rather than an admin edit form); re-submitting the
    exact same result is a safe no-op, and submitting a *different* result for an event
    that already has one is rejected (see `EventSettlementService`)."""

    event = models.OneToOneField(SportEvent, on_delete=models.PROTECT, related_name="result")
    is_cancelled = models.BooleanField(default=False)
    scores = models.JSONField(default=dict, blank=True)  # free-form, e.g. {"home": 2, "away": 1}
    # Selection ids (as strings) the settlement engine will mark WON / VOID; every other
    # selection in a settled market is implicitly LOST. Stored sorted for a stable equality
    # check when a resubmission is compared against what's on file.
    winning_selection_ids = models.JSONField(default=list, blank=True)
    voided_selection_ids = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    settled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Result for {self.event}"


class SettlementBatch(BaseModel):
    """One run of `EventSettlementService.settle_event()`. A retry (e.g. recovering from a
    FAILED run, or simply re-triggering to pick up bets placed after the first pass — which
    shouldn't happen once markets are settled, but the model doesn't forbid it) creates a
    new batch row rather than reusing one, so the run history is never overwritten."""

    event = models.ForeignKey(SportEvent, on_delete=models.PROTECT, related_name="settlement_batches")
    status = models.CharField(max_length=16, choices=SettlementStatus.choices, default=SettlementStatus.PROCESSING)
    total_bets_processed = models.PositiveIntegerField(default=0)
    # Keyed by currency (not a single DecimalField): one event's pending bets can span
    # several currencies, and summing across them would be meaningless.
    payout_totals = models.JSONField(default=dict, blank=True)
    errors_log = models.JSONField(default=list, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["event", "-created_at"])]

    def __str__(self) -> str:
        return f"Batch {self.id} for {self.event} ({self.status})"
