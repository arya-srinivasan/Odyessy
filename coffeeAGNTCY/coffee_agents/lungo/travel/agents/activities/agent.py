"""Activities Agent for TravelAgntcy.

Owns plan.days: two activities per day, outdoor ones only on dry days.
"""

from travel.common.data import outdoor_on_rain_days, resolve_city, search_activities, trip_days
from travel.common.models import Plan
from travel.common.negotiator import Negotiator


class ActivitiesAgent(Negotiator):
    name = "Activities"
    goal = "great activities that fit the weather"

    def has_my_part(self, msg) -> bool:
        """Have I filled in the daily activities yet? (Nothing to fill without dates.)"""
        return bool(msg.plan.days) or not trip_days(msg.request.depart_date, msg.request.return_date)

    def _catalog(self, msg, interests=None) -> list[dict]:
        if not msg.request.destination:
            return []
        return search_activities(msg.request.destination, interests)

    def _is_dry(self, msg, day: int) -> bool:
        """Is this day OK for outdoor plans? If there's no forecast yet, assume yes."""
        for f in msg.plan.forecast:
            if f["day"] == day:
                return f["outdoor_ok"]
        return True

    def _wanted(self, msg) -> tuple[list[dict], set[str], int]:
        """The traveler's preferences: activities they can have (best first, must-sees at the
        front, avoided categories gone), the must-see names, and activities per day."""
        r = msg.request
        interests = [i.lower().removesuffix("s") for i in r.interests]
        avoid = {a.lower().removesuffix("s") for a in r.avoid}
        catalog = [a for a in self._catalog(msg, interests) if a["category"] not in avoid]
        must = {n.lower() for n in r.must_do}
        catalog.sort(key=lambda a: a["name"].lower() not in must)   # stable: must-sees first
        return catalog, {a["name"].lower() for a in catalog if a["name"].lower() in must}, r.activities_per_day or 2

    def propose(self, msg) -> Plan:
        """Pick activities_per_day (default 2) per day: outdoor + indoor on dry days, indoor on
        rainy days. Must-sees go in first, then interests; avoided categories never appear.
        Every day gets its first activity before any day gets a second, so a long trip or a
        city with few activities is spread out instead of front-loaded."""
        unused, must, per_day = self._wanted(msg)
        dates = trip_days(msg.request.depart_date, msg.request.return_date)

        # Fill rainy days first: they can only use indoor activities, so they get first pick.
        order = sorted(range(len(dates)), key=lambda d: self._is_dry(msg, d))
        picks_by_day = {d: [] for d in order}
        # Must-sees first, each on the first day that suits it (outdoor ones need a dry day).
        for a in [a for a in unused if a["name"].lower() in must]:
            day = next((d for d in sorted(order) if len(picks_by_day[d]) < per_day
                        and (a["indoor"] or self._is_dry(msg, d))), None)
            if day is not None:
                picks_by_day[day].append(a)
                unused.remove(a)
        for slot in range(per_day):
            for d in order:
                if len(picks_by_day[d]) > slot:
                    continue
                dry = self._is_dry(msg, d)
                want_indoor = slot % 2 == 1 or not dry  # dry days: outdoor first, then indoor
                match = next((a for a in unused if a["indoor"] == want_indoor), None)
                if match is None and dry:
                    match = next(iter(unused), None)  # dry day: any type is fine
                if match:
                    picks_by_day[d].append(match)
                    unused.remove(match)

        p = msg.plan.model_copy(deep=True)
        p.days = [{"day": d, "date": dates[d], "activities": picks_by_day[d]}
                  for d in range(len(dates))]
        return p

    def satisfaction(self, msg) -> float:
        """0..1: average rating of my picks, minus 0.5 for each outdoor activity on a rainy day."""
        picks = [a for d in msg.plan.days for a in d["activities"]]
        if not picks:
            return 0.0 if msg.plan.days and self._catalog(msg) else 1.0   # nothing to plan
        avg = sum(a["rating"] for a in picks) / len(picks) / 5
        clashes = outdoor_on_rain_days(msg.plan.forecast, msg.plan.days)
        return max(0.0, avg - 0.5 * clashes)

    def needs_redo(self, msg) -> str | None:
        """Rebuild my schedule if it no longer fits the trip dates or the weather."""
        dates = trip_days(msg.request.depart_date, msg.request.return_date)
        if len(msg.plan.days) != len(dates) or any(
                d.get("date") != dates[i] for i, d in enumerate(msg.plan.days)):
            return "rebuilt the schedule to match the trip dates"
        city = resolve_city(msg.request.destination or "")
        if city and any(a["city"] != city["city"] for d in msg.plan.days for a in d["activities"]):
            return f"rebuilt the schedule for {city['city']}"
        if outdoor_on_rain_days(msg.plan.forecast, msg.plan.days) > 0:
            return "moved outdoor plans off the rainy days"
        return self._preferences_unmet(msg)

    def _preferences_unmet(self, msg) -> str | None:
        """A preference the schedule doesn't reflect (e.g. one added after it was built)."""
        r = msg.request
        planned = [a for d in msg.plan.days for a in d["activities"]]
        avoid = {a.lower().removesuffix("s") for a in r.avoid}
        if any(a["category"] in avoid for a in planned):
            return f"took out the {', '.join(sorted(avoid))} activities"
        per_day = r.activities_per_day or 2
        if any(len(d["activities"]) > per_day for d in msg.plan.days):
            return f"cut back to {per_day} activit{'y' if per_day == 1 else 'ies'} a day"
        fresh = self.propose(msg).days
        _, must, _ = self._wanted(msg)
        names = {a["name"].lower() for a in planned}
        if must - names and {a["name"].lower() for d in fresh for a in d["activities"]} & (must - names):
            return "added the must-see " + ", ".join(sorted(must - names))
        if "Budget" in msg.objections:
            return None   # the budget wins: don't add back what a budget swap took out
        if per_day > 2 and sum(len(d["activities"]) for d in fresh) > len(planned):
            return f"added more to do: up to {per_day} activities a day"
        interests = {i.lower().removesuffix("s") for i in r.interests}
        have = {a["category"] for a in planned}
        offered = {a["category"] for d in fresh for a in d["activities"]}
        if (interests & offered) - have:
            return "reworked the days around " + ", ".join(sorted((interests & offered) - have))
        return None

    def improve(self, msg) -> tuple[Plan, str] | None:
        """When Budget objects, make best-value swaps (one per slot) until the gap is closed."""
        budget = msg.request.budget_usd
        if "Budget" not in msg.objections or not budget:
            return None
        over = msg.plan.total(msg.request.travelers) - budget
        if over <= 0:
            return None  # someone earlier this round already fixed it

        min_rating = self.threshold(msg.round) * 5
        travelers = msg.request.travelers
        p = msg.plan.model_copy(deep=True)
        in_plan = {a["id"] for d in p.days for a in d["activities"]}
        allowed, must, _ = self._wanted(msg)
        unused = [a for a in allowed if a["id"] not in in_plan]
        interests = {i.lower().removesuffix("s") for i in msg.request.interests}

        # Keep making the best-value swap (one per slot) until the gap is closed.
        done, saved_total, touched = [], 0.0, set()
        while over > 0:
            swaps = []
            for di, day in enumerate(p.days):
                dry = self._is_dry(msg, day["day"])
                for slot, old in enumerate(day["activities"]):
                    if (di, slot) in touched or old["name"].lower() in must:
                        continue  # never trade away a must-see
                    for new in unused:
                        saved = (old["cost_usd"] - new["cost_usd"]) * travelers
                        if saved <= 0 or new["rating"] < min_rating:
                            continue  # not cheaper, or not good enough for me
                        if not dry and not new["indoor"]:
                            continue  # never put outdoor plans on a rainy day
                        loss = max(0.0, old["rating"] - new["rating"])
                        if old["category"] in interests and new["category"] not in interests:
                            loss += 1.0  # keep what they said they love, if there's any other way
                        swaps.append({"value": saved / (loss + 0.1), "saved": saved,
                                      "day": di, "slot": slot, "old": old, "new": new})
            if not swaps:
                break
            enough = [s for s in swaps if s["saved"] >= over]
            pick = max(enough or swaps, key=lambda s: s["value"])
            p.days[pick["day"]]["activities"][pick["slot"]] = pick["new"]
            unused.remove(pick["new"])
            touched.add((pick["day"], pick["slot"]))
            over -= pick["saved"]
            saved_total += pick["saved"]
            done.append(f"{pick['old']['name']} -> {pick['new']['name']}")
        if not done:
            return None
        return p, f"swapped {'; '.join(done)} to save ${saved_total:,.0f}"
