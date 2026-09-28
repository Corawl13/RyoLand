"""API plumbing shared by every app: one consistent error envelope."""
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .exceptions import DomainError


def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        return Response(
            {"error": {"code": exc.code, "message": exc.message}}, status=exc.http_status
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, ValidationError):
        error = {"code": "validation_error", "message": "Invalid input.", "fields": response.data}
    else:
        data = response.data if isinstance(response.data, dict) else {}
        detail = data.get("detail", "")
        error = {
            "code": data.get("code") or getattr(detail, "code", None) or "error",
            "message": str(detail) or "Request failed.",
        }
    response.data = {"error": error}
    return response