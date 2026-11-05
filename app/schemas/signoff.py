"""Request and response shapes for materials selection signoff.

The customer-facing approval of a chosen material. Kept apart from the contractual
selection `status`: this is what the buyer has done with the choice, not what the
builder has.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.models.selection import Selection, SelectionApprovalStatus, SelectionStatus

#: Money as a fixed-2dp string, so a float never rounds a price on its way out.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]


class RequestApproval(BaseModel):
    """Send a job's selections to the buyer for approval.

    `selection_ids` left off means every not-yet-submitted selection on the job,
    which is the common case: the builder finishes the sheet, then sends the lot.
    """

    deadline: date | None = None
    selection_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


class BuyerApprove(BaseModel):
    #: The buyer's typed name, recorded as the signature on the choice.
    signature: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=1000)


class BuyerDecline(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    signature: str | None = Field(default=None, max_length=200)
    comment: str | None = Field(default=None, max_length=1000)


class ChoiceCreate(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    price: Decimal = Field(default=Decimal("0"), ge=0)
    option_id: uuid.UUID | None = None
    sort_order: int = 0


class ChoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, min_length=1, max_length=300)
    price: Decimal | None = Field(default=None, ge=0)
    option_id: uuid.UUID | None = None
    sort_order: int | None = None


class ChoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    selection_id: uuid.UUID
    description: str
    price: Money
    option_id: uuid.UUID | None
    is_selected: bool
    sort_order: int
    over_allowance: Money | None


class SetAllowance(BaseModel):
    allowance_amount: Decimal | None = Field(default=None, ge=0)
    requires_client_selection: bool | None = None


class SelectionSignoffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    option_id: uuid.UUID
    option_name: str
    option_category: str | None
    status: SelectionStatus
    approval_status: SelectionApprovalStatus

    quantity: int
    price_at_selection: Money
    extended_price: Money

    allowance_amount: Money | None
    requires_client_selection: bool
    over_allowance: Money | None

    selected_date: date | None
    approval_requested_at: datetime | None
    approval_deadline: date | None
    decided_at: datetime | None
    buyer_signature: str | None
    approved_by_buyer_id: uuid.UUID | None
    decline_reason: str | None
    buyer_comment: str | None

    is_awaiting_buyer: bool
    approval_overdue: bool
    choices: list[ChoiceRead] = []

    @classmethod
    def of(cls, sel: Selection) -> "SelectionSignoffRead":
        """Build from a selection whose `option` and `choices` are loaded."""
        option = sel.option
        return cls(
            id=sel.id,
            job_id=sel.job_id,
            option_id=sel.option_id,
            option_name=option.name if option else "—",
            option_category=(str(option.category) if option and option.category else None),
            status=sel.status,
            approval_status=sel.approval_status,
            quantity=sel.quantity,
            price_at_selection=sel.price_at_selection,
            extended_price=sel.extended_price,
            allowance_amount=sel.allowance_amount,
            requires_client_selection=sel.requires_client_selection,
            over_allowance=sel.over_allowance,
            selected_date=sel.selected_date,
            approval_requested_at=sel.approval_requested_at,
            approval_deadline=sel.approval_deadline,
            decided_at=sel.decided_at,
            buyer_signature=sel.buyer_signature,
            approved_by_buyer_id=sel.approved_by_buyer_id,
            decline_reason=sel.decline_reason,
            buyer_comment=sel.buyer_comment,
            is_awaiting_buyer=sel.is_awaiting_buyer,
            approval_overdue=sel.approval_overdue,
            choices=sorted(
                (ChoiceRead.model_validate(c) for c in sel.choices),
                key=lambda c: c.sort_order,
            ),
        )


class SignoffSummary(BaseModel):
    """The counts above a job's signoff list."""

    job_id: uuid.UUID | None
    total: int
    not_submitted: int
    awaiting: int
    approved: int
    declined: int
    overdue: int
