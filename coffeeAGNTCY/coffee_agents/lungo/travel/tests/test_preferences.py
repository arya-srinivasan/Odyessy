"""Traveler preferences reach the plan: where to stay, must-sees, dislikes, interests and pace.

Runs the six agents in-process through the Gateway. Run from the lungo folder:
    TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
"""

import os

os.environ.setdefault("TRAVEL_DISABLE_LLM", "true")
os.environ["TRAVEL_LOCAL_MODE"] = "true"

import pytest

from travel.tests.test_gateway import call, fresh_gateway  # noqa: F401  (fixture used by every test)


def chat(*messages):
    return [r.json() for r in call(*[("/chat", {"session_id": "p", "message": m}) for m in messages])]


def activities(body):
    return [a for d in body["plan"]["days"] for a in d["activities"]]


# --- Where to stay ---

def test_stays_in_the_asked_for_area():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, I want to stay in Ueno")
    assert r["plan"]["hotel"]["area"] == "Ueno"
    assert "staying in Ueno" in r["message"]


def test_area_given_later_moves_the_hotel():
    first, second = chat("SFO to Tokyo Nov 12 to 16", "can we stay in Ginza?")
    assert first["plan"]["hotel"]["area"] != "Ginza"
    assert second["plan"]["hotel"]["area"] == "Ginza"


def test_area_kept_under_a_tight_budget_when_something_there_fits():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, stay in Asakusa, budget $2,000")
    assert r["plan"]["hotel"]["area"] == "Asakusa"
    assert r["total_usd"] <= 2000


def test_leaving_the_area_for_the_budget_is_said():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, stay in Ginza, budget $1,900")
    hotel = r["plan"]["hotel"]
    if hotel["area"] != "Ginza":
        assert f"No hotel in Ginza fit the budget, so you're staying in {hotel['area']}" in r["warnings"]


@pytest.mark.parametrize("message, note", [
    ("SFO to Tokyo Nov 12 to 16, stay in Harajuku", "I don't have hotels listed in Harajuku"),
    ("SFO to Tokyo Nov 12 to 16, stay in Le Marais", "Le Marais"),
])
def test_unusable_areas_are_explained(message, note):
    (r,) = chat(message)
    assert note in r["message"]


def test_a_stray_word_is_not_an_area():
    (r,) = chat("SFO to London Nov 12 to 16, our mission is fun")
    assert r["request"]["hotel_area"] is None and "Mission" not in (r.get("message") or "")


# --- Activities ---

def test_must_sees_are_in_the_plan():
    (r,) = chat("SFO to Paris Nov 12 to 16, I really want to see the Louvre and the Eiffel Tower")
    names = {a["name"] for a in activities(r)}
    assert {"Louvre highlights", "Eiffel Tower summit"} <= names
    assert "must-see" in r["message"]


def test_must_see_survives_a_tight_budget():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, must do the sushi-making class, budget $1,800")
    assert "Sushi-making class" in {a["name"] for a in activities(r)}


def test_must_see_in_another_city_is_flagged():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, I want to see the Louvre")
    assert "Louvre highlights is in Paris, not Tokyo" in r["message"]


def test_disliked_categories_never_appear():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, we love food but not into museums, no nightlife")
    cats = {a["category"] for a in activities(r)}
    assert not cats & {"museum", "nightlife"}
    assert "food" in cats
    assert r["request"]["avoid"] == ["museum", "nightlife"] and r["request"]["interests"] == ["food"]


def test_dislike_added_later_rebuilds_the_days():
    first, second = chat("SFO to Tokyo Nov 12 to 16", "actually, no temples please")
    assert "temple" in {a["category"] for a in activities(first)}
    assert "temple" not in {a["category"] for a in activities(second)}


def test_interest_added_later_shows_up():
    first, second = chat("SFO to London Nov 12 to 16", "we love nightlife")
    assert "nightlife" in {a["category"] for a in activities(second)}


@pytest.mark.parametrize("message, per_day", [
    ("SFO to Paris Nov 12 to 16, relaxed pace please", 1),
    ("SFO to Paris Nov 12 to 16, one thing a day", 1),
    ("SFO to Tokyo Nov 12 to 14, packed schedule", 3),
])
def test_pace(message, per_day):
    (r,) = chat(message)
    counts = [len(d["activities"]) for d in r["plan"]["days"]]
    assert max(counts) == per_day


def test_rain_still_wins_over_an_outdoor_must_see():
    (r,) = chat("SFO to Tokyo Nov 12 to 16, must see Senso-ji")
    call(*[("/event", {"session_id": "p", "type": "rain", "day": d}) for d in range(4)])
    (after,) = [x.json() for x in call(("/event", {"session_id": "p", "type": "rain", "day": 4}))]
    assert not any(not a["indoor"] for a in activities(after))
    assert "Couldn't fit Senso-ji Temple" in after["warnings"]


def test_new_trip_forgets_preferences():
    _, second = chat("SFO to Tokyo Nov 12 to 16, stay in Ginza, no museums, see teamLab", "SFO to Paris Dec 1 to 5")
    req = second["request"]
    assert (req["hotel_area"], req["avoid"], req["must_do"]) == (None, [], [])


# --- With the LLM on, its fields add to the rule-based reading instead of replacing it ---

def test_llm_intake_keeps_preferences_and_cannot_add_unknown_cities(monkeypatch):
    from travel.gateway import intake

    async def fake_llm(schema, messages, fallback=None):
        text = messages[-1]["content"]
        if "Rome" in text:
            return schema(origin="SFO", destination="Rome", depart_date="2026-11-12", return_date="2026-11-16")
        return schema(origin="SFO", destination="Paris", depart_date="2026-11-12", return_date="2026-11-16",
                      interests=["food"])

    monkeypatch.setattr(intake, "llm_structured", fake_llm)
    (rome,) = chat("SFO to Rome Nov 12 to 16")
    assert rome["status"] == "needs_info" and "don't have travel data for 'Rome'" in rome["message"]

    (paris,) = [r.json() for r in call(("/chat", {"session_id": "p2", "message":
        "SFO to Paris Nov 12 to 16, see the Louvre, no nightlife, stay in Montmartre, relaxed"}))]
    req = paris["request"]
    assert req["interests"] == ["food"] and req["avoid"] == ["nightlife"]
    assert req["must_do"] == ["Louvre highlights"] and req["hotel_area"] == "Montmartre"
    assert req["activities_per_day"] == 1
    assert paris["plan"]["hotel"]["area"] == "Montmartre"
