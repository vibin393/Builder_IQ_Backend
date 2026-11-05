"""The unified directory: every contact in one searchable list.

Read-only on purpose — editing a contact happens on its own screen (Users,
Trades, Leads, Realtors). This is the one place to find a person without first
knowing which kind they are.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, RequireStaff
from app.schemas.directory import ContactType, DirectoryContact, DirectorySummary
from app.services.directory import DirectoryService

router = APIRouter(prefix="/directory", tags=["directory"])


@router.get("/summary", response_model=DirectorySummary)
async def directory_summary(db: DbSession, user: RequireStaff):
    """How many contacts of each kind the builder holds."""
    data = await DirectoryService(db, user.tenant_id).summary()
    return DirectorySummary(**data)


@router.get("", response_model=list[DirectoryContact])
async def list_directory(
    db: DbSession,
    user: RequireStaff,
    type: Annotated[ContactType | None, Query(description="Limit to one kind of contact.")] = None,
    search: Annotated[str | None, Query(description="Match name, company or email.")] = None,
):
    """Every contact — team, trades, customers and realtors — in one shape."""
    return await DirectoryService(db, user.tenant_id).list_contacts(
        contact_type=type, search=search
    )
