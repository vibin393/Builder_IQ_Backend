"""Advanced homeowner surveys: lifecycle, reminders, detractor follow-up, star rating
and the NPS analytics. Asserts exact figures - a satisfaction dashboard that shows the
wrong NPS is worse than one that shows none."""

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.models.marketing import Survey
from app.services import survey as svc
from tests.conftest import post

# --- pure rules ---------------------------------------------------------------


def test_star_rating_bands():
    assert svc.star_rating(None) is None
    assert svc.star_rating(95) == 5
    assert svc.star_rating(90) == 5
    assert svc.star_rating(85) == 4
    assert svc.star_rating(72) == 3
    assert svc.star_rating(61) == 2
    assert svc.star_rating(40) == 1


def test_due_date_is_three_weeks_out():
    assert svc.due_date_for(date(2026, 1, 1)) == date(2026, 1, 22)


# --- lifecycle over HTTP ------------------------------------------------------


async def test_send_sets_pending_status_and_response_window(client, sold_job):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys",
                   {"job_id": sold_job["job"]["id"], "stage": "eleven_month"})
    assert s["status"] == "pending"
    assert s["sent_date"] == date.today().isoformat()
    assert s["due_date"] == (date.today() + timedelta(days=21)).isoformat()
    assert s["buyer_id"] is not None


async def test_promoter_response_needs_no_follow_up(client, sold_job):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    r = await post(client, h, f"/api/v1/surveys/{s['id']}/respond",
                   {"would_recommend": 10, "rating_quality": 5, "overall_satisfaction": 5},
                   expect=200)
    assert r["status"] == "responded"
    assert r["nps_band"] == "promoter"
    assert r["follow_up_required"] is False
    assert r["responded_date"] == date.today().isoformat()


async def test_detractor_response_auto_flags_follow_up(client, sold_job):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    r = await post(client, h, f"/api/v1/surveys/{s['id']}/respond",
                   {"would_recommend": 3, "comments": "Snags still open after move-in."},
                   expect=200)
    assert r["nps_band"] == "detractor"
    assert r["follow_up_required"] is True
    assert r["needs_follow_up"] is True

    # Log the follow-up and close it out.
    owner = None
    done = await post(client, h, f"/api/v1/surveys/{s['id']}/follow-up",
                      {"follow_up_notes": "Called, booked a snagging visit.",
                       "follow_up_done": True, "follow_up_owner_id": owner}, expect=200)
    assert done["follow_up_done"] is True
    assert done["needs_follow_up"] is False


async def test_remind_only_while_pending(client, sold_job):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    r = await post(client, h, f"/api/v1/surveys/{s['id']}/remind", None, expect=200)
    assert r["reminder_count"] == 1
    assert r["last_reminded_date"] == date.today().isoformat()

    await post(client, h, f"/api/v1/surveys/{s['id']}/respond", {"would_recommend": 8}, expect=200)
    # Cannot chase a survey that has already been answered.
    await post(client, h, f"/api/v1/surveys/{s['id']}/remind", None, expect=409)


async def test_decline_blocks_after_response(client, sold_job):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    d = await post(client, h, f"/api/v1/surveys/{s['id']}/decline", None, expect=200)
    assert d["status"] == "declined"

    s2 = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    await post(client, h, f"/api/v1/surveys/{s2['id']}/respond", {"would_recommend": 9}, expect=200)
    await post(client, h, f"/api/v1/surveys/{s2['id']}/decline", None, expect=409)


async def test_filters_band_status_and_follow_up(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    promoter = await post(client, h, "/api/v1/surveys", {"job_id": job_id})
    await post(client, h, f"/api/v1/surveys/{promoter['id']}/respond",
               {"would_recommend": 10}, expect=200)
    detractor = await post(client, h, "/api/v1/surveys", {"job_id": job_id})
    await post(client, h, f"/api/v1/surveys/{detractor['id']}/respond",
               {"would_recommend": 2}, expect=200)
    pending = await post(client, h, "/api/v1/surveys", {"job_id": job_id})

    det = (await client.get("/api/v1/surveys?band=detractor", headers=h)).json()
    assert {x["id"] for x in det} == {detractor["id"]}

    pend = (await client.get("/api/v1/surveys?status=pending", headers=h)).json()
    assert {x["id"] for x in pend} == {pending["id"]}

    fu = (await client.get("/api/v1/surveys?needs_follow_up=true", headers=h)).json()
    assert {x["id"] for x in fu} == {detractor["id"]}


async def test_summary_nps_star_rating_and_breakdowns(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    # 3 promoters, 1 detractor -> NPS = (75% - 25%) = 50; recommend 75% -> 3 stars.
    for score in (10, 9, 9):
        s = await post(client, h, "/api/v1/surveys", {"job_id": job_id, "stage": "thirty_day"})
        await post(client, h, f"/api/v1/surveys/{s['id']}/respond",
                   {"would_recommend": score, "rating_communication": 5, "rating_value": 4,
                    "overall_satisfaction": 5}, expect=200)
    d = await post(client, h, "/api/v1/surveys", {"job_id": job_id, "stage": "eleven_month"})
    await post(client, h, f"/api/v1/surveys/{d['id']}/respond",
               {"would_recommend": 2, "overall_satisfaction": 2}, expect=200)
    # One still pending (unanswered) -> response rate 4/5.
    await post(client, h, "/api/v1/surveys", {"job_id": job_id})

    summ = (await client.get("/api/v1/surveys/summary", headers=h)).json()
    assert summ["sent"] == 5
    assert summ["responses"] == 4
    assert summ["response_rate"] == 0.8
    assert summ["nps"] == 50
    assert summ["pct_recommend"] == 75.0
    assert summ["star_rating"] == 3
    assert summ["promoters"] == 3 and summ["detractors"] == 1
    assert summ["detractors_open"] == 1
    assert summ["ratings"]["communication"] == 5.0
    assert summ["ratings"]["value"] == 4.0
    assert summ["avg_overall"] is not None
    # by_stage carries every stage; thirty_day has 3 responses, eleven_month 1.
    by_stage = {b["stage"]: b for b in summ["by_stage"]}
    assert by_stage["thirty_day"]["responses"] == 3
    assert by_stage["eleven_month"]["responses"] == 1
    assert len(summ["trend"]) >= 1


async def test_overdue_pending_counted(client, sold_job, db_session):
    h = sold_job["headers"]
    s = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    row = await db_session.scalar(select(Survey).where(Survey.id == uuid.UUID(s["id"])))
    row.due_date = date.today() - timedelta(days=1)
    await db_session.commit()

    detail = (await client.get("/api/v1/surveys?status=pending", headers=h)).json()[0]
    assert detail["is_overdue"] is True

    summ = (await client.get("/api/v1/surveys/summary", headers=h)).json()
    assert summ["overdue"] == 1
