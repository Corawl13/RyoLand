"""Tests for the accounts app.

Run with: python manage.py test apps.accounts
"""
import base64
import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from eth_account import Account
from eth_account.messages import encode_defunct
from nacl.signing import SigningKey
from pytoniq_core import begin_cell
from pytoniq_core.tlb.account import StateInit

from apps.accounts import services, verifiers
from apps.accounts.exceptions import (
    AccountNotAllowed,
    IdentityAlreadyLinked,
    InvalidChallenge,
    InvalidSignature,
    InvalidTelegramData,
    InvalidTonProof,
    LastAuthMethod,
)
from apps.accounts.models import AuthChallenge, Chain, LinkedWallet, User, UserRole, UserStatus
from apps.accounts.serializers import (
    AuthResponseSerializer,
    TonWalletProofSerializer,
    WalletChallengeRequestSerializer,
)

BOT_TOKEN = "123456:TEST-TOKEN"
DOMAIN = "app.example.com"


def make_mini_app_init_data(user: dict, *, auth_date=None, token=BOT_TOKEN, extra=None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH-test",
        "user": json.dumps(user, separators=(",", ":")),
        **(extra or {}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def make_widget_data(user: dict, *, auth_date=None, token=BOT_TOKEN) -> dict:
    fields = {"auth_date": str(auth_date or int(time.time())), **{k: str(v) for k, v in user.items()}}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(token.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


class FakeTonWallet:
    """A wallet with a v4-style stateInit (seqno, subwallet_id, public key)."""

    def __init__(self, workchain=0):
        self.key = SigningKey.generate()
        self.public_key = bytes(self.key.verify_key)
        self.workchain = workchain
        data = (
            begin_cell()
            .store_uint(0, 32)
            .store_uint(698983191, 32)
            .store_bytes(self.public_key)
            .store_bit(0)
            .end_cell()
        )
        code = begin_cell().store_uint(0xC0DE, 16).end_cell()
        state_init = (
            begin_cell()
            .store_bit(0)
            .store_bit(0)
            .store_maybe_ref(code)
            .store_maybe_ref(data)
            .store_bit(0)
            .end_cell()
        )
        self.state_init_b64 = base64.b64encode(state_init.to_boc()).decode()
        self.address_hash = state_init.hash
        self.raw_address = f"{workchain}:{self.address_hash.hex()}"

    def proof(self, payload: str, *, domain=DOMAIN, timestamp=None, signing_key=None) -> verifiers.TonProof:
        timestamp = timestamp or int(time.time())
        domain_bytes = domain.encode()
        message = (
            b"ton-proof-item-v2/"
            + self.workchain.to_bytes(4, "big", signed=True)
            + self.address_hash
            + len(domain_bytes).to_bytes(4, "little")
            + domain_bytes
            + timestamp.to_bytes(8, "little")
            + payload.encode()
        )
        digest = hashlib.sha256(b"\xff\xffton-connect" + hashlib.sha256(message).digest()).digest()
        signature = (signing_key or self.key).sign(digest).signature
        return verifiers.TonProof(
            address=self.raw_address,
            public_key=self.public_key.hex(),
            wallet_state_init=self.state_init_b64,
            timestamp=timestamp,
            domain=domain,
            domain_length=len(domain_bytes),
            payload=payload,
            signature=base64.b64encode(signature).decode(),
        )


def evm_sign(account, message: str) -> str:
    return account.sign_message(encode_defunct(text=message)).signature.hex()


class UserModelTests(TestCase):
    def test_create_user_defaults(self):
        user = User.objects.create_user()
        self.assertTrue(user.username.startswith("player_"))
        self.assertFalse(user.has_usable_password())
        self.assertIsNone(user.email)
        self.assertEqual(user.role, UserRole.PLAYER)
        self.assertTrue(user.is_active and user.can_wager)
        self.assertEqual(len(str(user.id)), 36)

    def test_two_users_without_email_do_not_collide(self):
        User.objects.create_user()
        User.objects.create_user()

    def test_status_drives_is_active(self):
        user = User.objects.create_user()
        user.status = UserStatus.SELF_EXCLUDED
        self.assertTrue(user.is_active)
        self.assertFalse(user.can_wager)
        for status in (UserStatus.SUSPENDED, UserStatus.CLOSED):
            user.status = status
            self.assertFalse(user.is_active)

    def test_superuser(self):
        admin = User.objects.create_superuser("Admin", "pw-12345")
        self.assertEqual(admin.username, "admin")
        self.assertEqual(admin.role, UserRole.ADMIN)
        self.assertTrue(admin.is_staff and admin.is_superuser)
        self.assertEqual(User.objects.get_by_natural_key("ADMIN"), admin)

    def test_player_cannot_be_staff(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(is_staff=True)


class TelegramVerifierTests(TestCase):
    def verify(self, init_data, **kw):
        return verifiers.verify_telegram_mini_app(init_data, bot_token=BOT_TOKEN, max_age=3600, **kw)

    def test_valid(self):
        profile = self.verify(make_mini_app_init_data({"id": 42, "first_name": "Ann", "username": "ann"}))
        self.assertEqual((profile.id, profile.username), (42, "ann"))

    def test_tampered_user_rejected(self):
        data = make_mini_app_init_data({"id": 42})
        with self.assertRaises(InvalidTelegramData):
            self.verify(data.replace("%22id%22%3A42", "%22id%22%3A43"))

    def test_wrong_bot_token_rejected(self):
        with self.assertRaises(InvalidTelegramData):
            self.verify(make_mini_app_init_data({"id": 42}, token="999:OTHER"))

    def test_expired_rejected(self):
        with self.assertRaises(InvalidTelegramData):
            self.verify(make_mini_app_init_data({"id": 42}, auth_date=int(time.time()) - 7200))

    def test_future_auth_date_rejected(self):
        with self.assertRaises(InvalidTelegramData):
            self.verify(make_mini_app_init_data({"id": 42}, auth_date=int(time.time()) + 3600))

    def test_extra_signed_field_is_included_in_hash(self):
        self.verify(make_mini_app_init_data({"id": 42}, extra={"signature": "abc", "chat_type": "sender"}))

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(InvalidTelegramData):
            self.verify(make_mini_app_init_data({"id": 42}) + "&auth_date=1")

    def test_widget(self):
        data = make_widget_data({"id": 7, "first_name": "Bob", "photo_url": "https://t.me/i/u.jpg"})
        profile = verifiers.verify_telegram_widget(data, bot_token=BOT_TOKEN, max_age=3600)
        self.assertEqual(profile.id, 7)
        data["first_name"] = "Eve"
        with self.assertRaises(InvalidTelegramData):
            verifiers.verify_telegram_widget(data, bot_token=BOT_TOKEN, max_age=3600)


@override_settings(TELEGRAM_BOT_TOKEN=BOT_TOKEN)
class TelegramLoginServiceTests(TestCase):
    def test_register_then_login_same_user(self):
        init = make_mini_app_init_data({"id": 42, "first_name": "Ann", "last_name": "Lee", "username": "ann"})
        first = services.login_with_telegram_mini_app(init_data=init)
        self.assertTrue(first.created)
        self.assertEqual(first.user.telegram_id, 42)
        self.assertEqual(first.user.display_name, "Ann Lee")
        self.assertTrue(first.access and first.refresh)
        init2 = make_mini_app_init_data({"id": 42, "username": "ann_new", "language_code": "en"})
        second = services.login_with_telegram_mini_app(init_data=init2)
        self.assertFalse(second.created)
        self.assertEqual(second.user.pk, first.user.pk)
        second.user.refresh_from_db()
        self.assertEqual((second.user.telegram_username, second.user.language_code), ("ann_new", "en"))
        self.assertEqual(User.objects.count(), 1)

    def test_suspended_cannot_login(self):
        user = User.objects.create_user(telegram_id=42, status=UserStatus.SUSPENDED)
        with self.assertRaises(AccountNotAllowed):
            services.login_with_telegram_mini_app(init_data=make_mini_app_init_data({"id": user.telegram_id}))

    def test_widget_login(self):
        result = services.login_with_telegram_widget(auth_data=make_widget_data({"id": 9, "first_name": "Zed"}))
        self.assertTrue(result.created)

    def test_link_telegram_to_wallet_user(self):
        user = User.objects.create_user()
        services.link_telegram_mini_app(user=user, init_data=make_mini_app_init_data({"id": 55}))
        user.refresh_from_db()
        self.assertEqual(user.telegram_id, 55)
        other = User.objects.create_user()
        with self.assertRaises(IdentityAlreadyLinked):
            services.link_telegram_mini_app(user=other, init_data=make_mini_app_init_data({"id": 55}))
        with self.assertRaises(IdentityAlreadyLinked):
            services.link_telegram_mini_app(user=user, init_data=make_mini_app_init_data({"id": 56}))


class EvmServiceTests(TestCase):
    def setUp(self):
        self.account = Account.create()

    def challenge(self, **kw):
        return services.create_wallet_challenge(chain=Chain.EVM, address=self.account.address.lower(), **kw)

    def test_register_and_login(self):
        ch = self.challenge()
        self.assertIn(self.account.address, ch.message)
        self.assertIn(f"Nonce: {ch.nonce}", ch.message)
        result = services.login_with_evm(
            address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, ch.message)
        )
        self.assertTrue(result.created)
        self.assertEqual(result.user.linked_wallets.get().address, self.account.address)
        ch2 = self.challenge()
        again = services.login_with_evm(
            address=self.account.address.lower(), nonce=ch2.nonce, signature=evm_sign(self.account, ch2.message)
        )
        self.assertFalse(again.created)
        self.assertEqual(again.user.pk, result.user.pk)

    def test_replay_rejected(self):
        ch = self.challenge()
        sig = evm_sign(self.account, ch.message)
        services.login_with_evm(address=self.account.address, nonce=ch.nonce, signature=sig)
        with self.assertRaises(InvalidChallenge):
            services.login_with_evm(address=self.account.address, nonce=ch.nonce, signature=sig)

    def test_wrong_signer_rejected_and_nonce_survives(self):
        ch = self.challenge()
        with self.assertRaises(InvalidSignature):
            services.login_with_evm(
                address=self.account.address, nonce=ch.nonce, signature=evm_sign(Account.create(), ch.message)
            )
        ch.refresh_from_db()
        self.assertIsNone(ch.consumed_at)

    def test_signing_a_different_message_rejected(self):
        ch = self.challenge()
        with self.assertRaises(InvalidSignature):
            services.login_with_evm(
                address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, "hello")
            )

    def test_address_mismatch_rejected(self):
        ch = self.challenge()
        other = Account.create()
        with self.assertRaises(InvalidChallenge):
            services.login_with_evm(address=other.address, nonce=ch.nonce, signature=evm_sign(other, ch.message))

    def test_expired_rejected(self):
        ch = self.challenge()
        AuthChallenge.objects.filter(pk=ch.pk).update(expires_at=ch.created_at)
        with self.assertRaises(InvalidChallenge):
            services.login_with_evm(
                address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, ch.message)
            )

    def test_login_nonce_cannot_be_used_for_linking(self):
        user = User.objects.create_user()
        ch = self.challenge()
        with self.assertRaises(InvalidChallenge):
            services.link_wallet_to_user(
                user=user, chain=Chain.EVM, address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, ch.message)
            )

    def test_link_and_conflicts(self):
        user, other = User.objects.create_user(), User.objects.create_user()
        ch = self.challenge(purpose=AuthChallenge.Purpose.LINK_WALLET, user=user)
        wallet = services.link_wallet_to_user(
            user=user, chain=Chain.EVM, address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, ch.message)
        )
        self.assertEqual(wallet, user)
        ch2 = self.challenge(purpose=AuthChallenge.Purpose.LINK_WALLET, user=user)
        with self.assertRaises(InvalidChallenge):
            services.link_wallet_to_user(
                user=other, chain=Chain.EVM, address=self.account.address, nonce=ch2.nonce, signature=evm_sign(self.account, ch2.message)
            )
        ch3 = self.challenge(purpose=AuthChallenge.Purpose.LINK_WALLET, user=other)
        with self.assertRaises(IdentityAlreadyLinked):
            services.link_wallet_to_user(
                user=other, chain=Chain.EVM, address=self.account.address, nonce=ch3.nonce, signature=evm_sign(self.account, ch3.message)
            )

    def test_unlink_protects_last_auth_method(self):
        ch = self.challenge()
        result = services.login_with_evm(
            address=self.account.address, nonce=ch.nonce, signature=evm_sign(self.account, ch.message)
        )
        wallet = result.user.linked_wallets.get()
        with self.assertRaises(LastAuthMethod):
            services.unlink_wallet(user=result.user, wallet_id=wallet.pk)
        result.user.telegram_id = 1
        result.user.save()
        services.unlink_wallet(user=result.user, wallet_id=wallet.pk)
        self.assertFalse(LinkedWallet.objects.exists())

    def test_challenge_request_serializer(self):
        s = WalletChallengeRequestSerializer(data={"chain": "evm", "address": self.account.address.lower()})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["address"], self.account.address)
        self.assertFalse(WalletChallengeRequestSerializer(data={"chain": "evm"}).is_valid())
        self.assertFalse(WalletChallengeRequestSerializer(data={"chain": "evm", "address": "0x123"}).is_valid())
        self.assertTrue(WalletChallengeRequestSerializer(data={"chain": "ton"}).is_valid())


class TonServiceTests(TestCase):
    def setUp(self):
        self.wallet = FakeTonWallet()

    def challenge(self, **kw):
        return services.create_wallet_challenge(chain=Chain.TON, **kw)

    def test_register_and_login(self):
        ch = self.challenge()
        result = services.login_with_ton(proof=self.wallet.proof(ch.nonce))
        self.assertTrue(result.created)
        linked = result.user.linked_wallets.get()
        self.assertEqual((linked.chain, linked.address), (Chain.TON, self.wallet.raw_address))
        ch2 = self.challenge()
        again = services.login_with_ton(proof=self.wallet.proof(ch2.nonce))
        self.assertEqual(again.user.pk, result.user.pk)

    def test_replay_rejected(self):
        ch = self.challenge()
        proof = self.wallet.proof(ch.nonce)
        services.login_with_ton(proof=proof)
        with self.assertRaises(InvalidChallenge):
            services.login_with_ton(proof=proof)

    def test_unknown_payload_rejected(self):
        with self.assertRaises(InvalidChallenge):
            services.login_with_ton(proof=self.wallet.proof("not-a-real-nonce"))

    def test_attacker_key_with_victim_address_rejected(self):
        attacker = SigningKey.generate()
        ch = self.challenge()
        forged = self.wallet.proof(ch.nonce, signing_key=attacker)
        forged = verifiers.TonProof(**{**forged.__dict__, "public_key": bytes(attacker.verify_key).hex()})
        with self.assertRaises(InvalidTonProof):
            services.login_with_ton(proof=forged)
        self.assertFalse(User.objects.exists())

    def test_wrong_signature_rejected(self):
        ch = self.challenge()
        with self.assertRaises(InvalidTonProof):
            services.login_with_ton(proof=self.wallet.proof(ch.nonce, signing_key=SigningKey.generate()))

    def test_state_init_for_other_address_rejected(self):
        ch = self.challenge()
        other = FakeTonWallet()
        proof = self.wallet.proof(ch.nonce)
        proof = verifiers.TonProof(**{**proof.__dict__, "wallet_state_init": other.state_init_b64})
        with self.assertRaises(InvalidTonProof):
            services.login_with_ton(proof=proof)

    def test_wrong_domain_rejected(self):
        ch = self.challenge()
        with self.assertRaises(InvalidTonProof):
            services.login_with_ton(proof=self.wallet.proof(ch.nonce, domain="evil.example"))

    def test_old_timestamp_rejected(self):
        ch = self.challenge()
        with self.assertRaises(InvalidTonProof):
            services.login_with_ton(proof=self.wallet.proof(ch.nonce, timestamp=int(time.time()) - 3600))

    def test_masterchain_address(self):
        wallet = FakeTonWallet(workchain=-1)
        ch = self.challenge()
        result = services.login_with_ton(proof=wallet.proof(ch.nonce))
        self.assertTrue(result.user.linked_wallets.get().address.startswith("-1:"))

    def test_link_ton_wallet(self):
        user = User.objects.create_user()
        ch = self.challenge(purpose=AuthChallenge.Purpose.LINK_WALLET, user=user)
        linked = services.link_ton_wallet(user=user, proof=self.wallet.proof(ch.nonce))
        self.assertEqual(linked.user, user)

    def test_serializer_accepts_tonconnect_shape(self):
        proof = self.wallet.proof("abc")
        body = {
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
        s = TonWalletProofSerializer(data=body)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.to_proof(), proof)


try:
    from pytoniq.contract.wallets.wallet import WalletV3R1, WalletV3R2, WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
except ImportError:
    WalletV3R1 = None


@unittest.skipIf(WalletV3R1 is None, "pytoniq not installed")
class TonRealWalletLayoutTests(TestCase):
    def state_init(self, data_cell):
        code = begin_cell().store_uint(0xC0DE, 16).end_cell()
        cell = StateInit(code=code, data=data_cell).serialize()
        return base64.b64encode(cell.to_boc()).decode(), cell.hash

    def test_supported_wallet_versions_bind_their_own_key(self):
        pub = bytes(SigningKey.generate().verify_key)
        other = bytes(SigningKey.generate().verify_key)
        datas = {
            "v3r1": WalletV3R1.create_data_cell(public_key=pub, wallet_id=698983191),
            "v3r2": WalletV3R2.create_data_cell(public_key=pub, wallet_id=698983191),
            "v4r2": WalletV4R2.create_data_cell(public_key=pub, wallet_id=698983191),
            "v5r1": WalletV5R1.create_data_cell(public_key=pub, network_global_id=-239),
        }
        for version, data in datas.items():
            with self.subTest(version=version):
                b64, address_hash = self.state_init(data)
                self.assertEqual(verifiers._bind_public_key(b64, address_hash, pub.hex()), pub)
                with self.assertRaises(InvalidTonProof):
                    verifiers._bind_public_key(b64, address_hash, other.hex())


class AuthResponseSerializerTests(TestCase):
    @override_settings(TELEGRAM_BOT_TOKEN=BOT_TOKEN)
    def test_shape(self):
        result = services.login_with_telegram_mini_app(init_data=make_mini_app_init_data({"id": 3}))
        data = AuthResponseSerializer(result).data
        self.assertEqual(set(data), {"access", "refresh", "created", "user"})
        self.assertEqual(data["user"]["telegram_id"], 3)
        self.assertNotIn("password", data["user"])
