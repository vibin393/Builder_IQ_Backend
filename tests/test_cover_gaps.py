"""Scheduled work that outruns the trade's insurance.

The purchase-order gate refuses at the moment money moves. This asks the same
question from the schedule's side, far enough ahead that chasing a renewal is
still an option rather than a delay.
"""

from datetime import date, timedelta

from tests.conftest import post

TODAY = date.today()


def d(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


async def _scheduled_job(client, h, sold_job):
    """A home with a generated schedule, so there are tasks to book trades onto."""
    template = await post(
        client, h, "/api/v1/schedule-templates",
        {"code": "STD", "name": "Standard build"},
    )
    await post(
        client, h, f"/api/v1/schedule-templates/{template['id']}/tasks",
        {"sequence": 1, "name": "Framing", "offset_days": 5, "duration_days": 10},
    )
    tasks = await post(
        client, h, f"/api/v1/jobs/{sold_job['job']['id']}/schedule",
        {"template_id": template["id"]},
    )
    return tasks


async def _prequalify(client, h, vendor_id, expires):
    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(
        client, h, f"/api/v1/prequalification/{pq['id']}/documents",
        {
            "document_type": "general_liability",
            "reference": "GL-1",
            "expires_on": expires,
        },
    )
    return pq


async def test_no_gaps_when_cover_outlasts_the_work(client, sold_job):
    h = sold_job["headers"]
    tasks = await _scheduled_job(client, h, sold_job)
    vendor_id = sold_job["vendor"]["id"]

    await _prequalify(client, h, vendor_id, expires=d(400))
    await post(
        client, h, f"/api/v1/tasks/{tasks[0]['id']}/trade",
        {"vendor_id": vendor_id}, expect=200,
    )

    gaps = (await client.get("/api/v1/prequalification/cover-gaps", headers=h)).json()
    assert gaps == []


async def test_a_task_running_past_the_expiry_is_flagged(client, sold_job):
    h = sold_job["headers"]
    tasks = await _scheduled_job(client, h, sold_job)
    vendor_id = sold_job["vendor"]["id"]

    # Cover lapses tomorrow; the task is scheduled well beyond it.
    await _prequalify(client, h, vendor_id, expires=d(1))
    await post(
        client, h, f"/api/v1/tasks/{tasks[0]['id']}/trade",
        {"vendor_id": vendor_id}, expect=200,
    )

    gaps = (await client.get("/api/v1/prequalification/cover-gaps", headers=h)).json()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["vendor_name"] == sold_job["vendor"]["name"]
    assert gap["document_type"] == "general_liability"
    assert gap["days_uncovered"] > 0
    assert gap["already_lapsed"] is False


async def test_an_already_lapsed_certificate_is_marked(client, sold_job):
    h = sold_job["headers"]
    tasks = await _scheduled_job(client, h, sold_job)
    vendor_id = sold_job["vendor"]["id"]

    await _prequalify(client, h, vendor_id, expires=d(-5))
    await post(
        client, h, f"/api/v1/tasks/{tasks[0]['id']}/trade",
        {"vendor_id": vendor_id}, expect=200,
    )

    gaps = (await client.get("/api/v1/prequalification/cover-gaps", headers=h)).json()
    assert len(gaps) == 1
    assert gaps[0]["already_lapsed"] is True


async def test_a_trade_with_no_record_is_not_reported_here(client, sold_job):
    """That is the compliance module's job. Repeating it here would be noise."""
    h = sold_job["headers"]
    tasks = await _scheduled_job(client, h, sold_job)

    await client.patch(
        f"/api/v1/tasks/{tasks[0]['id']}",
        headers=h,
        json={"vendor_id": sold_job["vendor"]["id"]},
    )

    gaps = (await client.get("/api/v1/prequalification/cover-gaps", headers=h)).json()
    assert gaps == []


async def test_completed_tasks_are_not_reported(client, sold_job):
    """Work already done cannot be protected by a renewal."""
    h = sold_job["headers"]
    tasks = await _scheduled_job(client, h, sold_job)
    vendor_id = sold_job["vendor"]["id"]

    await _prequalify(client, h, vendor_id, expires=d(-5))
    await post(
        client, h, f"/api/v1/tasks/{tasks[0]['id']}/trade",
        {"vendor_id": vendor_id}, expect=200,
    )
    await post(client, h, f"/api/v1/tasks/{tasks[0]['id']}/complete", None, expect=200)

    gaps = (await client.get("/api/v1/prequalification/cover-gaps", headers=h)).json()
    assert gaps == []
