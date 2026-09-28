from django.urls import path

from apps.accounts.views import (
    AuthChallengeView,
    LoginEVMView,
    LoginTelegramMiniAppView,
    LoginTelegramWidgetView,
    LoginTONView,
    ProfileView,
    WalletLinkView,
)

urlpatterns = [
    path("auth/challenge/", AuthChallengeView.as_view(), name="auth-challenge"),
    path("auth/login/telegram-mini-app/", LoginTelegramMiniAppView.as_view(), name="auth-login-telegram-mini-app"),
    path("auth/login/telegram-widget/", LoginTelegramWidgetView.as_view(), name="auth-login-telegram-widget"),
    path("auth/login/evm/", LoginEVMView.as_view(), name="auth-login-evm"),
    path("auth/login/ton/", LoginTONView.as_view(), name="auth-login-ton"),
    path("auth/wallet/link/", WalletLinkView.as_view(), name="auth-wallet-link"),
    path("me/", ProfileView.as_view(), name="profile"),
]
