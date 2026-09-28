"""Django admin for accounts.

Identities (linked wallets, auth challenges) are security-relevant records, not things an
operator hand-edits, so they render read-only. Player status changes go through the
`suspend_players` / `reactivate_players` bulk actions (audited via Django's LogEntry)
rather than free-form field edits, and non-superusers can view but never change access
fields (role, staff/superuser flags, permissions) on the User change form.
"""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm

from .models import AuthChallenge, LinkedWallet, User, UserRole, UserStatus

ACCESS_FIELDS = ("role", "is_staff", "is_superuser", "groups", "user_permissions")


class LinkedWalletInline(admin.TabularInline):
    model = LinkedWallet
    extra = 0
    fields = ("chain", "address", "public_key", "created_at", "last_used_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class StaffUserCreationForm(AdminUserCreationForm):
    """Admin-created accounts are staff, never players."""

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "role")

    def clean_role(self):
        role = self.cleaned_data["role"]
        if role == UserRole.PLAYER:
            raise forms.ValidationError("Admin-created accounts need a staff role, not 'player'.")
        return role

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = True
        if commit:
            user.save()
        return user


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    inlines = [LinkedWalletInline]
    add_form = StaffUserCreationForm
    list_display = (
        "username",
        "display_name",
        "role",
        "status",
        "telegram_id",
        "is_staff",
        "created_at",
    )
    list_filter = ("role", "status", "is_staff")
    search_fields = (
        "username",
        "display_name",
        "email",
        "telegram_id",
        "telegram_username",
        "linked_wallets__address",
    )
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at", "last_login")
    filter_horizontal = ("groups", "user_permissions")
    actions = ("suspend_players", "reactivate_players")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": ("display_name", "email", "avatar_url", "language_code")}),
        ("Telegram", {"fields": ("telegram_id", "telegram_username")}),
        ("Access", {"fields": ACCESS_FIELDS}),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "role", "usable_password", "password1", "password2")}),
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_inline_instances(self, request, obj=None):
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if obj is not None:
            if not request.user.is_superuser:
                fields += list(ACCESS_FIELDS)
            if obj.status == UserStatus.SELF_EXCLUDED:
                fields.append("status")
        return fields

    @admin.action(description="Suspend selected players")
    def suspend_players(self, request, queryset):
        candidates = queryset.filter(role=UserRole.PLAYER).exclude(pk=request.user.pk)
        updated = 0
        for user in candidates:
            user.status = UserStatus.SUSPENDED
            user.save(update_fields=["status", "updated_at"])
            self.log_change(request, user, "Player suspended via admin bulk action.")
            updated += 1
        self.message_user(request, f"Suspended {updated} player(s).")

    @admin.action(description="Reactivate selected suspended players")
    def reactivate_players(self, request, queryset):
        candidates = queryset.filter(role=UserRole.PLAYER, status=UserStatus.SUSPENDED)
        updated = 0
        for user in candidates:
            user.status = UserStatus.ACTIVE
            user.save(update_fields=["status", "updated_at"])
            self.log_change(request, user, "Player reactivated via admin bulk action.")
            updated += 1
        self.message_user(request, f"Reactivated {updated} player(s).")


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Security artifacts: viewable and searchable, never hand-created or edited here."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(LinkedWallet)
class LinkedWalletAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "chain", "address", "created_at", "last_used_at")
    list_filter = ("chain",)
    search_fields = ("address", "user__username")
    readonly_fields = [f.name for f in LinkedWallet._meta.fields]


@admin.register(AuthChallenge)
class AuthChallengeAdmin(ReadOnlyModelAdmin):
    list_display = ("nonce_preview", "chain", "purpose", "address", "user", "expires_at", "consumed_at")
    list_filter = ("chain", "purpose")
    search_fields = ("nonce", "address", "user__username")
    readonly_fields = [f.name for f in AuthChallenge._meta.fields]

    @admin.display(description="Nonce")
    def nonce_preview(self, obj: AuthChallenge) -> str:
        return f"{obj.nonce[:10]}…"
