"""Gateway checks: chat intake, the negotiation, trip events and the itinerary email.

Runs the six agents in-process (TRAVEL_LOCAL_MODE) and composes the email locally,
so no SLIM, MCP server or LLM is needed. Run from the lungo folder:
    TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
"""

import asyncio
import os
from datetime import date

os.environ.setdefault("TRAVEL_DISABLE_LLM", "true")
os.environ["TRAVEL_LOCAL_MODE"] = "true"

import httpx
import pytest

from travel.email_mcp.server import compose
from travel.gateway import main
from travel.gateway.intake import _heuristic_extract, question_for


async def _local_email(to, plan, request):
    return {**compose(to, plan, request), "via": "local"}


@pytest.fixture(autouse=True)
def fresh_gateway(monkeypatch):
    monkeypatch.setattr(main, "compose_trip_email", _local_email)
    main.sessions.clear()


def call(*requests):
    """POST each (path, body) in order against the app; returns the JSON replies."""
    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            return [(await client.post(path, json=body)) for path, body in requests]
    return asyncio.run(go())


# --- Intake ---

@pytest.mark.parametrize("text, expected", [
    ("from SFO to Tokyo Nov 12 to 16, budget $3,000 for 2 travelers",
     dict(origin="SFO", destination="Tokyo", depart_date="2026-11-12", return_date="2026-11-16",
          budget_usd=3000.0, travelers=2)),
    ("Me and my wife want to visit Paris from NYC, Dec 28 to Jan 4, budget 5k",
     dict(origin="JFK", destination="Paris", depart_date="2026-12-28", return_date="2027-01-04",
          budget_usd=5000.0, travelers=2)),
    ("family of four going to London from LA Nov 28 to 3",
     dict(origin="LAX", destination="London", depart_date="2026-11-28", return_date="2026-12-03", travelers=4)),
    ("NYC to Boston on Nov 12 for 3 nights",
     dict(origin="JFK", destination="Boston", depart_date="2026-11-12", return_date="2026-11-15")),
    ("Paris for a week starting Nov 12", dict(destination="Paris", depart_date="2026-11-12", return_date="2026-11-19")),
])
def test_intake_reads_trip_details(text, expected):
    got = _heuristic_extract(text, today=date(2026, 10, 10))
    assert {k: got.get(k) for k in expected} == expected


def test_intake_reads_interests_and_does_not_mistake_days_for_people():
    got = _heuristic_extract("Tokyo for 5 days, we love temples and sushi", today=date(2026, 10, 10))
    assert "travelers" not in got
    assert set(got["interests"]) == {"temple", "food"}


def test_question_only_asks_for_what_is_missing():
    q = question_for(["origin", "departure date", "return date"])
    assert "where you'd like to go" not in q and "departing from" in q


# --- Endpoints ---

def test_chat_asks_then_plans_a_group_trip():
    first, second = call(
        ("/chat", {"session_id": "s", "message": "Me and my wife want to go to Tokyo"}),
        ("/chat", {"session_id": "s", "message": "from SFO, Nov 12 to 16, budget $3,000", "email": "a@b.co"}),
    )
    assert first.json()["status"] == "needs_info"
    assert "destination" not in first.json()["missing"]
    body = second.json()
    assert body["status"] == "complete"
    assert body["request"]["travelers"] == 2
    assert body["mode"] == "local"
    assert "OVER BUDGET" in body["warnings"]            # two people can't do Tokyo on $3,000
    assert body["email"]["mailto"].startswith("mailto:a@b.co")
    assert "BEGIN:VEVENT" in body["email"]["ics"]


def test_unknown_city_gets_a_friendly_message():
    (r,) = call(("/chat", {"session_id": "s", "message": "SFO to Atlantis Nov 12 to 16"}))
    # Atlantis isn't a known city, so intake can't fill the destination yet
    assert r.json()["status"] == "needs_info"


def test_rain_event_replans_from_the_current_plan():
    _, rain = call(
        ("/chat", {"session_id": "s", "message": "SFO to Tokyo Nov 12 to 16, $9000"}),
        ("/event", {"session_id": "s", "type": "rain", "day": 0}),
    )
    plan = rain.json()["plan"]
    assert plan["forecast"][0]["sky"] == "rain"
    assert all(a["indoor"] for a in plan["days"][0]["activities"])


def test_cancelling_a_booked_flight_rebooks_it():
    (first,) = call(("/chat", {"session_id": "s", "message": "SFO to Tokyo Nov 12 to 16, $9000"}))
    booked = first.json()["plan"]["flight"]["outbound"]["id"]
    (after,) = call(("/event", {"session_id": "s", "type": "cancel", "flight_id": booked}))
    assert after.json()["plan"]["flight"]["outbound"]["id"] != booked


@pytest.mark.parametrize("event", [
    {"type": "rain", "day": 99},
    {"type": "rain"},
    {"type": "cancel", "flight_id": "NOT-BOOKED"},
])
def test_bad_events_are_rejected(event):
    _, r = call(
        ("/chat", {"session_id": "s", "message": "SFO to Tokyo Nov 12 to 16, $9000"}),
        ("/event", {"session_id": "s", **event}),
    )
    assert r.status_code == 422


def test_events_and_commit_need_a_plan_first():
    ev, commit = call(
        ("/event", {"session_id": "nobody", "type": "rain", "day": 0}),
        ("/calendar/commit", {"session_id": "nobody"}),
    )
    assert ev.status_code == 404 and commit.status_code == 404


def test_commit_returns_email_and_calendar():
    _, commit = call(
        ("/chat", {"session_id": "s", "message": "SFO to Boston Nov 12 to 16, $4000"}),
        ("/calendar/commit", {"session_id": "s", "email": "me@x.org"}),
    )
    body = commit.json()
    assert body["status"] == "success"
    assert body["mailto"].startswith("mailto:me@x.org")
    assert body["ics"].startswith("BEGIN:VCALENDAR")
