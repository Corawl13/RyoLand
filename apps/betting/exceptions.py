from apps.core.exceptions import DomainError


class SelectionNotFoundError(DomainError):
    default_message = "One of the requested selections does not exist."
    default_code = "selection_not_found"
    http_status = 404


class MarketSuspendedError(DomainError):
    default_message = "This market is not open for betting right now."
    default_code = "market_suspended"
    http_status = 409


class EventNotOpenForBettingError(DomainError):
    default_message = "This event is not open for new bets."
    default_code = "event_not_open"
    http_status = 409


class OddsChangedError(DomainError):
    default_message = "The odds for one or more selections have changed."
    default_code = "odds_changed"
    http_status = 409


class InvalidComboError(DomainError):
    default_message = "This combination of selections is not a valid combo bet."
    default_code = "invalid_combo"


class StakeOutOfRangeError(DomainError):
    default_message = "The stake amount is outside the allowed range."
    default_code = "stake_out_of_range"


class BetLimitExceededError(DomainError):
    default_message = "The potential payout for this bet exceeds the allowed limit."
    default_code = "bet_limit_exceeded"


class PlayerNotEligibleError(DomainError):
    default_message = "This account is not eligible to place bets."
    default_code = "player_not_eligible"
    http_status = 403


class DuplicateIdempotencyKeyError(DomainError):
    default_message = "This idempotency key was already used for a different bet."
    default_code = "idempotency_key_conflict"
    http_status = 409
