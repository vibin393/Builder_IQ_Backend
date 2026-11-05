"""A buyer's chosen option on a specific job.

Price and cost are snapshotted at selection time. This is the effective-dating that
keeps a signed contract stable while the catalog moves on underneath it.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class SelectionStatus(StrEnum):
    PENDING = "pending"  # chosen, not yet on a signed addendum
    CONFIRMED = "confirmed"  # signed by the buyer, cascaded into budget
    CANCELLED = "cancelled"


class SelectionApprovalStatus(StrEnum):
    """The customer-facing signoff on a chosen material, tracked apart from the
    contractual `status`.

    `status` is what the *builder* has done with the choice (pending → confirmed →
    cascaded into the budget). This is what the *buyer* has done with it: a choice
    can be confirmed internally yet still be awaiting the homeowner's approval, and
    the two must not be conflated or a decline would look like a budget entry.
    """

    NOT_SUBMITTED = "not_submitted"  # not yet sent to the buyer for approval
    AWAITING = "awaiting"  # sent, waiting on the buyer
    APPROVED = "approved"  # buyer approved, with a signature
    DECLINED = "declined"  # buyer declined, with a reason


class Selection(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "selections"
    __table_args__ = (UniqueConstraint("job_id", "option_id", name="uq_selections_job_option"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    option_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("options.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[SelectionStatus] = mapped_column(
        Enum(
            SelectionStatus,
            name="selection_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SelectionStatus.PENDING,
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Snapshots taken from the catalog when the buyer selected. Never re-read.
    price_at_selection: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cost_at_selection: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    schedule_days_at_selection: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The option's cost per cost code at the moment it was chosen, one unit's worth:
    #: [{"cost_code_id", "vendor_id", "amount"}]. Confirming applies exactly this to the
    #: budget and cancelling takes exactly this back out. Using the catalog's *current*
    #: lines made the two disagree whenever the catalog changed in between.
    cost_breakdown: Mapped[list | None] = mapped_column(JSON)

    selected_date: Mapped[date | None] = mapped_column(Date)
    #: The studio session this was chosen at, where there was one. Without it the
    #: appointment is a diary entry rather than a record of what came out of it.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("design_appointments.id", ondelete="SET NULL"),
        index=True,
    )

    # ---- customer signoff ------------------------------------------------
    # The homeowner's approval of the chosen material, kept separate from the
    # builder-side `status` above. See SelectionApprovalStatus.
    approval_status: Mapped[SelectionApprovalStatus] = mapped_column(
        Enum(
            SelectionApprovalStatus,
            name="selection_approval_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=SelectionApprovalStatus.NOT_SUBMITTED,
        nullable=False,
        index=True,
    )
    #: When it was sent to the buyer, and the date their answer is wanted by. A
    #: deadline with nothing awaiting is just a note; it only bites while AWAITING.
    approval_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_deadline: Mapped[date | None] = mapped_column(Date)
    #: When the buyer actually answered (approved or declined).
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The buyer's typed name, captured as the signature on the choice. A decision
    #: without a name against it is an approval nobody can be held to.
    buyer_signature: Mapped[str | None] = mapped_column(String(200))
    approved_by_buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("buyers.id", ondelete="SET NULL")
    )
    #: Why the buyer declined. Required to decline: a decline the builder cannot act
    #: on is a delay with no cause attached.
    decline_reason: Mapped[str | None] = mapped_column(String(1000))
    #: An optional note the buyer leaves with either answer.
    buyer_comment: Mapped[str | None] = mapped_column(String(1000))

    # ---- choice-based selection (BuilderTrend-style) ---------------------
    #: The budget the buyer has for this selection — what a choice is measured
    #: against. A choice above it is an upgrade the buyer pays the difference on.
    allowance_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    #: Whether the buyer must pick before the job can proceed. A selection they can
    #: skip and one that blocks the build are not the same obligation.
    requires_client_selection: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="selections")  # noqa: F821
    option: Mapped["Option"] = relationship()  # noqa: F821
    choices: Mapped[list["SelectionChoice"]] = relationship(
        back_populates="selection", cascade="all, delete-orphan"
    )

    @property
    def extended_price(self) -> Decimal:
        return self.price_at_selection * self.quantity

    @property
    def extended_cost(self) -> Decimal:
        return self.cost_at_selection * self.quantity

    @property
    def is_awaiting_buyer(self) -> bool:
        return self.approval_status is SelectionApprovalStatus.AWAITING

    @property
    def approval_overdue(self) -> bool:
        """Awaiting, and past the date the answer was wanted by."""
        return bool(
            self.is_awaiting_buyer
            and self.approval_deadline is not None
            and self.approval_deadline < date.today()
        )

    @property
    def over_allowance(self) -> Decimal | None:
        """How far the chosen price is above the allowance. Positive is an upgrade the
        buyer owes; negative is under budget; None when no allowance was set."""
        if self.allowance_amount is None:
            return None
        return self.extended_price - self.allowance_amount

    def __repr__(self) -> str:
        return f"<Selection job={self.job_id} option={self.option_id} {self.status}>"


class SelectionChoice(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One priced option the buyer can pick for a selection.

    BuilderTrend presents a selection (e.g. "Front Door") as a menu of choices, each
    with a price, and the buyer picks one. Kept apart from the contractual
    `Selection.option_id` + budget cascade: this is the client-facing menu and which
    one they picked, not the money move — so adding choices can never disturb a
    confirmed budget."""

    __tablename__ = "selection_choices"

    selection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("selections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The price the buyer sees for this choice. Compared against the allowance.
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    #: The option this choice maps to in the catalog, where it maps to one.
    option_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("options.id", ondelete="SET NULL")
    )
    #: The one the buyer picked. At most one per selection is true.
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    selection: Mapped["Selection"] = relationship(back_populates="choices")

    @property
    def over_allowance(self) -> Decimal | None:
        """This choice's price against the selection's allowance, if both are set."""
        allowance = self.selection.allowance_amount if self.selection else None
        return None if allowance is None else self.price - allowance
