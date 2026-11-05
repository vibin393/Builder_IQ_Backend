"""Data access for safety and quality.

These four lived as raw SELECTs in the router, which is how the tenant filter ends
up being something somebody remembers rather than something the type guarantees.
"""

import uuid
from datetime import date

from sqlalchemy import func, or_, select

from app.models.safety import (
    ActionPlan,
    ActionPlanItem,
    ActionPlanStatus,
    Incident,
    Observation,
    ObservationStatus,
    RiddorCategory,
)
from app.repositories.base import NotFound, TenantRepository

#: Observation states that still owe somebody something. Ready-for-review is in
#: here on purpose: a claimed fix nobody has looked at is not a closed one.
OPEN_OBSERVATIONS = (
    ObservationStatus.INITIATED,
    ObservationStatus.READY_FOR_REVIEW,
    ObservationStatus.NOT_ACCEPTED,
)


class ObservationRepository(TenantRepository[Observation]):
    model = Observation
    label = "Observation"
    search_fields = ("title", "description", "location")

    async def open_for_job(self, job_id: uuid.UUID) -> list[Observation]:
        return list(
            await self.db.scalars(
                self._base()
                .where(
                    Observation.job_id == job_id,
                    Observation.status.in_(OPEN_OBSERVATIONS),
                )
                .order_by(Observation.due_on.nulls_last(), Observation.id)
            )
        )

    async def counts(self, job_id: uuid.UUID | None = None) -> dict[str, int]:
        """The numbers above the list, in one query rather than four.

        Counted in SQL rather than by loading the rows and filtering in Python,
        because the list is paged and the headline numbers are about everything.
        """
        today = date.today()
        open_clause = Observation.status.in_(OPEN_OBSERVATIONS)
        stmt = select(
            func.count().filter(open_clause).label("open"),
            func.count()
            .filter(Observation.status == ObservationStatus.READY_FOR_REVIEW)
            .label("awaiting_review"),
            func.count()
            .filter(
                open_clause,
                Observation.due_on.is_not(None),
                Observation.due_on < today,
            )
            .label("overdue"),
            func.count().filter(open_clause, Observation.severity == "critical").label("critical"),
            func.count().filter(Observation.inspection_id.is_not(None)).label("from_inspections"),
        ).where(Observation.tenant_id == self.tenant_id)
        if job_id is not None:
            stmt = stmt.where(Observation.job_id == job_id)
        row = (await self.db.execute(stmt)).one()
        return {
            "open_observations": row.open or 0,
            "awaiting_review": row.awaiting_review or 0,
            "overdue": row.overdue or 0,
            "critical": row.critical or 0,
            "from_inspections": row.from_inspections or 0,
        }

    async def for_inspection(self, inspection_id: uuid.UUID) -> list[Observation]:
        return list(
            await self.db.scalars(self._base().where(Observation.inspection_id == inspection_id))
        )


class IncidentRepository(TenantRepository[Incident]):
    model = Incident
    label = "Incident"
    search_fields = ("title", "description", "person_involved", "location")

    async def counts(self, job_id: uuid.UUID | None = None) -> dict[str, int]:
        today = date.today()
        reportable = Incident.riddor_category != RiddorCategory.NOT_REPORTABLE
        unreported = Incident.reported_to_hse_on.is_(None)
        stmt = select(
            func.count().label("total"),
            func.count().filter(reportable).label("reportable"),
            func.count().filter(reportable, unreported).label("awaiting_report"),
            func.count().filter(Incident.lost_time).label("lost_time"),
            func.count().filter(Incident.incident_type == "near_miss").label("near_misses"),
            func.count().filter(Incident.closed_on.is_(None)).label("open"),
        ).where(Incident.tenant_id == self.tenant_id)
        if job_id is not None:
            stmt = stmt.where(Incident.job_id == job_id)
        row = (await self.db.execute(stmt)).one()

        # The overdue count needs the per-category deadline, which lives in Python
        # because it is a rule rather than a column. Only the unreported
        # reportable ones are loaded, which is a handful by construction - if it
        # ever is not, the business has a much larger problem than this query.
        pending = await self.db.scalars(
            self._base().where(
                Incident.riddor_category != RiddorCategory.NOT_REPORTABLE,
                Incident.reported_to_hse_on.is_(None),
            )
            if job_id is None
            else self._base().where(
                Incident.job_id == job_id,
                Incident.riddor_category != RiddorCategory.NOT_REPORTABLE,
                Incident.reported_to_hse_on.is_(None),
            )
        )
        overdue = sum(1 for i in pending if i.riddor_deadline and i.riddor_deadline < today)
        return {
            "incidents": row.total or 0,
            "open_incidents": row.open or 0,
            "reportable": row.reportable or 0,
            "awaiting_hse_report": row.awaiting_report or 0,
            "hse_report_overdue": overdue,
            "lost_time": row.lost_time or 0,
            "near_misses": row.near_misses or 0,
        }

    async def awaiting_report(self) -> list[Incident]:
        """Reportable incidents the HSE has not been told about."""
        return list(
            await self.db.scalars(
                self._base()
                .where(
                    Incident.riddor_category != RiddorCategory.NOT_REPORTABLE,
                    Incident.reported_to_hse_on.is_(None),
                )
                .order_by(Incident.occurred_on, Incident.id)
            )
        )


class ActionPlanRepository(TenantRepository[ActionPlan]):
    model = ActionPlan
    label = "Action plan"
    default_loads = ("items",)
    search_fields = ("name", "description")

    async def get(self, obj_id: uuid.UUID, *loads: str) -> ActionPlan:
        """Re-read the plan and its steps, ignoring what the session already holds.

        A plan is almost always fetched straight after its steps were changed -
        added, completed, signed off - and SQLAlchemy's identity map would hand
        back the collection as it was when first loaded. The route then answers
        200 with the step it has just created missing from the response, which
        reads to the caller as the write having silently done nothing.
        """
        obj = await self.db.scalar(
            self._base(*loads)
            .where(ActionPlan.id == obj_id)
            .execution_options(populate_existing=True)
        )
        if obj is None:
            raise NotFound(self.label, obj_id)
        return obj

    async def templates(self) -> list[ActionPlan]:
        return list(
            await self.db.scalars(
                self._base().where(ActionPlan.is_template.is_(True)).order_by(ActionPlan.name)
            )
        )

    async def counts(self, job_id: uuid.UUID | None = None) -> dict[str, int]:
        today = date.today()
        live = ActionPlan.is_template.is_(False)
        unfinished = ActionPlan.status != ActionPlanStatus.COMPLETE
        stmt = select(
            func.count().filter(live).label("total"),
            func.count().filter(live, unfinished).label("in_progress"),
            func.count()
            .filter(
                live,
                unfinished,
                ActionPlan.due_on.is_not(None),
                ActionPlan.due_on < today,
            )
            .label("overdue"),
            func.count().filter(ActionPlan.is_template.is_(True)).label("templates"),
        ).where(ActionPlan.tenant_id == self.tenant_id)
        if job_id is not None:
            stmt = stmt.where(or_(ActionPlan.job_id == job_id, ActionPlan.is_template.is_(True)))
        row = (await self.db.execute(stmt)).one()
        return {
            "action_plans": row.total or 0,
            "plans_in_progress": row.in_progress or 0,
            "plans_overdue": row.overdue or 0,
            "plan_templates": row.templates or 0,
        }


class ActionPlanItemRepository(TenantRepository[ActionPlanItem]):
    model = ActionPlanItem
    label = "Action plan step"
