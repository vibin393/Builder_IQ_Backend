"""Business tier for the design studio.

This is the room where a buyer spends their options allowance, and the two things
that make it more than a diary are both about limits:

**The allowance.** A buyer is given a budget inside the base price. Everything
they choose draws it down, and once it is gone the rest is extra on the contract.
"You are four thousand over" is the sentence a design consultant says most, and
before this there was no allowance to be over.

**The structural cutoff.** A moved staircase, an extra window, a garage
conversion - these have to be decided before the frame goes up. After the cutoff
the honest answer stops being "we can price that" and becomes "no". Enforcing it
is the single most valuable rule on this screen, because the alternative is a
buyer being told yes and a superintendent finding out later.

Everything else is endings that were missing: an appointment that was rescheduled
or that nobody attended, and an addendum that was declined or withdrawn.

Nothing here imports FastAPI. It raises domain errors and the API tier decides
what they mean in HTTP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleViolation
from app.models.contract import Contract
from app.models.job import JobStatus
from app.models.option import Option
from app.models.sales_ops import (
    AppointmentStatus,
    DesignAppointment,
    SignatureRequest,
    SignatureStatus,
)
from app.models.selection import Selection, SelectionStatus
from app.repositories.base import Conflict, NotFound, TenantRepository
from app.repositories.job import JobRepository
from app.schemas.common import PageParams

#: Where an appointment may go from where it is. All three endings are terminal:
#: an appointment that did not happen is re-booked as a new one rather than
#: edited back to life, so the history of how many times a buyer moved survives.
APPOINTMENT_MOVES: dict[AppointmentStatus, tuple[AppointmentStatus, ...]] = {
    AppointmentStatus.SCHEDULED: (
        AppointmentStatus.COMPLETE,
        AppointmentStatus.NO_SHOW,
        AppointmentStatus.CANCELLED,
    ),
    AppointmentStatus.COMPLETE: (),
    AppointmentStatus.NO_SHOW: (),
    AppointmentStatus.CANCELLED: (),
}

#: A signature request's life. `draft` can be abandoned; once sent it is out of
#: our hands and only the signer resolves it, except for a void we initiate.
SIGNATURE_MOVES: dict[SignatureStatus, tuple[SignatureStatus, ...]] = {
    SignatureStatus.DRAFT: (SignatureStatus.SENT,),
    SignatureStatus.SENT: (
        SignatureStatus.VIEWED,
        SignatureStatus.SIGNED,
        SignatureStatus.DECLINED,
        SignatureStatus.EXPIRED,
    ),
    SignatureStatus.VIEWED: (
        SignatureStatus.SIGNED,
        SignatureStatus.DECLINED,
        SignatureStatus.EXPIRED,
    ),
    SignatureStatus.SIGNED: (),
    SignatureStatus.DECLINED: (),
    SignatureStatus.EXPIRED: (),
}

#: How far ahead a structural cutoff counts as "coming up". Thirty days is the
#: window a studio works to: long enough to get somebody in, short enough that it
#: is still urgent.
CUTOFF_WARNING_DAYS = 30


class AppointmentRepository(TenantRepository[DesignAppointment]):
    model = DesignAppointment
    label = "Appointment"
    search_fields = ("location", "notes")

    async def page(self, params: PageParams, **filters):
        return await self.paginate(
            params, order_by=DesignAppointment.scheduled_for.desc().nullslast(), **filters
        )

    async def open_for_job(self, job_id: uuid.UUID) -> DesignAppointment | None:
        """The live booking on this home, if there is one."""
        return await self.db.scalar(
            self._base().where(
                DesignAppointment.job_id == job_id,
                DesignAppointment.status == AppointmentStatus.SCHEDULED,
            )
        )

    async def counts(self) -> dict:
        rows = await self.db.execute(
            select(DesignAppointment.status, func.count(DesignAppointment.id))
            .where(DesignAppointment.tenant_id == self.tenant_id)
            .group_by(DesignAppointment.status)
        )
        found = {r[0].value: r[1] for r in rows}
        return {s.value: found.get(s.value, 0) for s in AppointmentStatus}


class SignatureRepository(TenantRepository[SignatureRequest]):
    model = SignatureRequest
    label = "Signature request"
    search_fields = ("document_name", "signer_name", "signer_email")

    async def page(self, params: PageParams, **filters):
        return await self.paginate(params, order_by=SignatureRequest.created_at.desc(), **filters)

    async def counts(self) -> dict:
        rows = await self.db.execute(
            select(SignatureRequest.status, func.count(SignatureRequest.id))
            .where(SignatureRequest.tenant_id == self.tenant_id)
            .group_by(SignatureRequest.status)
        )
        found = {r[0].value: r[1] for r in rows}
        return {s.value: found.get(s.value, 0) for s in SignatureStatus}


class DesignStudioService:
    """Appointments, selections and signatures, scoped to one builder."""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.appointments = AppointmentRepository(db, tenant_id)
        self.signatures = SignatureRepository(db, tenant_id)
        self.jobs = JobRepository(db, tenant_id)

    # ================================================= the allowance =====

    async def allowance(self, job_id: uuid.UUID) -> dict:
        """What the buyer has to spend, what they have spent, and what is left.

        Pending and confirmed selections both count against it. A pending choice
        is money the buyer believes they have committed - showing them a budget
        that ignores it and then a bill that does not is how trust goes.
        """
        job = await self.jobs.get(job_id)
        contract = await self.db.scalar(
            select(Contract).where(Contract.tenant_id == self.tenant_id, Contract.job_id == job_id)
        )

        rows = await self.db.execute(
            select(Selection.status, Selection.price_at_selection, Selection.quantity).where(
                Selection.tenant_id == self.tenant_id,
                Selection.job_id == job_id,
                Selection.status != SelectionStatus.CANCELLED,
            )
        )
        pending = confirmed = Decimal("0")
        for status, price, quantity in rows:
            amount = (price or Decimal("0")) * (quantity or 1)
            if status is SelectionStatus.CONFIRMED:
                confirmed += amount
            else:
                pending += amount

        allowance = contract.options_allowance if contract else Decimal("0")
        spent = pending + confirmed
        return {
            "job_id": job.id,
            "has_contract": contract is not None,
            "allowance": allowance,
            "confirmed": confirmed,
            "pending": pending,
            "spent": spent,
            "remaining": allowance - spent,
            "over_by": max(Decimal("0"), spent - allowance),
            "is_over": spent > allowance,
        }

    async def set_allowance(self, job_id: uuid.UUID, amount: Decimal) -> dict:
        """Agree what the buyer may spend on options.

        Held on the contract because that is what it is part of - the allowance is
        money inside the base price, not a preference on the home.
        """
        await self.jobs.require_exists(job_id)
        contract = await self.db.scalar(
            select(Contract).where(Contract.tenant_id == self.tenant_id, Contract.job_id == job_id)
        )
        if contract is None:
            raise BusinessRuleViolation(
                "This home has no contract.",
                "An options allowance is part of what the buyer agreed to pay, so "
                "there has to be a contract for it to sit in. Sell the home first.",
            )
        if amount < 0:
            raise BusinessRuleViolation(
                "An allowance cannot be negative.",
                "Use zero if the buyer has none - everything they choose is then "
                "an extra on top of the base price.",
            )
        contract.options_allowance = amount
        await self.db.commit()
        return await self.allowance(job_id)

    # ================================================== appointments =====

    async def list_appointments(self, params: PageParams, **filters):
        return await self.appointments.page(params, **filters)

    async def get_appointment(self, appointment_id: uuid.UUID) -> DesignAppointment:
        return await self.appointments.get(appointment_id)

    async def book(
        self,
        *,
        job_id: uuid.UUID,
        scheduled_for: datetime,
        actor_id: uuid.UUID | None = None,
        **data,
    ) -> DesignAppointment:
        """Put a studio session in the diary.

        Refused when the home already has a live booking. Two open appointments on
        one house is a diary conflict rather than a feature: somebody turns up on
        the wrong day and nobody knows which record is the real one.
        """
        job = await self.jobs.require_exists(job_id)

        if job.status is JobStatus.CANCELLED:
            raise BusinessRuleViolation(
                f"{job.job_number} was cancelled.",
                "There is nothing left to choose options for.",
            )
        if job.status in (JobStatus.CLOSED, JobStatus.WARRANTY):
            raise BusinessRuleViolation(
                f"{job.job_number} has already been handed over.",
                "Selections are agreed before the build, not after the buyer has "
                "the keys. Raise a change order instead.",
            )

        existing = await self.appointments.open_for_job(job_id)
        if existing is not None:
            raise Conflict(
                f"{job.job_number} already has an appointment booked"
                + (f" for {existing.scheduled_for:%d %b %Y}" if existing.scheduled_for else "")
                + "."
            )

        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=UTC)
        if scheduled_for < datetime.now(UTC):
            raise BusinessRuleViolation(
                "That appointment is in the past.",
                "Book a future slot, or record what happened at a past one by completing it.",
            )

        if data.get("consultant_id"):
            await self._require_colleague(data["consultant_id"])

        # The buyer is the person attending, so it comes from the contract rather
        # than being typed in again. The thin endpoint this replaced did this and
        # it would have been a quiet regression to drop it - the appointment would
        # have had no idea who it was for.
        if not data.get("buyer_id"):
            contract = await self.db.scalar(
                select(Contract).where(
                    Contract.tenant_id == self.tenant_id, Contract.job_id == job_id
                )
            )
            if contract is not None:
                data["buyer_id"] = contract.buyer_id

        return await self.appointments.create(job_id=job_id, scheduled_for=scheduled_for, **data)

    async def reschedule(
        self, appointment_id: uuid.UUID, when: datetime, note: str | None = None
    ) -> DesignAppointment:
        """Move a booking.

        The old one is cancelled and a new one created pointing back at it, rather
        than the date being edited. A buyer who has moved three times is a fact
        worth being able to see, and editing the date in place erases it.
        """
        old = await self.appointments.get(appointment_id)
        if not old.is_open:
            raise Conflict(f"That appointment is already {old.status.value.replace('_', ' ')}.")
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when < datetime.now(UTC):
            raise BusinessRuleViolation("That appointment is in the past.", "Pick a future slot.")

        old.status = AppointmentStatus.CANCELLED
        old.outcome_note = note or "Rescheduled."
        await self.db.flush()

        fresh = self.appointments.stage(
            job_id=old.job_id,
            buyer_id=old.buyer_id,
            consultant_id=old.consultant_id,
            scheduled_for=when,
            duration_minutes=old.duration_minutes,
            location=old.location,
            structural_cutoff=old.structural_cutoff,
            notes=old.notes,
            rescheduled_from_id=old.id,
        )
        await self.db.commit()
        return await self.appointments.get(fresh.id)

    async def complete_appointment(
        self, appointment_id: uuid.UUID, note: str | None = None
    ) -> DesignAppointment:
        """The session happened."""
        appointment = await self.appointments.get(appointment_id)
        self._require_appointment_move(appointment, AppointmentStatus.COMPLETE)
        appointment.status = AppointmentStatus.COMPLETE
        appointment.completed_at = datetime.now(UTC)
        appointment.outcome_note = note
        await self.db.commit()
        return await self.appointments.get(appointment_id)

    async def mark_no_show(
        self, appointment_id: uuid.UUID, note: str | None = None
    ) -> DesignAppointment:
        """Nobody came.

        Its own ending rather than a cancellation, because they are different
        problems: a cancellation is somebody telling you, a no-show is a wasted
        slot and a consultant sitting alone for three hours.
        """
        appointment = await self.appointments.get(appointment_id)
        self._require_appointment_move(appointment, AppointmentStatus.NO_SHOW)
        appointment.status = AppointmentStatus.NO_SHOW
        appointment.outcome_note = note
        await self.db.commit()
        return await self.appointments.get(appointment_id)

    async def cancel_appointment(
        self, appointment_id: uuid.UUID, note: str | None = None
    ) -> DesignAppointment:
        appointment = await self.appointments.get(appointment_id)
        self._require_appointment_move(appointment, AppointmentStatus.CANCELLED)
        appointment.status = AppointmentStatus.CANCELLED
        appointment.outcome_note = note
        await self.db.commit()
        return await self.appointments.get(appointment_id)

    async def set_cutoff(self, appointment_id: uuid.UUID, cutoff: date) -> DesignAppointment:
        """Set the date after which structural options are off the table."""
        appointment = await self.appointments.get(appointment_id)
        appointment.structural_cutoff = cutoff
        await self.db.commit()
        return await self.appointments.get(appointment_id)

    def _require_appointment_move(
        self, appointment: DesignAppointment, to: AppointmentStatus
    ) -> None:
        if to in APPOINTMENT_MOVES[appointment.status]:
            return
        raise BusinessRuleViolation(
            f"That appointment is already {appointment.status.value.replace('_', ' ')}.",
            "Book a new one rather than editing this record - how many times a "
            "buyer moved is worth keeping.",
        )

    # ============================================== the structural gate =====

    async def structural_cutoff_for(self, job_id: uuid.UUID) -> date | None:
        """The cutoff in force for this home.

        Taken from the most recent appointment that carries one, so rescheduling
        forward moves it and a cancelled booking does not leave a stale date
        governing what the buyer may choose.
        """
        return await self.db.scalar(
            select(DesignAppointment.structural_cutoff)
            .where(
                DesignAppointment.tenant_id == self.tenant_id,
                DesignAppointment.job_id == job_id,
                DesignAppointment.structural_cutoff.is_not(None),
                DesignAppointment.status != AppointmentStatus.CANCELLED,
            )
            .order_by(DesignAppointment.structural_cutoff.desc())
            .limit(1)
        )

    async def require_selectable(self, job_id: uuid.UUID, option_id: uuid.UUID) -> None:
        """Refuse a structural option chosen after the frame is committed.

        The most valuable rule on this screen. Without it a buyer is told yes in
        the studio and a superintendent discovers on site that the staircase has
        moved - and by then somebody is paying for it either way.
        """
        option = await self.db.scalar(
            select(Option).where(Option.tenant_id == self.tenant_id, Option.id == option_id)
        )
        if option is None:
            raise NotFound("Option", option_id)
        if not option.is_structural:
            return

        cutoff = await self.structural_cutoff_for(job_id)
        if cutoff and cutoff < date.today():
            raise BusinessRuleViolation(
                f"{option.name} is structural, and the cutoff for this home was {cutoff:%d %b %Y}.",
                "Structural choices have to be made before the frame goes up. This "
                "one now needs a change order, which will carry its own price and "
                "schedule impact.",
            )

    # ================================================== signatures =====

    async def list_signatures(self, params: PageParams, **filters):
        return await self.signatures.page(params, **filters)

    async def create_signature_request(self, *, job_id: uuid.UUID, **data) -> SignatureRequest:
        await self.jobs.require_exists(job_id)
        if data.get("signer_email"):
            data["signer_email"] = str(data["signer_email"])
        return await self.signatures.create(job_id=job_id, **data)

    async def send_for_signature(self, request_id: uuid.UUID) -> SignatureRequest:
        request = await self.signatures.get(request_id)
        self._require_signature_move(request, SignatureStatus.SENT)
        request.status = SignatureStatus.SENT
        request.sent_at = datetime.now(UTC)
        await self.db.commit()
        return await self.signatures.get(request_id)

    async def remind(self, request_id: uuid.UUID) -> SignatureRequest:
        """Chase an unsigned document, and count the chase.

        Counted so "we only sent it once" cannot be claimed when the record says
        otherwise - which matters when an addendum holding up a frame has been
        sitting unsigned for three weeks.
        """
        request = await self.signatures.get(request_id)
        if not request.is_outstanding:
            raise Conflict(f"That document is {request.status.value}; there is nothing to chase.")
        request.reminders_sent += 1
        request.last_reminder_at = datetime.now(UTC)
        await self.db.commit()
        return await self.signatures.get(request_id)

    async def mark_signed(self, request_id: uuid.UUID) -> SignatureRequest:
        """Record that the document came back signed.

        Worth being plain about: this asserts that signing happened, it does not
        make it binding. That takes a provider, which is what `provider` and
        `external_id` are for.
        """
        request = await self.signatures.get(request_id)
        self._require_signature_move(request, SignatureStatus.SIGNED)
        request.status = SignatureStatus.SIGNED
        request.signed_at = datetime.now(UTC)
        await self.db.commit()
        return await self.signatures.get(request_id)

    async def mark_viewed(self, request_id: uuid.UUID) -> SignatureRequest:
        request = await self.signatures.get(request_id)
        self._require_signature_move(request, SignatureStatus.VIEWED)
        request.status = SignatureStatus.VIEWED
        await self.db.commit()
        return await self.signatures.get(request_id)

    async def decline(self, request_id: uuid.UUID, reason: str) -> SignatureRequest:
        """The buyer refused to sign.

        The reason is required, and it is the most valuable field on the record: a
        declined addendum means the selections behind it are wrong, and somebody
        has to find out how before the studio books another three hours.
        """
        request = await self.signatures.get(request_id)
        self._require_signature_move(request, SignatureStatus.DECLINED)
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "A declined document needs a reason.",
                "It means the selections behind it are wrong. Without knowing why, "
                "the next version will be wrong in the same way.",
            )
        request.status = SignatureStatus.DECLINED
        request.declined_at = datetime.now(UTC)
        request.decline_reason = reason.strip()
        await self.db.commit()
        return await self.signatures.get(request_id)

    async def void(self, request_id: uuid.UUID, reason: str) -> SignatureRequest:
        """Withdraw a document we sent in error, or that has been superseded."""
        request = await self.signatures.get(request_id)
        if request.status is SignatureStatus.SIGNED:
            raise BusinessRuleViolation(
                "That document has been signed.",
                "A signed document is withdrawn by superseding it with another, "
                "not by voiding the record of it.",
            )
        if not (reason or "").strip():
            raise BusinessRuleViolation(
                "Voiding a document needs a reason.",
                "Somebody was asked to sign it and will ask why it vanished.",
            )
        request.status = SignatureStatus.EXPIRED
        request.void_reason = reason.strip()
        await self.db.commit()
        return await self.signatures.get(request_id)

    def _require_signature_move(self, request: SignatureRequest, to: SignatureStatus) -> None:
        allowed = SIGNATURE_MOVES[request.status]
        if to in allowed:
            return
        if request.status is SignatureStatus.SIGNED:
            raise Conflict("That document has already been signed.")
        raise BusinessRuleViolation(
            f"A {request.status.value} document cannot be marked {to.value}.",
            "Next steps from here: " + (", ".join(s.value for s in allowed) or "none") + ".",
        )

    # ==================================================== the summary =====

    async def summary(self) -> dict:
        """The counters above the studio, and the two that mean work.

        `awaiting_signature` is money agreed but not committed; `cutoff_soon` is
        buyers about to lose the ability to change anything structural. Both are
        things somebody should act on today.
        """
        today = date.today()
        appointments = await self.appointments.counts()
        signatures = await self.signatures.counts()

        outstanding = (
            await self.db.scalar(
                select(func.count())
                .select_from(SignatureRequest)
                .where(
                    SignatureRequest.tenant_id == self.tenant_id,
                    SignatureRequest.status.in_((SignatureStatus.SENT, SignatureStatus.VIEWED)),
                )
            )
            or 0
        )

        # Buyers who are about to lose the ability to change anything structural.
        # Thirty days is the window a studio actually works to: long enough to get
        # somebody in, short enough that it is still urgent.
        soon = today + timedelta(days=CUTOFF_WARNING_DAYS)
        cutoff_soon = (
            await self.db.scalar(
                select(func.count())
                .select_from(DesignAppointment)
                .where(
                    DesignAppointment.tenant_id == self.tenant_id,
                    DesignAppointment.status == AppointmentStatus.SCHEDULED,
                    DesignAppointment.structural_cutoff.is_not(None),
                    DesignAppointment.structural_cutoff >= today,
                    DesignAppointment.structural_cutoff <= soon,
                )
            )
            or 0
        )

        return {
            "appointments": appointments,
            "signatures": signatures,
            "scheduled": appointments.get(AppointmentStatus.SCHEDULED.value, 0),
            "awaiting_signature": outstanding,
            "signed": signatures.get(SignatureStatus.SIGNED.value, 0),
            "cutoff_soon": cutoff_soon,
        }

    # ======================================================= helpers =====

    async def _require_colleague(self, user_id: uuid.UUID) -> None:
        from app.models.user import User

        found = await self.db.scalar(
            select(User.id).where(User.id == user_id, User.tenant_id == self.tenant_id)
        )
        if found is None:
            raise NotFound("User", user_id)
