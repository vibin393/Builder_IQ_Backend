"""The unified directory: every contact the builder holds, in one shape.

BuilderTrend's Directory puts internal users, subcontractors/vendors, customers and
realtors behind one searchable list. BuilderIQ already held all four, but on four
separate screens. This normalises them so a name can be found without first knowing
which kind of person it belongs to.
"""

import uuid
from typing import Literal

from pydantic import BaseModel

ContactType = Literal["team", "trade", "customer", "realtor"]


class DirectoryContact(BaseModel):
    type: ContactType
    id: uuid.UUID
    name: str
    #: Firm, brokerage or (for a trade) the trade itself — whatever reads as "who
    #: they are with". None for a customer, who is with nobody.
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    #: The one line that places them: a user's role, a realtor's licence, a trade's
    #: payment status. Kept generic so the list stays one shape.
    detail: str | None = None
    #: The human reference where there is one (vendor/realtor code).
    code: str | None = None
    #: Portal/account status, as BuilderTrend's Client Contacts Activation Status:
    #: active (has an active account), inactive (deactivated), or none where the
    #: concept does not apply to this kind of contact.
    status: str | None = None


class DirectorySummary(BaseModel):
    total: int
    team: int
    trades: int
    customers: int
    realtors: int
