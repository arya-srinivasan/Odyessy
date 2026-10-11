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

    def propose(self, msg) -> Plan:
        """Pick up to 2 activities per day: outdoor + indoor on dry days, 2 indoor on rainy days.
        Every day gets its first activity before any day gets a second, so a long trip or a
        city with few activities is spread out instead of front-loaded."""
        interests = [i.lower().removesuffix("s") for i in msg.request.interests]
        unused = self._catalog(msg, interests)  # best first
        dates = trip_days(msg.request.depart_date, msg.request.return_date)

        # Fill rainy days first: they can only use indoor activities, so they get first pick.
        order = sorted(range(len(dates)), key=lambda d: self._is_dry(msg, d))
        picks_by_day = {d: [] for d in order}
        for slot in range(2):
            for d in order:
                dry = self._is_dry(msg, d)
                want_indoor = slot == 1 or not dry  # dry days: outdoor first, then indoor
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
        unused = [a for a in self._catalog(msg) if a["id"] not in in_plan]

        # Keep making the best-value swap (one per slot) until the gap is closed.
        done, saved_total, touched = [], 0.0, set()
        while over > 0:
            swaps = []
            for di, day in enumerate(p.days):
                dry = self._is_dry(msg, day["day"])
                for slot, old in enumerate(day["activities"]):
                    if (di, slot) in touched:
                        continue
                    for new in unused:
                        saved = (old["cost_usd"] - new["cost_usd"]) * travelers
                        if saved <= 0 or new["rating"] < min_rating:
                            continue  # not cheaper, or not good enough for me
                        if not dry and not new["indoor"]:
                            continue  # never put outdoor plans on a rainy day
                        loss = max(0.0, old["rating"] - new["rating"])
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
