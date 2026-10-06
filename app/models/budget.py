"""Job budget: one budget per job, one line per cost code.

original_amount  - generated from the plan's standard costs when the budget is created
revised_amount   - original plus the effect of confirmed selections and change orders

Committed and actual costs are not stored here; they are derived from purchase orders
and invoices so there is a single source of truth for each.
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BudgetStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    LOCKED = "locked"


class Budget(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("job_id", name="uq_budgets_job"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    status: Mapped[BudgetStatus] = mapped_column(
        Enum(BudgetStatus, name="budget_status", values_callable=lambda e: [m.value for m in e]),
        default=BudgetStatus.DRAFT,
        nullable=False,
    )

    lines: Mapped[list["BudgetLine"]] = relationship(
        back_populates="budget", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def original_total(self) -> Decimal:
        return sum((line.original_amount for line in self.lines), Decimal("0"))

    @property
    def revised_total(self) -> Decimal:
        return sum((line.revised_amount for line in self.lines), Decimal("0"))

    def __repr__(self) -> str:
        return f"<Budget job={self.job_id} {self.status}>"


class BudgetLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "budget_lines"
    __table_args__ = (
        UniqueConstraint("budget_id", "cost_code_id", name="uq_budget_lines_budget_code"),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("budgets.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    original_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    # What the work was supposed to take. Nullable because plenty of cost codes
    # are materials, where hours mean nothing - and a zero would read as "nobody
    # was supposed to spend any time on this", which is a different claim from
    # "we do not measure this one in hours".
    budgeted_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    revised_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(300))

    budget: Mapped[Budget] = relationship(back_populates="lines")

    @property
    def variance(self) -> Decimal:
        return self.revised_amount - self.original_amount
