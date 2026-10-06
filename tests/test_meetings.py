"""The advanced meetings module: closing actions, carrying them into a follow-up,
the meeting lifecycle, and correspondence that demands a reply.
"""

from datetime import date, timedelta

from tests.conftest import post


async def _meeting(client, h, job_id, **extra):
    return await post(
        client, h, "/api/v1/meetings",
        {"title": "Weekly trade coordination", "job_id": job_id, **extra},
    )


async def _add(client, h, mid, topic, **extra):
    return await post(
        client, h, f"/api/v1/meetings/{mid}/items", {"topic": topic, **extra}, expect=200
    )


async def test_create_carries_a_type(client, sold_job):
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"], meeting_type="pre_construction")
    assert m["meeting_type"] == "pre_construction"
    assert m["status"] == "scheduled"


async def test_an_action_can_be_completed_and_reopened(client, sold_job):
    """The queue the old screen could count but never close."""
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"])
    m = await _add(client, h, m["id"], "Chase the electrical submittal", is_action=True)
    item_id = m["items"][0]["id"]
    assert m["open_actions"] == 1

    done = await post(
        client, h, f"/api/v1/meetings/items/{item_id}/complete", None, expect=200
    )
    assert done["open_actions"] == 0
    assert done["items"][0]["completed_on"] is not None
    assert done["items"][0]["is_open_action"] is False

    again = await post(
        client, h, f"/api/v1/meetings/items/{item_id}/reopen", None, expect=200
    )
    assert again["open_actions"] == 1


async def test_completing_a_discussion_item_is_refused(client, sold_job):
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"])
    m = await _add(client, h, m["id"], "Framing progress", is_action=False)
    refused = await client.post(
        f"/api/v1/meetings/items/{m['items'][0]['id']}/complete", headers=h
    )
    assert refused.status_code == 409
    assert "discussion item" in refused.json()["detail"]


async def test_overdue_action_is_flagged(client, sold_job):
    h = sold_job["headers"]
    past = (date.today() - timedelta(days=2)).isoformat()
    m = await _meeting(client, h, sold_job["job"]["id"])
    m = await _add(client, h, m["id"], "Overdue thing", is_action=True, due_on=past)
    assert m["items"][0]["is_overdue"] is True
    assert m["overdue_actions"] == 1


async def test_follow_up_carries_open_actions_only(client, sold_job):
    """A follow-up meeting pulls the still-open actions forward, linked back to where
    they were raised — a completed action and a discussion item do not come across."""
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"])
    m = await _add(client, h, m["id"], "Discussion only", is_action=False)
    m = await _add(client, h, m["id"], "Open action", is_action=True)
    m = await _add(client, h, m["id"], "Done action", is_action=True)
    open_item = next(i for i in m["items"] if i["topic"] == "Open action")
    done_item = next(i for i in m["items"] if i["topic"] == "Done action")
    await post(client, h, f"/api/v1/meetings/items/{done_item['id']}/complete", None, expect=200)

    follow = await post(client, h, f"/api/v1/meetings/{m['id']}/follow-up", None, expect=201)
    assert follow["previous_meeting_id"] == m["id"]
    assert follow["status"] == "scheduled"
    assert follow["title"].endswith("(follow-up)")
    # Only the one open action came across.
    assert len(follow["items"]) == 1
    carried = follow["items"][0]
    assert carried["topic"] == "Open action"
    assert carried["is_action"] is True
    assert carried["carried_from_id"] == open_item["id"]


async def test_lifecycle_guards(client, sold_job):
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"])
    held = await post(client, h, f"/api/v1/meetings/{m['id']}/hold", None, expect=200)
    assert held["status"] == "held"
    assert held["meeting_date"] is not None      # set from today if none was given

    refused = await client.post(f"/api/v1/meetings/{m['id']}/cancel", headers=h)
    assert refused.status_code == 409             # a held meeting's minutes stand


async def test_item_can_be_removed(client, sold_job):
    h = sold_job["headers"]
    m = await _meeting(client, h, sold_job["job"]["id"])
    m = await _add(client, h, m["id"], "Mistake", is_action=False)
    item_id = m["items"][0]["id"]
    after = await client.delete(f"/api/v1/meetings/{m['id']}/items/{item_id}", headers=h)
    assert after.status_code == 200
    assert after.json()["item_count"] == 0


async def test_correspondence_awaiting_a_reply(client, sold_job):
    h = sold_job["headers"]
    past = (date.today() - timedelta(days=1)).isoformat()
    c = await post(
        client, h, "/api/v1/correspondence",
        {"subject": "Notice of delay", "kind": "letter", "direction": "outbound",
         "job_id": sold_job["job"]["id"], "requires_response": True, "response_due": past},
    )
    assert c["is_awaiting_response"] is True
    assert c["is_overdue"] is True

    awaiting = (
        await client.get("/api/v1/correspondence", headers=h, params={"awaiting_only": True})
    ).json()
    assert c["id"] in [x["id"] for x in awaiting]

    replied = await post(
        client, h, f"/api/v1/correspondence/{c['id']}/respond", None, expect=200
    )
    assert replied["is_awaiting_response"] is False
    assert replied["responded_on"] is not None


async def test_summary_counts_meetings_actions_and_correspondence(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    m = await _meeting(client, h, job_id, meeting_type="coordination")
    await _add(client, h, m["id"], "Open action", is_action=True)
    await post(client, h, "/api/v1/correspondence",
               {"subject": "Query", "requires_response": True, "job_id": job_id})

    s = (await client.get("/api/v1/meetings/summary", headers=h, params={"job_id": job_id})).json()
    assert s["meetings"] == 1
    assert s["scheduled"] == 1
    assert s["open_actions"] == 1
    assert s["correspondence"] == 1
    assert s["awaiting_response"] == 1
    assert s["by_type"]["coordination"] == 1
