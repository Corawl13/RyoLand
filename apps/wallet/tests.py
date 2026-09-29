"""Tests for apps.wallet. Run with: python manage.py test apps.wallet"""
import threading
import uuid
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase
from rest_framework.test import APITestCase

from apps.accounts.models import User

from .exceptions import (
    DuplicateIdempotencyKeyError,
    InsufficientBalanceError,
    InsufficientLockedBalanceError,
    InvalidAmountError,
)
from .models import (
    AccountType,
    Currency,
    EntryType,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    TransactionType,
    Wallet,
)
from .services import WalletService

D = Decimal


def key() -> str:
    return str(uuid.uuid4())


class LedgerBalanceInvariantTests(TransactionTestCase):
    """Every posted transaction must be debit == credit, and every account/amount must
    agree on currency. `TransactionTestCase` (not `TestCase`) because the concurrency
    tests below need real, separately-committing DB transactions."""

    def setUp(self):
        self.user = User.objects.create_user()

    def all_transactions_balance(self):
        for txn in LedgerTransaction.objects.all():
            debit = sum((e.amount for e in txn.entries.all() if e.entry_type == EntryType.DEBIT), D(0))
            credit = sum((e.amount for e in txn.entries.all() if e.entry_type == EntryType.CREDIT), D(0))
            self.assertEqual(debit, credit, f"{txn.transaction_type} {txn.id} unbalanced")
            self.assertEqual(len({e.account.currency for e in txn.entries.all()} | {txn.currency}), 1)

    def test_deposit_balances_and_credits_available(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100.5"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("100.5"), D("0")))
        self.all_transactions_balance()

    def test_bonus_grant_uses_bonus_pool_not_deposits(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.BONUS, amount=D("10"),
            transaction_type=TransactionType.BONUS_GRANT, idempotency_key=key(),
        )
        entry = LedgerEntry.objects.get(entry_type=EntryType.CREDIT)
        self.assertEqual(entry.account.account_type, AccountType.SYSTEM_BONUS_POOL)
        self.assertFalse(LedgerAccount.objects.filter(account_type=AccountType.SYSTEM_DEPOSITS).exists())
        self.all_transactions_balance()

    def test_withdrawal_debits_available_and_rejects_overdraw(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.TON, amount=D("50"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.debit_wallet(user=self.user, currency=Currency.TON, amount=D("30"), idempotency_key=key())
        wallet = Wallet.objects.get(user=self.user, currency=Currency.TON)
        self.assertEqual(wallet.available_balance, D("20"))
        with self.assertRaises(InsufficientBalanceError):
            WalletService.debit_wallet(user=self.user, currency=Currency.TON, amount=D("21"), idempotency_key=key())
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, D("20"))  # rejected attempt changed nothing
        self.all_transactions_balance()

    def test_reserve_and_release_round_trip(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.USDT, amount=D("40"),
            transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
        )
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("60"), D("40")))

        WalletService.release_funds(user=self.user, currency=Currency.USDT, amount=D("40"), idempotency_key=key())
        wallet.refresh_from_db()
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("100"), D("0")))
        self.all_transactions_balance()

    def test_reserve_rejects_overdraw_and_release_rejects_over_unlock(self):
        with self.assertRaises(InsufficientBalanceError):
            WalletService.reserve_funds(
                user=self.user, currency=Currency.USDT, amount=D("1"),
                transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
            )
        with self.assertRaises(InsufficientLockedBalanceError):
            WalletService.release_funds(user=self.user, currency=Currency.USDT, amount=D("1"), idempotency_key=key())

    def test_reserve_funds_rejects_wrong_transaction_type(self):
        with self.assertRaises(ValueError):
            WalletService.reserve_funds(
                user=self.user, currency=Currency.USDT, amount=D("1"),
                transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
            )

    def test_settle_payout_total_loss_goes_to_house(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.USDT, amount=D("20"),
            transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
        )
        WalletService.settle_payout(
            user=self.user, currency=Currency.USDT, locked_amount=D("20"), payout_amount=D("0"),
            idempotency_key=key(),
        )
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("80"), D("0")))
        house = LedgerAccount.objects.get(account_type=AccountType.SYSTEM_HOUSE_REVENUE, currency=Currency.USDT)
        self.assertEqual(house.entries.get().amount, D("20"))
        self.assertEqual(house.entries.get().entry_type, EntryType.DEBIT)
        self.all_transactions_balance()

    def test_settle_payout_push_returns_full_stake_no_house_entry(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.USDT, amount=D("20"),
            transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
        )
        txn = WalletService.settle_payout(
            user=self.user, currency=Currency.USDT, locked_amount=D("20"), payout_amount=D("20"),
            idempotency_key=key(),
        )
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("100"), D("0")))
        self.assertEqual(txn.entries.count(), 2)  # only the locked->available pair, no house entry
        self.assertFalse(LedgerAccount.objects.filter(account_type=AccountType.SYSTEM_HOUSE_REVENUE).exists())

    def test_settle_payout_win_beyond_stake_debits_house(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.USDT, amount=D("20"),
            transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
        )
        WalletService.settle_payout(
            user=self.user, currency=Currency.USDT, locked_amount=D("20"), payout_amount=D("50"),
            idempotency_key=key(),
        )
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual((wallet.available_balance, wallet.locked_balance), (D("130"), D("0")))
        house = LedgerAccount.objects.get(account_type=AccountType.SYSTEM_HOUSE_REVENUE, currency=Currency.USDT)
        entry = house.entries.get()
        self.assertEqual((entry.entry_type, entry.amount), (EntryType.CREDIT, D("30")))
        self.all_transactions_balance()

    def test_settle_payout_can_route_to_telegram_escrow(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.STARS, amount=D("500"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.STARS, amount=D("100"),
            transaction_type=TransactionType.PURCHASE_TG_STARS, idempotency_key=key(),
        )
        WalletService.settle_payout(
            user=self.user, currency=Currency.STARS, locked_amount=D("100"), payout_amount=D("0"),
            idempotency_key=key(), transaction_type=TransactionType.PURCHASE_TG_STARS,
            house_account_type=AccountType.TELEGRAM_SERVICES_ESCROW,
        )
        escrow = LedgerAccount.objects.get(account_type=AccountType.TELEGRAM_SERVICES_ESCROW, currency=Currency.STARS)
        self.assertEqual(escrow.entries.get().amount, D("100"))
        self.assertFalse(LedgerAccount.objects.filter(account_type=AccountType.SYSTEM_HOUSE_REVENUE).exists())

    def test_invalid_amount_rejected(self):
        for bad in (D("0"), D("-5")):
            with self.assertRaises(InvalidAmountError):
                WalletService.credit_wallet(
                    user=self.user, currency=Currency.USDT, amount=bad,
                    transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
                )

    def test_non_decimal_amount_rejected(self):
        with self.assertRaises(TypeError):
            WalletService.credit_wallet(
                user=self.user, currency=Currency.USDT, amount=10.5,  # float, not Decimal
                transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
            )


class IdempotencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()

    def test_duplicate_key_returns_original_without_double_crediting(self):
        k = key()
        first = WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=k,
        )
        second = WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=k,
        )
        self.assertEqual(first.pk, second.pk)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.available_balance, D("10"))  # not 20
        self.assertEqual(LedgerTransaction.objects.filter(idempotency_key=k).count(), 1)

    def test_replay_across_every_write_method(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        cases = [
            lambda k: WalletService.debit_wallet(user=self.user, currency=Currency.USDT, amount=D("5"), idempotency_key=k),
            lambda k: WalletService.reserve_funds(
                user=self.user, currency=Currency.USDT, amount=D("5"),
                transaction_type=TransactionType.BET_RESERVATION, idempotency_key=k,
            ),
        ]
        for make_call in cases:
            k = key()
            first = make_call(k)
            before = Wallet.objects.get(user=self.user, currency=Currency.USDT)
            second = make_call(k)
            after = Wallet.objects.get(user=self.user, currency=Currency.USDT)
            self.assertEqual(first.pk, second.pk)
            self.assertEqual((before.available_balance, before.locked_balance), (after.available_balance, after.locked_balance))

    def test_key_reused_for_different_shape_conflicts(self):
        k = key()
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=k,
        )
        with self.assertRaises(DuplicateIdempotencyKeyError):
            WalletService.credit_wallet(
                user=self.user, currency=Currency.TON, amount=D("10"),  # different currency, same key
                transaction_type=TransactionType.DEPOSIT, idempotency_key=k,
            )

    def test_concurrent_replay_of_the_same_key_credits_once(self):
        """Two requests racing on the SAME new idempotency key — the create-race path in
        `_start_or_replay`, not the already-exists path above."""
        k = key()
        barrier = threading.Barrier(2)
        errors = []

        def attempt():
            try:
                barrier.wait(timeout=5)
                WalletService.credit_wallet(
                    user=self.user, currency=Currency.USDT, amount=D("10"),
                    transaction_type=TransactionType.DEPOSIT, idempotency_key=k,
                )
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)
            finally:
                connections.close_all()  # each thread needs its own DB connection

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(LedgerTransaction.objects.filter(idempotency_key=k).count(), 1)
        wallet = Wallet.objects.get(user=self.user, currency=Currency.USDT)
        self.assertEqual(wallet.available_balance, D("10"))


class ConcurrencyLockingTests(TransactionTestCase):
    """`select_for_update()` in `_locked_wallet` must make concurrent operations on the
    SAME wallet serialize rather than race, even under real overlapping DB transactions."""

    def setUp(self):
        self.user = User.objects.create_user()
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )

    def test_concurrent_withdrawals_cannot_overdraw(self):
        """Ten concurrent $20 withdrawals against a $100 balance: exactly 5 may succeed."""
        results = []
        lock = threading.Lock()

        def attempt():
            try:
                WalletService.debit_wallet(
                    user=self.user, currency=Currency.USDT, amount=D("20"), idempotency_key=key()
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
        self.assertEqual(wallet.available_balance, D("0"))
        self.assertGreaterEqual(wallet.available_balance, D("0"))  # the DB CheckConstraint held too


class AccountChartTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user()

    def test_system_accounts_are_singletons_across_currencies_and_never_collide(self):
        a = LedgerAccount.objects.system(AccountType.SYSTEM_HOUSE_REVENUE, Currency.USDT)
        b = LedgerAccount.objects.system(AccountType.SYSTEM_HOUSE_REVENUE, Currency.USDT)
        c = LedgerAccount.objects.system(AccountType.SYSTEM_HOUSE_REVENUE, Currency.TON)
        self.assertEqual(a.pk, b.pk)
        self.assertNotEqual(a.pk, c.pk)
        self.assertIsNone(a.user_id)

    def test_player_accounts_are_scoped_per_user_and_currency(self):
        other = User.objects.create_user()
        mine = LedgerAccount.objects.player(self.user, AccountType.PLAYER_AVAILABLE, Currency.USDT)
        theirs = LedgerAccount.objects.player(other, AccountType.PLAYER_AVAILABLE, Currency.USDT)
        self.assertNotEqual(mine.pk, theirs.pk)

    def test_manager_rejects_wrong_scope(self):
        with self.assertRaises(ValueError):
            LedgerAccount.objects.system(AccountType.PLAYER_AVAILABLE, Currency.USDT)
        with self.assertRaises(ValueError):
            LedgerAccount.objects.player(self.user, AccountType.SYSTEM_HOUSE_REVENUE, Currency.USDT)

    def test_ledger_entry_is_immutable(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        entry = LedgerEntry.objects.first()
        entry.amount = D("999")
        with self.assertRaises(ValueError):
            entry.save()

    def test_wallet_balance_cannot_go_negative_at_the_db_level(self):
        """Belt-and-braces: even if application code somehow skipped the balance check,
        the DB CheckConstraint on Wallet is the last line of defense."""
        from django.db import IntegrityError

        wallet = Wallet.objects.create(user=self.user, currency=Currency.USDT)
        wallet.available_balance = D("-1")
        with self.assertRaises(IntegrityError):
            wallet.save()


class WalletApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user()
        self.client.force_authenticate(self.user)

    def test_balances_endpoint_returns_every_currency(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.TON, amount=D("42"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        response = self.client.get("/api/v1/wallet/balances/")
        self.assertEqual(response.status_code, 200)
        body = {row["currency"]: row for row in response.json()}
        self.assertEqual(set(body), set(Currency.values))
        self.assertEqual(body["ton"]["available_balance"], "42.00000000")
        self.assertEqual(body["usdt"]["available_balance"], "0.00000000")

    def test_balances_require_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/wallet/balances/").status_code, 401)

    def test_transactions_list_shows_only_my_entries_not_house_accounts(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("100"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.reserve_funds(
            user=self.user, currency=Currency.USDT, amount=D("20"),
            transaction_type=TransactionType.BET_RESERVATION, idempotency_key=key(),
        )
        WalletService.settle_payout(
            user=self.user, currency=Currency.USDT, locked_amount=D("20"), payout_amount=D("0"),
            idempotency_key=key(),
        )
        response = self.client.get("/api/v1/wallet/transactions/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 3)
        settle = next(r for r in body["results"] if r["transaction_type"] == "bet_payout")
        self.assertEqual(len(settle["my_entries"]), 1)  # only the PLAYER_LOCKED credit — not the house debit
        self.assertEqual(settle["my_entries"][0]["account_type"], "player_locked")

    def test_transactions_list_does_not_leak_another_users_history(self):
        other = User.objects.create_user()
        WalletService.credit_wallet(
            user=other, currency=Currency.USDT, amount=D("999"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        response = self.client.get("/api/v1/wallet/transactions/")
        self.assertEqual(response.json()["count"], 0)

    def test_transactions_filter_by_currency_and_status(self):
        WalletService.credit_wallet(
            user=self.user, currency=Currency.USDT, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        WalletService.credit_wallet(
            user=self.user, currency=Currency.TON, amount=D("10"),
            transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
        )
        response = self.client.get("/api/v1/wallet/transactions/", {"currency": "ton"})
        self.assertEqual(response.json()["count"], 1)
        response = self.client.get("/api/v1/wallet/transactions/", {"status": "failed"})
        self.assertEqual(response.json()["count"], 0)

    def test_transactions_filter_rejects_invalid_choice(self):
        response = self.client.get("/api/v1/wallet/transactions/", {"currency": "usd"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_transactions_are_paginated(self):
        for _ in range(3):
            WalletService.credit_wallet(
                user=self.user, currency=Currency.USDT, amount=D("1"),
                transaction_type=TransactionType.DEPOSIT, idempotency_key=key(),
            )
        response = self.client.get("/api/v1/wallet/transactions/", {"page_size": 2})
        body = response.json()
        self.assertEqual((body["count"], len(body["results"])), (3, 2))
        self.assertIsNotNone(body["next"])
