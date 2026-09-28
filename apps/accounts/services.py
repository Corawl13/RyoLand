"""Authentication use cases for wallet and Telegram sign-ins."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts import verifiers
from apps.accounts.exceptions import (
    AccountNotAllowed,
    IdentityAlreadyLinked,
    InvalidAddress,
    InvalidChallenge,
    LastAuthMethod,
    LinkedWalletLimitReached,
)
from apps.accounts.models import AuthChallenge, Chain, LinkedWallet, User
from apps.accounts.verifiers import TelegramProfile, TonProof

_MISSING = object()


def _conf(name: str, default=_MISSING):
    if hasattr(settings, name):
        return getattr(settings, name)
    if default is _MISSING:
        raise ImproperlyConfigured(f"Missing required setting: {name}")
    return default


@dataclass(frozen=True)
class AuthResult:
    user: User
    created: bool
    access: str
    refresh: str


def issue_tokens(user: User) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def create_wallet_challenge(
    *,
    chain: str,
    address: str | None = None,
    purpose: str = AuthChallenge.Purpose.LOGIN,
    user: User | None = None,
    ip_address: str | None = None,
) -> AuthChallenge:
    if purpose == AuthChallenge.Purpose.LINK_WALLET and user is None:
        raise InvalidChallenge("Linking a wallet requires an authenticated user.")

    now = timezone.now()
    expires_at = now + timedelta(seconds=_conf("WEB3_CHALLENGE_TTL_SECONDS", 600))
    nonce = secrets.token_hex(16)
    canonical, message = "", ""

    if chain == Chain.EVM:
        if not address:
            raise InvalidAddress("An address is required for EVM challenges.")
        canonical = verifiers.normalize_evm_address(address)
        domains = _conf("WEB3_AUTH_DOMAINS")
        statement = (
            "Link this wallet to your account."
            if purpose == AuthChallenge.Purpose.LINK_WALLET
            else "Sign in. This request will not trigger a transaction or cost any gas."
        )
        message = verifiers.build_evm_sign_in_message(
            domain=domains[0],
            uri=_conf("WEB3_AUTH_URI"),
            address=canonical,
            statement=statement,
            nonce=nonce,
            chain_id=_conf("WEB3_EVM_CHAIN_ID", 1),
            issued_at=now,
            expires_at=expires_at,
        )
    elif chain != Chain.TON:
        raise InvalidChallenge("Unsupported chain.")

    return AuthChallenge.objects.create(
        nonce=nonce,
        chain=chain,
        purpose=purpose,
        address=canonical,
        message=message,
        user=user if purpose == AuthChallenge.Purpose.LINK_WALLET else None,
        ip_address=ip_address,
        expires_at=expires_at,
    )


def purge_expired_challenges(*, older_than: timedelta = timedelta(days=1)) -> int:
    deleted, _ = AuthChallenge.objects.filter(expires_at__lt=timezone.now() - older_than).delete()
    return deleted


def _lock_challenge(
    *, nonce: str, chain: str, purpose: str, user: User | None = None
) -> AuthChallenge:
    challenge = (
        AuthChallenge.objects.select_for_update()
        .filter(
            nonce=nonce,
            chain=chain,
            purpose=purpose,
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if challenge is None:
        raise InvalidChallenge()
    if purpose == AuthChallenge.Purpose.LINK_WALLET and (
        user is None or challenge.user_id != user.id
    ):
        raise InvalidChallenge()
    return challenge


def _consume(challenge: AuthChallenge) -> None:
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at", "updated_at"])


def _verify_evm(challenge: AuthChallenge, *, address: str, signature: str) -> str:
    canonical = verifiers.normalize_evm_address(address)
    if canonical != challenge.address:
        raise InvalidChallenge()
    verifiers.verify_evm_signature(
        message=challenge.message,
        signature=signature,
        expected_address=canonical,
    )
    return canonical


def _verify_ton(proof: TonProof) -> verifiers.VerifiedTonWallet:
    return verifiers.verify_ton_proof(
        proof,
        allowed_domains=_conf("WEB3_AUTH_DOMAINS"),
        max_age=_conf("WEB3_CHALLENGE_TTL_SECONDS", 600),
    )


def _telegram_from_mini_app(init_data: str) -> TelegramProfile:
    return verifiers.verify_telegram_mini_app(
        init_data,
        bot_token=_conf("TELEGRAM_BOT_TOKEN"),
        max_age=_conf("TELEGRAM_AUTH_MAX_AGE_SECONDS", 3600),
    )


def _telegram_from_widget(auth_data: dict) -> TelegramProfile:
    return verifiers.verify_telegram_widget(
        auth_data,
        bot_token=_conf("TELEGRAM_BOT_TOKEN"),
        max_age=_conf("TELEGRAM_AUTH_MAX_AGE_SECONDS", 3600),
    )


def _ensure_can_login(user: User) -> None:
    if not user.is_active:
        raise AccountNotAllowed()


def _finalize_login(user: User, created: bool) -> AuthResult:
    _ensure_can_login(user)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login", "updated_at"])
    access, refresh = issue_tokens(user)
    return AuthResult(user=user, created=created, access=access, refresh=refresh)


def _short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def _user_for_wallet(*, chain: str, address: str, public_key: str = "") -> tuple[User, bool]:
    now = timezone.now()
    linked = LinkedWallet.objects.select_related("user").filter(chain=chain, address=address).first()
    if linked:
        linked.last_used_at = now
        linked.save(update_fields=["last_used_at", "updated_at"])
        return linked.user, False
    try:
        with transaction.atomic():
            user = User.objects.create_user(display_name=_short_address(address))
            LinkedWallet.objects.create(
                user=user, chain=chain, address=address, public_key=public_key, last_used_at=now
            )
    except IntegrityError:
        linked = LinkedWallet.objects.select_related("user").get(chain=chain, address=address)
        return linked.user, False
    return user, True


def _sync_telegram_fields(user: User, profile: TelegramProfile) -> None:
    changed = []
    for field, value in (
        ("telegram_username", profile.username),
        ("language_code", profile.language_code),
        ("avatar_url", profile.photo_url),
    ):
        if value and getattr(user, field) != value:
            setattr(user, field, value)
            changed.append(field)
    if not user.display_name and profile.full_name:
        user.display_name = profile.full_name[:100]
        changed.append("display_name")
    if changed:
        user.save(update_fields=[*changed, "updated_at"])


def _user_for_telegram(profile: TelegramProfile) -> tuple[User, bool]:
    user = User.objects.filter(telegram_id=profile.id).first()
    if user:
        _sync_telegram_fields(user, profile)
        return user, False
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                telegram_id=profile.id,
                telegram_username=profile.username,
                display_name=profile.full_name[:100] if profile.full_name else "",
                avatar_url=profile.photo_url,
                language_code=profile.language_code,
            )
    except IntegrityError:
        user = User.objects.get(telegram_id=profile.id)
        _sync_telegram_fields(user, profile)
        return user, False
    return user, True


def login_with_telegram_mini_app(*, init_data: str) -> AuthResult:
    profile = _telegram_from_mini_app(init_data)
    user, created = _user_for_telegram(profile)
    return _finalize_login(user, created)


def login_with_telegram_widget(*, auth_data: dict) -> AuthResult:
    profile = _telegram_from_widget(auth_data)
    user, created = _user_for_telegram(profile)
    return _finalize_login(user, created)


def link_telegram_mini_app(*, user: User, init_data: str) -> User:
    profile = _telegram_from_mini_app(init_data)
    if user.telegram_id and user.telegram_id != profile.id:
        raise IdentityAlreadyLinked()
    existing = User.objects.filter(telegram_id=profile.id).exclude(pk=user.pk).first()
    if existing:
        raise IdentityAlreadyLinked()
    user.telegram_id = profile.id
    user.telegram_username = profile.username
    user.avatar_url = profile.photo_url or user.avatar_url
    user.language_code = profile.language_code or user.language_code
    if not user.display_name and profile.full_name:
        user.display_name = profile.full_name[:100]
    user.save(
        update_fields=[
            "telegram_id",
            "telegram_username",
            "avatar_url",
            "language_code",
            "display_name",
            "updated_at",
        ]
    )
    return user


def login_with_wallet(*, chain: str, address: str, signature: str, nonce: str) -> AuthResult:
    challenge = _lock_challenge(nonce=nonce, chain=chain, purpose=AuthChallenge.Purpose.LOGIN)
    try:
        canonical = _verify_evm(challenge, address=address, signature=signature)
        user, created = _user_for_wallet(chain=chain, address=canonical)
        _consume(challenge)
    finally:
        challenge.refresh_from_db()
    return _finalize_login(user, created)


def login_with_evm(*, address: str, nonce: str, signature: str) -> AuthResult:
    return login_with_wallet(chain=Chain.EVM, address=address, signature=signature, nonce=nonce)


def login_with_ton(*, proof: TonProof) -> AuthResult:
    challenge = _lock_challenge(
        nonce=proof.payload,
        chain=Chain.TON,
        purpose=AuthChallenge.Purpose.LOGIN,
    )
    wallet = _verify_ton(proof)
    try:
        _consume(challenge)
        user, created = _user_for_wallet(
            chain=Chain.TON,
            address=wallet.address,
            public_key=wallet.public_key,
        )
    finally:
        challenge.refresh_from_db()
    return _finalize_login(user, created)


def login_with_ton_proof(*, proof: TonProof) -> AuthResult:
    return login_with_ton(proof=proof)


def _attach_wallet(user: User, *, chain: str, address: str, public_key: str = "") -> LinkedWallet:
    existing = LinkedWallet.objects.filter(chain=chain, address=address).first()
    if existing:
        if existing.user_id == user.id:
            return existing
        raise IdentityAlreadyLinked("This wallet belongs to another account.")
    if user.linked_wallets.count() >= _conf("ACCOUNTS_MAX_LINKED_WALLETS", 10):
        raise LinkedWalletLimitReached()
    try:
        with transaction.atomic():
            return LinkedWallet.objects.create(
                user=user,
                chain=chain,
                address=address,
                public_key=public_key,
                last_used_at=timezone.now(),
            )
    except IntegrityError:
        raise IdentityAlreadyLinked("This wallet belongs to another account.") from None


def _attach_telegram(user: User, profile: TelegramProfile) -> User:
    if user.telegram_id == profile.id:
        _sync_telegram_fields(user, profile)
        return user
    if user.telegram_id is not None:
        raise IdentityAlreadyLinked("This account already has a different Telegram account.")
    if User.objects.filter(telegram_id=profile.id).exists():
        raise IdentityAlreadyLinked("This Telegram account belongs to another account.")
    user.telegram_id = profile.id
    try:
        with transaction.atomic():
            user.save(update_fields=["telegram_id", "updated_at"])
    except IntegrityError:
        user.telegram_id = None
        raise IdentityAlreadyLinked("This Telegram account belongs to another account.") from None
    _sync_telegram_fields(user, profile)
    return user


def link_wallet_to_user(*, user: User, chain: str, address: str, signature: str, nonce: str) -> User:
    challenge = _lock_challenge(
        nonce=nonce,
        chain=chain,
        purpose=AuthChallenge.Purpose.LINK_WALLET,
        user=user,
    )
    try:
        canonical = _verify_evm(challenge, address=address, signature=signature)
        existing = LinkedWallet.objects.filter(chain=chain, address=canonical).exclude(user=user).first()
        if existing:
            raise IdentityAlreadyLinked()
        if user.linked_wallets.count() >= getattr(settings, "ACCOUNTS_MAX_LINKED_WALLETS", 10):
            raise LinkedWalletLimitReached()
        LinkedWallet.objects.create(user=user, chain=chain, address=canonical, last_used_at=timezone.now())
        _consume(challenge)
    finally:
        challenge.refresh_from_db()
    return user


def link_ton_wallet(*, user: User, proof: TonProof) -> LinkedWallet:
    challenge = _lock_challenge(
        nonce=proof.payload,
        chain=Chain.TON,
        purpose=AuthChallenge.Purpose.LINK_WALLET,
        user=user,
    )
    wallet = _verify_ton(proof)
    try:
        _consume(challenge)
        return _attach_wallet(user, chain=Chain.TON, address=wallet.address, public_key=wallet.public_key)
    finally:
        challenge.refresh_from_db()


def link_telegram_widget(*, user: User, auth_data: dict) -> User:
    return _attach_telegram(user, _telegram_from_widget(auth_data))


def unlink_wallet(*, user: User, wallet_id) -> None:
    locked = User.objects.select_for_update().get(pk=user.pk)
    target = locked.linked_wallets.filter(pk=wallet_id).first()
    if target is None:
        return
    other_methods = (
        locked.telegram_id is not None
        or locked.has_usable_password()
        or locked.linked_wallets.exclude(pk=target.pk).exists()
    )
    if not other_methods:
        raise LastAuthMethod()
    target.delete()
