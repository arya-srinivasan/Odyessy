"""Edge cases found by stress testing: group trips, budgets, bad input and re-planning.

Run from the lungo folder:
    TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
"""

import asyncio
import os

os.environ.setdefault("TRAVEL_DISABLE_LLM", "true")  # fast, free, same result every run

import pytest

from travel.common.channel_local import default_agents, run_local
from travel.common.data import trip_days
from travel.common.models import Plan, TripRequest
from travel.email_mcp.server import compose


def tokyo(**changes) -> TripRequest:
    """The demo trip: SFO to Tokyo, Nov 12-16, $2,000. Pass keyword args to change any field."""
    base = dict(origin="SFO", destination="Tokyo", depart_date="2026-11-12",
                return_date="2026-11-16", budget_usd=2000)
    base.update(changes)
    return TripRequest(**base)


def run(request, agents=None, plan=None):
    return asyncio.run(run_local(request, agents or default_agents(), plan=plan, verbose=False))


# --- Budget is a hard limit, and group trips cost more ---

@pytest.mark.parametrize("travelers", [2, 3, 4])
def test_group_trip_over_budget_is_never_approved_silently(travelers):
    m = run(tokyo(travelers=travelers))
    total = m.plan.total(travelers)
    assert total > 2000  # two or more people can't fly SFO-Tokyo for $2,000
    assert "OVER BUDGET" in m.note
    budget_accepts = [line for line in m.history if line.sender == "Budget" and line.move == "ACCEPT"]
    assert not budget_accepts


def test_group_trip_with_enough_budget_reaches_consensus():
    m = run(tokyo(travelers=2, budget_usd=8000))
    assert set(m.accepts) == set(m.participants)
    assert m.plan.total(2) <= 8000
    assert "OVER BUDGET" not in m.note


def test_vote_picks_the_cheapest_version_when_nothing_fits():
    m = run(tokyo(travelers=2))
    totals = [v.total(2) for v in m.versions.values() if v.flight and v.hotel and v.days]
    assert m.plan.total(2) == min(totals)


def test_hotel_rooms_scale_with_travelers():
    plan = Plan(hotel={"price_per_night_usd": 100, "nights": 2})
    assert plan.costs(1)["hotel"] == 200
    assert plan.costs(2)["hotel"] == 200  # two share a room
    assert plan.costs(3)["hotel"] == 400
    assert tokyo(travelers=5).rooms == 3


# --- Bad input is cleaned up instead of crashing an agent ---

def test_zero_travelers_and_zero_budget_are_cleaned_up():
    r = tokyo(travelers=0, budget_usd=0)
    assert r.travelers == 1
    assert r.budget_usd is None


@pytest.mark.parametrize("depart, ret, expected_return", [
    ("not a date", "2026-11-16", "2026-11-16"),
    ("2026-11-16", "2026-11-12", None),   # return before departure
    ("2026-11-01", "2026-12-20", None),   # longer than we plan for
])
def test_bad_dates_are_dropped_so_the_gateway_asks_again(depart, ret, expected_return):
    r = tokyo(depart_date=depart, return_date=ret)
    assert r.return_date == expected_return
    if depart == "not a date":
        assert r.depart_date is None
        assert "depart_date" in r.missing_fields()


def test_trip_without_dates_does_not_crash():
    m = run(tokyo(depart_date=None, return_date=None))
    assert m.type == "FINAL"
    assert m.plan.flight and m.plan.hotel


def test_missing_return_date_uses_the_same_length_everywhere():
    r = tokyo(return_date=None)
    assert len(trip_days(r.depart_date, r.return_date)) == r.nights + 1


# --- Re-planning an existing trip never brings back stale versions ---

def test_cancelling_the_booked_flights_rebooks_them():
    first = run(tokyo(budget_usd=5000))
    booked = [first.plan.flight[leg]["id"] for leg in ("outbound", "return")]
    m = run(tokyo(budget_usd=5000, cancelled_flights=booked), plan=first.plan)
    assert m.type == "FINAL"
    assert not {m.plan.flight[leg]["id"] for leg in ("outbound", "return")} & set(booked)


def test_new_dates_resize_the_hotel_stay_and_schedule():
    first = run(tokyo(budget_usd=5000))
    m = run(tokyo(budget_usd=5000, return_date="2026-11-14"), plan=first.plan)
    assert m.plan.hotel["nights"] == 2
    assert [d["date"] for d in m.plan.days] == ["2026-11-12", "2026-11-13", "2026-11-14"]


def test_new_destination_rebooks_everything_there():
    first = run(tokyo(budget_usd=5000))
    m = run(tokyo(budget_usd=5000, destination="Paris"), plan=first.plan)
    assert m.plan.hotel["city"] == "Paris"
    assert m.plan.flight["outbound"]["destination_city"] == "Paris"
    assert all(a["city"] == "Paris" for d in m.plan.days for a in d["activities"])


# --- The itinerary email ---

def test_calendar_file_has_every_activity():
    m = run(tokyo(budget_usd=5000))
    out = compose("traveler@example.com", m.plan, m.request)
    activities = sum(len(d["activities"]) for d in m.plan.days)
    for d in m.plan.days:
        for a in d["activities"]:
            assert a["name"].replace(",", "\\,") in out["ics"]
    assert out["ics"].count("BEGIN:VEVENT") >= activities


def test_email_warns_when_over_budget():
    m = run(tokyo(travelers=2))
    assert "OVER budget" in compose("traveler@example.com", m.plan, m.request)["text"]
