from django.contrib import admin

from apps.accounts.models import AuthChallenge, LinkedWallet, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "role", "status", "telegram_username", "email")
    list_filter = ("role", "status")
    search_fields = ("username", "email", "telegram_username", "display_name")


@admin.register(LinkedWallet)
class LinkedWalletAdmin(admin.ModelAdmin):
    list_display = ("user", "chain", "address", "last_used_at")
    search_fields = ("address", "user__username")


@admin.register(AuthChallenge)
class AuthChallengeAdmin(admin.ModelAdmin):
    list_display = ("nonce", "chain", "purpose", "expires_at", "consumed_at")
    list_filter = ("chain", "purpose", "consumed_at")
    search_fields = ("nonce", "address")
