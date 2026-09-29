"""Wallet HTTP API. Read-only: balances and transaction history. Crediting, debiting,
reserving and settling funds happen through `WalletService`, called directly by the app
that drives them (a future betting engine, `telegram_services`, `payments`) — not exposed
here."""
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LedgerTransaction
from .serializers import (
    LedgerTransactionSerializer,
    TransactionsFilterSerializer,
    WalletBalanceSerializer,
)
from .services import WalletService


class WalletBalancesView(APIView):
    """GET: every currency's available/locked/total balance for the signed-in user."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallets = WalletService.list_balances(request.user)
        data = [
            {
                "currency": w.currency,
                "available_balance": w.available_balance,
                "locked_balance": w.locked_balance,
                "total_balance": w.total_balance,
            }
            for w in wallets
        ]
        return Response(WalletBalanceSerializer(data, many=True).data)


class TransactionsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class WalletTransactionsView(ListAPIView):
    """GET: the signed-in user's transaction history, newest first. Filter with
    `?currency=`, `?status=`, `?transaction_type=`."""

    permission_classes = [IsAuthenticated]
    serializer_class = LedgerTransactionSerializer
    pagination_class = TransactionsPagination

    def get_queryset(self):
        filters = TransactionsFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = (
            LedgerTransaction.objects.filter(entries__account__user=self.request.user)
            .distinct()
            .prefetch_related("entries__account")
            .order_by("-created_at")
        )
        for field in ("currency", "status", "transaction_type"):
            value = filters.validated_data.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset
