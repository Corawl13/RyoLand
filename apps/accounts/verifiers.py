"""Cryptographic verification helpers for external identities."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pytoniq_core import Cell
from pytoniq_core.tlb.account import StateInit

from apps.accounts.exceptions import (
    InvalidAddress,
    InvalidSignature,
    InvalidTelegramData,
    InvalidTonProof,
)

CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True)
class TelegramProfile:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    photo_url: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


def _check_telegram_hash(fields: Mapping[str, str], secret_key: bytes) -> None:
    received = fields.get("hash", "")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()) if k != "hash")
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received.lower().encode()):
        raise InvalidTelegramData("Telegram signature mismatch.")


def _check_auth_date(fields: Mapping[str, str], max_age: int, now: float | None) -> None:
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise InvalidTelegramData("Missing or malformed auth_date.") from None
    current = int(now if now is not None else time.time())
    if auth_date > current + CLOCK_SKEW_SECONDS or current - auth_date > max_age:
        raise InvalidTelegramData("Telegram data has expired.")


def _profile_from_mapping(data: Mapping) -> TelegramProfile:
    raw_id = data.get("id")
    if isinstance(raw_id, bool):
        raise InvalidTelegramData("Malformed Telegram user id.")
    try:
        telegram_id = int(raw_id)
    except (TypeError, ValueError):
        raise InvalidTelegramData("Malformed Telegram user id.") from None
    if telegram_id <= 0:
        raise InvalidTelegramData("Malformed Telegram user id.")

    def text(key: str) -> str:
        return str(data.get(key) or "")

    return TelegramProfile(
        id=telegram_id,
        first_name=text("first_name"),
        last_name=text("last_name"),
        username=text("username"),
        language_code=text("language_code"),
        photo_url=text("photo_url"),
    )


def verify_telegram_mini_app(
    init_data: str, *, bot_token: str, max_age: int, now: float | None = None
) -> TelegramProfile:
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = dict(pairs)
    if not pairs or len(fields) != len(pairs):
        raise InvalidTelegramData("Malformed initData.")
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    _check_telegram_hash(fields, secret_key)
    _check_auth_date(fields, max_age, now)
    try:
        user = json.loads(fields["user"])
    except (KeyError, json.JSONDecodeError):
        raise InvalidTelegramData("initData has no user.") from None
    if not isinstance(user, dict):
        raise InvalidTelegramData("initData user is malformed.")
    return _profile_from_mapping(user)


def verify_telegram_widget(
    auth_data: Mapping, *, bot_token: str, max_age: int, now: float | None = None
) -> TelegramProfile:
    fields = {str(k): str(v) for k, v in auth_data.items() if v is not None}
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    _check_telegram_hash(fields, secret_key)
    _check_auth_date(fields, max_age, now)
    return _profile_from_mapping(fields)


def normalize_evm_address(address: str) -> str:
    if not isinstance(address, str) or not is_address(address):
        raise InvalidAddress("Not a valid EVM address.")
    return to_checksum_address(address)


def build_evm_sign_in_message(
    *,
    domain: str,
    uri: str,
    address: str,
    statement: str,
    nonce: str,
    chain_id: int,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    def iso(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        f"{statement}\n\n"
        f"URI: {uri}\n"
        f"Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {iso(issued_at)}\n"
        f"Expiration Time: {iso(expires_at)}"
    )


def verify_evm_signature(*, message: str, signature: str, expected_address: str) -> None:
    try:
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        raise InvalidSignature() from None
    if recovered != expected_address:
        raise InvalidSignature()


@dataclass(frozen=True)
class TonProof:
    address: str
    public_key: str
    wallet_state_init: str
    timestamp: int
    domain: str
    domain_length: int
    payload: str
    signature: str


@dataclass(frozen=True)
class VerifiedTonWallet:
    address: str
    public_key: str


MAX_STATE_INIT_B64 = 8192


def parse_ton_raw_address(raw: str) -> tuple[int, bytes]:
    try:
        workchain_text, hash_hex = raw.split(":")
        workchain = int(workchain_text)
        address_hash = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        raise InvalidAddress("Not a valid raw TON address.") from None
    if workchain not in (0, -1) or len(address_hash) != 32:
        raise InvalidAddress("Not a valid raw TON address.")
    return workchain, address_hash


def canonical_ton_address(workchain: int, address_hash: bytes) -> str:
    return f"{workchain}:{address_hash.hex()}"


def _bind_public_key(state_init_b64: str, address_hash: bytes, reported_hex: str) -> bytes:
    if len(state_init_b64) > MAX_STATE_INIT_B64:
        raise InvalidTonProof("walletStateInit is too large.")
    try:
        cell = Cell.one_from_boc(base64.b64decode(state_init_b64, validate=True))
        state_init = StateInit.deserialize(cell.begin_parse())
    except Exception:
        raise InvalidTonProof("Malformed walletStateInit.") from None
    if not hmac.compare_digest(cell.hash, address_hash):
        raise InvalidTonProof("walletStateInit does not match the address.")
    if state_init.data is None:
        raise InvalidTonProof("walletStateInit has no data.")

    candidates: set[bytes] = set()
    for skip_bits in (64, 65):
        data = state_init.data.begin_parse()
        if data.remaining_bits >= skip_bits + 256:
            data.skip_bits(skip_bits)
            candidates.add(data.load_uint(256).to_bytes(32, "big"))
    try:
        reported = bytes.fromhex(reported_hex)
    except ValueError:
        raise InvalidTonProof("Malformed public key.") from None
    if reported not in candidates:
        raise InvalidTonProof("Public key does not belong to this wallet.")
    return reported


def verify_ton_proof(
    proof: TonProof,
    *,
    allowed_domains: Collection[str],
    max_age: int,
    now: float | None = None,
) -> VerifiedTonWallet:
    workchain, address_hash = parse_ton_raw_address(proof.address)

    domain_bytes = proof.domain.encode()
    if proof.domain not in allowed_domains or proof.domain_length != len(domain_bytes):
        raise InvalidTonProof("Proof was not issued for this application.")

    current = int(now if now is not None else time.time())
    if not (current - max_age <= proof.timestamp <= current + CLOCK_SKEW_SECONDS):
        raise InvalidTonProof("Proof has expired.")

    public_key = _bind_public_key(proof.wallet_state_init, address_hash, proof.public_key)

    message = (
        b"ton-proof-item-v2/"
        + workchain.to_bytes(4, "big", signed=True)
        + address_hash
        + len(domain_bytes).to_bytes(4, "little")
        + domain_bytes
        + proof.timestamp.to_bytes(8, "little")
        + proof.payload.encode()
    )
    digest = hashlib.sha256(
        b"\xff\xff" + b"ton-connect" + hashlib.sha256(message).digest()
    ).digest()
    try:
        signature = base64.b64decode(proof.signature, validate=True)
        VerifyKey(public_key).verify(digest, signature)
    except (BadSignatureError, binascii.Error, ValueError):
        raise InvalidTonProof("Invalid signature.") from None

    return VerifiedTonWallet(
        address=canonical_ton_address(workchain, address_hash), public_key=public_key.hex()
    )
