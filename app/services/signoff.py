"""Materials selection signoff: the homeowner's approval of chosen materials.

This completes the selection story. A selection already carried a builder-side
`status` (pending → confirmed → cascaded into the budget). What it could not say
was whether the *customer* had approved the material — so an approval was a thing
that happened in a design-studio conversation and lived nowhere.

Here it lives. The builder sends a job's selections for approval with a deadline;
the buyer approves each with a typed signature, or declines with a reason. The
approval track is kept apart from the contractual status on purpose: a decline is
a buyer saying "not that one", not a budget movement, and conflating the two would
let a decline look like a confirmed cost.
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BusinessRuleViolation
from app.models.selection import Selection, SelectionApprovalStatus, SelectionChoice
from app.repositories.base import NotFound

# A selection can be (re)sent for approval from these states. An already-awaiting
# or already-approved choice is left alone rather than having its clock reset.
_SENDABLE = (SelectionApprovalStatus.NOT_SUBMITTED, SelectionApprovalStatus.DECLINED)
# A buyer can answer a choice that is awaiting them, or one sent straight to signoff
# in person (not_submitted) — but not re-answer one already decided.
_ANSWERABLE = (SelectionApprovalStatus.NOT_SUBMITTED, SelectionApprovalStatus.AWAITING)


class SignoffService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id

    # -- reads ---------------------------------------------------------------

    def _base(self):
        return (
            select(Selection)
            .options(selectinload(Selection.option), selectinload(Selection.choices))
            .where(Selection.tenant_id == self.tenant_id)
        )

    async def _get(self, selection_id: uuid.UUID) -> Selection:
        # populate_existing so a choices collection changed in this session reloads.
        sel = await self.db.scalar(
            self._base()
            .where(Selection.id == selection_id)
            .execution_options(populate_existing=True)
        )
        if sel is None:
            raise NotFound("Selection", selection_id)
        return sel

    # -- choices & allowance -------------------------------------------------

    async def set_allowance(self, selection_id: uuid.UUID, **data) -> Selection:
        sel = await self._get(selection_id)
        for field, value in data.items():
            if value is not None:
                setattr(sel, field, value)
        await self.db.commit()
        return await self._get(selection_id)

    async def add_choice(self, selection_id: uuid.UUID, **data) -> Selection:
        await self._get(selection_id)
        self.db.add(SelectionChoice(tenant_id=self.tenant_id, selection_id=selection_id, **data))
        await self.db.commit()
        return await self._get(selection_id)

    async def _choice(self, choice_id: uuid.UUID) -> SelectionChoice:
        c = await self.db.scalar(
            select(SelectionChoice).where(
                SelectionChoice.tenant_id == self.tenant_id, SelectionChoice.id == choice_id
            )
        )
        if c is None:
            raise NotFound("Selection choice", choice_id)
        return c

    async def update_choice(self, choice_id: uuid.UUID, **data) -> Selection:
        c = await self._choice(choice_id)
        for field, value in data.items():
            setattr(c, field, value)
        await self.db.commit()
        return await self._get(c.selection_id)

    async def delete_choice(self, choice_id: uuid.UUID) -> Selection:
        c = await self._choice(choice_id)
        selection_id = c.selection_id
        await self.db.delete(c)
        await self.db.commit()
        return await self._get(selection_id)

    async def select_choice(self, choice_id: uuid.UUID) -> Selection:
        """Mark one choice as the buyer's pick; clears the others on the selection."""
        c = await self._choice(choice_id)
        sel = await self._get(c.selection_id)
        for other in sel.choices:
            other.is_selected = other.id == choice_id
        await self.db.commit()
        return await self._get(c.selection_id)

    async def list_for_job(
        self,
        job_id: uuid.UUID,
        approval_status: SelectionApprovalStatus | None = None,
    ) -> list[Selection]:
        stmt = self._base().where(Selection.job_id == job_id)
        if approval_status is not None:
            stmt = stmt.where(Selection.approval_status == approval_status)
        stmt = stmt.order_by(Selection.approval_status, Selection.selected_date.desc())
        return list(await self.db.scalars(stmt))

    async def summary(self, job_id: uuid.UUID | None = None) -> dict:
        S = SelectionApprovalStatus
        today = date.today()
        stmt = select(
            func.count().label("total"),
            func.count()
            .filter(Selection.approval_status == S.NOT_SUBMITTED)
            .label("not_submitted"),
            func.count().filter(Selection.approval_status == S.AWAITING).label("awaiting"),
            func.count().filter(Selection.approval_status == S.APPROVED).label("approved"),
            func.count().filter(Selection.approval_status == S.DECLINED).label("declined"),
            func.count()
            .filter(
                Selection.approval_status == S.AWAITING,
                Selection.approval_deadline.is_not(None),
                Selection.approval_deadline < today,
            )
            .label("overdue"),
        ).where(Selection.tenant_id == self.tenant_id)
        if job_id is not None:
            stmt = stmt.where(Selection.job_id == job_id)
        row = (await self.db.execute(stmt)).one()
        return {
            "job_id": job_id,
            "total": row.total or 0,
            "not_submitted": row.not_submitted or 0,
            "awaiting": row.awaiting or 0,
            "approved": row.approved or 0,
            "declined": row.declined or 0,
            "overdue": row.overdue or 0,
        }

    # -- writes --------------------------------------------------------------

    async def request_approval(
        self,
        job_id: uuid.UUID,
        *,
        deadline: date | None = None,
        selection_ids: list[uuid.UUID] | None = None,
    ) -> list[Selection]:
        """Send a job's selections to the buyer. Returns the ones actually sent.

        Idempotent by design: a selection already awaiting or approved is skipped,
        not reset, so re-sending the sheet after adding one choice does not restart
        everyone else's clock.
        """
        if deadline is not None and deadline < date.today():
            raise BusinessRuleViolation("The approval deadline cannot be in the past.")

        stmt = self._base().where(
            Selection.job_id == job_id,
            Selection.approval_status.in_(_SENDABLE),
        )
        if selection_ids:
            stmt = stmt.where(Selection.id.in_(selection_ids))
        selections = list(await self.db.scalars(stmt))
        if not selections:
            raise BusinessRuleViolation(
                "No selections are waiting to be sent for approval on this job."
            )

        now = datetime.now(UTC)
        for sel in selections:
            sel.approval_status = SelectionApprovalStatus.AWAITING
            sel.approval_requested_at = now
            sel.approval_deadline = deadline
            # A fresh request clears any prior decline so the record reads cleanly.
            sel.decline_reason = None
            sel.decided_at = None
        await self.db.commit()
        return await self.list_for_job(job_id, SelectionApprovalStatus.AWAITING)

    async def approve(
        self, selection_id: uuid.UUID, *, signature: str, comment: str | None = None
    ) -> Selection:
        sel = await self._get(selection_id)
        if sel.approval_status not in _ANSWERABLE:
            raise BusinessRuleViolation(
                f"This selection has already been {sel.approval_status.value}."
            )
        sel.approval_status = SelectionApprovalStatus.APPROVED
        sel.buyer_signature = signature.strip()
        sel.buyer_comment = comment
        sel.decline_reason = None
        sel.decided_at = datetime.now(UTC)
        await self.db.commit()
        return await self._get(selection_id)

    async def decline(
        self,
        selection_id: uuid.UUID,
        *,
        reason: str,
        signature: str | None = None,
        comment: str | None = None,
    ) -> Selection:
        sel = await self._get(selection_id)
        if sel.approval_status not in _ANSWERABLE:
            raise BusinessRuleViolation(
                f"This selection has already been {sel.approval_status.value}."
            )
        sel.approval_status = SelectionApprovalStatus.DECLINED
        sel.decline_reason = reason.strip()
        sel.buyer_signature = signature.strip() if signature else None
        sel.buyer_comment = comment
        sel.decided_at = datetime.now(UTC)
        await self.db.commit()
        return await self._get(selection_id)

    async def reset(self, selection_id: uuid.UUID) -> Selection:
        """Pull a choice back out of signoff, e.g. to correct it before re-sending."""
        sel = await self._get(selection_id)
        sel.approval_status = SelectionApprovalStatus.NOT_SUBMITTED
        sel.approval_requested_at = None
        sel.approval_deadline = None
        sel.decided_at = None
        sel.buyer_signature = None
        sel.decline_reason = None
        sel.buyer_comment = None
        await self.db.commit()
        return await self._get(selection_id)
