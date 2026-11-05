"""What the trade scorecard remembers: the builder's own weights, and a monthly snapshot.

The score itself is computed live from the records (see app.services.scorecard). What
cannot be computed live is *history*: last month's schedule score is gone once this
month's tasks land. A snapshot per trade per month is what makes "getting better or
worse?" answerable.
"""

import uuid
from datetime import date

from sqlalchemy import JSON, Date, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ScorecardSettings(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """One row per builder: how much each dimension counts in the overall score."""

    __tablename__ = "scorecard_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_scorecard_settings_tenant"),)

    #: {"schedule": 25, "quality": 20, ...} - whole-number weights; they are
    #: normalised, so they need not add to 100.
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)


class ScorecardSnapshot(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A trade's score in one month. The current month is rewritten on each read;
    earlier months are frozen."""

    __tablename__ = "scorecard_snapshots"
    __table_args__ = (
        UniqueConstraint("vendor_id", "period", name="uq_scorecard_snapshots_period"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)  # first of the month
    overall: Mapped[int | None] = mapped_column(Integer)
    #: {"schedule": 82, "quality": null, ...}
    dimensions: Mapped[dict] = mapped_column(JSON, nullable=False)
