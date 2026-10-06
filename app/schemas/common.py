"""Shapes shared by every feature: paging, searching, and the standard message."""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, Field


class Page[T](BaseModel):
    """One page of results, plus what the caller needs to ask for the next.

    `total` is the count *after* filtering, not the size of the table, so a UI
    can say "12 of 340 match" rather than lying about either number.
    """

    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return (self.total + self.size - 1) // self.size if self.size else 0


class Message(BaseModel):
    detail: str


class PageParams(BaseModel):
    """Paging and searching, as one dependency.

    Every list endpoint takes these, so a builder with five thousand purchase
    orders gets the first fifty rather than all of them. The default of 50 is
    what fits a screen; the ceiling of 200 is what a browser can render without
    stuttering.
    """

    page: int = 1
    size: int = 50
    search: str | None = None
    #: Include records that have been archived. Off by default - archived rows
    #: are hidden precisely so nobody picks them by accident.
    include_archived: bool = False

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def page_params(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    size: Annotated[int, Query(ge=1, le=200, description="Rows per page.")] = 50,
    search: Annotated[
        str | None, Query(max_length=200, description="Free text over this feature's key fields.")
    ] = None,
    include_archived: Annotated[
        bool, Query(description="Include archived records, which are hidden by default.")
    ] = False,
) -> PageParams:
    """FastAPI dependency so every list endpoint declares paging the same way."""
    return PageParams(
        page=page, size=size, search=search or None, include_archived=include_archived
    )


class ArchiveRequest(BaseModel):
    """Why a record is being retired.

    A reason is not bureaucracy: six months later, "why did this trade stop
    appearing in the picker" is a real question, and the answer should be on the
    record rather than in somebody's memory.
    """

    reason: str | None = Field(default=None, max_length=500)


class BulkIds(BaseModel):
    """Several records at once, for the actions where one-at-a-time is absurd."""

    ids: list[str] = Field(min_length=1, max_length=200)
