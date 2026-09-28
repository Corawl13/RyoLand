"""End-to-end API tests: real HTTP requests through URLs, throttles, serializers and services."""
import time
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from eth_account import Account
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .models import LinkedWallet, User, UserStatus
from .tests import (
    BOT_TOKEN,
    FakeTonWallet,
    evm_sign,
    make_mini_app_init_data,
    make_widget_data,
)


def ton_body(proof) -> dict:
    return {
        "account": {
            "address": proof.address,
            "chain": "-239",
            "publicKey": proof.public_key,
            "walletStateInit": proof.wallet_state_init,
        },
        "proof": {
            "timestamp": proof.timestamp,
            "domain": {"lengthBytes": proof.domain_length, "value": proof.domain},
            "payload": proof.payload,
            "signature": proof.signature,
        },
    }


@override_settings(TELEGRAM_BOT_TOKEN=BOT_TOKEN)
class ApiTestBase(APITestCase):
    def setUp(self):
        cache.clear()

    def bearer(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def evm_login(self, account=None):
        account = account or Account.create()
        challenge = self.client.post(
            "/api/v1/auth/wallet/challenge/", {"chain": "evm", "address": account.address}, format="json"
        ).json()
        return account, self.client.post(
            "/api/v1/auth/wallet/evm/",
            {
                "address": account.address,
                "nonce": challenge["nonce"],
                "signature": evm_sign(account, challenge["message"]),
            },
            format="json",
        )


class LoginFlowTests(ApiTestBase):
    def test_evm_full_flow(self):
        account, response = self.evm_login()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["user"]["linked_wallets"][0]["address"], account.address)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.bearer(body["access"])
        me = self.client.get("/api/v1/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["id"], body["user"]["id"])
        self.client.credentials()
        _, again = self.evm_login(account)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["user"]["id"], body["user"]["id"])

    def test_ton_full_flow(self):
        wallet = FakeTonWallet()
        challenge = self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json")
        self.assertEqual(challenge.status_code, 201)
        self.assertEqual(challenge.json()["message"], "")
        response = self.client.post(
            "/api/v1/auth/wallet/ton/", ton_body(wallet.proof(challenge.json()["nonce"])), format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user"]["linked_wallets"][0]["chain"], "ton")

    def test_telegram_mini_app_and_widget(self):
        response = self.client.post(
            "/api/v1/auth/telegram/mini-app/",
            {"init_data": make_mini_app_init_data({"id": 42, "first_name": "Ann"})},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user"]["telegram_id"], 42)
        widget = self.client.post(
            "/api/v1/auth/telegram/widget/",
            {"auth_data": make_widget_data({"id": 43, "first_name": "Bob"})},
            format="json",
        )
        self.assertEqual(widget.status_code, 201)

    def test_stale_authorization_header_does_not_break_login(self):
        self.bearer("garbage.token.value")
        response = self.client.post(
            "/api/v1/auth/telegram/mini-app/",
            {"init_data": make_mini_app_init_data({"id": 42})},
            format="json",
        )
        self.assertEqual(response.status_code, 201)


class ErrorEnvelopeTests(ApiTestBase):
    def test_bad_signature_is_400_with_code(self):
        account = Account.create()
        challenge = self.client.post(
            "/api/v1/auth/wallet/challenge/", {"chain": "evm", "address": account.address}, format="json"
        ).json()
        response = self.client.post(
            "/api/v1/auth/wallet/evm/",
            {
                "address": account.address,
                "nonce": challenge["nonce"],
                "signature": evm_sign(Account.create(), challenge["message"]),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_signature")
        self.assertFalse(User.objects.exists())

    def test_replayed_login_rejected(self):
        account = Account.create()
        challenge = self.client.post(
            "/api/v1/auth/wallet/challenge/", {"chain": "evm", "address": account.address}, format="json"
        ).json()
        payload = {
            "address": account.address,
            "nonce": challenge["nonce"],
            "signature": evm_sign(account, challenge["message"]),
        }
        self.assertEqual(self.client.post("/api/v1/auth/wallet/evm/", payload, format="json").status_code, 201)
        replay = self.client.post("/api/v1/auth/wallet/evm/", payload, format="json")
        self.assertEqual((replay.status_code, replay.json()["error"]["code"]), (400, "invalid_challenge"))

    def test_validation_errors_use_the_same_envelope(self):
        response = self.client.post(
            "/api/v1/auth/wallet/challenge/", {"chain": "evm", "address": "0x12"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "validation_error")
        self.assertIn("address", error["fields"])

    def test_unauthenticated_me_is_401(self):
        response = self.client.get("/api/v1/me/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.json())

    def test_suspended_user_gets_403_on_login(self):
        User.objects.create_user(telegram_id=42, status=UserStatus.SUSPENDED)
        response = self.client.post(
            "/api/v1/auth/telegram/mini-app/", {"init_data": make_mini_app_init_data({"id": 42})}, format="json"
        )
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "account_not_allowed"))

    def test_json_only(self):
        response = self.client.post("/api/v1/auth/wallet/challenge/", "chain=evm", content_type="text/plain")
        self.assertEqual(response.status_code, 415)


class TokenLifecycleTests(ApiTestBase):
    def test_refresh_rotates_and_old_token_is_dead(self):
        _, login = self.evm_login()
        old_refresh = login.json()["refresh"]
        refreshed = self.client.post("/api/v1/auth/token/refresh/", {"refresh": old_refresh}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.json())
        reuse = self.client.post("/api/v1/auth/token/refresh/", {"refresh": old_refresh}, format="json")
        self.assertEqual(reuse.status_code, 401)
        self.assertIn("error", reuse.json())

    def test_logout_blacklists_refresh_and_is_idempotent(self):
        _, login = self.evm_login()
        refresh = login.json()["refresh"]
        for _ in range(2):
            self.assertEqual(self.client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json").status_code, 204)
        self.assertEqual(
            self.client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json").status_code, 401
        )
        self.assertEqual(self.client.post("/api/v1/auth/logout/", {"refresh": "nonsense"}, format="json").status_code, 204)

    def test_suspension_takes_effect_immediately(self):
        _, login = self.evm_login()
        user = User.objects.get(pk=login.json()["user"]["id"])
        self.bearer(login.json()["access"])
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 200)
        user.status = UserStatus.SUSPENDED
        user.save()
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 401)
        self.client.credentials()
        refresh = self.client.post("/api/v1/auth/token/refresh/", {"refresh": login.json()["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 401)

    def test_self_excluded_can_still_read_profile(self):
        user = User.objects.create_user(status=UserStatus.SELF_EXCLUDED)
        self.bearer(str(RefreshToken.for_user(user).access_token))
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 200)


class MeEndpointTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        _, login = self.evm_login()
        self.user = User.objects.get(pk=login.json()["user"]["id"])
        self.bearer(login.json()["access"])

    def test_patch_profile_only_touches_allowed_fields(self):
        response = self.client.patch(
            "/api/v1/me/",
            {"display_name": "  Ace  ", "language_code": "en", "role": "admin", "status": "closed", "is_staff": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.display_name, self.user.language_code), ("Ace", "en"))
        self.assertEqual((self.user.role, self.user.status, self.user.is_staff), ("player", "active", False))

    def test_display_name_validation(self):
        response = self.client.patch("/api/v1/me/", {"display_name": "x"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_link_second_evm_wallet(self):
        second = Account.create()
        challenge = self.client.post(
            "/api/v1/me/wallets/challenge/", {"chain": "evm", "address": second.address}, format="json"
        )
        self.assertEqual(challenge.status_code, 201)
        response = self.client.post(
            "/api/v1/me/wallets/evm/",
            {
                "address": second.address,
                "nonce": challenge.json()["nonce"],
                "signature": evm_sign(second, challenge.json()["message"]),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.user.linked_wallets.count(), 2)

    def test_link_ton_wallet_and_conflict(self):
        wallet = FakeTonWallet()
        challenge = self.client.post("/api/v1/me/wallets/challenge/", {"chain": "ton"}, format="json").json()
        ok = self.client.post("/api/v1/me/wallets/ton/", ton_body(wallet.proof(challenge["nonce"])), format="json")
        self.assertEqual(ok.status_code, 201)
        other = User.objects.create_user()
        self.bearer(str(RefreshToken.for_user(other).access_token))
        challenge2 = self.client.post("/api/v1/me/wallets/challenge/", {"chain": "ton"}, format="json").json()
        conflict = self.client.post("/api/v1/me/wallets/ton/", ton_body(wallet.proof(challenge2["nonce"])), format="json")
        self.assertEqual((conflict.status_code, conflict.json()["error"]["code"]), (409, "identity_already_linked"))

    def test_link_telegram(self):
        response = self.client.post(
            "/api/v1/me/telegram/mini-app/", {"init_data": make_mini_app_init_data({"id": 777})}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["telegram_id"], 777)

    def test_unlink_last_method_is_409_then_ok_with_another(self):
        wallet = self.user.linked_wallets.get()
        blocked = self.client.delete(f"/api/v1/me/wallets/{wallet.pk}/")
        self.assertEqual((blocked.status_code, blocked.json()["error"]["code"]), (409, "last_auth_method"))
        self.client.post("/api/v1/me/telegram/mini-app/", {"init_data": make_mini_app_init_data({"id": 777})}, format="json")
        self.assertEqual(self.client.delete(f"/api/v1/me/wallets/{wallet.pk}/").status_code, 204)
        self.assertFalse(LinkedWallet.objects.filter(pk=wallet.pk).exists())
        self.assertEqual(self.client.delete(f"/api/v1/me/wallets/{wallet.pk}/").status_code, 204)

    def test_cannot_unlink_someone_elses_wallet(self):
        victim_wallet = LinkedWallet.objects.create(
            user=User.objects.create_user(telegram_id=5), chain="evm", address=Account.create().address
        )
        self.assertEqual(self.client.delete(f"/api/v1/me/wallets/{victim_wallet.pk}/").status_code, 204)
        self.assertTrue(LinkedWallet.objects.filter(pk=victim_wallet.pk).exists())

    def test_link_challenge_cannot_be_used_by_another_user(self):
        second = Account.create()
        challenge = self.client.post(
            "/api/v1/me/wallets/challenge/", {"chain": "evm", "address": second.address}, format="json"
        ).json()
        other = User.objects.create_user()
        self.bearer(str(RefreshToken.for_user(other).access_token))
        response = self.client.post(
            "/api/v1/me/wallets/evm/",
            {"address": second.address, "nonce": challenge["nonce"], "signature": evm_sign(second, challenge["message"])},
            format="json",
        )
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "invalid_challenge"))

    def test_link_endpoints_require_authentication(self):
        self.client.credentials()
        for path in ("/api/v1/me/wallets/challenge/", "/api/v1/me/wallets/evm/", "/api/v1/me/telegram/mini-app/"):
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 401, path)


class ThrottleTests(ApiTestBase):
    def test_challenge_endpoint_is_rate_limited_per_ip(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"auth_challenge": "3/min"}):
            statuses = [
                self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json").status_code
                for _ in range(5)
            ]
        self.assertEqual(statuses, [201, 201, 201, 429, 429])

    def test_throttled_response_uses_error_envelope_and_retry_after(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"auth_login": "1/min"}):
            body = {"init_data": "x=1"}
            self.client.post("/api/v1/auth/telegram/mini-app/", body, format="json")
            response = self.client.post("/api/v1/auth/telegram/mini-app/", body, format="json")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "throttled")
        self.assertIn("Retry-After", response)

    def test_limits_are_per_client_ip(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"auth_challenge": "1/min"}):
            first = self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json", REMOTE_ADDR="10.0.0.1")
            blocked = self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json", REMOTE_ADDR="10.0.0.1")
            other = self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json", REMOTE_ADDR="10.0.0.2")
        self.assertEqual((first.status_code, blocked.status_code, other.status_code), (201, 429, 201))

    def test_forwarded_header_ignored_without_trusted_proxy(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"auth_challenge": "1/min"}):
            for spoofed in ("1.1.1.1", "2.2.2.2"):
                last = self.client.post(
                    "/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json", HTTP_X_FORWARDED_FOR=spoofed
                )
        self.assertEqual(last.status_code, 429)

    def test_challenge_records_client_ip(self):
        from .models import AuthChallenge

        self.client.post("/api/v1/auth/wallet/challenge/", {"chain": "ton"}, format="json", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(AuthChallenge.objects.get().ip_address, "203.0.113.9")

    def test_time_is_not_frozen(self):
        self.assertGreater(time.time(), 0)