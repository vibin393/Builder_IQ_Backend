"""Cost codes: the shared key across estimating, purchasing, job costing and accounting.

Everything that touches money references a cost code. Changing this taxonomy later
invalidates historical comparison, so it is deliberately simple and tenant-owned.
"""

from enum import StrEnum

from sqlalchemy import Boolean, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.mixins import ArchivableMixin, AuditedMixin


class CostType(StrEnum):
    """What kind of money lands on the code. Labour and subcontract are where CIS and
    hours apply; materials and plant are where supplier prices move."""

    LABOUR = "labour"
    MATERIALS = "materials"
    SUBCONTRACT = "subcontract"
    PLANT = "plant"
    PRELIMINARIES = "preliminaries"  # site set-up, welfare, scaffolding, management
    FEES = "fees"  # design, surveys, warranty provider, building control
    OTHER = "other"


class CostCode(UUIDMixin, TenantMixin, TimestampMixin, ArchivableMixin, AuditedMixin, Base):
    __tablename__ = "cost_codes"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_cost_codes_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    division: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Nominal code in the accounting package. Unset, cost posts to the default cost
    # account in accounting settings — still balanced, but not analysed.
    gl_account: Mapped[str | None] = mapped_column(String(20))
    cost_type: Mapped[CostType | None] = mapped_column(
        Enum(CostType, name="cost_code_type", values_callable=lambda e: [m.value for m in e])
    )

    def __repr__(self) -> str:
        return f"<CostCode {self.code} {self.name}>"
