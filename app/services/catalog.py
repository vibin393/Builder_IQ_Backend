"""Business tier for the configuration catalog.

What a builder sets up before a home can be sold: cost codes, trades,
communities and lots, plans, options.

The rules here are small but they are rules, not plumbing:

  * a code must be unique within the builder, and case-insensitively so - a
    builder with both "03-100" and "03-100 " has two cost codes and one of them
    will silently collect the wrong money
  * a lot number is unique within its community, not across the business. Every
    community has a Lot 1.
  * anything referencing another record must reference one this builder owns,
    checked before the write rather than left to a foreign key to reject

Nothing in this module imports FastAPI or SQLAlchemy. It raises domain errors;
the API tier decides what those mean in HTTP.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.repositories.base import Conflict
from app.repositories.catalog import (
    CommunityRepository,
    CostCodeRepository,
    LotRepository,
    OptionCostLineRepository,
    OptionRepository,
    PlanCostLineRepository,
    PlanRepository,
    VendorRepository,
)
from app.schemas.common import PageParams


class CatalogService:
    """Every catalog operation, scoped to one builder for its whole lifetime."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.cost_codes = CostCodeRepository(db, tenant_id)
        self.vendors = VendorRepository(db, tenant_id)
        self.communities = CommunityRepository(db, tenant_id)
        self.lots = LotRepository(db, tenant_id)
        self.plans = PlanRepository(db, tenant_id)
        self.plan_cost_lines = PlanCostLineRepository(db, tenant_id)
        self.options = OptionRepository(db, tenant_id)
        self.option_cost_lines = OptionCostLineRepository(db, tenant_id)

    # -- cost codes ---------------------------------------------------------

    async def list_cost_codes(self, params: PageParams | None = None):
        if params is None:
            return await self.cost_codes.all_ordered()
        return await self.cost_codes.page(params)

    async def get_cost_code(self, code_id: uuid.UUID):
        return await self.cost_codes.get(code_id)

    async def create_cost_code(self, *, actor_id: uuid.UUID | None = None, **data):
        await self.cost_codes.require_unique_code(data["code"])
        return await self.cost_codes.create(created_by_id=actor_id, **data)

    async def update_cost_code(self, code_id: uuid.UUID, *, actor_id=None, **data):
        """Correct a cost code. The code itself is editable on purpose - a
        mistyped cost code is the most expensive typo in this system, because
        everything reconciles through it."""
        obj = await self.cost_codes.get(code_id)
        new_code = data.get("code")
        if new_code and await self.cost_codes.code_taken_by_other(new_code, code_id):
            raise Conflict(f"Cost code {new_code} already exists.")
        return await self.cost_codes.update(obj, updated_by_id=actor_id, **data)

    async def archive_cost_code(self, code_id: uuid.UUID, actor_id, reason=None):
        """Retire a cost code.

        Refused while anything still points at it. Archiving a code that budget
        lines and purchase orders reference would hide it from the pickers while
        money continued to land on it - the worst of both.
        """
        obj = await self.cost_codes.get(code_id)
        in_use = await self._cost_code_usage(code_id)
        if in_use:
            raise BusinessRuleViolation(
                f"Cost code {obj.code} is still used by {in_use}.",
                "Move that work to another code first, or mark this one inactive "
                "instead of archiving it.",
            )
        return await self.cost_codes.archive(obj, actor_id, reason)

    async def restore_cost_code(self, code_id: uuid.UUID):
        return await self.cost_codes.restore(
            await self.cost_codes.get(code_id)
        )

    async def _cost_code_usage(self, code_id: uuid.UUID) -> str:
        """A readable summary of what still references this code, or ""."""
        from sqlalchemy import func, select

        from app.models.bidding import BidPackage
        from app.models.budget import BudgetLine
        from app.models.change_order import ChangeOrderLine
        from app.models.invoice import InvoiceLine
        from app.models.option import OptionCostLine
        from app.models.plan import PlanCostLine
        from app.models.purchase_order import PurchaseOrderLine

        # Everywhere money is priced, committed or spent against a code. This checked
        # only budgets and POs, so a code still on a plan's standard costs could be
        # archived - and the next budget generated from that plan landed on it anyway.
        counts = []
        pairs = (
            (BudgetLine, "budget line"),
            (PurchaseOrderLine, "purchase order line"),
            (InvoiceLine, "invoice line"),
            (PlanCostLine, "plan standard cost"),
            (OptionCostLine, "option cost line"),
            (ChangeOrderLine, "change order line"),
            (BidPackage, "bid package"),
        )
        for model, noun in pairs:
            n = await self.db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == self.tenant_id, model.cost_code_id == code_id)
            ) or 0
            if n:
                counts.append(f"{n} {noun}{'s' if n != 1 else ''}")
        return " and ".join(counts)

    # -- trades -------------------------------------------------------------

    async def list_vendors(self, params: PageParams | None = None):
        if params is None:
            return await self.vendors.all_ordered()
        return await self.vendors.page(params)

    async def get_vendor(self, vendor_id: uuid.UUID):
        return await self.vendors.get(vendor_id)

    async def create_vendor(self, *, actor_id: uuid.UUID | None = None, **data):
        await self.vendors.require_unique_code(data["code"])
        # EmailStr is a rich type; the column is a string. Coercing here keeps
        # the repository free of schema concerns.
        if data.get("email"):
            data["email"] = str(data["email"])
        return await self.vendors.create(created_by_id=actor_id, **data)

    async def update_vendor(self, vendor_id: uuid.UUID, *, actor_id=None, **data):
        obj = await self.vendors.get(vendor_id)
        new_code = data.get("code")
        if new_code and await self.vendors.code_taken_by_other(new_code, vendor_id):
            raise Conflict(f"Trade {new_code} already exists.")
        if data.get("email"):
            data["email"] = str(data["email"])
        return await self.vendors.update(obj, updated_by_id=actor_id, **data)

    async def archive_vendor(self, vendor_id: uuid.UUID, actor_id, reason=None):
        """Retire a trade.

        Allowed even with history - a trade you no longer use still holds every
        purchase order they ever had, and that history must survive. What
        archiving stops is them appearing in a picker. Open commitments are a
        different matter: those are live work.
        """
        obj = await self.vendors.get(vendor_id)
        open_pos = await self._open_commitments(vendor_id)
        if open_pos:
            raise BusinessRuleViolation(
                f"{obj.name} still holds {open_pos} open purchase order"
                f"{'s' if open_pos != 1 else ''}.",
                "Close or void them first - archiving a trade mid-job hides them "
                "from the people who need to chase them.",
            )
        return await self.vendors.archive(obj, actor_id, reason)

    async def restore_vendor(self, vendor_id: uuid.UUID):
        return await self.vendors.restore(await self.vendors.get(vendor_id))

    async def _open_commitments(self, vendor_id: uuid.UUID) -> int:
        from sqlalchemy import func, select

        from app.models.purchase_order import PurchaseOrder, PurchaseOrderStatus

        return await self.db.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(
                PurchaseOrder.tenant_id == self.tenant_id,
                PurchaseOrder.vendor_id == vendor_id,
                PurchaseOrder.status.in_(
                    (PurchaseOrderStatus.ISSUED, PurchaseOrderStatus.ACKNOWLEDGED)
                ),
            )
        ) or 0
