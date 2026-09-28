from django.test import TestCase
from django.urls import reverse
from eth_account import Account

from .models import LinkedWallet, User, UserRole, UserStatus


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("root", "pw-12345-abc")
        cls.support = User.objects.create_user("agent", "pw-12345-abc", role=UserRole.SUPPORT, is_staff=True)
        cls.player = User.objects.create_user(telegram_id=42, display_name="Ann")
        LinkedWallet.objects.create(user=cls.player, chain="evm", address=Account.create().address)

    def login(self, user):
        self.client.force_login(user)

    def test_changelist_search_and_filters(self):
        self.login(self.superuser)
        url = reverse("admin:accounts_user_changelist")
        self.assertEqual(self.client.get(url).status_code, 200)
        wallet = self.player.linked_wallets.get()
        found = self.client.get(url, {"q": wallet.address})
        self.assertContains(found, self.player.username)
        self.assertEqual(self.client.get(url, {"q": "42"}).status_code, 200)
        self.assertEqual(self.client.get(url, {"role": "player", "status": "active"}).status_code, 200)

    def test_change_page_renders_for_password_less_player(self):
        self.login(self.superuser)
        response = self.client.get(reverse("admin:accounts_user_change", args=[self.player.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, wallet_address := self.player.linked_wallets.get().address)
        self.assertTrue(wallet_address)

    def test_add_form_creates_staff_only(self):
        self.login(self.superuser)
        url = reverse("admin:accounts_user_add")
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            url,
            {"username": "Finance1", "role": "finance", "usable_password": "true", "password1": "S3cure-pass-987", "password2": "S3cure-pass-987"},
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["adminform"].form.errors)
        created = User.objects.get(username="finance1")
        self.assertTrue(created.is_staff)
        self.assertEqual(created.role, UserRole.FINANCE)
        bad = self.client.post(url, {"username": "x1", "role": "player", "usable_password": "true", "password1": "S3cure-pass-987", "password2": "S3cure-pass-987"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(User.objects.filter(username="x1").exists())

    def test_non_superuser_cannot_escalate(self):
        self.login(self.support)
        url = reverse("admin:accounts_user_change", args=[self.support.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        from django.contrib.auth.models import Permission

        self.support.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounts", content_type__model="user"))
        self.support = User.objects.get(pk=self.support.pk)
        self.login(self.support)
        page = self.client.get(reverse("admin:accounts_user_change", args=[self.player.pk]))
        self.assertEqual(page.status_code, 200)
        readonly = set(page.context["adminform"].readonly_fields)
        self.assertTrue({"role", "is_staff", "is_superuser", "user_permissions", "groups"} <= readonly)

    def test_nobody_can_delete_users(self):
        self.login(self.superuser)
        response = self.client.post(reverse("admin:accounts_user_delete", args=[self.player.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.player.pk).exists())

    def action(self, name, users):
        return self.client.post(
            reverse("admin:accounts_user_changelist"),
            {"action": name, "_selected_action": [str(user.pk) for user in users]},
            follow=True,
        )

    def test_bulk_suspend_and_reactivate_with_audit_log(self):
        from django.contrib.admin.models import LogEntry

        self.login(self.superuser)
        self.action("suspend_players", [self.player, self.support, self.superuser])
        self.player.refresh_from_db()
        self.support.refresh_from_db()
        self.superuser.refresh_from_db()
        self.assertEqual(self.player.status, UserStatus.SUSPENDED)
        self.assertEqual(self.support.status, UserStatus.ACTIVE)
        self.assertEqual(self.superuser.status, UserStatus.ACTIVE)
        entry = LogEntry.objects.get(object_id=str(self.player.pk))
        self.assertEqual(entry.user, self.superuser)
        self.assertIn("suspended", entry.change_message)
        self.action("reactivate_players", [self.player])
        self.player.refresh_from_db()
        self.assertEqual(self.player.status, UserStatus.ACTIVE)

    def test_self_exclusion_cannot_be_lifted_from_admin(self):
        excluded = User.objects.create_user(status=UserStatus.SELF_EXCLUDED)
        self.login(self.superuser)
        self.action("reactivate_players", [excluded])
        excluded.refresh_from_db()
        self.assertEqual(excluded.status, UserStatus.SELF_EXCLUDED)
        page = self.client.get(reverse("admin:accounts_user_change", args=[excluded.pk]))
        self.assertIn("status", page.context["adminform"].readonly_fields)

    def test_read_only_models(self):
        self.login(self.superuser)
        for name in ("linkedwallet", "authchallenge"):
            self.assertEqual(self.client.get(reverse(f"admin:accounts_{name}_changelist")).status_code, 200)
            self.assertEqual(self.client.get(reverse(f"admin:accounts_{name}_add")).status_code, 403)