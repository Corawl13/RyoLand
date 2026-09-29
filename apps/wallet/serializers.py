"""Read-only DRF serializers for the wallet API (see views.py — Step 2 exposes no writes
over HTTP; `WalletService` is the write API, called directly by other Python code)."""
from rest_framework import serializers

from .models import Currency, LedgerTransaction, TransactionStatus, TransactionType


class WalletBalanceSerializer(serializers.Serializer):
    currency = serializers.ChoiceField(choices=Currency.choices)
    available_balance = serializers.DecimalField(max_digits=28, decimal_places=8)
    locked_balance = serializers.DecimalField(max_digits=28, decimal_places=8)
    total_balance = serializers.DecimalField(max_digits=28, decimal_places=8)


class MyLedgerEntrySerializer(serializers.Serializer):
    """One entry of a transaction, scoped to the requesting user's own accounts — a
    transaction may also touch system accounts (house revenue, clearing, ...), which are
    deliberately left out of this view."""

    account_type = serializers.CharField(source="account.account_type")
    entry_type = serializers.CharField()
    amount = serializers.DecimalField(max_digits=28, decimal_places=8)


class LedgerTransactionSerializer(serializers.ModelSerializer):
    my_entries = serializers.SerializerMethodField()

    class Meta:
        model = LedgerTransaction
        fields = (
            "id",
            "transaction_type",
            "status",
            "currency",
            "created_at",
            "completed_at",
            "my_entries",
        )
        read_only_fields = fields

    def get_my_entries(self, obj: LedgerTransaction):
        user = self.context["request"].user
        entries = [e for e in obj.entries.all() if e.account.user_id == user.id]
        return MyLedgerEntrySerializer(entries, many=True).data


class TransactionsFilterSerializer(serializers.Serializer):
    """Validates the `?currency=&status=&transaction_type=` query params."""

    currency = serializers.ChoiceField(choices=Currency.choices, required=False)
    status = serializers.ChoiceField(choices=TransactionStatus.choices, required=False)
    transaction_type = serializers.ChoiceField(choices=TransactionType.choices, required=False)
