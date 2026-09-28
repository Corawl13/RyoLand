"""Identity models: the user, the external wallets that identify them, and sign-in challenges.

Naming note: `LinkedWallet` here is an *identity* (an external crypto wallet the user proved
they control by signing a message). It is NOT the platform balance — that will be the
`wallet` app's `Wallet`/ledger models.
"""
import secrets

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import BaseModel


class UserRole(models.TextChoices):
    PLAYER = "player", "Player"
    SUPPORT = "support", "Support agent"
    TRADER = "trader", "Trader"
    FINANCE = "finance", "Finance"
    RISK_ANALYST = "risk_analyst", "Risk analyst"
    ADMIN = "admin", "Administrator"


class UserStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SELF_EXCLUDED = "self_excluded", "Self-excluded"
    SUSPENDED = "suspended", "Suspended"
    CLOSED = "closed", "Closed"


class Chain(models.TextChoices):
    """Blockchain families we can verify wallet ownership on."""

    EVM = "evm", "EVM (Ethereum, BSC, Polygon, ...)"
    TON = "ton", "TON"


def generate_username() -> str:
    return f"player_{secrets.token_hex(5)}"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        return self.get(**{self.model.USERNAME_FIELD: self.model.normalize_username(username)})

    def create_user(self, username=None, password=None, **extra_fields):
        """Create a user. Players authenticate via wallet/Telegram, so no password is the norm.

        A generated `player_xxxxxxxxxx` username is used when none is given.
        """
        if not username:
            for _ in range(5):
                candidate = generate_username()
                if not self.model.objects.filter(username=candidate).exists():
                    username = candidate
                    break
            else:  # pragma: no cover - 40 bits of randomness
                raise RuntimeError("Could not generate a unique username.")
        username = self.model.normalize_username(username)
        email = self.normalize_email(extra_fields.pop("email", None)) or None
        user = self.model(username=username, email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault("role", UserRole.ADMIN)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not password:
            raise ValueError("Superusers need a password to sign in to the admin.")
        return self.create_user(username, password, **extra_fields)


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    """Platform user.

    Players sign in with a crypto wallet signature or Telegram (no password).
    Staff sign in to the Django admin with username + password.
    `status` is the single source of truth for whether an account may sign in; `is_active`
    is derived from it so the two can never disagree.
    """

    username = models.CharField(
        max_length=150, unique=True, validators=[UnicodeUsernameValidator()]
    )
    email = models.EmailField(null=True, blank=True, unique=True)  # optional, never required
    display_name = models.CharField(max_length=100, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    language_code = models.CharField(max_length=10, blank=True)

    # Telegram identity (a user has at most one Telegram account).
    telegram_id = models.BigIntegerField(null=True, blank=True, unique=True)
    telegram_username = models.CharField(max_length=64, blank=True)

    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.PLAYER)
    status = models.CharField(
        max_length=20, choices=UserStatus.choices, default=UserStatus.ACTIVE, db_index=True
    )
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        constraints = [
            # Players never get admin-site access.
            models.CheckConstraint(
                condition=~Q(role=UserRole.PLAYER, is_staff=True),
                name="accounts_user_player_not_staff",
            ),
            # Superusers are always administrators.
            models.CheckConstraint(
                condition=Q(is_superuser=False) | Q(role=UserRole.ADMIN),
                name="accounts_user_superuser_is_admin",
            ),
        ]

    def __str__(self) -> str:
        return self.username

    @classmethod
    def normalize_username(cls, username):
        username = super().normalize_username(username)
        return username.lower() if isinstance(username, str) else username

    def save(self, *args, **kwargs):
        self.email = self.email or None  # '' would violate the unique constraint
        super().save(*args, **kwargs)

    # --- account state -------------------------------------------------------------
    @property
    def is_active(self) -> bool:  # overrides AbstractBaseUser.is_active
        """May sign in. Self-excluded players can still sign in (e.g. to withdraw funds)
        but cannot wager — see `can_wager`."""
        return self.status in (UserStatus.ACTIVE, UserStatus.SELF_EXCLUDED)

    @property
    def can_wager(self) -> bool:
        return self.status == UserStatus.ACTIVE

    @property
    def is_player(self) -> bool:
        return self.role == UserRole.PLAYER


class LinkedWallet(BaseModel):
    """An external crypto wallet a user has proven ownership of (by signing a challenge)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="linked_wallets")
    chain = models.CharField(max_length=10, choices=Chain.choices)
    # Canonical form: EVM = EIP-55 checksum address, TON = raw "workchain:hex_hash".
    address = models.CharField(max_length=128)
    public_key = models.CharField(max_length=64, blank=True)  # hex; set for TON
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chain", "address"], name="accounts_linkedwallet_unique_chain_address"
            )
        ]

    def __str__(self) -> str:
        return f"{self.chain}:{self.address}"


class AuthChallenge(BaseModel):
    """Single-use, short-lived nonce that a wallet must sign to prove ownership.

    EVM: the server builds the exact message (SIWE / EIP-4361 style) and stores it here;
    verification always uses the stored text, never text supplied by the client.
    TON: `nonce` is the `payload` the wallet embeds in its ton_proof (the address is not
    known yet when the payload is requested, so `address` stays empty).
    """

    class Purpose(models.TextChoices):
        LOGIN = "login", "Login / register"
        LINK_WALLET = "link_wallet", "Link wallet to existing account"

    nonce = models.CharField(max_length=64, unique=True)
    chain = models.CharField(max_length=10, choices=Chain.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    address = models.CharField(max_length=128, blank=True)
    message = models.TextField(blank=True)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="auth_challenges"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.purpose}:{self.chain}:{self.nonce[:8]}"

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()
