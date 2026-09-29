"""Double-entry ledger models.

Accounting convention (deliberately simplified for a custody ledger — "which bucket is
this money currently sitting in" — rather than an accrual P&L ledger): every account uses
the SAME rule. A DEBIT to an account increases its balance; a CREDIT decreases it. A
balanced `LedgerTransaction`'s entries always have sum(debit amounts) == sum(credit
amounts) for the whole transaction — read informally as "value flows from the credited
account(s) into the debited account(s))". There is deliberately no separate "asset vs
revenue normal-balance" bookkeeping to get wrong; see services.py for how this plays out
for deposits, withdrawals, bet reservation/settlement, and Telegram purchases.

Only `Wallet.available_balance` / `Wallet.locked_balance` are cached for fast reads (both
columns live on the SAME row, since a user's available and locked funds for one currency
are two numbers on one wallet, not two accounts to separately lock). System accounts
(house revenue, deposit/withdrawal clearing, ...) have no cache — nothing reads them at
player-facing request rates yet, so their balance is computed on demand from `LedgerEntry`
aggregation (see admin.py) rather than kept in sync as a second cache that could drift.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import BaseModel


class Currency(models.TextChoices):
    USDT = "usdt", "USDT"
    TON = "ton", "TON"
    STARS = "stars", "Telegram Stars"
    BONUS = "bonus", "Bonus credits"


class AccountType(models.TextChoices):
    # Player-scoped: one row per (user, currency). Every player has exactly these two.
    PLAYER_AVAILABLE = "player_available", "Player available balance"
    PLAYER_LOCKED = "player_locked", "Player locked balance"
    # System-scoped: one row per currency, shared by every player.
    SYSTEM_DEPOSITS = "system_deposits", "Deposits clearing"
    SYSTEM_WITHDRAWALS = "system_withdrawals", "Withdrawals clearing"
    SYSTEM_HOUSE_REVENUE = "system_house_revenue", "House revenue"
    SYSTEM_ESCROW = "system_escrow", "Escrow (reserved for future multi-party markets)"
    SYSTEM_FEES = "system_fees", "Platform fees"
    TELEGRAM_SERVICES_ESCROW = "telegram_services_escrow", "Telegram services escrow"
    SYSTEM_BONUS_POOL = "system_bonus_pool", "Bonus credit pool"


PLAYER_ACCOUNT_TYPES = frozenset({AccountType.PLAYER_AVAILABLE, AccountType.PLAYER_LOCKED})


class TransactionType(models.TextChoices):
    DEPOSIT = "deposit", "Deposit"
    WITHDRAWAL = "withdrawal", "Withdrawal"
    BET_RESERVATION = "bet_reservation", "Bet reservation"
    BET_PAYOUT = "bet_payout", "Bet payout"
    BET_CANCEL_REFUND = "bet_cancel_refund", "Bet cancel / refund"
    PURCHASE_TG_STARS = "purchase_tg_stars", "Telegram Stars purchase"
    PURCHASE_TG_PREMIUM = "purchase_tg_premium", "Telegram Premium purchase"
    PURCHASE_NFT = "purchase_nft", "NFT purchase"
    BONUS_GRANT = "bonus_grant", "Bonus grant"


class TransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class EntryType(models.TextChoices):
    DEBIT = "debit", "Debit"
    CREDIT = "credit", "Credit"


AMOUNT_KWARGS = dict(max_digits=28, decimal_places=8)


class Wallet(BaseModel):
    """A player's balance in one currency. `available_balance` is spendable now;
    `locked_balance` is reserved against an open bet or a pending purchase. Both are
    caches — the ledger, not this row, is the source of truth — kept in sync by
    `WalletService` inside the same atomic block as the `LedgerEntry` rows that justify
    the change."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallets")
    currency = models.CharField(max_length=10, choices=Currency.choices)
    available_balance = models.DecimalField(**AMOUNT_KWARGS, default=0)
    locked_balance = models.DecimalField(**AMOUNT_KWARGS, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "currency"], name="wallet_wallet_unique_user_currency"),
            models.CheckConstraint(condition=Q(available_balance__gte=0), name="wallet_wallet_available_gte_0"),
            models.CheckConstraint(condition=Q(locked_balance__gte=0), name="wallet_wallet_locked_gte_0"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.currency}"

    @property
    def total_balance(self):
        return self.available_balance + self.locked_balance


class LedgerAccountManager(models.Manager):
    def system(self, account_type: str, currency: str) -> "LedgerAccount":
        if account_type in PLAYER_ACCOUNT_TYPES:
            raise ValueError(f"{account_type!r} is a player-scoped account type, not a system one.")
        account, _ = self.get_or_create(account_type=account_type, currency=currency, user=None)
        return account

    def player(self, user, account_type: str, currency: str) -> "LedgerAccount":
        if account_type not in PLAYER_ACCOUNT_TYPES:
            raise ValueError(f"{account_type!r} is a system account type, not player-scoped.")
        account, _ = self.get_or_create(account_type=account_type, currency=currency, user=user)
        return account


class LedgerAccount(BaseModel):
    """A named bucket in the chart of accounts. Rows are static anchors that `LedgerEntry`
    rows point at — an account has no mutable balance field of its own (see module
    docstring), so creating one needs no row lock, only the usual unique-constraint race
    handled by `get_or_create` (used exclusively via the manager's `system`/`player`
    helpers rather than direct `.objects.create()`, so every account type only ever ends
    up with the right `user` value — see the CheckConstraint below)."""

    account_type = models.CharField(max_length=32, choices=AccountType.choices)
    currency = models.CharField(max_length=10, choices=Currency.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ledger_accounts",
    )

    objects = LedgerAccountManager()

    class Meta:
        constraints = [
            # NULL != NULL under a plain UniqueConstraint, so system accounts (user IS
            # NULL) need their own partial index rather than sharing one with (user,
            # account_type, currency) — otherwise two SYSTEM_HOUSE_REVENUE/TON rows could
            # both be created with user_id NULL and neither would violate the constraint.
            models.UniqueConstraint(
                fields=["account_type", "currency"],
                condition=Q(user__isnull=True),
                name="wallet_ledgeraccount_unique_system",
            ),
            models.UniqueConstraint(
                fields=["user", "account_type", "currency"],
                condition=Q(user__isnull=False),
                name="wallet_ledgeraccount_unique_player",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(account_type__in=PLAYER_ACCOUNT_TYPES) & Q(user__isnull=False))
                    | (~Q(account_type__in=PLAYER_ACCOUNT_TYPES) & Q(user__isnull=True))
                ),
                name="wallet_ledgeraccount_user_matches_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account_type}:{self.currency}" + (f":{self.user_id}" if self.user_id else "")


class LedgerTransaction(BaseModel):
    """Groups the entries of one balanced double-entry movement. `idempotency_key` makes
    replaying the same client request safe: `WalletService` looks it up before doing any
    work and returns the original transaction untouched on a repeat. `currency` is
    denormalized from its entries' accounts (all entries in one transaction must share a
    currency — see `services._post_entries`) purely so transaction history can be filtered
    by currency without joining through entries."""

    transaction_type = models.CharField(max_length=32, choices=TransactionType.choices)
    status = models.CharField(
        max_length=16, choices=TransactionStatus.choices, default=TransactionStatus.PENDING, db_index=True
    )
    currency = models.CharField(max_length=10, choices=Currency.choices)
    idempotency_key = models.CharField(max_length=255, unique=True)
    metadata = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["transaction_type", "status"])]

    def __str__(self) -> str:
        return f"{self.transaction_type}:{self.status}:{self.id}"


class LedgerEntry(BaseModel):
    """One immutable debit or credit line. Rows are create-only: `save()` raises if called
    on a row that already exists in the database, since correcting a mistake means posting
    a new offsetting entry, never editing history."""

    transaction = models.ForeignKey(LedgerTransaction, on_delete=models.CASCADE, related_name="entries")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="entries")
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(**AMOUNT_KWARGS)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="wallet_ledgerentry_amount_gt_0")]
        indexes = [models.Index(fields=["account", "created_at"])]

    def __str__(self) -> str:
        return f"{self.entry_type} {self.amount} {self.account_id}"

    def save(self, *args, **kwargs):
        if self.adding is False:
            raise ValueError("LedgerEntry rows are immutable; post an offsetting entry instead of editing one.")
        super().save(*args, **kwargs)

    @property
    def adding(self) -> bool:
        return self._state.adding
