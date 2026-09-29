from django.urls import path

from . import views

app_name = "wallet"

urlpatterns = [
    path("wallet/balances/", views.WalletBalancesView.as_view(), name="balances"),
    path("wallet/transactions/", views.WalletTransactionsView.as_view(), name="transactions"),
]
