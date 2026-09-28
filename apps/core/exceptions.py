"""Project-wide base exceptions for domain rule failures."""


class DomainError(Exception):
    """Base class for expected business-rule failures raised by service layers."""

    default_message = "A business rule was violated."
    default_code = "domain_error"
    http_status = 400

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.message = message or self.default_message
        self.code = code or self.default_code
        super().__init__(self.message)
