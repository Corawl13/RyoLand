"""Tests for apps.betting. Run with: python manage.py test apps.betting

Uses TransactionTestCase (not TestCase) throughout: the concurrency tests need real,
separately-committing DB transactions across threads, and mixing TestCase (wrapped in a
rolled-back outer transaction) with TransactionTestCase in the same run causes surprising
cross-test state, so every class here uses TransactionTestCase for consistency.
"""
import threading
import uuid
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserStatus
from apps.wallet.models import Currency, TransactionType, Wallet
from apps.wallet.services import WalletService

from .exceptions import (
    BetLimitExceededError,
    DuplicateIdempotencyKeyError,
    EventNotOpenForBettingError,
    InvalidComboError,
    MarketSuspendedError,
    OddsChangedError,
    PlayerNotEligibleError,
    StakeOutOfRangeError,
)
from .models import (
    Bet,
    BetStatus,
    BetType,
    EventStatus,
    Market,
    MarketStatus,
    Selection,
    SportEvent,
)
from .services import BetPlacementService, InsufficientBalanceError

D = Decimal


def key() -> str:
    return str(uuid.uuid4())


def make_event(*, status=EventStatus.UPCOMING, start_time=None, sport="football"):
    return SportEvent.objects.create(
        name="Home vs Away", sport=sport, status=status, start_time=start_time or timezone.now() + timezone.timedelta(hours=1)
    )


def make_market(event=None, *, status=MarketStatus.OPEN, name="1X2"):
    return Market.objects.create(event=event or make_event(), name=name, market_type="1x2", status=status)


def make_selection(market=None, *, odds="2.0000", name="Home"):
    return Selection.objects.create(market=market or make_market(), name=name, current_odds=D(odds))


def fund(user, currency=Currency.USDT, amount=None):
    amount = D("1000") if amount is None else amount
    WalletService.credit_wallet(
        user=user, currency=currency, amount=amount, transaction_type=TransactionType.DEPOSIT, idempotency_key=key()
    )


class BetPlacementTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)

    def place(self, legs, **kw):
        return BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=kw.pop("stake_amount", D("10")),
            legs=legs, idempotency_key=kw.pop("idempotency_key", key()), **kw,
        )

    def test_single_bet_placement_and_payout_calculation(self):
        selection = make_selection(odds="2.5000")
        bet, created = self.place([(str(selection.id), D("2.5000"))], stake_amount=D("10"))
        self.assertTrue(created)
        self.assertEqual(bet.bet_type, BetType.SINGLE)
        self.assertEqual(bet.status, BetStatus.PENDING)
        self.assertEqual(bet.total_odds, D("2.5000"))
        self.assertEqual(bet.potential_payout, D("25.00000000"))
        self.assertEqual(bet.selections.count(), 1)
        leg = bet.selections.get()
        self.assertEqual(leg.locked_odds, D("2.5000"))
        self.assertEqual(leg.event_id, selection.market.event_id)

    def test_combo_bet_multiplies_odds_across_different_markets(self):
        s1 = make_selection(odds="2.0000")
        s2 = make_selection(make_market(name="Totals"), odds="1.5000")
        bet, created = self.place([(str(s1.id), D("2.0000")), (str(s2.id), D("1.5000"))], stake_amount=D("10"))
        self.assertTrue(created)
        self.assertEqual(bet.bet_type, BetType.COMBO)
        self.assertEqual(bet.total_odds, D("3.0000"))
        self.assertEqual(bet.potential_payout, D("30.00000000"))
        self.assertEqual(bet.selections.count(), 2)

    def test_combo_rejects_two_selections_from_the_same_market(self):
        market = make_market()
        s1 = make_selection(market, name="Home", odds="2.0")
        s2 = make_selection(market, name="Away", odds="3.0")
        with self.assertRaises(InvalidComboError):
            self.place([(str(s1.id), D("2.0")), (str(s2.id), D("3.0"))])

    def test_wallet_locked_balance_increases_by_stake_on_placement(self):
        selection = make_selection()
        self.place([(str(selection.id), D("2.0000"))], stake_amount=D("40"))
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("40"))
        self.assertEqual(wallet.available_balance, D("960"))

    def test_reservation_transaction_is_linked_and_balanced(self):
        selection = make_selection()
        bet, _ = self.place([(str(selection.id), D("2.0000"))], stake_amount=D("15"))
        self.assertIsNotNone(bet.reservation_transaction)
        txn = bet.reservation_transaction
        self.assertEqual(txn.transaction_type, TransactionType.BET_RESERVATION)
        debit = sum(e.amount for e in txn.entries.all() if e.entry_type == "debit")
        credit = sum(e.amount for e in txn.entries.all() if e.entry_type == "credit")
        self.assertEqual(debit, credit)

    def test_rejects_suspended_market(self):
        selection = make_selection(make_market(status=MarketStatus.SUSPENDED))
        with self.assertRaises(MarketSuspendedError):
            self.place([(str(selection.id), D("2.0000"))])
        self.assertFalse(Bet.objects.exists())
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("0"))  # nothing reserved on a rejected bet

    def test_rejects_finished_event(self):
        selection = make_selection(make_market(make_event(status=EventStatus.FINISHED)))
        with self.assertRaises(EventNotOpenForBettingError):
            self.place([(str(selection.id), D("2.0000"))])

    def test_rejects_upcoming_event_past_kickoff_even_if_status_is_stale(self):
        stale = make_event(status=EventStatus.UPCOMING, start_time=timezone.now() - timezone.timedelta(minutes=1))
        selection = make_selection(make_market(stale))
        with self.assertRaises(EventNotOpenForBettingError):
            self.place([(str(selection.id), D("2.0000"))])

    def test_allows_live_event(self):
        selection = make_selection(make_market(make_event(status=EventStatus.LIVE)))
        bet, created = self.place([(str(selection.id), D("2.0000"))])
        self.assertTrue(created)

    def test_odds_drift_within_tolerance_accepted_locks_db_odds(self):
        selection = make_selection(odds="2.0000")
        # 2% tolerance: submitted 1.99 vs current 2.00 is within range, and the DB value
        # (not the submitted one) is what gets locked into the ticket.
        bet, created = self.place([(str(selection.id), D("1.9900"))])
        self.assertTrue(created)
        self.assertEqual(bet.selections.get().locked_odds, D("2.0000"))

    def test_odds_drift_beyond_tolerance_rejected(self):
        selection = make_selection(odds="2.0000")
        with self.assertRaises(OddsChangedError):
            self.place([(str(selection.id), D("1.5000"))])

    def test_insufficient_balance_rejected_and_nothing_persisted(self):
        selection = make_selection()
        with self.assertRaises(InsufficientBalanceError):
            self.place([(str(selection.id), D("2.0000"))], stake_amount=D("5000"))  # within limits, exceeds funded balance
        self.assertFalse(Bet.objects.exists())

    def test_stake_out_of_range_rejected(self):
        selection = make_selection()
        with self.assertRaises(StakeOutOfRangeError):
            self.place([(str(selection.id), D("2.0000"))], stake_amount=D("0.0000001"))

    def test_self_excluded_player_cannot_place_bets(self):
        self.user.status = UserStatus.SELF_EXCLUDED
        self.user.save()
        selection = make_selection()
        with self.assertRaises(PlayerNotEligibleError):
            self.place([(str(selection.id), D("2.0000"))])

    def test_bet_limit_exceeded_rejected(self):
        selection = make_selection(odds="9999.0000")
        with self.assertRaises(BetLimitExceededError):
            self.place([(str(selection.id), D("9999.0000"))], stake_amount=D("100"))


class IdempotencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        self.selection = make_selection(odds="2.0000")

    def test_duplicate_key_returns_existing_bet_without_double_reserving(self):
        k = key()
        first, created1 = BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=k,
        )
        second, created2 = BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=k,
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Bet.objects.filter(idempotency_key=k).count(), 1)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("10"))  # not 20

    def test_key_reused_by_a_different_user_conflicts(self):
        k = key()
        BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=k,
        )
        other = User.objects.create_user()
        fund(other)
        with self.assertRaises(DuplicateIdempotencyKeyError):
            BetPlacementService.place_bet(
                user=other, currency=Currency.USDT, stake_amount=D("10"),
                legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=k,
            )

    def test_concurrent_replay_of_the_same_key_places_one_bet(self):
        k = key()
        barrier = threading.Barrier(2)
        errors = []

        def attempt():
            try:
                barrier.wait(timeout=5)
                BetPlacementService.place_bet(
                    user=self.user, currency=Currency.USDT, stake_amount=D("10"),
                    legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=k,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(Bet.objects.filter(idempotency_key=k).count(), 1)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("10"))


class ConcurrentStakeReservationTests(TransactionTestCase):
    """Distinct bets (different idempotency keys) racing against a wallet that cannot
    cover all of them — `WalletService`'s row lock, exercised through the betting layer."""

    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user, amount=D("100"))
        self.selection = make_selection(odds="2.0000")

    def test_concurrent_bets_cannot_overdraw_the_wallet(self):
        results = []
        lock = threading.Lock()

        def attempt():
            try:
                BetPlacementService.place_bet(
                    user=self.user, currency=Currency.USDT, stake_amount=D("20"),
                    legs=[(str(self.selection.id), D("2.0000"))], idempotency_key=key(),
                )
                outcome = "ok"
            except InsufficientBalanceError:
                outcome = "rejected"
            finally:
                connections.close_all()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ok"), 5)
        self.assertEqual(results.count("rejected"), 5)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.locked_balance, D("100"))
        self.assertEqual(wallet.available_balance, D("0"))
        self.assertEqual(Bet.objects.filter(status=BetStatus.PENDING).count(), 5)


class BetTransitionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        selection = make_selection()
        self.bet, _ = BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(selection.id), D("2.0000"))], idempotency_key=key(),
        )
        self.leg = self.bet.selections.get()

    def test_mark_won_transitions_from_pending(self):
        self.bet.mark_won()
        self.bet.refresh_from_db()
        self.assertEqual(self.bet.status, BetStatus.WON)

    def test_cannot_transition_an_already_settled_bet(self):
        self.bet.mark_lost()
        with self.assertRaises(ValueError):
            self.bet.mark_won()

    def test_bet_selection_transitions_independently(self):
        self.leg.mark_won()
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, BetStatus.WON)
        self.bet.refresh_from_db()
        self.assertEqual(self.bet.status, BetStatus.PENDING)  # leg result alone doesn't settle the bet


class BettingApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        fund(self.user)
        self.client.force_authenticate(self.user)

    def test_place_bet_endpoint(self):
        selection = make_selection(odds="2.0000")
        response = self.client.post(
            "/api/v1/betting/bets/place/",
            {
                "currency": "usdt", "stake_amount": "10", "idempotency_key": key(),
                "selections": [{"selection_id": str(selection.id), "odds": "2.0000"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["potential_payout"], "20.00000000")

    def test_place_bet_replay_returns_200(self):
        selection = make_selection(odds="2.0000")
        payload = {
            "currency": "usdt", "stake_amount": "10", "idempotency_key": key(),
            "selections": [{"selection_id": str(selection.id), "odds": "2.0000"}],
        }
        first = self.client.post("/api/v1/betting/bets/place/", payload, format="json")
        second = self.client.post("/api/v1/betting/bets/place/", payload, format="json")
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_place_bet_error_uses_standard_envelope(self):
        selection = make_selection(make_market(status=MarketStatus.SUSPENDED))
        response = self.client.post(
            "/api/v1/betting/bets/place/",
            {
                "currency": "usdt", "stake_amount": "10", "idempotency_key": key(),
                "selections": [{"selection_id": str(selection.id), "odds": "2.0000"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "market_suspended")

    def test_place_bet_requires_authentication(self):
        self.client.force_authenticate(None)
        response = self.client.post("/api/v1/betting/bets/place/", {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_bets_list_filters_by_status_and_is_scoped_to_user(self):
        selection = make_selection(odds="2.0000")
        bet, _ = BetPlacementService.place_bet(
            user=self.user, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(selection.id), D("2.0000"))], idempotency_key=key(),
        )
        other = User.objects.create_user()
        fund(other)
        BetPlacementService.place_bet(
            user=other, currency=Currency.USDT, stake_amount=D("10"),
            legs=[(str(selection.id), D("2.0000"))], idempotency_key=key(),
        )

        response = self.client.get("/api/v1/betting/bets/")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["id"], str(bet.id))

        bet.mark_won()
        self.assertEqual(self.client.get("/api/v1/betting/bets/", {"status": "won"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/v1/betting/bets/", {"status": "lost"}).json()["count"], 0)

    def test_events_endpoint_is_public_and_hides_non_open_markets(self):
        self.client.force_authenticate(None)
        event = make_event()
        make_selection(make_market(event, status=MarketStatus.OPEN, name="Open Market"))
        make_selection(make_market(event, status=MarketStatus.SUSPENDED, name="Suspended Market"))
        make_event(status=EventStatus.FINISHED)  # excluded entirely

        response = self.client.get("/api/v1/betting/events/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        markets = body["results"][0]["markets"]
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0]["name"], "Open Market")
