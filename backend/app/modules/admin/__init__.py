"""Local-only administration API."""

from app.modules.admin.router import (
    AdminApiError,
    admin_error_handler,
    admin_request_validation_handler,
    guard_admin_request,
    router,
)
from app.modules.admin.pages import pages_router, static_directory

__all__ = [
    "AdminApiError",
    "admin_error_handler",
    "admin_request_validation_handler",
    "guard_admin_request",
    "pages_router",
    "router",
    "static_directory",
]
