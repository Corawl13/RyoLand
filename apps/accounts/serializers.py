"""DRF serializers for the accounts API."""
import re
import unicodedata

from rest_framework import serializers

from apps.accounts import verifiers
from apps.accounts.exceptions import InvalidAddress
from apps.accounts.models import AuthChallenge, Chain, LinkedWallet, User
from apps.accounts.verifiers import TonProof

EVM_SIGNATURE_RE = re.compile(r"^(0x)?[0-9a-fA-F]{130}$")


class LinkedWalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = LinkedWallet
        fields = ("id", "chain", "address", "created_at", "last_used_at")
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    linked_wallets = LinkedWalletSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "display_name",
            "avatar_url",
            "language_code",
            "role",
            "status",
            "telegram_id",
            "telegram_username",
            "linked_wallets",
            "created_at",
        )
        read_only_fields = fields


class AuthResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    created = serializers.BooleanField()
    user = UserSerializer()


class WalletChallengeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthChallenge
        fields = ("chain", "nonce", "message", "expires_at")
        read_only_fields = fields


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("display_name", "language_code")

    def validate_display_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Display name must be at least 2 characters.")
        if any(unicodedata.category(ch).startswith("C") for ch in value):
            raise serializers.ValidationError("Display name contains invalid characters.")
        return value


class WalletChallengeRequestSerializer(serializers.Serializer):
    chain = serializers.ChoiceField(choices=Chain.choices)
    address = serializers.CharField(required=False, allow_blank=True, max_length=128)

    def validate(self, attrs):
        if attrs["chain"] == Chain.EVM:
            if not attrs.get("address"):
                raise serializers.ValidationError({"address": "Required for EVM wallets."})
            try:
                attrs["address"] = verifiers.normalize_evm_address(attrs["address"])
            except InvalidAddress as exc:
                raise serializers.ValidationError({"address": exc.message}) from None
        else:
            attrs["address"] = None
        return attrs


class EVMWalletProofSerializer(serializers.Serializer):
    address = serializers.CharField(max_length=64)
    nonce = serializers.CharField(max_length=64)
    signature = serializers.CharField(max_length=200)

    def validate_address(self, value):
        try:
            return verifiers.normalize_evm_address(value)
        except InvalidAddress as exc:
            raise serializers.ValidationError(exc.message) from None

    def validate_signature(self, value):
        if not EVM_SIGNATURE_RE.match(value):
            raise serializers.ValidationError("Signature must be a 65-byte hex string.")
        return value


class TonAccountSerializer(serializers.Serializer):
    address = serializers.CharField(max_length=80)
    chain = serializers.CharField(required=False, allow_blank=True)
    publicKey = serializers.CharField(source="public_key", max_length=64)
    walletStateInit = serializers.CharField(
        source="wallet_state_init", max_length=verifiers.MAX_STATE_INIT_B64
    )


class TonProofDomainSerializer(serializers.Serializer):
    lengthBytes = serializers.IntegerField(source="length_bytes", min_value=1, max_value=255)
    value = serializers.CharField(max_length=255)


class TonProofBodySerializer(serializers.Serializer):
    timestamp = serializers.IntegerField(min_value=1)
    domain = TonProofDomainSerializer()
    payload = serializers.CharField(max_length=128)
    signature = serializers.CharField(max_length=200)


class TonWalletProofSerializer(serializers.Serializer):
    account = TonAccountSerializer()
    proof = TonProofBodySerializer()

    def to_proof(self) -> TonProof:
        account, proof = self.validated_data["account"], self.validated_data["proof"]
        return TonProof(
            address=account["address"],
            public_key=account["public_key"],
            wallet_state_init=account["wallet_state_init"],
            timestamp=proof["timestamp"],
            domain=proof["domain"]["value"],
            domain_length=proof["domain"]["length_bytes"],
            payload=proof["payload"],
            signature=proof["signature"],
        )


class TelegramMiniAppAuthSerializer(serializers.Serializer):
    init_data = serializers.CharField(max_length=4096)


class TelegramWidgetAuthSerializer(serializers.Serializer):
    auth_data = serializers.DictField(child=serializers.CharField(allow_blank=True))

    def validate_auth_data(self, value):
        if len(value) > 20:
            raise serializers.ValidationError("Too many fields.")
        missing = {"id", "auth_date", "hash"} - value.keys()
        if missing:
            raise serializers.ValidationError(f"Missing fields: {', '.join(sorted(missing))}.")
        return value
