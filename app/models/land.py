"""Land development: turning a site into serviced plots.

This happens before any home exists, on a different clock and a different budget.
A land project eventually produces the plots the rest of the system sells, so
`community_id` is the join back to the vertical side - and delivering plots creates
them there.

In the UK the long pole is planning: a permission (outline, then reserved matters, or
full), its conditions to discharge before and during the build, and the obligations
that come with it - S106 contributions, CIL, and the highway and sewer agreements
(S38 / S278 / S104). A permission not implemented in time lapses. That is what this
module tracks alongside the money.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


def _values(e):
    return [m.value for m in e]


class LandPhaseStatus(StrEnum):
    PLANNING = "planning"  # appraisal: is the deal worth doing
    ENTITLEMENT = "entitlement"  # securing planning consent - the long pole
    DEVELOPMENT = "development"  # infrastructure works: roads, drains, services
    COMPLETE = "complete"
    ON_HOLD = "on_hold"


class PlanningStatus(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    OUTLINE = "outline"  # outline consent; reserved matters to follow
    RESERVED_MATTERS = "reserved_matters"  # reserved matters approved: buildable
    FULL = "full"  # full permission: buildable
    REFUSED = "refused"
    APPEAL = "appeal"


class ObligationKind(StrEnum):
    CONDITION = "condition"  # a planning condition to discharge
    S106 = "s106"  # Section 106 planning obligation
    CIL = "cil"  # Community Infrastructure Levy
    S38 = "s38"  # road adoption agreement
    S278 = "s278"  # works on the existing highway
    S104 = "s104"  # sewer adoption agreement
    OTHER = "other"


class ObligationStatus(StrEnum):
    OUTSTANDING = "outstanding"
    SUBMITTED = "submitted"  # sent to the council, awaiting sign-off
    DISCHARGED = "discharged"
    PAID = "paid"
    WAIVED = "waived"


OPEN_OBLIGATIONS = (ObligationStatus.OUTSTANDING, ObligationStatus.SUBMITTED)


class LandProject(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "land_projects"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_land_projects_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("communities.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[LandPhaseStatus] = mapped_column(
        Enum(LandPhaseStatus, name="land_phase_status", values_callable=_values),
        default=LandPhaseStatus.PLANNING,
        nullable=False,
        index=True,
    )
    acres: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: Site area in hectares - how UK land is measured and priced.
    site_area_ha: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    planned_lots: Mapped[int | None] = mapped_column(Integer)
    delivered_lots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    acquisition_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    #: The appraisal: what the finished homes should sell for, and cost to build.
    gdv: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    build_cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    purchased_on: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)
    projected_completion: Mapped[date | None] = mapped_column(Date)

    # --- planning ---------------------------------------------------------------
    local_authority: Mapped[str | None] = mapped_column(String(120))
    planning_ref: Mapped[str | None] = mapped_column(String(64))
    planning_status: Mapped[PlanningStatus] = mapped_column(
        Enum(PlanningStatus, name="land_planning_status", values_callable=_values),
        default=PlanningStatus.NOT_SUBMITTED,
        nullable=False,
    )
    planning_approved_on: Mapped[date | None] = mapped_column(Date)
    #: The permission lapses if not implemented by this date (usually three years).
    planning_expires_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(2000))

    budget_lines: Mapped[list["LandBudgetLine"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    obligations: Mapped[list["LandObligation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def budget_total(self) -> Decimal:
        return sum((line.budget_amount for line in self.budget_lines), Decimal("0"))

    @property
    def actual_total(self) -> Decimal:
        return sum((line.actual_amount for line in self.budget_lines), Decimal("0"))

    @property
    def forecast_total(self) -> Decimal:
        """Each line at the larger of budget and actual: an overspend is not undone by
        another line's saving that has not happened yet."""
        return sum(
            (max(line.budget_amount, line.actual_amount) for line in self.budget_lines),
            Decimal("0"),
        )

    @property
    def cost_per_lot(self) -> Decimal | None:
        """The number that decides whether the land deal works at all - on the forecast,
        so an overspend shows here the day it is booked."""
        if not self.planned_lots:
            return None
        return ((self.acquisition_cost + self.forecast_total) / self.planned_lots).quantize(
            Decimal("0.01")
        )

    @property
    def profit(self) -> Decimal | None:
        """GDV less land, infrastructure and build. The appraisal's bottom line."""
        if self.gdv is None:
            return None
        return (
            self.gdv
            - self.acquisition_cost
            - self.forecast_total
            - (self.build_cost_estimate or Decimal("0"))
        )

    @property
    def margin_on_gdv(self) -> Decimal | None:
        if not self.gdv or self.profit is None:
            return None
        return (self.profit / self.gdv * 100).quantize(Decimal("0.1"))

    @property
    def is_buildable(self) -> bool:
        return self.planning_status in (PlanningStatus.RESERVED_MATTERS, PlanningStatus.FULL)

    @property
    def days_to_consent_expiry(self) -> int | None:
        if self.planning_expires_on is None:
            return None
        return (self.planning_expires_on - date.today()).days

    def __repr__(self) -> str:
        return f"<LandProject {self.code} {self.status}>"


class LandBudgetLine(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """Development cost by category - engineering, earthworks, utilities, roads."""

    __tablename__ = "land_budget_lines"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("land_projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    budget_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    actual_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(500))

    project: Mapped[LandProject] = relationship(back_populates="budget_lines")

    @property
    def variance(self) -> Decimal:
        return self.budget_amount - self.actual_amount


class LandObligation(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A planning condition or legal obligation attached to the permission.

    Pre-commencement conditions have to be discharged before a spade goes in; S106
    and CIL payments fall due at triggers ("before occupation of the 25th dwelling").
    Missing either is how a site gets a stop notice.
    """

    __tablename__ = "land_obligations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("land_projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[ObligationKind] = mapped_column(
        Enum(ObligationKind, name="land_obligation_kind", values_callable=_values),
        nullable=False,
    )
    reference: Mapped[str | None] = mapped_column(String(64))  # "Condition 7"
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: When it bites, in the council's words: "prior to commencement".
    trigger: Mapped[str | None] = mapped_column(String(300))
    #: Must be dealt with before any work starts on site.
    pre_commencement: Mapped[bool] = mapped_column(default=False, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[ObligationStatus] = mapped_column(
        Enum(ObligationStatus, name="land_obligation_status", values_callable=_values),
        default=ObligationStatus.OUTSTANDING,
        nullable=False,
        index=True,
    )
    completed_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(1000))

    project: Mapped[LandProject] = relationship(back_populates="obligations")

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.status in OPEN_OBLIGATIONS and self.due_date and self.due_date < date.today()
        )
