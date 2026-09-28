"""Project-wide exception base classes."""


class DomainError(Exception):
    """Base class for expected business-rule failures raised by service layers.

    Services raise subclasses of this; the API layer (added with the views) maps them
    to HTTP 4xx responses using `code`, so views never need per-service try/except.
    """

    default_message = "A business rule was violated."
    default_code = "domain_error"

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.message = message or self.default_message
        self.code = code or self.default_code
        super().__init__(self.message)
