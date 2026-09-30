from apps.core.exceptions import DomainError


class ResultAlreadySubmittedError(DomainError):
    """A different result was already recorded for this event. Resubmitting the exact same
    result is a safe no-op — this only fires on a genuine conflict."""

    default_message = "A different official result has already been submitted for this event."
    default_code = "result_already_submitted"
    http_status = 409


class EventResultRequiredError(DomainError):
    default_message = "This event has no official result on file yet."
    default_code = "event_result_required"
    http_status = 409
