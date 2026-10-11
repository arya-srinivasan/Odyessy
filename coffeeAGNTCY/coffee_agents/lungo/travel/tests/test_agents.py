"""End-to-end checks for the six travel agents on the local channel.

Run from the lungo folder:
    TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
"""

import asyncio
import os

os.environ.setdefault("TRAVEL_DISABLE_LLM", "true")  # fast, free, same result every run

from travel.agents.activities.agent import ActivitiesAgent
from travel.agents.budget.agent import BudgetAgent
from travel.agents.weather.agent import WeatherAgent
from travel.common.channel_local import default_agents, run_local
from travel.common.data import outdoor_on_rain_days
from travel.common.models import TripRequest


def tokyo(**changes) -> TripRequest:
    """The demo trip: SFO to Tokyo, Nov 12-16, $2,000. Pass keyword args to change any field."""
    base = dict(origin="SFO", destination="Tokyo", depart_date="2026-11-12",
                return_date="2026-11-16", budget_usd=2000)
    base.update(changes)
    return TripRequest(**base)


def run(request, agents=None, plan=None):
    return asyncio.run(run_local(request, agents or default_agents(), plan=plan, verbose=False))


def activity_names(plan, day):
    return [a["name"] for a in plan.days[day]["activities"]]


def test_six_agents_reach_consensus():
    m = run(tokyo())
    assert m.type == "FINAL"
    assert all(line.sender != "Vote" for line in m.history), "ended by ranked vote, not agreement"
    assert set(m.accepts) == set(m.participants)
    assert m.plan.forecast and m.plan.flight and m.plan.hotel and m.plan.days
    assert m.plan.total(m.request.travelers) <= m.request.budget_usd


def test_no_outdoor_activity_on_a_rainy_day():
    m = run(tokyo())
    assert outdoor_on_rain_days(m.plan.forecast, m.plan.days) == 0


def test_rain_added_mid_trip_moves_outdoor_plans():
    first = run(tokyo())
    m = run(tokyo(rain_days=[1]), plan=first.plan)
    assert m.type == "FINAL"
    assert m.plan.forecast[1]["sky"] == "rain"
    assert all(a["indoor"] for a in m.plan.days[1]["activities"])
    assert outdoor_on_rain_days(m.plan.forecast, m.plan.days) == 0


def test_interests_come_first():
    m = run(tokyo(interests=["temples"]), agents=[WeatherAgent(), ActivitiesAgent()])
    first_two_days = activity_names(m.plan, 0) + activity_names(m.plan, 1)
    assert "Senso-ji Temple" in first_two_days and "Meiji Shrine" in first_two_days


def test_no_activity_repeats():
    m = run(tokyo())
    ids = [a["id"] for d in m.plan.days for a in d["activities"]]
    assert len(ids) == len(set(ids))


def test_activities_swap_to_fit_a_tight_budget():
    m = run(tokyo(budget_usd=200), agents=[WeatherAgent(), ActivitiesAgent(), BudgetAgent()])
    assert m.plan.total() <= 200
    assert outdoor_on_rain_days(m.plan.forecast, m.plan.days) == 0


def test_trains_does_not_re_add_a_dropped_train():
    m = run(tokyo())
    trains = [line for line in m.history if line.sender == "Trains"]
    dropped = [i for i, line in enumerate(trains) if "dropped" in line.note]
    if dropped:
        later = trains[dropped[0] + 1:]
        assert not any("added my part" in line.note for line in later)


# --- Bad input is stopped at the door with a friendly message, not a crash ---

import pytest


@pytest.mark.parametrize("changes, expected", [
    (dict(destination="Atlantis"), "don't have travel data for 'Atlantis'"),
    (dict(origin=None), "Where are you flying from?"),
    (dict(origin="BOS"), "don't have flights from Boston to Tokyo"),
])
def test_bad_requests_are_stopped_with_a_clear_message(changes, expected):
    with pytest.raises(ValueError, match=expected):
        run(tokyo(**changes))
