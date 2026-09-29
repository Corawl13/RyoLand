"""Odds verification and bet placement.

`BetPlacementService.place_bet()` is the only entry point that writes a `Bet`. It always
runs inside one `@transaction.atomic` block: idempotency check, odds re-verification,
`Bet`/`BetSelection` creation, and the `WalletService.reserve_funds()` call all succeed or
roll back together, so a rejected bet (insufficient funds, changed odds, a suspended
market) never leaves an orphan `Bet` row or a stake reserved with no ticket behind it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
import time

from django.conf import settings
from django.db import IntegrityError, OperationalError, connections, transaction

from apps.accounts.models import User
from apps.wallet.exceptions import (
    InsufficientBalanceError,  # noqa: F401 - re-exported: see module docstring below
)
from apps.wallet.models import TransactionType
from apps.wallet.services import WalletService

from .exceptions import (
    BetLimitExceededError,
    DuplicateIdempotencyKeyError,
    EventNotOpenForBettingError,
    InvalidComboError,
    MarketSuspendedError,
    OddsChangedError,
    PlayerNotEligibleError,
    SelectionNotFoundError,
    StakeOutOfRangeError,
)
from .models import Bet, BetSelection, BetType, MarketStatus, Selection

# InsufficientBalanceError is intentionally NOT wrapped: WalletService.reserve_funds()
# already raises it with the exact `insufficient_balance` code the spec asks for, so
# BetPlacementService just lets it propagate. The import above exists so callers of this
# module can catch `apps.betting.services.InsufficientBalanceError` without also needing
# to import from `apps.wallet` directly.


@dataclass(frozen=True)
class VerifiedLeg:
    selection: Selection
    locked_odds: Decimal


class OddsVerificationService:
    """Pure read-side checks — no writes, no locking. `BetPlacementService` calls this
    inside its own atomic block; nothing here needs its own transaction."""

    @staticmethod
    def verify_odds(legs: list[tuple[str, Decimal]]) -> list[VerifiedLeg]:
        """`legs`: (selection_id, submitted_odds) pairs, as the client last saw them.
        Returns one `VerifiedLeg` per input, in the same order, each carrying the *current*
        database odds to lock into the ticket (not the client's submitted value — the
        client's number is only used to detect drift, never stored)."""
        selection_ids = [sid for sid, _ in legs]
        selections = {
            str(pk): obj
            for pk, obj in Selection.objects.select_related("market__event").in_bulk(selection_ids).items()
        }
        verified = []
        for selection_id, submitted_odds in legs:
            selection = selections.get(selection_id)
            if selection is None:
                raise SelectionNotFoundError()
            market = selection.market
            if market.status != MarketStatus.OPEN:
                raise MarketSuspendedError()
            if not market.event.is_open_for_new_bets:
                raise EventNotOpenForBettingError()
            if not OddsVerificationService._within_tolerance(submitted_odds, selection.current_odds):
                raise OddsChangedError()
            verified.append(VerifiedLeg(selection=selection, locked_odds=selection.current_odds))
        return verified

    @staticmethod
    def _within_tolerance(submitted: Decimal, current: Decimal) -> bool:
        tolerance = settings.BETTING_ODDS_DRIFT_TOLERANCE
        return abs(submitted - current) <= current * tolerance


def _retry_sqlite_lock(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        for attempt in range(32):
            try:
                return func(*args, **kwargs)
            except OperationalError as exc:
                if connections["default"].vendor != "sqlite" or "locked" not in str(exc).lower() or attempt == 31:
                    raise
                time.sleep(min(0.005 * (2**attempt), 0.1))

    return wrapped


class BetPlacementService:
    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def place_bet(
        *,
        user: User,
        currency: str,
        stake_amount: Decimal,
        legs: list[tuple[str, Decimal]],
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> tuple[Bet, bool]:
        """Returns `(bet, created)` — `created` is False when `idempotency_key` matches an
        existing bet, which is returned untouched (no re-verification, no re-reservation)."""
        existing = Bet.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.user_id != user.id or existing.currency != currency:
                raise DuplicateIdempotencyKeyError()
            return existing, False

        if not user.can_wager:
            raise PlayerNotEligibleError()
        if not (settings.BETTING_MIN_STAKE <= stake_amount <= settings.BETTING_MAX_STAKE):
            raise StakeOutOfRangeError()
        if not legs:
            raise InvalidComboError("A bet needs at least one selection.")
        if len(legs) > settings.BETTING_MAX_COMBO_SELECTIONS:
            raise InvalidComboError(f"A combo bet may not exceed {settings.BETTING_MAX_COMBO_SELECTIONS} selections.")

        verified_legs = OddsVerificationService.verify_odds(legs)
        market_ids = [leg.selection.market_id for leg in verified_legs]
        if len(set(market_ids)) != len(market_ids):
            raise InvalidComboError("A combo bet cannot include two selections from the same market.")

        total_odds = Decimal(1)
        for leg in verified_legs:
            total_odds *= leg.locked_odds
        potential_payout = (stake_amount * total_odds).quantize(Decimal("0.00000001"))
        if potential_payout > settings.BETTING_MAX_POTENTIAL_PAYOUT:
            raise BetLimitExceededError()

        bet_type = BetType.SINGLE if len(verified_legs) == 1 else BetType.COMBO
        try:
            with transaction.atomic():  # savepoint: a lost idempotency-key race must not poison the caller
                bet = Bet.objects.create(
                    user=user,
                    currency=currency,
                    bet_type=bet_type,
                    stake_amount=stake_amount,
                    total_odds=total_odds,
                    potential_payout=potential_payout,
                    idempotency_key=idempotency_key,
                    metadata=metadata or {},
                )
        except IntegrityError:  # concurrent request placed this exact bet first
            existing = Bet.objects.get(idempotency_key=idempotency_key)
            if existing.user_id != user.id or existing.currency != currency:
                raise DuplicateIdempotencyKeyError() from None
            return existing, False

        BetSelection.objects.bulk_create(
            BetSelection(
                bet=bet,
                selection=leg.selection,
                event=leg.selection.market.event,
                locked_odds=leg.locked_odds,
            )
            for leg in verified_legs
        )

        reservation = WalletService.reserve_funds(
            user=user,
            currency=currency,
            amount=stake_amount,
            transaction_type=TransactionType.BET_RESERVATION,
            idempotency_key=f"bet-reserve:{idempotency_key}",
            metadata={"bet_id": str(bet.id)},
        )
        bet.reservation_transaction = reservation
        bet.save(update_fields=["reservation_transaction", "updated_at"])
        return bet, True
