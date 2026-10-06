"""Turning domain errors into HTTP responses, in one place.

The point of the three tiers is that each one speaks its own language. A service
that decides a purchase order cannot be issued should say exactly that - it
should not know what a 409 is, and it should not import FastAPI to say so.

So the business tier raises domain exceptions, and this module is the only thing
that knows their HTTP equivalents. Swap FastAPI for something else and this file
changes; nothing below it does.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.repositories.base import Conflict, NotFound


class BusinessRuleViolation(Exception):
    """A rule the business defined refused this operation.

    Carries an optional `override_hint` because several of this system's guards
    are deliberately overridable - blocking people outright just means they work
    around the system, and then the record is worthless. The hint tells the
    caller how to proceed deliberately rather than leaving them stuck.
    """

    def __init__(self, message: str, override_hint: str | None = None):
        self.message = message
        self.override_hint = override_hint
        super().__init__(message)

    @property
    def detail(self) -> str:
        return f"{self.message} {self.override_hint}".strip()


class PermissionDenied(Exception):
    """The caller is authenticated but not allowed to do this."""

    def __init__(self, message: str = "Insufficient permissions for this operation"):
        self.message = message
        super().__init__(message)


def register_error_handlers(app: FastAPI) -> None:
    """Wire the domain exceptions to status codes."""

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        # 404 rather than 403 even when the row exists under another tenant: a
        # 403 would confirm it exists.
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"{exc.entity} not found"},
        )

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": exc.message}
        )

    @app.exception_handler(BusinessRuleViolation)
    async def _rule(_: Request, exc: BusinessRuleViolation) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": exc.detail}
        )

    @app.exception_handler(PermissionDenied)
    async def _denied(_: Request, exc: PermissionDenied) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"detail": exc.message}
        )
