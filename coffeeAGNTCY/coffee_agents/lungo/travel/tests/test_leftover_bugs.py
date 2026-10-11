"""Bugs found by hand-testing the page (Oct 10). Each test is one thing a traveler typed.

Run from the lungo folder:
    TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
"""

import asyncio
import os
from datetime import date, timedelta

os.environ.setdefault("TRAVEL_DISABLE_LLM", "true")
os.environ["TRAVEL_LOCAL_MODE"] = "true"

import httpx
import pytest

from travel.email_mcp.server import compose
from travel.gateway import main


async def _local_email(to, plan, request):
    return {**compose(to, plan, request), "via": "local"}


@pytest.fixture(autouse=True)
def fresh_gateway(monkeypatch):
    monkeypatch.setattr(main, "compose_trip_email", _local_email)
    main.sessions.clear()


def chat(*messages, session="s1"):
    """Send chat messages in one session; returns the last JSON reply."""
    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            reply = None
            for m in messages:
                reply = (await client.post("/chat", json={"session_id": session, "message": m})).json()
            return reply
    return asyncio.run(go())


def test_unsupported_interests_are_explained():
    r = chat("SFO to Boston Dec 1 to 5, I like skiing")
    assert "don't have skiing" in r["message"]


def test_a_passed_date_says_which_year():
    passed = date.today() - timedelta(days=9)
    if passed.year != date.today().year:
        pytest.skip("needs a date earlier this year")
    r = chat(f"SFO to Tokyo, {passed:%b} {passed.day} to {passed.day + 3 if passed.day < 25 else passed.day}")
    assert f"planned it for {passed.year + 1}" in (r.get("message") or "")


@pytest.mark.parametrize("budget", ["0", "-500"])
def test_impossible_budget_is_explained(budget):
    r = chat(f"SFO to Tokyo Nov 12 to 16, budget {budget}")
    assert "more than $0" in r["message"] and r["request"]["budget_usd"] is None


def test_too_many_travelers_is_explained():
    r = chat("SFO to Tokyo Nov 12 to 16, 100 travelers")
    assert "up to 9 travelers" in r["message"] and r["request"]["travelers"] == 1


def test_short_trips_to_smaller_cities_have_no_empty_days():
    r = chat("SFO to Boston Dec 1 to 9")
    assert all(d["activities"] for d in r["plan"]["days"])


def test_trains_says_when_there_is_no_train():
    r = chat("SFO to Tokyo Nov 12 to 16")
    notes = [l["note"] for l in r["log"] if l["sender"] == "Trains"]
    assert notes[0].startswith("no train connects SFO and Tokyo")


def test_email_is_branded_odyssey():
    r = chat("SFO to Tokyo Nov 12 to 16")
    assert r["email"]["subject"].startswith("Your Odyssey trip")


# --- Rail vs flights: book one way of getting there, not both ---

def test_direct_rail_replaces_flights():
    r = chat("London to Paris, Nov 12 to 14")
    assert r["plan"]["flight"] is None
    assert r["plan"]["train"]["operator"] == "Eurostar" and r["plan"]["train"]["return"]
    assert r["costs"]["train"] == 240 and r["warnings"] == ""


def test_tight_budget_swaps_rail_for_flights():
    r = chat("London to Paris, Nov 12 to 14, budget 400")
    assert r["plan"]["train"] is None and r["plan"]["flight"]["outbound"]


def test_rain_keeps_the_train_without_a_flight_warning():
    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            await client.post("/chat", json={"session_id": "s1", "message": "Tokyo to Kyoto, Nov 12 to 14"})
            return (await client.post("/event", json={"session_id": "s1", "type": "rain", "day": 1})).json()
    r = asyncio.run(go())
    assert r["plan"]["train"] and r["plan"]["flight"] is None and r["warnings"] == ""


def test_trip_email_lists_both_trains():
    r = chat("London to Paris, Nov 12 to 14")
    assert r["email"]["text"].count("Train: Eurostar") == 2
