"""The errors of the API: an HTTP status, a stable code and a message.

The message is translated when the error is made, in the language of the
request (i18n.language() is active while a request is handled); the code
stays the same in every language, for programs.
"""

from __future__ import annotations

from tavletta.ui.i18n import _


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def bad_request(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


def invalid_field(name: str) -> ApiError:
    return ApiError(400, "invalid_field", _("Invalid value for %s") % name)


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)
