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


# --- Found in the demo rehearsal ---

def test_unknown_destination_is_named_not_asked_for_again():
    (r,) = call(("/chat", {"session_id": "s", "message": "SFO to Atlantis Nov 12 to 16"}))
    assert r.json()["status"] == "needs_info"
    assert "Atlantis" in r.json()["message"]


def test_two_week_trip_is_planned():
    (r,) = call(("/chat", {"session_id": "s", "message": "SFO to Paris for two weeks starting Nov 12, budget $6,000"}))
    body = r.json()
    assert body["status"] == "complete"
    assert len(body["plan"]["days"]) == 15


def test_past_dates_are_refused():
    (r,) = call(("/chat", {"session_id": "s", "message": "SFO to Tokyo 2025-01-01 to 2025-01-05"}))
    assert r.json()["status"] == "needs_info"
    assert "in the past" in r.json()["message"]


def test_offline_agent_is_reported(monkeypatch):
    from travel.common import channel_local

    every_agent = channel_local.default_agents
    monkeypatch.setattr(channel_local, "default_agents",
                        lambda: [a for a in every_agent() if a.name != "Hotels"])
    (r,) = call(("/chat", {"session_id": "s", "message": "SFO to Tokyo Nov 12 to 16, $5000"}))
    body = r.json()
    assert body["offline_agents"] == ["Hotels"]
    assert "Hotels agent is offline: no hotel booked" in body["warnings"]


# --- Found in the live stress test ---

@pytest.mark.parametrize("text, expected", [
    ("Narnia to Tokyo Nov 12 to 16", dict(origin="Narnia", destination="Tokyo")),
    ("sfo to tokyo nov 12 to 16", dict(origin="SFO", destination="Tokyo")),
    ("from jfk to paris", dict(origin="JFK", destination="Paris")),
    ("I'm lax about plans, Tokyo please", dict(origin=None, destination="Tokyo")),
])
def test_intake_reads_unknown_origins_and_lowercase_codes(text, expected):
    got = _heuristic_extract(text, today=date(2026, 10, 10))
    assert {k: got.get(k) for k in expected} == expected


def test_unknown_origin_is_named():
    (r,) = call(("/chat", {"session_id": "s", "message": "Narnia to Tokyo Nov 12 to 16"}))
    assert r.json()["status"] == "needs_info"
    assert "Narnia" in r.json()["message"]


@pytest.mark.parametrize("message, expected", [
    ("SFO to Paris Nov 20 to Nov 10", "before your departure date"),
    ("SFO to Paris Nov 1 to Dec 20", "That's 49 nights"),
])
def test_unusable_dates_are_explained(message, expected):
    (r,) = call(("/chat", {"session_id": "s", "message": message}))
    assert r.json()["status"] == "needs_info"
    assert expected in r.json()["message"]


# --- Conversation edge cases (live conversation test) ---

def chat_turns(*messages, session="c"):
    return [r.json() for r in call(*[("/chat", {"session_id": session, "message": m}) for m in messages])]


@pytest.mark.parametrize("message, named", [
    ("SFO to Rome Nov 12 to 16", "Rome"),
    ("i want to go to sydney from sfo nov 12 to 16", "Sydney"),
    ("SFO to São Paulo Nov 12 to 16", "São Paulo"),
    ("from Zürich to London Nov 12 to 16", "Zürich"),
    ("New York to the UK Nov 12 to 16", "UK"),
    ("SFO to Paris, Texas Nov 12 to 16", "Paris, Texas"),
    ("SFO to London, Ontario Nov 12 to 16", "London, Ontario"),
    ("SFO to FCO Nov 12 to 16", "FCO"),
])
def test_places_we_cannot_plan_are_named_with_the_supported_list(message, named):
    (r,) = chat_turns(message)
    assert r["status"] == "needs_info"
    assert named in r["message"] and "I can plan trips between" in r["message"]


def test_a_bare_unknown_place_is_flagged_right_away():
    _, r = chat_turns("hi", "Rome")
    assert "don't have travel data for 'Rome'" in r["message"]


def test_qualified_names_of_supported_cities_still_plan():
    (r,) = chat_turns("New York, NY to Tokyo, Japan Nov 12 to 16")
    assert r["status"] == "complete"
    assert (r["request"]["origin"], r["request"]["destination"]) == ("JFK", "Tokyo")


def test_same_city_both_ways_is_explained():
    (r,) = chat_turns("Tokyo to Tokyo Nov 12 to 16")
    assert "You're starting in Tokyo" in r["message"]


def test_answers_one_question_at_a_time():
    *_, last = chat_turns("hello", "Tokyo", "SFO", "November 12", "November 16")
    assert last["status"] == "complete"
    assert (last["request"]["origin"], last["request"]["destination"]) == ("SFO", "Tokyo")


def test_a_correction_keeps_the_destination():
    _, r = chat_turns("to Paris", "from Boston? no wait, from New York")
    assert (r["request"]["origin"], r["request"]["destination"]) == ("JFK", "Paris")


def test_people_joining_are_added():
    *_, r = chat_turns("SFO to Kyoto Nov 12 to 16", "I'm bringing my friend", "and my brother is coming too")
    assert r["request"]["travelers"] == 3


def test_second_destination_is_mentioned():
    (r,) = chat_turns("SFO to Tokyo and Kyoto Nov 12 to 16")
    assert r["status"] == "complete" and "ask me about Kyoto next" in r["message"]


@pytest.mark.parametrize("text, depart, ret", [
    ("SFO to Paris next weekend", "2026-10-16", "2026-10-18"),
    ("SFO to Tokyo tomorrow, back Wednesday", "2026-10-11", "2026-10-14"),
    ("JFK to London in 3 weeks for 5 nights", "2026-10-31", "2026-11-05"),
    ("LA to Tokyo leaving Friday, back Wednesday", "2026-10-16", "2026-10-21"),
])
def test_relative_dates(text, depart, ret):
    got = _heuristic_extract(text, today=date(2026, 10, 10))   # a Saturday
    assert (got.get("depart_date"), got.get("return_date")) == (depart, ret)


@pytest.mark.parametrize("text, origin, destination", [
    ("sf to chicago dec 1 to 5", "SFO", "Chicago"),
    ("san francisco to chicago", "SFO", "Chicago"),
    ("chicago to sf", "Chicago", "San Francisco"),
    ("la to nyc nov 12 to 16", "LAX", "New York"),
    ("new york to london dec 1st-5th", "JFK", "London"),
    ("from home to Tokyo", None, "Tokyo"),
])
def test_lowercase_routes(text, origin, destination):
    got = _heuristic_extract(text, today=date(2026, 10, 10))
    assert (got.get("origin"), got.get("destination")) == (origin, destination)
