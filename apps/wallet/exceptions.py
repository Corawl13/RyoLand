from apps.core.exceptions import DomainError


class InvalidAmountError(DomainError):
    default_message = "The amount must be a positive number."
    default_code = "invalid_amount"


class InsufficientBalanceError(DomainError):
    default_message = "Available balance is insufficient for this operation."
    default_code = "insufficient_balance"
    http_status = 409


class InsufficientLockedBalanceError(DomainError):
    default_message = "Locked balance is insufficient for this operation."
    default_code = "insufficient_locked_balance"
    http_status = 409


class DuplicateIdempotencyKeyError(DomainError):
    """Raised only if the same idempotency key is reused for a transaction whose shape
    (user/currency/type) doesn't match the original — a caller bug, not a safe replay."""

    default_message = "This idempotency key was already used for a different operation."
    default_code = "idempotency_key_conflict"
    http_status = 409
