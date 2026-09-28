from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("auth/wallet/challenge/", views.WalletChallengeView.as_view(), name="wallet-challenge"),
    path("auth/wallet/evm/", views.EVMLoginView.as_view(), name="evm-login"),
    path("auth/wallet/ton/", views.TonLoginView.as_view(), name="ton-login"),
    path("auth/telegram/mini-app/", views.TelegramMiniAppLoginView.as_view(), name="telegram-mini-app-login"),
    path("auth/telegram/widget/", views.TelegramWidgetLoginView.as_view(), name="telegram-widget-login"),
    path("auth/token/refresh/", views.TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/wallets/challenge/", views.WalletLinkChallengeView.as_view(), name="wallet-link-challenge"),
    path("me/wallets/evm/", views.EVMLinkView.as_view(), name="evm-link"),
    path("me/wallets/ton/", views.TonLinkView.as_view(), name="ton-link"),
    path("me/wallets/<uuid:wallet_id>/", views.WalletUnlinkView.as_view(), name="wallet-unlink"),
    path("me/telegram/mini-app/", views.TelegramMiniAppLinkView.as_view(), name="telegram-mini-app-link"),
    path("me/telegram/widget/", views.TelegramWidgetLinkView.as_view(), name="telegram-widget-link"),
]
