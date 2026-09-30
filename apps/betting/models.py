"""Sports events, markets, selections, and the bet ticket itself.

Settlement (resolving a PENDING bet to WON/LOST/... and paying it out via
`WalletService.settle_payout`) is explicitly out of scope here — see the `mark_*()`
transition methods on `Bet` and `BetSelection`, which exist only as the hook the Step 4
settlement engine will call. Nothing in this app calls them.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import BaseModel
from apps.wallet.models import Currency, LedgerTransaction


class EventStatus(models.TextChoices):
    UPCOMING = "upcoming", "Upcoming"
    LIVE = "live", "Live"
    FINISHED = "finished", "Finished"
    CANCELLED = "cancelled", "Cancelled"


class MarketStatus(models.TextChoices):
    OPEN = "open", "Open"
    SUSPENDED = "suspended", "Suspended"
    SETTLED = "settled", "Settled"


class BetType(models.TextChoices):
    SINGLE = "single", "Single"
    COMBO = "combo", "Combo"


class BetStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    WON = "won", "Won"
    LOST = "lost", "Lost"
    CANCELLED = "cancelled", "Cancelled"
    REFUNDED = "refunded", "Refunded"
    VOID = "void", "Void"


class SelectionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    WON = "won", "Won"
    LOST = "lost", "Lost"
    VOID = "void", "Void"

BET_SETTLED_STATUSES = frozenset({BetStatus.WON, BetStatus.LOST, BetStatus.CANCELLED, BetStatus.REFUNDED, BetStatus.VOID})

ODDS_KWARGS = dict(max_digits=12, decimal_places=4)
AMOUNT_KWARGS = dict(max_digits=28, decimal_places=8)  # matches apps.wallet's money precision


class SportEvent(BaseModel):
    """A match or game session that markets are offered on."""

    name = models.CharField(max_length=255)  # e.g. "Real Madrid vs Barcelona"
    sport = models.CharField(max_length=64)  # e.g. "football", "esports_dota2" — free text on purpose: no fixed sport list yet
    start_time = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=16, choices=EventStatus.choices, default=EventStatus.UPCOMING, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)  # league, venue, external feed id, ...

    class Meta:
        indexes = [models.Index(fields=["status", "start_time"])]

    def __str__(self) -> str:
        return self.name

    @property
    def is_open_for_new_bets(self) -> bool:
        """LIVE covers in-play betting; UPCOMING is only bettable while genuinely in the
        future — a safety net against a status that's gone stale (e.g. a feed outage left
        it UPCOMING past kickoff)."""
        from django.utils import timezone

        if self.status == EventStatus.LIVE:
            return True
        return self.status == EventStatus.UPCOMING and self.start_time > timezone.now()


class Market(BaseModel):
    """A specific market within an event, e.g. "1X2 Match Winner" or "Total Over/Under"."""

    event = models.ForeignKey(SportEvent, on_delete=models.CASCADE, related_name="markets")
    name = models.CharField(max_length=255)
    market_type = models.CharField(max_length=64)  # e.g. "1x2", "totals", "btts" — see SportEvent.sport note
    status = models.CharField(max_length=16, choices=MarketStatus.choices, default=MarketStatus.OPEN, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["event", "status"])]

    def __str__(self) -> str:
        return f"{self.event}: {self.name}"


class Selection(BaseModel):
    """One outcome inside a market, e.g. "Home Team" or "Over 2.5", with its live odds."""

    market = models.ForeignKey(Market, on_delete=models.CASCADE, related_name="selections")
    name = models.CharField(max_length=255)
    current_odds = models.DecimalField(**ODDS_KWARGS)
    status = models.CharField(max_length=10, choices=SelectionStatus.choices, default=SelectionStatus.PENDING)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(current_odds__gte=1), name="betting_selection_odds_gte_1")]

    def __str__(self) -> str:
        return f"{self.market}: {self.name} @ {self.current_odds}"


class Bet(BaseModel):
    """The master ticket for a single or combo bet. `total_odds` and `potential_payout`
    are computed and frozen at placement — they never change afterwards, even if
    individual `BetSelection.locked_odds` values would suggest a different product (they
    can't drift apart, since both are derived from the same locked odds in the same
    request; this is just documenting that neither is ever recomputed later)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bets")
    currency = models.CharField(max_length=10, choices=Currency.choices)
    bet_type = models.CharField(max_length=10, choices=BetType.choices)
    status = models.CharField(max_length=16, choices=BetStatus.choices, default=BetStatus.PENDING, db_index=True)
    stake_amount = models.DecimalField(**AMOUNT_KWARGS)
    total_odds = models.DecimalField(**ODDS_KWARGS)
    potential_payout = models.DecimalField(**AMOUNT_KWARGS)
    idempotency_key = models.CharField(max_length=255, unique=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    # Set by BetPlacementService when the stake is reserved; settlement_transaction is
    # filled in later by the Step 4 settlement engine when the bet is finally resolved.
    reservation_transaction = models.ForeignKey(
        LedgerTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    settlement_transaction = models.ForeignKey(
        LedgerTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["user", "status", "-placed_at"])]

    def __str__(self) -> str:
        return f"{self.bet_type}:{self.status}:{self.id}"

    # --- settlement readiness hooks (called by the future settlement engine, not here) ---
    def _transition(self, new_status: str, *, save: bool) -> None:
        if self.status != BetStatus.PENDING:
            raise ValueError(f"Cannot move bet {self.id} from {self.status} to {new_status}: it is already settled.")
        self.status = new_status
        if save:
            self.save(update_fields=["status", "updated_at"])

    def mark_won(self, *, save: bool = True) -> None:
        self._transition(BetStatus.WON, save=save)

    def mark_lost(self, *, save: bool = True) -> None:
        self._transition(BetStatus.LOST, save=save)

    def mark_cancelled(self, *, save: bool = True) -> None:
        self._transition(BetStatus.CANCELLED, save=save)

    def mark_refunded(self, *, save: bool = True) -> None:
        self._transition(BetStatus.REFUNDED, save=save)

    def mark_void(self, *, save: bool = True) -> None:
        self._transition(BetStatus.VOID, save=save)


class BetSelection(BaseModel):
    """One leg of a `Bet`. `locked_odds` is the odds snapshot frozen at placement time —
    the whole reason a ticket survives later odds movements. `event` is denormalized from
    `selection.market.event` purely for query convenience (a bet's events without joining
    through market); `status` mirrors `Bet.status` per-leg, for a future combo settlement
    engine that needs to know which individual legs won before it can score the parlay."""

    bet = models.ForeignKey(Bet, on_delete=models.CASCADE, related_name="selections")
    selection = models.ForeignKey(Selection, on_delete=models.PROTECT, related_name="bet_selections")
    event = models.ForeignKey(SportEvent, on_delete=models.PROTECT, related_name="bet_selections")
    locked_odds = models.DecimalField(**ODDS_KWARGS)
    status = models.CharField(max_length=16, choices=BetStatus.choices, default=BetStatus.PENDING)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bet", "selection"], name="betting_betselection_unique_bet_selection"),
            models.CheckConstraint(condition=Q(locked_odds__gte=1), name="betting_betselection_odds_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.bet_id}: {self.selection} @ {self.locked_odds}"

    def mark_won(self, *, save: bool = True) -> None:
        self._transition(BetStatus.WON, save=save)

    def mark_lost(self, *, save: bool = True) -> None:
        self._transition(BetStatus.LOST, save=save)

    def mark_void(self, *, save: bool = True) -> None:
        self._transition(BetStatus.VOID, save=save)

    def _transition(self, new_status: str, *, save: bool) -> None:
        if self.status != BetStatus.PENDING:
            raise ValueError(f"Cannot move bet selection {self.id} from {self.status} to {new_status}: already settled.")
        self.status = new_status
        if save:
            self.save(update_fields=["status", "updated_at"])
