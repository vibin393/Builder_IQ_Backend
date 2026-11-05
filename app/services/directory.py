"""The unified directory.

Reads four tables — users, vendors, buyers, realtors — and returns them as one
normalised list of contacts. Read-only: every one of these has its own screen for
editing; this exists only to find a person across all of them at once.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contract import Buyer
from app.models.crm import Realtor
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.directory import ContactType, DirectoryContact

_TYPES: tuple[ContactType, ...] = ("team", "trade", "customer", "realtor")


def _like(term: str) -> str:
    return f"%{term.strip().lower()}%"


class DirectoryService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def _team(self, search: str | None) -> list[DirectoryContact]:
        stmt = select(User).where(User.tenant_id == self.tenant_id)
        if search:
            like = _like(search)
            stmt = stmt.where(
                or_(
                    User.full_name.ilike(like),
                    User.email.ilike(like),
                )
            )
        rows = await self.db.scalars(stmt.order_by(User.full_name))
        return [
            DirectoryContact(
                type="team",
                id=u.id,
                name=u.full_name,
                company=None,
                email=u.email,
                phone=u.phone,
                detail=(u.job_title or u.role.value.replace("_", " ").title()),
                status="active" if getattr(u, "is_active", True) else "inactive",
            )
            for u in rows
        ]

    async def _trades(self, search: str | None) -> list[DirectoryContact]:
        stmt = select(Vendor).where(
            Vendor.tenant_id == self.tenant_id, Vendor.is_archived.is_(False)
        )
        if search:
            like = _like(search)
            stmt = stmt.where(
                or_(
                    Vendor.name.ilike(like),
                    Vendor.contact_name.ilike(like),
                    Vendor.email.ilike(like),
                    Vendor.code.ilike(like),
                )
            )
        rows = await self.db.scalars(stmt.order_by(Vendor.name))
        return [
            DirectoryContact(
                type="trade",
                id=v.id,
                name=v.contact_name or v.name,
                company=v.name,
                email=v.email,
                phone=v.phone,
                detail=v.type.value.title() if getattr(v, "type", None) else None,
                code=v.code,
                status="active",
            )
            for v in rows
        ]

    async def _customers(self, search: str | None) -> list[DirectoryContact]:
        stmt = select(Buyer).where(Buyer.tenant_id == self.tenant_id)
        if search:
            like = _like(search)
            stmt = stmt.where(
                or_(
                    Buyer.first_name.ilike(like),
                    Buyer.last_name.ilike(like),
                    Buyer.email.ilike(like),
                )
            )
        rows = await self.db.scalars(stmt.order_by(Buyer.last_name, Buyer.first_name))
        return [
            DirectoryContact(
                type="customer",
                id=b.id,
                name=b.full_name,
                company=None,
                email=b.email,
                phone=b.phone,
                detail="Customer",
                status="active",
            )
            for b in rows
        ]

    async def _realtors(self, search: str | None) -> list[DirectoryContact]:
        stmt = select(Realtor).where(
            Realtor.tenant_id == self.tenant_id, Realtor.is_archived.is_(False)
        )
        if search:
            like = _like(search)
            stmt = stmt.where(
                or_(
                    Realtor.name.ilike(like),
                    Realtor.brokerage.ilike(like),
                    Realtor.email.ilike(like),
                    Realtor.code.ilike(like),
                )
            )
        rows = await self.db.scalars(stmt.order_by(Realtor.name))
        return [
            DirectoryContact(
                type="realtor",
                id=r.id,
                name=r.name,
                company=r.brokerage,
                email=r.email,
                phone=r.phone,
                detail=(f"Licence {r.licence_number}" if r.licence_number else "Realtor"),
                code=r.code,
                status="active",
            )
            for r in rows
        ]

    async def list_contacts(
        self, *, contact_type: ContactType | None = None, search: str | None = None
    ) -> list[DirectoryContact]:
        want = (contact_type,) if contact_type else _TYPES
        out: list[DirectoryContact] = []
        if "team" in want:
            out += await self._team(search)
        if "trade" in want:
            out += await self._trades(search)
        if "customer" in want:
            out += await self._customers(search)
        if "realtor" in want:
            out += await self._realtors(search)
        return out

    async def summary(self) -> dict:
        all_contacts = await self.list_contacts()
        counts = {t: 0 for t in _TYPES}
        for c in all_contacts:
            counts[c.type] += 1
        return {
            "total": len(all_contacts),
            "team": counts["team"],
            "trades": counts["trade"],
            "customers": counts["customer"],
            "realtors": counts["realtor"],
        }
