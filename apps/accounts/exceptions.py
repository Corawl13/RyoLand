from apps.core.exceptions import DomainError


class InvalidChallenge(DomainError):
    default_message = "The sign-in challenge is invalid, expired or already used."
    default_code = "invalid_challenge"


class InvalidAddress(DomainError):
    default_message = "The wallet address is not valid."
    default_code = "invalid_address"


class InvalidSignature(DomainError):
    default_message = "Signature verification failed."
    default_code = "invalid_signature"


class InvalidTonProof(DomainError):
    default_message = "TON proof verification failed."
    default_code = "invalid_ton_proof"


class InvalidTelegramData(DomainError):
    default_message = "Telegram authentication data is invalid or expired."
    default_code = "invalid_telegram_data"


class AccountNotAllowed(DomainError):
    default_message = "This account is not allowed to sign in."
    default_code = "account_not_allowed"


class IdentityAlreadyLinked(DomainError):
    default_message = "This identity is already linked to an account."
    default_code = "identity_already_linked"


class LinkedWalletLimitReached(DomainError):
    default_message = "The maximum number of linked wallets has been reached."
    default_code = "linked_wallet_limit_reached"


class LastAuthMethod(DomainError):
    default_message = "You cannot remove your only way to sign in."
    default_code = "last_auth_method"
