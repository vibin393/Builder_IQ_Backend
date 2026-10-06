"""Safety and quality.

Three chains of reasoning shape this module.

**An observation is a fix somebody owes, not a note.** So it has an owner, a date,
and a state it moves through - and the state it cannot move to directly is closed.
Somebody other than whoever did the work has to look at the fix and accept it.
This is the same rule the punch list needed, for the same reason: a record that
closes on the word of the person who owed the work records an intention, not an
outcome.

**A failed inspection raises one automatically.** That sentence was on the screen
before anything did it. It is the chain that matters: without it, an inspection is
a record that a problem existed, and with it the inspection record shows what was
done about the problem. The observation is the *corrective action for the failure*
- one per inspection, owed by a trade, due before the re-inspection. The punch
items generated from the individual failed checks are the work list. They are not
duplicates: one says what to fix, the other says who owes the fixing of this
failure and by when.

**Whether an incident is reportable is a legal question with a deadline.** In the
UK that is RIDDOR, and it is the deadline that makes it real - "should we report
this" is a conversation, "this was due with the HSE four days ago" is not. So the
category is suggested from what the incident records, the deadline is derived from
the category, and closing a reportable incident that has never been reported is
refused.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.field import Inspection, InspectionResult, InspectionType
from app.models.safety import (
    ActionPlan,
    ActionPlanItem,
    ActionPlanStatus,
    Incident,
    IncidentType,
    Observation,
    ObservationStatus,
    ObservationType,
    RiddorCategory,
    Severity,
)
from app.repositories.base import Conflict, NotFound
from app.repositories.job import JobRepository
from app.repositories.safety import (
    ActionPlanItemRepository,
    ActionPlanRepository,
    IncidentRepository,
    ObservationRepository,
)
from app.schemas.common import PageParams

#: Where an observation can go from where it is.
#:
#: Nothing reaches CLOSED except through READY_FOR_REVIEW, which is the whole
#: point: the fix is claimed, then somebody accepts or rejects it. NOT_ACCEPTED
#: goes back to the person who owed it rather than sideways into closed.
OBSERVATION_MOVES: dict[ObservationStatus, frozenset[ObservationStatus]] = {
    ObservationStatus.INITIATED: frozenset(
        {ObservationStatus.READY_FOR_REVIEW}
    ),
    ObservationStatus.READY_FOR_REVIEW: frozenset(
        {ObservationStatus.CLOSED, ObservationStatus.NOT_ACCEPTED}
    ),
    ObservationStatus.NOT_ACCEPTED: frozenset(
        {ObservationStatus.READY_FOR_REVIEW}
    ),
    # Reopening is deliberate and goes back to the start, so the second round is
    # a full round rather than a shortcut to closed.
    ObservationStatus.CLOSED: frozenset({ObservationStatus.INITIATED}),
}

#: How long a trade gets to fix something, by how bad it is. A critical safety
#: observation with a fortnight on it is not a corrective action, it is a note
#: with a date attached.
DEFAULT_DUE_DAYS: dict[Severity, int] = {
    Severity.CRITICAL: 1,
    Severity.HIGH: 3,
    Severity.MEDIUM: 7,
    Severity.LOW: 14,
}


def suggest_riddor(
    *,
    incident_type: IncidentType,
    lost_time: bool = False,
    medical_treatment: bool = False,
    severity: Severity = Severity.MEDIUM,
) -> RiddorCategory:
    """What RIDDOR category this incident looks like.

    A suggestion, not a determination - the regulations turn on facts this system
    does not hold, such as whether an injury is on the specified list. It exists
    because the alternative was a bare tick box, and a tick box defaults to
    unticked: the failure mode is silently not reporting something, which is the
    expensive direction to be wrong in.

    The caller can override it. What it must not do is leave the question unasked.
    """
    if incident_type is IncidentType.ILLNESS:
        return RiddorCategory.OCCUPATIONAL_DISEASE
    if incident_type in (IncidentType.NEAR_MISS, IncidentType.PROPERTY_DAMAGE):
        # Only the serious ones are dangerous occurrences. A dropped hammer is
        # not, and categorising every near miss as reportable would train people
        # to ignore the flag.
        return (
            RiddorCategory.DANGEROUS_OCCURRENCE
            if severity is Severity.CRITICAL
            else RiddorCategory.NOT_REPORTABLE
        )
    if incident_type is IncidentType.INJURY:
        if severity is Severity.CRITICAL:
            return RiddorCategory.SPECIFIED_INJURY
        if lost_time:
            return RiddorCategory.OVER_7_DAY
        if medical_treatment:
            return RiddorCategory.SPECIFIED_INJURY
    return RiddorCategory.NOT_REPORTABLE


class SafetyService:
    """Observations, incidents and action plans."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.observations = ObservationRepository(db, tenant_id)
        self.incidents = IncidentRepository(db, tenant_id)
        self.plans = ActionPlanRepository(db, tenant_id)
        self.steps = ActionPlanItemRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)

    # ============================================== observations =====

    async def list_observations(self, params: PageParams, **filters):
        return await self.observations.paginate(
            params, order_by=Observation.due_on.nulls_last(), **filters
        )

    async def raise_observation(self, *, actor_id=None, **data) -> Observation:
        """Raise a corrective action.

        A due date is filled in from the severity when none is given. An
        observation with no date is a note, and the whole claim this screen makes
        is that these are not notes.
        """
        await self.jobs.require_exists(data["job_id"])
        if data.get("assigned_vendor_id"):
            from app.repositories.catalog import VendorRepository

            await VendorRepository(self.db, self.tenant_id).require_exists(
                data["assigned_vendor_id"]
            )
        if not data.get("due_on"):
            severity = data.get("severity") or Severity.MEDIUM
            data["due_on"] = date.today() + _days(DEFAULT_DUE_DAYS[Severity(severity)])
        return await self.observations.create(raised_by_id=actor_id, **data)

    async def update_observation(self, obs_id: uuid.UUID, **data) -> Observation:
        """Correct the details.

        Status is not settable here, for the same reason it is not on a punch
        item: it would route round the review. The routes below move it.
        """
        obs = await self.observations.get(obs_id)
        data.pop("status", None)
        if obs.status is ObservationStatus.CLOSED:
            raise Conflict(f"{obs.title[:40]} has already been closed.")
        return await self.observations.update(obs, **data)

    def _require_move(self, obs: Observation, to: ObservationStatus) -> None:
        allowed = OBSERVATION_MOVES[obs.status]
        if to in allowed:
            return
        raise BusinessRuleViolation(
            f"An observation that is {obs.status.value.replace('_', ' ')} cannot "
            f"become {to.value.replace('_', ' ')}.",
            "Next steps from here: "
            + (", ".join(s.value.replace("_", " ") for s in allowed) or "none")
            + ".",
        )

    async def assign_observation(
        self, obs_id: uuid.UUID, vendor_id: uuid.UUID, due_on: date | None = None
    ) -> Observation:
        from app.repositories.catalog import VendorRepository

        obs = await self.observations.get(obs_id)
        if obs.status is ObservationStatus.CLOSED:
            raise Conflict(f"{obs.title[:40]} has already been closed.")
        await VendorRepository(self.db, self.tenant_id).require_exists(vendor_id)
        obs.assigned_vendor_id = vendor_id
        if due_on:
            obs.due_on = due_on
        await self.db.commit()
        return await self.observations.get(obs_id)

    async def submit_observation(self, obs_id: uuid.UUID, resolution: str) -> Observation:
        """The fix has been made - claimed, not concluded.

        A resolution is required here rather than at closing, because the person
        who did the work is the one who knows what they did. Asking the reviewer
        to write it down produces "fixed".
        """
        obs = await self.observations.get(obs_id)
        self._require_move(obs, ObservationStatus.READY_FOR_REVIEW)
        if not (resolution or "").strip():
            raise BusinessRuleViolation(
                "Say what was done about it.",
                "An observation is a fix somebody owes. What closes it is the "
                "record of the fix, not the fact that somebody pressed a button.",
            )
        obs.status = ObservationStatus.READY_FOR_REVIEW
        obs.resolution = resolution.strip()
        await self.db.commit()
        return await self.observations.get(obs_id)

    async def close_observation(self, obs_id: uuid.UUID, *, actor_id: uuid.UUID) -> Observation:
        """Somebody has inspected the fix and accepted it."""
        obs = await self.observations.get(obs_id)
        self._require_move(obs, ObservationStatus.CLOSED)
        obs.status = ObservationStatus.CLOSED
        obs.closed_on = date.today()
        obs.closed_by_id = actor_id
        await self.db.commit()
        return await self.observations.get(obs_id)

    async def reject_observation(self, obs_id: uuid.UUID, reason: str) -> Observation:
        """The fix was inspected and failed.

        This is the state the enum has always had and nothing could reach. Without
        it a bad fix either closes or sits in review forever, and neither tells the
        trade to come back.
        """
        obs = await self.observations.get(obs_id)
        self._require_move(obs, ObservationStatus.NOT_ACCEPTED)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Say why the fix was not accepted.",
                "The trade has to know what is still wrong, and a rejected fix "
                "with no reason reads as somebody being difficult.",
            )
        obs.status = ObservationStatus.NOT_ACCEPTED
        obs.resolution = reason.strip()
        await self.db.commit()
        return await self.observations.get(obs_id)

    async def reopen_observation(self, obs_id: uuid.UUID, reason: str) -> Observation:
        obs = await self.observations.get(obs_id)
        self._require_move(obs, ObservationStatus.INITIATED)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Say why it is being reopened.",
                "It was closed by somebody who accepted the fix; the record should "
                "say what changed.",
            )
        obs.status = ObservationStatus.INITIATED
        obs.closed_on = None
        obs.closed_by_id = None
        obs.resolution = reason.strip()
        await self.db.commit()
        return await self.observations.get(obs_id)

    # ------------------------------------------- the inspection chain -----

    async def observations_for_failure(
        self, inspection: Inspection, *, actor_id=None
    ) -> Observation | None:
        """Raise the corrective action for a failed inspection.

        Called by the field service when a result is recorded, so the sentence
        "a failed inspection raises one automatically" is true of the code rather
        than only of the screen.

        One per inspection, not one per failed check. The per-check work list is
        the punch list; this is the single tracked action that says who owes the
        fixing of this failure and by when, and it is what has to be closed before
        anybody claims the failure was dealt with.

        Returns None when there is already one, so re-recording a result does not
        raise a second.
        """
        if inspection.result is not InspectionResult.FAILED:
            return None
        existing = await self.observations.for_inspection(inspection.id)
        if existing:
            return None

        # A municipal failure is a legal stop, so it gets the shortest clock and
        # the highest severity. An internal QA failure is ours to manage.
        municipal = inspection.inspection_type is InspectionType.MUNICIPAL
        severity = Severity.CRITICAL if municipal else Severity.HIGH

        # Whoever the failed checks belonged to. Where they all belong to one
        # trade, it is theirs; where they do not, it stays with the builder to
        # apportion rather than being pinned on whichever check came first.
        vendors = {i.vendor_id for i in inspection.failed_items if i.vendor_id}
        vendor_id = vendors.pop() if len(vendors) == 1 else None

        return await self.observations.create(
            job_id=inspection.job_id,
            title=f"{inspection.name} failed",
            description=inspection.failure_reason,
            location=inspection.name,
            observation_type=(
                ObservationType.SAFETY if municipal else ObservationType.QUALITY
            ),
            severity=severity,
            inspection_id=inspection.id,
            assigned_vendor_id=vendor_id,
            raised_by_id=actor_id,
            due_on=date.today() + _days(DEFAULT_DUE_DAYS[severity]),
        )

    # ================================================== incidents =====

    async def list_incidents(self, params: PageParams, **filters):
        return await self.incidents.paginate(
            params, order_by=Incident.occurred_on.desc(), **filters
        )

    async def report_incident(self, *, actor_id=None, **data) -> Incident:
        """Record what happened.

        The RIDDOR category is suggested from the facts unless the caller states
        one. Leaving it unasked is how an incident that had to reach the HSE
        within ten days sits in a list for a month.
        """
        await self.jobs.require_exists(data["job_id"])
        data["occurred_on"] = data.get("occurred_on") or date.today()
        if data["occurred_on"] > date.today():
            raise BusinessRuleViolation(
                "An incident cannot have happened in the future.",
                "This records what happened, not what might.",
            )
        if not data.get("riddor_category"):
            data["riddor_category"] = suggest_riddor(
                incident_type=data["incident_type"],
                lost_time=bool(data.get("lost_time")),
                medical_treatment=bool(data.get("medical_treatment")),
                severity=Severity(data.get("severity") or Severity.MEDIUM),
            )
        # The old boolean is kept in step with the category rather than left to
        # drift, because reports and the scorecard already read it.
        data["reportable"] = (
            RiddorCategory(data["riddor_category"]) is not RiddorCategory.NOT_REPORTABLE
        )
        return await self.incidents.create(reported_by_id=actor_id, **data)

    async def classify_incident(
        self, incident_id: uuid.UUID, category: RiddorCategory
    ) -> Incident:
        """Change the RIDDOR category, which changes the deadline with it."""
        incident = await self.incidents.get(incident_id)
        if incident.reported_to_hse_on and category is RiddorCategory.NOT_REPORTABLE:
            raise BusinessRuleViolation(
                "This incident has already been reported to the HSE.",
                "Marking it not reportable now would leave a report on file "
                "against an incident the system says never needed one.",
            )
        incident.riddor_category = category
        incident.reportable = category is not RiddorCategory.NOT_REPORTABLE
        await self.db.commit()
        return await self.incidents.get(incident_id)

    async def record_hse_report(
        self, incident_id: uuid.UUID, *, reported_on: date | None = None, reference: str | None = None
    ) -> Incident:
        """Record that the report has actually gone in."""
        incident = await self.incidents.get(incident_id)
        if incident.riddor_category is RiddorCategory.NOT_REPORTABLE:
            raise BusinessRuleViolation(
                "This incident is not categorised as reportable.",
                "Set the RIDDOR category first. Recording a report against an "
                "incident that did not need one makes the reportable count wrong "
                "in the direction nobody checks.",
            )
        when = reported_on or date.today()
        if when > date.today():
            raise BusinessRuleViolation(
                "A report cannot have been made in the future.", "Use today or earlier."
            )
        if when < incident.occurred_on:
            raise BusinessRuleViolation(
                "The report predates the incident.",
                f"It happened on {incident.occurred_on}.",
            )
        incident.reported_to_hse_on = when
        if reference:
            incident.hse_reference = reference.strip()
        await self.db.commit()
        return await self.incidents.get(incident_id)

    async def investigate_incident(
        self, incident_id: uuid.UUID, root_cause: str
    ) -> Incident:
        incident = await self.incidents.get(incident_id)
        if not (root_cause or "").strip():
            raise BusinessRuleViolation(
                "Record the root cause.",
                "An incident with no cause recorded cannot be prevented, which is "
                "the only reason to keep the record.",
            )
        incident.root_cause = root_cause.strip()
        await self.db.commit()
        return await self.incidents.get(incident_id)

    async def close_incident(self, incident_id: uuid.UUID) -> Incident:
        """Close the investigation.

        Refused while a reportable incident has not been reported. That is the one
        rule here with a legal consequence, and an incident that closes without it
        is a record of the obligation being missed rather than met.
        """
        incident = await self.incidents.get(incident_id)
        if incident.closed_on:
            raise Conflict(f"{incident.title[:40]} is already closed.")
        # Both reasons at once. Reporting them one at a time means fixing the
        # first, pressing close again, and being refused for the second - so the
        # operator finds out what closing actually needs by trial and error.
        blockers: list[str] = []
        hints: list[str] = []
        if (
            incident.riddor_category is not RiddorCategory.NOT_REPORTABLE
            and not incident.reported_to_hse_on
        ):
            blockers.append(
                f"it is a {incident.riddor_category.value.replace('_', ' ')} and the "
                "HSE has not been told"
            )
            hints.append(
                "Record the report, or change the category if it was classified "
                "wrongly - closing it unreported would file the obligation as met."
            )
        if not (incident.root_cause or "").strip():
            blockers.append("no root cause has been recorded")
            hints.append(
                "An investigation that concluded nothing leaves nothing to act on "
                "and nothing to show anybody who asks."
            )
        if blockers:
            raise BusinessRuleViolation(
                f"{incident.title[:40]} cannot be closed: " + ", and ".join(blockers) + ".",
                " ".join(hints),
            )
        incident.closed_on = date.today()
        await self.db.commit()
        return await self.incidents.get(incident_id)

    # =============================================== action plans =====

    async def list_plans(self, params: PageParams, **filters):
        return await self.plans.paginate(
            params, order_by=ActionPlan.created_at.desc(), **filters
        )

    async def create_plan(self, *, actor_id=None, **data) -> ActionPlan:
        if data.get("job_id"):
            await self.jobs.require_exists(data["job_id"])
        if data.get("is_template") and data.get("job_id"):
            raise BusinessRuleViolation(
                "A template does not belong to a home.",
                "It is the pattern that gets copied onto one. Leave the home off, "
                "then start it against whichever homes need it.",
            )
        return await self.plans.create(owner_id=actor_id, **data)

    async def add_step(self, plan_id: uuid.UUID, **data) -> ActionPlan:
        plan = await self.plans.get(plan_id)
        if plan.status is ActionPlanStatus.COMPLETE:
            raise Conflict(f"{plan.name} is complete; its steps are fixed.")
        if data.get("sequence") is None:
            data["sequence"] = len(plan.items) + 1
        self.steps.stage(plan_id=plan_id, **data)
        await self.db.commit()
        return await self.plans.get(plan_id)

    async def start_from_template(
        self, template_id: uuid.UUID, job_id: uuid.UUID, *, actor_id=None, due_on: date | None = None
    ) -> ActionPlan:
        """Copy a template onto a home and start it.

        The model said templates were "copied onto a job when it is started" and
        nothing did the copying, so every plan was typed out again per house -
        which is exactly how the pre-pour checklist ends up with four steps on one
        home and six on the next.

        The steps are copied, not referenced. A template edited next month must not
        rewrite the checklist somebody signed last month.
        """
        template = await self.plans.get(template_id)
        if not template.is_template:
            raise BusinessRuleViolation(
                f"{template.name} is not a template.",
                "Only a template can be started against a home.",
            )
        if not template.items:
            raise BusinessRuleViolation(
                f"{template.name} has no steps.",
                "Starting it would put an empty checklist on the home, which reads "
                "as complete.",
            )
        await self.jobs.require_exists(job_id)

        plan = self.plans.stage(
            job_id=job_id,
            name=template.name,
            description=template.description,
            status=ActionPlanStatus.ACTIVE,
            due_on=due_on,
            owner_id=actor_id,
            is_template=False,
            template_id=template.id,
            started_on=date.today(),
        )
        await self.db.flush()
        for step in sorted(template.items, key=lambda i: i.sequence):
            self.steps.stage(
                plan_id=plan.id,
                sequence=step.sequence,
                description=step.description,
                requires_signoff=step.requires_signoff,
                vendor_id=step.vendor_id,
                due_on=due_on,
            )
        await self.db.commit()
        return await self.plans.get(plan.id)

    async def complete_step(
        self, plan_id: uuid.UUID, item_id: uuid.UUID, *, actor_id: uuid.UUID, notes: str | None = None
    ) -> ActionPlan:
        """Mark a step done.

        In order. These are the sequences a builder runs the same way on every
        home, and a pre-pour checklist that lets the pour be signed off before the
        rebar check is a form, not a checklist.
        """
        plan = await self.plans.get(plan_id)
        item = next((i for i in plan.items if i.id == item_id), None)
        if item is None:
            raise NotFound("Action plan step", item_id)
        if item.completed_on:
            raise Conflict(f"Step {item.sequence} is already done.")

        waiting_on = plan.next_item
        if waiting_on is not None and waiting_on.id != item.id:
            raise BusinessRuleViolation(
                f"Step {waiting_on.sequence} has not been done yet.",
                f"These run in order. {waiting_on.description[:80]} comes first.",
            )

        item.completed_on = date.today()
        item.completed_by_id = actor_id
        if notes:
            item.notes = notes.strip()
        self._restatus(plan)
        await self.db.commit()
        return await self.plans.get(plan_id)

    async def sign_off_step(
        self, plan_id: uuid.UUID, item_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> ActionPlan:
        """Witness a step somebody else completed.

        The model has always said a sign-off step is "signed off by someone other
        than whoever did it" and the code set both to the same user, which made the
        rule a comment. Two columns and one check make it a rule.
        """
        plan = await self.plans.get(plan_id)
        item = next((i for i in plan.items if i.id == item_id), None)
        if item is None:
            raise NotFound("Action plan step", item_id)
        if not item.requires_signoff:
            raise BusinessRuleViolation(
                f"Step {item.sequence} does not need a witness.",
                "It is done once it is marked complete.",
            )
        if not item.completed_on:
            raise BusinessRuleViolation(
                f"Step {item.sequence} has not been done yet.",
                "There is nothing to witness until somebody does the work.",
            )
        if item.signed_off_on:
            raise Conflict(f"Step {item.sequence} has already been signed off.")
        if item.completed_by_id == actor_id:
            raise BusinessRuleViolation(
                "You completed this step, so you cannot witness it.",
                "The point of a sign-off step is a second pair of eyes. Somebody "
                "else has to look at it.",
            )
        item.signed_off_by_id = actor_id
        item.signed_off_on = date.today()
        self._restatus(plan)
        await self.db.commit()
        return await self.plans.get(plan_id)

    @staticmethod
    def _restatus(plan: ActionPlan) -> None:
        """Keep the plan's status in step with its steps.

        Complete means every step is done *and* witnessed where a witness was
        required - counting a step that is waiting on its sign-off as finished
        would report the plan complete while the thing it exists to guarantee had
        not happened.
        """
        if all(i.is_done for i in plan.items) and plan.items:
            plan.status = ActionPlanStatus.COMPLETE
        elif plan.due_on and plan.due_on < date.today():
            plan.status = ActionPlanStatus.OVERDUE
        elif plan.status is ActionPlanStatus.DRAFT:
            plan.status = ActionPlanStatus.ACTIVE

    # ==================================================== summary =====

    async def summary(self, job_id: uuid.UUID | None = None) -> dict[str, int]:
        if job_id is not None:
            await self.jobs.require_exists(job_id)
        return {
            **await self.observations.counts(job_id),
            **await self.incidents.counts(job_id),
            **await self.plans.counts(job_id),
        }


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)
