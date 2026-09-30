"""Tests for apps.settlement. Run with: python manage.py test apps.settlement

TransactionTestCase throughout — the concurrency tests need real, separately-committing DB
transactions across threads.
"""
import threading
import uuid
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.betting.models import (
    Bet,
    BetStatus,
    EventStatus,
    Market,
    MarketStatus,
    Selection,
    SelectionStatus,
    SportEvent,
)
from apps.betting.services import BetPlacementService
from apps.wallet.models import Currency, TransactionType, Wallet
from apps.wallet.services import WalletService

from .exceptions import ResultAlreadySubmittedError
from .models import SettlementBatch, SettlementStatus
from .services import EventSettlementService

D = Decimal


def key() -> str:
    return str(uuid.uuid4())


def make_event(*, status=EventStatus.LIVE):
    return SportEvent.objects.create(
        name="Home vs Away", sport="football", status=status, start_time=timezone.now() - timezone.timedelta(minutes=5)
    )


def make_market(event, *, name="1X2"):
    return Market.objects.create(event=event, name=name, market_type="1x2", status=MarketStatus.OPEN)


def make_selection(market, *, odds="2.0000", name="Home"):
    return Selection.objects.create(market=market, name=name, current_odds=D(odds))


def fund(user, amount=None, currency=Currency.USDT):
    amount = D("1000") if amount is None else amount
    WalletService.credit_wallet(
        user=user, currency=currency, amount=amount, transaction_type=TransactionType.DEPOSIT, idempotency_key=key()
    )


def place(user, selection, *, stake=None, odds=None, currency=Currency.USDT):
    stake = D("10") if stake is None else stake
    bet, _ = BetPlacementService.place_bet(
        user=user, currency=currency, stake_amount=stake,
        legs=[(str(selection.id), odds or selection.current_odds)], idempotency_key=key(),
    )
    return bet


def place_combo(user, selections, *, stake=None, currency=Currency.USDT):
    stake = D("10") if stake is None else stake
    bet, _ = BetPlacementService.place_bet(
        user=user, currency=currency, stake_amount=stake,
        legs=[(str(s.id), s.current_odds) for s in selections], idempotency_key=key(),
    )
    return bet


class SingleBetSettlementTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        self.event = make_event()
        self.market = make_market(self.event)
        self.home = make_selection(self.market, name="Home", odds="2.0000")
        self.away = make_selection(self.market, name="Away", odds="3.0000")

    def settle(self, **kw):
        return EventSettlementService.settle_event(event=self.event, **kw)

    def test_single_win(self):
        bet = place(self.user, self.home, stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)
        self.assertEqual(bet.selections.get().status, BetStatus.WON)
        self.assertIsNotNone(bet.settlement_transaction)

        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("0"))
        self.assertEqual(wallet.available_balance, D("1000") - D("10") + D("20"))  # stake back out, 20 paid in

    def test_single_loss(self):
        bet = place(self.user, self.away, stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id)])  # home wins -> away loses

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.LOST)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("0"))
        self.assertEqual(wallet.available_balance, D("990"))  # stake gone, nothing paid back

    def test_single_void_full_refund(self):
        bet = place(self.user, self.home, stake=D("10"))
        self.settle(voided_selection_ids=[str(self.home.id), str(self.away.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.VOID)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.locked_balance, wallet.available_balance), (D("0"), D("1000")))

    def test_event_cancelled_voids_every_bet(self):
        bet1 = place(self.user, self.home, stake=D("10"))
        bet2 = place(self.user, self.away, stake=D("15"))
        self.settle(is_cancelled=True)

        for bet in (bet1, bet2):
            bet.refresh_from_db()
            self.assertEqual(bet.status, BetStatus.VOID)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.CANCELLED)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.locked_balance, wallet.available_balance), (D("0"), D("1000")))

    def test_market_and_selections_resolved(self):
        place(self.user, self.home, stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id)])

        self.market.refresh_from_db()
        self.home.refresh_from_db()
        self.away.refresh_from_db()
        self.assertEqual(self.market.status, MarketStatus.SETTLED)
        self.assertEqual(self.home.status, SelectionStatus.WON)
        self.assertEqual(self.away.status, SelectionStatus.LOST)

    def test_ledger_balances_zero_sum_after_settlement(self):
        bet = place(self.user, self.home, stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id)])
        bet.refresh_from_db()
        for txn in (bet.reservation_transaction, bet.settlement_transaction):
            debit = sum(e.amount for e in txn.entries.all() if e.entry_type == "debit")
            credit = sum(e.amount for e in txn.entries.all() if e.entry_type == "credit")
            self.assertEqual(debit, credit)


class ComboBetSettlementTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        self.event = make_event()
        self.market1 = make_market(self.event, name="1X2")
        self.market2 = make_market(self.event, name="Totals")
        self.home = make_selection(self.market1, name="Home", odds="2.0000")
        self.away = make_selection(self.market1, name="Away", odds="3.0000")
        self.over = make_selection(self.market2, name="Over 2.5", odds="1.5000")
        self.under = make_selection(self.market2, name="Under 2.5", odds="2.5000")

    def settle(self, **kw):
        return EventSettlementService.settle_event(event=self.event, **kw)

    def test_combo_all_legs_win(self):
        bet = place_combo(self.user, [self.home, self.over], stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id), str(self.over.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)
        # total_odds = 2.0 * 1.5 = 3.0; payout = 30
        self.assertEqual(bet.potential_payout, D("30.00000000"))
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.available_balance, D("1000") - D("10") + D("30"))

    def test_combo_one_leg_loses_whole_bet_loses(self):
        bet = place_combo(self.user, [self.home, self.over], stake=D("10"))
        # home loses (away wins), over wins — one loss kills the parlay
        self.settle(winning_selection_ids=[str(self.away.id), str(self.over.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.LOST)
        legs = {leg.selection_id: leg.status for leg in bet.selections.all()}
        self.assertEqual(legs[self.home.id], BetStatus.LOST)
        self.assertEqual(legs[self.over.id], BetStatus.WON)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.available_balance, D("990"))

    def test_combo_voided_leg_reduces_to_remaining_legs_odds(self):
        bet = place_combo(self.user, [self.home, self.over], stake=D("10"))
        # `over` is voided (e.g. postponed prop); home still wins on its own.
        self.settle(winning_selection_ids=[str(self.home.id)], voided_selection_ids=[str(self.over.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)
        # effective odds = 2.0 (voided leg contributes x1, not its own 1.5)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.available_balance, D("1000") - D("10") + D("20"))

    def test_combo_all_legs_voided_full_refund(self):
        bet = place_combo(self.user, [self.home, self.over], stake=D("10"))
        self.settle(voided_selection_ids=[str(self.home.id), str(self.away.id), str(self.over.id), str(self.under.id)])

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.VOID)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.locked_balance, wallet.available_balance), (D("0"), D("1000")))

    def test_combo_spanning_two_events_waits_for_both(self):
        other_event = make_event()
        other_market = make_market(other_event)
        other_selection = make_selection(other_market, odds="2.0000")

        bet = place_combo(self.user, [self.home, other_selection], stake=D("10"))
        self.settle(winning_selection_ids=[str(self.home.id)])  # only settles self.event

        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.PENDING)  # still waiting on other_event
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("10"))  # stake still reserved

        EventSettlementService.settle_event(event=other_event, winning_selection_ids=[str(other_selection.id)])
        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)


class SettlementIdempotencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        self.event = make_event()
        self.market = make_market(self.event)
        self.home = make_selection(self.market, odds="2.0000")

    def test_rerunning_settlement_does_not_double_pay(self):
        bet = place(self.user, self.home, stake=D("10"))
        EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        wallet_after_first = Wallet.objects.get(user=self.user, currency=Currency.USDT)

        # exact same result resubmitted — must be a safe no-op, not a second payout
        EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        wallet_after_second = Wallet.objects.get(user=self.user, currency=Currency.USDT)

        self.assertEqual(wallet_after_first.available_balance, wallet_after_second.available_balance)
        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)

    def test_conflicting_resubmission_rejected(self):
        away = make_selection(self.market, name="Away", odds="3.0000")
        EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        with self.assertRaises(ResultAlreadySubmittedError):
            EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(away.id)])

    def test_batch_created_per_run_and_marked_completed(self):
        place(self.user, self.home, stake=D("10"))
        batch1 = EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        batch2 = EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        self.assertNotEqual(batch1.pk, batch2.pk)
        self.assertEqual(batch1.status, SettlementStatus.COMPLETED)
        self.assertEqual(batch1.total_bets_processed, 1)
        self.assertEqual(batch2.total_bets_processed, 0)  # nothing left PENDING on the re-run
        self.assertEqual(SettlementBatch.objects.filter(event=self.event).count(), 2)

    def test_bet_already_settled_by_another_path_is_skipped_safely(self):
        """Directly exercises `_settle_one_bet`'s own PENDING re-check."""
        from .services import BetSettlementEngine

        bet = place(self.user, self.home, stake=D("10"))
        EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
        batch = SettlementBatch.objects.filter(event=self.event).first()
        # bet is already WON; calling the per-bet settler again must be a no-op
        did_settle = BetSettlementEngine._settle_one_bet(bet.pk, batch.pk)
        self.assertFalse(did_settle)


class ConcurrentSettlementTests(TransactionTestCase):
    def setUp(self):
        self.event = make_event()
        self.market = make_market(self.event)
        self.home = make_selection(self.market, odds="2.0000")
        self.users = []
        for _ in range(10):
            user = User.objects.create_user()
            fund(user)
            place(user, self.home, stake=D("10"))
            self.users.append(user)

    def test_multiple_workers_settling_the_same_batch_pay_each_bet_once(self):
        """Simulates N worker processes all calling settle_event for the SAME event at
        roughly the same time (e.g. a retried Celery task, or two operators clicking the
        admin action back to back) — every bet must be paid exactly once."""
        errors = []

        def worker():
            try:
                EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for user in self.users:
            bet = Bet.objects.get(user=user)
            self.assertEqual(bet.status, BetStatus.WON)
            wallet = Wallet.objects.get(user=user, currency=Currency.USDT)
            self.assertEqual(wallet.available_balance, D("1000") - D("10") + D("20"))  # exactly one payout

        total_processed = sum(b.total_bets_processed for b in SettlementBatch.objects.filter(event=self.event))
        self.assertEqual(total_processed, 10)  # each of the 10 bets counted in exactly one batch's total


class SettlementApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "pw-12345-abc")
        self.player = User.objects.create_user()
        fund(self.player)
        self.event = make_event()
        self.market = make_market(self.event)
        self.home = make_selection(self.market, odds="2.0000")
        self.away = make_selection(self.market, name="Away", odds="3.0000")

    def test_settle_endpoint_requires_staff(self):
        self.client.force_authenticate(self.player)
        response = self.client.post(f"/api/v1/settlement/events/{self.event.id}/settle/", {"winning_selection_ids": [str(self.home.id)]}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_settle_endpoint_settles_bets_and_returns_batch(self):
        bet = place(self.player, self.home, stake=D("10"))
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f"/api/v1/settlement/events/{self.event.id}/settle/",
            {"winning_selection_ids": [str(self.home.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["total_bets_processed"], 1)
        bet.refresh_from_db()
        self.assertEqual(bet.status, BetStatus.WON)

    def test_settle_endpoint_requires_winners_unless_cancelled(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(f"/api/v1/settlement/events/{self.event.id}/settle/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_settle_endpoint_conflict_returns_standard_envelope(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/v1/settlement/events/{self.event.id}/settle/", {"winning_selection_ids": [str(self.home.id)]}, format="json")
        response = self.client.post(f"/api/v1/settlement/events/{self.event.id}/settle/", {"winning_selection_ids": [str(self.away.id)]}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "result_already_submitted")

    def test_batches_list_requires_staff_and_filters_by_event(self):
        place(self.player, self.home, stake=D("10"))
        EventSettlementService.settle_event(event=self.event, winning_selection_ids=[str(self.home.id)])

        self.client.force_authenticate(self.player)
        self.assertEqual(self.client.get("/api/v1/settlement/batches/").status_code, 403)

        self.client.force_authenticate(self.admin)
        response = self.client.get("/api/v1/settlement/batches/", {"event": str(self.event.id)})
        self.assertEqual(response.json()["count"], 1)
