"""Vendors: subcontractors (trades) and material suppliers."""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.mixins import ArchivableMixin, AuditedMixin


class VendorType(StrEnum):
    TRADE = "trade"
    SUPPLIER = "supplier"
    BOTH = "both"


class CISStatus(StrEnum):
    """The trade's Construction Industry Scheme status, as verified with HMRC.

    A UK contractor paying a subcontractor must deduct tax from the labour part of each
    payment at the rate HMRC gives on verification, and pay it over monthly.
    """

    GROSS = "gross"            # gross payment status — no deduction
    NET = "net"                # registered — 20% deducted from labour
    UNVERIFIED = "unverified"  # not registered / not matched — 30% deducted from labour


#: Deduction rate per status, as a percentage of the labour element.
CIS_RATES = {
    CISStatus.GROSS: Decimal("0"),
    CISStatus.NET: Decimal("20"),
    CISStatus.UNVERIFIED: Decimal("30"),
}


class Vendor(UUIDMixin, TenantMixin, TimestampMixin, ArchivableMixin, AuditedMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_vendors_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor_type: Mapped[VendorType] = mapped_column(
        Enum(VendorType, name="vendor_type", values_callable=lambda e: [m.value for m in e]),
        default=VendorType.TRADE,
        nullable=False,
    )
    contact_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # What this trade charges an hour, used as the default on a timecard. Without
    # it the rate is retyped on every entry, and an average rate that drifts is
    # one nobody decided to change.
    labour_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # CIS: null means not yet verified with HMRC. Nothing is deducted while null, but
    # the invoice flags it — paying an unverified subcontractor gross is the builder's
    # liability if HMRC later says they should have been taxed at 30%.
    cis_status: Mapped[CISStatus | None] = mapped_column(
        Enum(CISStatus, name="vendor_cis_status", values_callable=lambda e: [m.value for m in e])
    )
    utr: Mapped[str | None] = mapped_column(String(20))   # Unique Taxpayer Reference

    # --- the business itself ---------------------------------------------------
    address: Mapped[str | None] = mapped_column(String(300))
    postcode: Mapped[str | None] = mapped_column(String(20))
    #: Companies House registration. A sole trader has none.
    company_number: Mapped[str | None] = mapped_column(String(8))
    vat_number: Mapped[str | None] = mapped_column(String(20))
    website: Mapped[str | None] = mapped_column(String(200))
    #: Agreed payment terms. Sets the due date on this trade's invoices; unset means
    #: the standard 30 days.
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(2000))

    def __repr__(self) -> str:
        return f"<Vendor {self.code} {self.name}>"


class VendorContact(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A person at the trade: the director who signs, the QS who prices, the site lead."""

    __tablename__ = "vendor_contacts"

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class VendorSpecialism(UUIDMixin, TenantMixin, TimestampMixin, Base):
    """A cost code this trade does. Answers "who can we ask to price the plastering?"."""

    __tablename__ = "vendor_specialisms"
    __table_args__ = (
        UniqueConstraint("vendor_id", "cost_code_id", name="uq_vendor_specialisms_pair"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    cost_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cost_codes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
