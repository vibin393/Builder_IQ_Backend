"""Labour and productivity.

The Timecard docstring states the purpose exactly: "the budget says a house should
take so many framing hours, and this says what it actually took." Everything here
serves the comparison in that sentence, and the comparison was the half the screen
could not do - it showed hours and cost by cost code, which tells you what you
spent, which you already knew.

Three things follow from taking the sentence seriously.

**A variance needs a target.** So the budget carries hours, and they come from the
plan rather than being typed per house. Where no target exists the row says so
rather than showing a variance against zero, because a variance against zero is
just the actual with a minus sign in front of it.

**Hours with no rate are not free hours.** The cost of an unrated entry is
unknown, not nought. Counting it as nought leaves the hours in the denominator and
nothing in the numerator, so the average rate falls every time somebody forgets a
rate - and the number that moves is the one people quote.

**An approval needs an author and a moment.** The column existed with nothing to
set it. Hours that nobody has checked are a claim about what a trade is owed, and
a claim is not a cost.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.budget import BudgetLine
from app.models.safety import Timecard
from app.repositories.base import Conflict
from app.repositories.job import BudgetRepository, JobRepository
from app.repositories.labour import TimecardRepository
from app.schemas.common import PageParams

ZERO = Decimal("0")

#: Overtime multiplier. Time and a half is the common default; it is a constant
#: rather than a literal buried in the model so there is one place to argue about.
OVERTIME_MULTIPLIER = Decimal("1.5")

#: The most one person can be booked for in a day, across every home. Not 24:
#: sixteen already implies a double shift, and every entry above it that I have
#: ever seen was the same shift typed twice.
MAX_DAY_HOURS = Decimal("16")


class LabourService:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.cards = TimecardRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)
        self.budgets = BudgetRepository(db, tenant_id)

    # ================================================== timecards =====

    async def list_timecards(self, params: PageParams, **filters):
        return await self.cards.paginate(
            params, order_by=Timecard.worked_on.desc(), **filters
        )

    async def record(self, *, actor_id=None, **data) -> Timecard:
        """Book hours against a home and a cost code."""
        await self.jobs.require_exists(data["job_id"])
        data["worked_on"] = data.get("worked_on") or date.today()
        await self._validate(data)
        data["hourly_rate"] = await self._rate_for(data)
        return await self.cards.create(entered_by_id=actor_id, **data)

    async def amend(self, card_id: uuid.UUID, **data) -> Timecard:
        """Correct an entry.

        Refused once it has been approved. Somebody signed the hours off; changing
        them underneath that signature makes the approval a record of something
        that no longer exists. Withdraw the approval first, deliberately.
        """
        card = await self.cards.get(card_id)
        if card.approved_on:
            raise Conflict(
                f"{card.worker_name}'s hours for {card.worked_on} have been approved. "
                "Withdraw the approval before changing them."
            )
        merged = {
            "worker_name": data.get("worker_name") or card.worker_name,
            "worked_on": data.get("worked_on") or card.worked_on,
            "hours": data.get("hours") if data.get("hours") is not None else card.hours,
            "overtime_hours": (
                data["overtime_hours"]
                if data.get("overtime_hours") is not None
                else card.overtime_hours
            ),
        }
        await self._validate(merged, exclude=card.id)
        return await self.cards.update(card, **data)

    async def delete(self, card_id: uuid.UUID) -> None:
        card = await self.cards.get(card_id)
        if card.approved_on:
            raise Conflict(
                f"{card.worker_name}'s hours for {card.worked_on} have been approved "
                "and cannot be deleted. Withdraw the approval first."
            )
        await self.db.delete(card)
        await self.db.commit()

    async def _validate(self, data: dict, exclude: uuid.UUID | None = None) -> None:
        worked_on = data["worked_on"]
        if worked_on > date.today():
            raise BusinessRuleViolation(
                "Hours cannot be booked for a day that has not happened.",
                "This records work that was done, not work that is planned - the "
                "schedule is where the plan lives.",
            )

        hours = Decimal(str(data.get("hours") or 0))
        overtime = Decimal(str(data.get("overtime_hours") or 0))
        if hours <= ZERO:
            raise BusinessRuleViolation(
                "An entry needs some hours on it.",
                "Nought hours is not a record of anything.",
            )
        if overtime < ZERO:
            raise BusinessRuleViolation("Overtime cannot be negative.", "")

        # Across every home, not just this one.
        name = data.get("worker_name") or ""
        if name.strip():
            same_day = await self.cards.for_worker_day(
                worker_name=name, worked_on=worked_on, exclude=exclude
            )
            already = sum((c.total_hours for c in same_day), ZERO)
            if already + hours + overtime > MAX_DAY_HOURS:
                where = ", ".join(sorted({str(c.job_id)[:8] for c in same_day}))
                raise BusinessRuleViolation(
                    f"{name.strip()} would be booked for "
                    f"{already + hours + overtime} hours on {worked_on}.",
                    f"{already} are already booked that day"
                    + (f" (homes {where})" if where else "")
                    + ". Above sixteen it is nearly always the same shift entered "
                    "twice - check before adding more.",
                )

    async def _rate_for(self, data: dict) -> Decimal | None:
        """The rate to book at.

        Whatever was typed, otherwise the trade's standard rate. Falling back to
        the vendor is what stops the rate being retyped forty times a week, and
        retyping is how an average rate drifts without anybody deciding it should.
        """
        if data.get("hourly_rate") is not None:
            return data["hourly_rate"]
        if not data.get("vendor_id"):
            return None
        from app.repositories.catalog import VendorRepository

        vendor = await VendorRepository(self.db, self.tenant_id).find(data["vendor_id"])
        return vendor.labour_rate if vendor else None

    # ================================================== approval =====

    async def approve(self, card_id: uuid.UUID, *, actor_id: uuid.UUID) -> Timecard:
        """Sign the hours off.

        Refused on an entry with no rate: approving it would turn unknown money
        into nought money, and the nought is the one that reaches the job cost.
        """
        card = await self.cards.get(card_id)
        if card.approved_on:
            raise Conflict(
                f"{card.worker_name}'s hours for {card.worked_on} are already approved."
            )
        if card.hourly_rate is None:
            raise BusinessRuleViolation(
                f"No rate on {card.worker_name}'s hours for {card.worked_on}.",
                "Approving it would book the hours at nothing, which reads as free "
                "labour rather than as a missing rate. Set the rate, or set the "
                "trade's standard rate so it fills itself in.",
            )
        card.approved_on = date.today()
        card.approved_by_id = actor_id
        await self.db.commit()
        return await self.cards.get(card_id)

    async def withdraw_approval(self, card_id: uuid.UUID) -> Timecard:
        card = await self.cards.get(card_id)
        if not card.approved_on:
            raise Conflict(
                f"{card.worker_name}'s hours for {card.worked_on} are not approved."
            )
        card.approved_on = None
        card.approved_by_id = None
        await self.db.commit()
        return await self.cards.get(card_id)

    async def approve_many(self, card_ids: list[uuid.UUID], *, actor_id: uuid.UUID) -> dict:
        """Sign off a batch, reporting what could not be done rather than stopping.

        A week's timecards are approved together. Refusing the whole batch because
        one of forty has no rate means the other thirty-nine wait, and whoever is
        doing it at five on a Friday will approve them one at a time instead.
        """
        approved: list[Timecard] = []
        skipped: list[dict] = []
        for card_id in card_ids:
            try:
                approved.append(await self.approve(card_id, actor_id=actor_id))
            except (BusinessRuleViolation, Conflict) as exc:
                skipped.append(
                    {
                        "timecard_id": card_id,
                        "reason": getattr(exc, "message", None) or str(exc),
                    }
                )
        return {"approved": approved, "skipped": skipped}

    # ================================================ productivity =====

    async def productivity(self, job_id: uuid.UUID | None = None) -> dict:
        """Hours and cost by cost code, against what the home was budgeted.

        The whole point of the feature, and the half that was missing.
        """
        if job_id is not None:
            await self.jobs.require_exists(job_id)
        cards = await self.cards.for_scope(job_id=job_id)
        targets = await self._budgeted(job_id)

        from app.repositories.catalog import CostCodeRepository

        codes = {c.id: c for c in await CostCodeRepository(self.db, self.tenant_id).list()}

        buckets: dict[uuid.UUID | None, dict] = {}
        for card in cards:
            b = buckets.setdefault(
                card.cost_code_id,
                {
                    "hours": ZERO,
                    "cost": ZERO,
                    "entries": 0,
                    "unrated_hours": ZERO,
                    "pending_hours": ZERO,
                },
            )
            b["hours"] += card.total_hours
            b["entries"] += 1
            if card.labour_cost is None:
                # The hours are real; the money is unknown. Kept apart so the
                # average rate below is not quietly diluted by them.
                b["unrated_hours"] += card.total_hours
            else:
                b["cost"] += card.labour_cost
            if not card.approved_on:
                b["pending_hours"] += card.total_hours

        rows = []
        for key, b in buckets.items():
            code = codes.get(key) if key else None
            budget_hours, budget_cost = targets.get(key, (None, None))
            rated_hours = b["hours"] - b["unrated_hours"]
            rows.append(
                {
                    "cost_code_id": key,
                    "cost_code": code.code if code else "—",
                    "cost_code_name": code.name if code else "Unassigned",
                    "hours": b["hours"],
                    "labour_cost": b["cost"],
                    "entries": b["entries"],
                    "unrated_hours": b["unrated_hours"],
                    "pending_hours": b["pending_hours"],
                    "budgeted_hours": budget_hours,
                    "budgeted_cost": budget_cost,
                    "hours_variance": (
                        b["hours"] - budget_hours if budget_hours is not None else None
                    ),
                    "cost_variance": (
                        b["cost"] - budget_cost if budget_cost is not None else None
                    ),
                    "average_rate": _divide(b["cost"], rated_hours),
                }
            )

        # Over budget first: the point of the screen is what is running away, and
        # sorting by hours puts the biggest trade at the top whether or not it is
        # in any trouble.
        rows.sort(
            key=lambda r: (
                -(r["hours_variance"] or ZERO),
                -r["hours"],
            )
        )

        total_hours = sum((r["hours"] for r in rows), ZERO)
        total_cost = sum((r["labour_cost"] for r in rows), ZERO)
        total_unrated = sum((r["unrated_hours"] for r in rows), ZERO)
        budgeted_hours = sum(
            (r["budgeted_hours"] for r in rows if r["budgeted_hours"] is not None), ZERO
        )
        budgeted_cost = sum(
            (r["budgeted_cost"] for r in rows if r["budgeted_cost"] is not None), ZERO
        )
        return {
            "job_id": job_id,
            "total_hours": total_hours,
            "total_labour_cost": total_cost,
            "overtime_hours": sum((c.overtime_hours for c in cards), ZERO),
            "unrated_hours": total_unrated,
            "pending_hours": sum(
                (c.total_hours for c in cards if not c.approved_on), ZERO
            ),
            # Divided by the hours that actually have a rate. Dividing by all of
            # them reports a lower rate every time somebody forgets one.
            "average_rate": _divide(total_cost, total_hours - total_unrated),
            "budgeted_hours": budgeted_hours or None,
            "budgeted_cost": budgeted_cost or None,
            "hours_variance": (total_hours - budgeted_hours) if budgeted_hours else None,
            "cost_variance": (total_cost - budgeted_cost) if budgeted_cost else None,
            "rows": rows,
        }

    async def _budgeted(
        self, job_id: uuid.UUID | None
    ) -> dict[uuid.UUID, tuple[Decimal | None, Decimal | None]]:
        """Budgeted hours and labour cost per cost code.

        Only for one home. Summing targets across every home in the business would
        produce a number that looks like a company-wide labour budget and is not
        one - homes at different stages have spent different fractions of theirs.
        """
        if job_id is None:
            return {}
        from sqlalchemy import select

        budget = await self.budgets.for_job(job_id)
        if budget is None:
            return {}
        lines = await self.db.scalars(
            select(BudgetLine).where(
                BudgetLine.tenant_id == self.tenant_id,
                BudgetLine.budget_id == budget.id,
            )
        )
        # The budgeted *labour* cost, which is not the budget line's amount. That
        # amount covers everything on the cost code - materials, plant, the lot -
        # so comparing labour spend against it reported a home 100k under on
        # labour when it was 14 hours over. The honest figure is the hours we
        # expected times the rate we expected to pay for them, and where no rate
        # is known there is no figure rather than a misleading one.
        from app.repositories.catalog import VendorRepository

        vendors = VendorRepository(self.db, self.tenant_id)
        out: dict[uuid.UUID, tuple[Decimal | None, Decimal | None]] = {}
        for line in lines:
            cost = None
            if line.budgeted_hours is not None and line.vendor_id:
                vendor = await vendors.find(line.vendor_id)
                if vendor is not None and vendor.labour_rate is not None:
                    cost = line.budgeted_hours * vendor.labour_rate
            out[line.cost_code_id] = (line.budgeted_hours, cost)
        return out

    async def set_budgeted_hours(
        self, job_id: uuid.UUID, targets: list[tuple[uuid.UUID, Decimal | None]]
    ) -> dict:
        """Set the hours this home's labour is measured against.

        Only on cost codes the budget already has a line for. Creating a line to
        hold a target would put a cost code on the budget with no money against it,
        and the budget is the document the money argument is had over.
        """
        from sqlalchemy import select

        await self.jobs.require_exists(job_id)
        budget = await self.budgets.for_job(job_id)
        if budget is None:
            raise BusinessRuleViolation(
                "This home has no budget yet.",
                "Generate the budget first - the hours are set against its lines.",
            )
        lines = {
            line.cost_code_id: line
            for line in await self.db.scalars(
                select(BudgetLine).where(
                    BudgetLine.tenant_id == self.tenant_id,
                    BudgetLine.budget_id == budget.id,
                )
            )
        }
        missing = [str(code_id) for code_id, _ in targets if code_id not in lines]
        if missing:
            raise BusinessRuleViolation(
                f"{len(missing)} cost code(s) are not on this home's budget.",
                "Hours are set against budget lines. Add the line first, so a "
                "target never exists on a code the budget has no money for.",
            )
        for code_id, hours in targets:
            lines[code_id].budgeted_hours = hours
        await self.db.commit()
        return await self.productivity(job_id)

    # ===================================================== rollups =====

    async def by_worker(self, job_id: uuid.UUID | None = None) -> list[dict]:
        """Who was on site, for how long, and what it cost."""
        if job_id is not None:
            await self.jobs.require_exists(job_id)
        cards = await self.cards.for_scope(job_id=job_id)
        buckets: dict[str, dict] = {}
        for card in cards:
            b = buckets.setdefault(
                card.worker_name,
                {
                    "worker_name": card.worker_name,
                    "hours": ZERO,
                    "overtime_hours": ZERO,
                    "labour_cost": ZERO,
                    "unrated_hours": ZERO,
                    "entries": 0,
                    "days": set(),
                    "vendor_ids": set(),
                },
            )
            b["hours"] += card.total_hours
            b["overtime_hours"] += card.overtime_hours
            b["entries"] += 1
            b["days"].add(card.worked_on)
            if card.vendor_id:
                b["vendor_ids"].add(card.vendor_id)
            if card.labour_cost is None:
                b["unrated_hours"] += card.total_hours
            else:
                b["labour_cost"] += card.labour_cost

        rows = []
        for b in buckets.values():
            days = len(b["days"])
            rows.append(
                {
                    **{k: v for k, v in b.items() if k not in ("days", "vendor_ids")},
                    "days_on_site": days,
                    # One trade, or more than one. A name booked against three
                    # trades is usually three different people with the same name.
                    "vendor_id": (
                        next(iter(b["vendor_ids"])) if len(b["vendor_ids"]) == 1 else None
                    ),
                    "vendor_count": len(b["vendor_ids"]),
                    "average_day": _divide(b["hours"], Decimal(days)),
                    "average_rate": _divide(
                        b["labour_cost"], b["hours"] - b["unrated_hours"]
                    ),
                }
            )
        rows.sort(key=lambda r: -r["hours"])
        return rows

    async def by_vendor(self, job_id: uuid.UUID | None = None) -> list[dict]:
        """What each trade's labour came to."""
        if job_id is not None:
            await self.jobs.require_exists(job_id)
        cards = await self.cards.for_scope(job_id=job_id)
        buckets: dict[uuid.UUID | None, dict] = {}
        for card in cards:
            b = buckets.setdefault(
                card.vendor_id,
                {
                    "vendor_id": card.vendor_id,
                    "hours": ZERO,
                    "overtime_hours": ZERO,
                    "labour_cost": ZERO,
                    "unrated_hours": ZERO,
                    "entries": 0,
                    "workers": set(),
                },
            )
            b["hours"] += card.total_hours
            b["overtime_hours"] += card.overtime_hours
            b["entries"] += 1
            b["workers"].add(card.worker_name)
            if card.labour_cost is None:
                b["unrated_hours"] += card.total_hours
            else:
                b["labour_cost"] += card.labour_cost

        rows = [
            {
                **{k: v for k, v in b.items() if k != "workers"},
                "workers": len(b["workers"]),
                "average_rate": _divide(b["labour_cost"], b["hours"] - b["unrated_hours"]),
                "overtime_share": _divide(b["overtime_hours"], b["hours"]),
            }
            for b in buckets.values()
        ]
        rows.sort(key=lambda r: -r["hours"])
        return rows

    # ===================================================== summary =====

    async def summary(self, job_id: uuid.UUID | None = None) -> dict:
        prod = await self.productivity(job_id)
        counts = await self.cards.counts(job_id)
        return {
            **counts,
            "total_hours": prod["total_hours"],
            "total_labour_cost": prod["total_labour_cost"],
            "overtime_hours": prod["overtime_hours"],
            "unrated_hours": prod["unrated_hours"],
            "pending_hours": prod["pending_hours"],
            "average_rate": prod["average_rate"],
            "budgeted_hours": prod["budgeted_hours"],
            "hours_variance": prod["hours_variance"],
            "cost_variance": prod["cost_variance"],
        }


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """None rather than zero when there is nothing to divide by.

    Zero would be a number somebody could read as a measurement. None shows as a
    dash, which is what "we cannot say" looks like.
    """
    if not denominator:
        return None
    return (numerator / denominator).quantize(Decimal("0.01"))
