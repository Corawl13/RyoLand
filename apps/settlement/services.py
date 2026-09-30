"""Result submission and bet settlement.

Design for concurrency: settling an event happens in two phases, deliberately NOT one
giant transaction. Phase one (`EventSettlementService._resolve_event_and_markets`) briefly
locks the event, its markets and its `EventResult`/`SettlementBatch` rows to record the
official result and flip every market to SETTLED — fast, and over quickly. Phase two
(`BetSettlementEngine`) then processes each PENDING bet touching that event in its OWN,
independent `@transaction.atomic` call — not nested under one shared transaction. That's
what lets multiple workers genuinely settle different bets of the same batch in parallel
(each one only ever locks the one `Bet` row, the `Wallet` row(s) `WalletService` touches,
and the one `SettlementBatch` row, all released the moment that bet is done) rather than
serializing behind a single lock held for the entire event.

Idempotency has two independent layers, same pattern as `apps.wallet` and `apps.betting`:
- Per bet: `_settle_one_bet` re-checks `bet.status == PENDING` under its own row lock before
  doing anything, so a bet already settled by an earlier or concurrent run is a no-op.
- Per wallet operation: the settlement idempotency key is derived from the bet's own key
  (`bet-settle:{bet.idempotency_key}`), so even if `_settle_one_bet` somehow ran twice for
  the same bet, `WalletService` would refuse to pay out twice.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.betting.models import (
    Bet,
    BetSelection,
    BetStatus,
    EventStatus,
    Market,
    MarketStatus,
    Selection,
    SelectionStatus,
    SportEvent,
)
from apps.wallet.models import AccountType, TransactionType
from apps.wallet.services import WalletService

from .exceptions import ResultAlreadySubmittedError
from .models import EventResult, SettlementBatch, SettlementStatus


def _normalize_ids(ids) -> list[str]:
    return sorted(str(x) for x in (ids or []))


class MarketSettlementService:
    @staticmethod
    @transaction.atomic
    def settle_market(market: Market, *, winning_selection_ids: list[str], voided_selection_ids: list[str]) -> None:
        """Marks every selection in `market` WON / VOID / (implicitly) LOST and flips the
        market to SETTLED. A no-op if the market is already SETTLED — re-running settlement
        for an event must never re-derive (and potentially re-shuffle) selection outcomes."""
        if market.status == MarketStatus.SETTLED:
            return
        winning = set(winning_selection_ids)
        voided = set(voided_selection_ids)
        selections = list(Selection.objects.select_for_update().filter(market=market))
        for selection in selections:
            sid = str(selection.id)
            if sid in winning:
                selection.status = SelectionStatus.WON
            elif sid in voided:
                selection.status = SelectionStatus.VOID
            else:
                selection.status = SelectionStatus.LOST
        Selection.objects.bulk_update(selections, ["status"])
        market.status = MarketStatus.SETTLED
        market.save(update_fields=["status", "updated_at"])


class EventSettlementService:
    @staticmethod
    @transaction.atomic
    def _resolve_event_and_markets(
        *,
        event: SportEvent,
        winning_selection_ids: list[str],
        voided_selection_ids: list[str],
        is_cancelled: bool,
        scores: dict | None,
        submitted_by,
        metadata: dict | None,
    ) -> tuple[EventResult, SettlementBatch]:
        winning = [] if is_cancelled else _normalize_ids(winning_selection_ids)
        voided = _normalize_ids(voided_selection_ids)

        result = EventResult.objects.select_for_update().filter(event=event).first()
        if result is None:
            try:
                with transaction.atomic():  # savepoint: a lost create-race must not poison the caller
                    result = EventResult.objects.create(
                        event=event,
                        is_cancelled=is_cancelled,
                        scores=scores or {},
                        winning_selection_ids=winning,
                        voided_selection_ids=voided,
                        metadata=metadata or {},
                        submitted_by=submitted_by,
                    )
            except IntegrityError:  # concurrent submission for the same event
                result = EventResult.objects.select_for_update().get(event=event)

        submitted = (is_cancelled, winning, voided)
        on_file = (result.is_cancelled, result.winning_selection_ids, result.voided_selection_ids)
        if submitted != on_file:
            raise ResultAlreadySubmittedError()

        locked_event = SportEvent.objects.select_for_update().get(pk=event.pk)
        if locked_event.status not in (EventStatus.FINISHED, EventStatus.CANCELLED):
            locked_event.status = EventStatus.CANCELLED if is_cancelled else EventStatus.FINISHED
            locked_event.save(update_fields=["status", "updated_at"])

        markets = list(Market.objects.filter(event=locked_event).exclude(status=MarketStatus.SETTLED))
        if is_cancelled:
            # The whole event is void: every selection in every still-open market voids,
            # regardless of what (if anything) was submitted as "winning".
            for market in markets:
                all_ids = [str(sid) for sid in market.selections.values_list("id", flat=True)]
                MarketSettlementService.settle_market(market, winning_selection_ids=[], voided_selection_ids=all_ids)
        else:
            for market in markets:
                MarketSettlementService.settle_market(market, winning_selection_ids=winning, voided_selection_ids=voided)

        batch = SettlementBatch.objects.create(event=locked_event, status=SettlementStatus.PROCESSING)
        return result, batch

    @staticmethod
    def settle_event(
        *,
        event: SportEvent,
        winning_selection_ids: list[str] | None = None,
        voided_selection_ids: list[str] | None = None,
        is_cancelled: bool = False,
        scores: dict | None = None,
        submitted_by=None,
        metadata: dict | None = None,
    ) -> SettlementBatch:
        """The one entry point: records the official result (or reuses the one on file),
        settles every market, then processes every PENDING bet touching this event. Safe
        to call again for the same event with the same result — every phase is idempotent
        on its own terms (see module docstring)."""
        result, batch = EventSettlementService._resolve_event_and_markets(
            event=event,
            winning_selection_ids=winning_selection_ids or [],
            voided_selection_ids=voided_selection_ids or [],
            is_cancelled=is_cancelled,
            scores=scores,
            submitted_by=submitted_by,
            metadata=metadata,
        )
        try:
            BetSettlementEngine.settle_pending_bets_for_event(event, batch=batch)
        except Exception as exc:  # a bug in market resolution itself, NOT a single bet's
            # failure (those are caught per-bet inside the engine and logged, not raised)
            SettlementBatch.objects.filter(pk=batch.pk).update(
                status=SettlementStatus.FAILED, completed_at=timezone.now()
            )
            raise exc
        SettlementBatch.objects.filter(pk=batch.pk).update(
            status=SettlementStatus.COMPLETED, completed_at=timezone.now()
        )
        if result.settled_at is None:
            EventResult.objects.filter(pk=result.pk, settled_at__isnull=True).update(settled_at=timezone.now())
        return SettlementBatch.objects.get(pk=batch.pk)


class BetSettlementEngine:
    @staticmethod
    def settle_pending_bets_for_event(event: SportEvent, *, batch: SettlementBatch) -> dict:
        """Every PENDING bet with at least one leg on `event`. A combo bet with legs on
        OTHER events too is only actually settled once every one of its legs' markets is
        SETTLED — see `_settle_one_bet`, which skips (leaves PENDING) anything still
        waiting on another event."""
        bet_ids = list(
            Bet.objects.filter(status=BetStatus.PENDING, selections__event=event).values_list("id", flat=True).distinct()
        )
        settled = 0
        for bet_id in bet_ids:
            try:
                did_settle = BetSettlementEngine._settle_one_bet(bet_id, batch.pk)
            except Exception as exc:  # noqa: BLE001 - one bad bet must not sink the batch
                BetSettlementEngine._log_error(batch.pk, bet_id, exc)
                continue
            settled += int(did_settle)
        return {"settled": settled, "candidates": len(bet_ids)}

    @staticmethod
    @transaction.atomic
    def _log_error(batch_id, bet_id, exc: Exception) -> None:
        batch = SettlementBatch.objects.select_for_update().get(pk=batch_id)
        batch.errors_log = [*batch.errors_log, {"bet_id": str(bet_id), "error": str(exc)}]
        batch.save(update_fields=["errors_log", "updated_at"])

    @staticmethod
    @transaction.atomic
    def _settle_one_bet(bet_id, batch_id) -> bool:
        bet = Bet.objects.select_for_update().select_related("user").get(pk=bet_id)
        if bet.status != BetStatus.PENDING:
            return False  # already settled — by this run or an earlier/concurrent one

        legs = list(BetSelection.objects.select_related("selection__market").filter(bet=bet))
        if any(leg.selection.market.status != MarketStatus.SETTLED for leg in legs):
            return False  # still waiting on another event's market(s)

        any_lost = False
        all_void = True
        effective_odds = Decimal("1")
        for leg in legs:
            sel_status = leg.selection.status
            if sel_status == SelectionStatus.WON:
                leg.status = BetStatus.WON
                effective_odds *= leg.locked_odds
                all_void = False
            elif sel_status == SelectionStatus.LOST:
                leg.status = BetStatus.LOST
                any_lost = True
                all_void = False
            else:  # VOID — contributes nothing to effective_odds (equivalent to x1)
                leg.status = BetStatus.VOID
        BetSelection.objects.bulk_update(legs, ["status", "updated_at"])

        if any_lost:
            bet.status = BetStatus.LOST
            payout_amount = Decimal("0")
        elif all_void:
            bet.status = BetStatus.VOID
            payout_amount = bet.stake_amount
        else:
            bet.status = BetStatus.WON
            payout_amount = (bet.stake_amount * effective_odds).quantize(Decimal("0.00000001"))

        settlement_key = f"bet-settle:{bet.idempotency_key}"
        if bet.status == BetStatus.VOID:
            txn = WalletService.release_funds(
                user=bet.user,
                currency=bet.currency,
                amount=bet.stake_amount,
                idempotency_key=settlement_key,
                transaction_type=TransactionType.BET_CANCEL_REFUND,
                metadata={"bet_id": str(bet.id)},
            )
        else:
            txn = WalletService.settle_payout(
                user=bet.user,
                currency=bet.currency,
                locked_amount=bet.stake_amount,
                payout_amount=payout_amount,
                idempotency_key=settlement_key,
                transaction_type=TransactionType.BET_PAYOUT,
                house_account_type=AccountType.SYSTEM_HOUSE_REVENUE,
                metadata={"bet_id": str(bet.id)},
            )
        bet.settlement_transaction = txn
        bet.save(update_fields=["status", "settlement_transaction", "updated_at"])

        batch = SettlementBatch.objects.select_for_update().get(pk=batch_id)
        batch.total_bets_processed += 1
        totals = dict(batch.payout_totals)
        totals[bet.currency] = str(Decimal(totals.get(bet.currency, "0")) + payout_amount)
        batch.payout_totals = totals
        batch.save(update_fields=["total_bets_processed", "payout_totals", "updated_at"])
        return True
