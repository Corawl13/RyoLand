"""Wallet & ledger service layer.

See the module docstring in models.py for the accounting convention (DEBIT increases an
account's balance, CREDIT decreases it, uniformly for every account). Every public method
here:

1. Looks up `idempotency_key` first and, if it already exists, returns that transaction
   untouched — no new entries, no balance change. This makes retried requests safe.
2. Locks the one `Wallet` row it will mutate with `select_for_update()` before reading its
   current balance, so a concurrent call for the same user/currency serializes behind it
   rather than racing.
3. Does everything inside one `@transaction.atomic` block, so a failure (insufficient
   funds, an unbalanced entry set) leaves no partial state: either every row commits or
   none does.

`transaction_type` is not validated against arbitrary user input in Step 2 — these methods
are a Python API for other apps (a future betting engine, `telegram_services`, `payments`)
to call directly, not something exposed over HTTP here. The wallet HTTP API (views.py) is
read-only. A wrong `transaction_type` therefore raises a plain `ValueError` (a bug in the
calling code) rather than a DomainError a client would need to handle.
"""
from __future__ import annotations

from decimal import Decimal
from functools import wraps
import time

from django.db import IntegrityError, OperationalError, connections, transaction
from django.utils import timezone

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
    TransactionStatus,
    TransactionType,
    Wallet,
)

_RESERVABLE_TYPES = frozenset(
    {
        TransactionType.BET_RESERVATION,
        TransactionType.PURCHASE_TG_STARS,
        TransactionType.PURCHASE_TG_PREMIUM,
        TransactionType.PURCHASE_NFT,
    }
)
_CREDITABLE_TYPES = frozenset({TransactionType.DEPOSIT, TransactionType.BONUS_GRANT})


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


def _validate_amount(amount) -> Decimal:
    if not isinstance(amount, Decimal):
        raise TypeError(f"amount must be a Decimal, got {type(amount).__name__}.")
    if amount <= 0:
        raise InvalidAmountError()
    return amount


def _locked_wallet(user, currency: str) -> Wallet:
    """Row-lock the one Wallet that will be mutated, creating it first if this is the
    user's first activity in this currency. The create step races safely: on a concurrent
    first-use `get_or_create` may raise IntegrityError, which just means another request
    won the insert — we fall through to locking the row it created."""
    try:
        Wallet.objects.get_or_create(user=user, currency=currency)
    except IntegrityError:
        pass
    return Wallet.objects.select_for_update().get(user=user, currency=currency)


class _TransactionHandle:
    """Result of `_start_or_replay`: either a fresh PENDING transaction the caller must
    finish posting entries for, or an existing one to return as-is (idempotent replay)."""

    __slots__ = ("ledger_transaction", "is_new")

    def __init__(self, ledger_transaction: LedgerTransaction, is_new: bool):
        self.ledger_transaction = ledger_transaction
        self.is_new = is_new


def _start_or_replay(
    *, idempotency_key: str, transaction_type: str, currency: str, metadata: dict | None
) -> _TransactionHandle:
    existing = LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.transaction_type != transaction_type or existing.currency != currency:
            raise DuplicateIdempotencyKeyError()
        return _TransactionHandle(existing, is_new=False)
    try:
        with transaction.atomic():  # savepoint: a lost create-race must not poison the caller's transaction
            created = LedgerTransaction.objects.create(
                transaction_type=transaction_type,
                currency=currency,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
            )
    except IntegrityError:  # concurrent request created it first
        existing = LedgerTransaction.objects.get(idempotency_key=idempotency_key)
        if existing.transaction_type != transaction_type or existing.currency != currency:
            raise DuplicateIdempotencyKeyError() from None
        return _TransactionHandle(existing, is_new=False)
    return _TransactionHandle(created, is_new=True)


def _post_entries(ledger_transaction: LedgerTransaction, entries: list[tuple[LedgerAccount, str, Decimal]]) -> None:
    """Validate the entry set balances and belongs to one currency, then write it and mark
    the transaction COMPLETED. Must run inside the same atomic block that locked every
    Wallet row this entry set implies a change to."""
    total_debit = sum((amt for _, et, amt in entries if et == EntryType.DEBIT), Decimal(0))
    total_credit = sum((amt for _, et, amt in entries if et == EntryType.CREDIT), Decimal(0))
    if total_debit != total_credit:  # a bug in a caller, never a symptom of bad user input
        raise AssertionError(f"Unbalanced ledger transaction {ledger_transaction.id}: debit {total_debit} != credit {total_credit}.")
    if any(account.currency != ledger_transaction.currency for account, _, _ in entries):
        raise AssertionError(f"Ledger transaction {ledger_transaction.id} mixes currencies across its entries.")
    LedgerEntry.objects.bulk_create(
        LedgerEntry(transaction=ledger_transaction, account=account, entry_type=entry_type, amount=amount)
        for account, entry_type, amount in entries
    )
    ledger_transaction.status = TransactionStatus.COMPLETED
    ledger_transaction.completed_at = timezone.now()
    ledger_transaction.save(update_fields=["status", "completed_at", "updated_at"])


class WalletService:
    """Stateless facade over the ledger — every method is a `@staticmethod`; there is no
    instance state to hold."""

    # ----------------------------------------------------------------------------------
    # Reads
    # ----------------------------------------------------------------------------------
    @staticmethod
    def list_balances(user) -> list[Wallet]:
        """Every currency's wallet for `user`, creating zero-balance rows for any currency
        they haven't touched yet, so the API always returns a complete, stable set."""
        existing = {w.currency: w for w in Wallet.objects.filter(user=user)}
        missing = [c for c in Currency.values if c not in existing]
        if missing:
            Wallet.objects.bulk_create([Wallet(user=user, currency=c) for c in missing], ignore_conflicts=True)
            existing = {w.currency: w for w in Wallet.objects.filter(user=user)}
        return [existing[c] for c in Currency.values]

    # ----------------------------------------------------------------------------------
    # Writes
    # ----------------------------------------------------------------------------------
    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def credit_wallet(
        *,
        user,
        currency: str,
        amount: Decimal,
        transaction_type: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> LedgerTransaction:
        """External funds arriving into `available_balance` — a deposit or a bonus grant.
        The source system account is chosen from `transaction_type`: `SYSTEM_DEPOSITS` for
        a real deposit, `SYSTEM_BONUS_POOL` for a promotional grant, so the two are never
        mixed in the same clearing account for reporting."""
        _validate_amount(amount)
        if transaction_type not in _CREDITABLE_TYPES:
            raise ValueError(f"credit_wallet does not support transaction_type={transaction_type!r}.")
        handle = _start_or_replay(
            idempotency_key=idempotency_key, transaction_type=transaction_type, currency=currency, metadata=metadata
        )
        if not handle.is_new:
            return handle.ledger_transaction

        wallet = _locked_wallet(user, currency)
        source_type = (
            AccountType.SYSTEM_BONUS_POOL if transaction_type == TransactionType.BONUS_GRANT else AccountType.SYSTEM_DEPOSITS
        )
        player_available = LedgerAccount.objects.player(user, AccountType.PLAYER_AVAILABLE, currency)
        source = LedgerAccount.objects.system(source_type, currency)
        _post_entries(
            handle.ledger_transaction,
            [(player_available, EntryType.DEBIT, amount), (source, EntryType.CREDIT, amount)],
        )
        wallet.available_balance += amount
        wallet.save(update_fields=["available_balance", "updated_at"])
        return handle.ledger_transaction

    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def debit_wallet(
        *, user, currency: str, amount: Decimal, idempotency_key: str, metadata: dict | None = None
    ) -> LedgerTransaction:
        """`available_balance` leaving the platform — a withdrawal."""
        _validate_amount(amount)
        handle = _start_or_replay(
            idempotency_key=idempotency_key,
            transaction_type=TransactionType.WITHDRAWAL,
            currency=currency,
            metadata=metadata,
        )
        if not handle.is_new:
            return handle.ledger_transaction

        wallet = _locked_wallet(user, currency)
        if wallet.available_balance < amount:
            raise InsufficientBalanceError()
        player_available = LedgerAccount.objects.player(user, AccountType.PLAYER_AVAILABLE, currency)
        destination = LedgerAccount.objects.system(AccountType.SYSTEM_WITHDRAWALS, currency)
        _post_entries(
            handle.ledger_transaction,
            [(destination, EntryType.DEBIT, amount), (player_available, EntryType.CREDIT, amount)],
        )
        wallet.available_balance -= amount
        wallet.save(update_fields=["available_balance", "updated_at"])
        return handle.ledger_transaction

    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def reserve_funds(
        *,
        user,
        currency: str,
        amount: Decimal,
        transaction_type: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> LedgerTransaction:
        """Moves `amount` from available to locked: a bet stake, or funds held while a
        Telegram Stars/Premium/NFT purchase is confirmed."""
        _validate_amount(amount)
        if transaction_type not in _RESERVABLE_TYPES:
            raise ValueError(f"reserve_funds does not support transaction_type={transaction_type!r}.")
        handle = _start_or_replay(
            idempotency_key=idempotency_key, transaction_type=transaction_type, currency=currency, metadata=metadata
        )
        if not handle.is_new:
            return handle.ledger_transaction

        wallet = _locked_wallet(user, currency)
        if wallet.available_balance < amount:
            raise InsufficientBalanceError()
        player_available = LedgerAccount.objects.player(user, AccountType.PLAYER_AVAILABLE, currency)
        player_locked = LedgerAccount.objects.player(user, AccountType.PLAYER_LOCKED, currency)
        _post_entries(
            handle.ledger_transaction,
            [(player_locked, EntryType.DEBIT, amount), (player_available, EntryType.CREDIT, amount)],
        )
        wallet.available_balance -= amount
        wallet.locked_balance += amount
        wallet.save(update_fields=["available_balance", "locked_balance", "updated_at"])
        return handle.ledger_transaction

    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def release_funds(
        *,
        user,
        currency: str,
        amount: Decimal,
        idempotency_key: str,
        transaction_type: str = TransactionType.BET_CANCEL_REFUND,
        metadata: dict | None = None,
    ) -> LedgerTransaction:
        """Unlocks `amount` back to available in full — a cancelled bet or a failed/refused
        purchase. For a partial settlement (a bet that actually resolved), use
        `settle_payout` instead."""
        _validate_amount(amount)
        handle = _start_or_replay(
            idempotency_key=idempotency_key, transaction_type=transaction_type, currency=currency, metadata=metadata
        )
        if not handle.is_new:
            return handle.ledger_transaction

        wallet = _locked_wallet(user, currency)
        if wallet.locked_balance < amount:
            raise InsufficientLockedBalanceError()
        player_available = LedgerAccount.objects.player(user, AccountType.PLAYER_AVAILABLE, currency)
        player_locked = LedgerAccount.objects.player(user, AccountType.PLAYER_LOCKED, currency)
        _post_entries(
            handle.ledger_transaction,
            [(player_available, EntryType.DEBIT, amount), (player_locked, EntryType.CREDIT, amount)],
        )
        wallet.locked_balance -= amount
        wallet.available_balance += amount
        wallet.save(update_fields=["available_balance", "locked_balance", "updated_at"])
        return handle.ledger_transaction

    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def settle_payout(
        *,
        user,
        currency: str,
        locked_amount: Decimal,
        payout_amount: Decimal,
        idempotency_key: str,
        transaction_type: str = TransactionType.BET_PAYOUT,
        house_account_type: str = AccountType.SYSTEM_HOUSE_REVENUE,
        metadata: dict | None = None,
    ) -> LedgerTransaction:
        """Finalizes a previously-reserved `locked_amount`. `payout_amount` returns to the
        player's available balance — 0 for a total loss, exactly `locked_amount` for a
        push/void, up to any amount above `locked_amount` for a win beyond the stake. The
        difference settles against `house_account_type`: `SYSTEM_HOUSE_REVENUE` for a bet,
        `TELEGRAM_SERVICES_ESCROW` once a Stars/Premium/NFT purchase is fulfilled."""
        _validate_amount(locked_amount)
        if payout_amount < 0:
            raise InvalidAmountError()
        handle = _start_or_replay(
            idempotency_key=idempotency_key, transaction_type=transaction_type, currency=currency, metadata=metadata
        )
        if not handle.is_new:
            return handle.ledger_transaction

        wallet = _locked_wallet(user, currency)
        if wallet.locked_balance < locked_amount:
            raise InsufficientLockedBalanceError()
        player_locked = LedgerAccount.objects.player(user, AccountType.PLAYER_LOCKED, currency)
        entries = [(player_locked, EntryType.CREDIT, locked_amount)]
        if payout_amount > 0:
            player_available = LedgerAccount.objects.player(user, AccountType.PLAYER_AVAILABLE, currency)
            entries.append((player_available, EntryType.DEBIT, payout_amount))
        house_delta = locked_amount - payout_amount
        if house_delta != 0:
            house = LedgerAccount.objects.system(house_account_type, currency)
            entries.append((house, EntryType.DEBIT if house_delta > 0 else EntryType.CREDIT, abs(house_delta)))
        _post_entries(handle.ledger_transaction, entries)

        wallet.locked_balance -= locked_amount
        wallet.available_balance += payout_amount
        wallet.save(update_fields=["available_balance", "locked_balance", "updated_at"])
        return handle.ledger_transaction

    @staticmethod
    @_retry_sqlite_lock
    @transaction.atomic
    def mark_failed(ledger_transaction: LedgerTransaction, *, reason: str) -> LedgerTransaction:
        """Marks a still-PENDING transaction FAILED. No Step 2 flow leaves a transaction
        PENDING (every method above posts entries and completes synchronously), but this
        exists for future async flows (e.g. a blockchain deposit awaiting confirmations,
        or a payment-provider webhook reporting failure) that create a PENDING transaction
        up front and settle it later."""
        locked = LedgerTransaction.objects.select_for_update().get(pk=ledger_transaction.pk)
        if locked.status != TransactionStatus.PENDING:
            return locked  # already settled one way or another: no-op, not an error
        locked.status = TransactionStatus.FAILED
        locked.failure_reason = reason
        locked.save(update_fields=["status", "failure_reason", "updated_at"])
        return locked
