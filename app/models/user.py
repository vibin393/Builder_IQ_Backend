"""Platform users. Internal staff and external trade contacts share this table."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class UserRole(StrEnum):
    """Who can log in, modelled on how both source products divide a builder.

    BuildTopia splits the company by department - sales, purchasing, construction,
    warranty, finance, management. Procore splits by *relationship to the project*
    and tags every template internal or external. Both distinctions matter, so both
    are here: the role names follow the departments, and `is_external` carries the
    Procore idea that some of these people do not work for you.

    ARCHITECT exists because the RFI and submittal models already route the ball to
    an architect or engineer. Modelling a destination nobody can log in to was a gap,
    not a design.
    """

    # --- internal: the builder's own staff ---------------------------------
    OWNER = "owner"
    ADMIN = "admin"
    EXECUTIVE = "executive"            # reads everything, changes nothing
    SALES = "sales"
    PURCHASING = "purchasing"
    ACCOUNTING = "accounting"          # AP, invoices, GL export
    SUPERINTENDENT = "superintendent"
    WARRANTY = "warranty"              # customer care after handover

    # --- external: they work with you, not for you -------------------------
    ARCHITECT = "architect"            # answers RFIs, reviews submittals
    TRADE = "trade"                    # subcontractor; sees only their own work
    HOMEOWNER = "homeowner"            # the buyer; sees only their own home


# Anyone the builder does not employ. They share the tenant but must never see
# margin, budgets or another party's records.
EXTERNAL_ROLES = (UserRole.ARCHITECT, UserRole.TRADE, UserRole.HOMEOWNER)

# Internal staff whose job is to read rather than change.
READ_ONLY_ROLES = (UserRole.EXECUTIVE,)


class User(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        default=UserRole.SALES,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- profile, editable by the user themselves ---------------------------
    # Path under /uploads, not the image bytes. Storing pictures in the database
    # would bloat every /auth/me response the app makes on each page load.
    avatar_url: Mapped[str | None] = mapped_column(String(300))
    job_title: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    #: Set on each successful sign-in. An account nobody has used in months is a
    #: leaver whose access was never taken away.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_external(self) -> bool:
        """They work with the builder, not for them."""
        return self.role in EXTERNAL_ROLES

    @property
    def is_read_only(self) -> bool:
        return self.role in READ_ONLY_ROLES

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"
