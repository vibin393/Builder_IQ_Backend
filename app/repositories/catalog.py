"""Data access for the configuration catalog.

Cost codes, trades, communities and their lots, plans and options - everything a
builder sets up before a home can be sold.
"""

import uuid

from app.models.community import Community, Lot
from app.models.cost_code import CostCode
from app.models.option import Option, OptionCostLine
from app.models.plan import Plan, PlanCostLine
from app.models.vendor import Vendor
from app.repositories.base import TenantRepository


class CostCodeRepository(TenantRepository[CostCode]):
    model = CostCode
    label = "Cost code"
    search_fields = ("code", "name", "division")

    async def all_ordered(self, *, include_archived: bool = False) -> list[CostCode]:
        return await self.list(order_by=CostCode.code, include_archived=include_archived)

    async def page(self, params, **filters):
        return await self.paginate(params, order_by=CostCode.code, **filters)

    async def code_taken_by_other(self, code: str, exclude_id) -> bool:
        """Uniqueness on edit: the record may keep its own code."""
        from sqlalchemy import func, select

        found = await self.db.scalar(
            select(CostCode.id).where(
                CostCode.tenant_id == self.tenant_id,
                func.lower(CostCode.code) == code.lower(),
                CostCode.id != exclude_id,
            )
        )
        return found is not None


class VendorRepository(TenantRepository[Vendor]):
    model = Vendor
    label = "Trade"
    search_fields = ("code", "name", "contact_name", "email")

    async def all_ordered(self, *, include_archived: bool = False) -> list[Vendor]:
        return await self.list(order_by=Vendor.name, include_archived=include_archived)

    async def page(self, params, **filters):
        return await self.paginate(params, order_by=Vendor.name, **filters)

    async def code_taken_by_other(self, code: str, exclude_id) -> bool:
        from sqlalchemy import func, select

        found = await self.db.scalar(
            select(Vendor.id).where(
                Vendor.tenant_id == self.tenant_id,
                func.lower(Vendor.code) == code.lower(),
                Vendor.id != exclude_id,
            )
        )
        return found is not None


class CommunityRepository(TenantRepository[Community]):
    model = Community
    label = "Community"

    async def all_ordered(self) -> list[Community]:
        return await self.list(order_by=Community.name)


class LotRepository(TenantRepository[Lot]):
    model = Lot
    label = "Lot"

    async def for_community(self, community_id: uuid.UUID) -> list[Lot]:
        return await self.list(community_id=community_id, order_by=Lot.lot_number)

    async def exists_in_community(self, community_id: uuid.UUID, lot_number: str) -> bool:
        """Lot numbers repeat across communities; within one they must not."""
        from sqlalchemy import func, select

        found = await self.db.scalar(
            select(Lot.id).where(
                Lot.tenant_id == self.tenant_id,
                Lot.community_id == community_id,
                func.lower(Lot.lot_number) == lot_number.lower(),
            )
        )
        return found is not None


class PlanRepository(TenantRepository[Plan]):
    model = Plan
    label = "Plan"

    async def all_ordered(self) -> list[Plan]:
        return await self.list(order_by=Plan.code)


class PlanCostLineRepository(TenantRepository[PlanCostLine]):
    model = PlanCostLine
    label = "Plan cost line"

    async def for_plan(self, plan_id: uuid.UUID) -> list[PlanCostLine]:
        return await self.list(plan_id=plan_id)


class OptionRepository(TenantRepository[Option]):
    model = Option
    label = "Option"
    # An option is never useful without its cost lines: they are what makes a
    # selection land on the right budget lines rather than in a lump sum.
    default_loads = ("cost_lines",)

    async def all_ordered(self) -> list[Option]:
        return await self.list(order_by=Option.code)

    async def with_cost_lines(self, option_id: uuid.UUID) -> Option:
        return await self.get(option_id, "cost_lines")


class OptionCostLineRepository(TenantRepository[OptionCostLine]):
    model = OptionCostLine
    label = "Option cost line"
