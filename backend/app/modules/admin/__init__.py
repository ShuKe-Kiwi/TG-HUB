"""Local-only administration API."""

from app.modules.admin.router import (
    AdminApiError,
    admin_error_handler,
    admin_request_validation_handler,
    guard_admin_request,
    router,
)

__all__ = [
    "AdminApiError",
    "admin_error_handler",
    "admin_request_validation_handler",
    "guard_admin_request",
    "router",
]
