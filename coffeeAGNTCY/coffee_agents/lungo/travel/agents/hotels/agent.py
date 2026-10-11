from travel.common.negotiator import Negotiator
from travel.common.data import cheaper_hotels, resolve_city, search_hotels   # same for any other travel imports

class HotelsAgent(Negotiator):
    name = "Hotels"

    def _options(self, msg):
        if not msg.request.destination:
            return []
        return search_hotels(msg.request.destination, nights=msg.request.nights,
                             area=msg.request.hotel_area)

    def has_my_part(self, msg):
        """Once I've spoken, my decision stands, even if that was 'no hotel found'."""
        return msg.plan.hotel is not None or any(l.sender == self.name for l in msg.history)

    def _wrong_city(self, msg) -> bool:
        city = resolve_city(msg.request.destination or "")
        return bool(msg.plan.hotel and city and msg.plan.hotel["city"] != city["city"])

    def _wrong_area(self, msg) -> bool:
        """A neighborhood was asked for, the hotel isn't in it, and the city has hotels there."""
        area, h = msg.request.hotel_area, msg.plan.hotel
        if not area or not h or h["area"].lower() == area.strip().lower():
            return False
        return any(o["area"].lower() == area.strip().lower() for o in self._options(msg))

    def needs_redo(self, msg):
        h = msg.plan.hotel
        if self._wrong_city(msg):
            return f"{h['name']} is in {h['city']}, not {msg.request.destination}; rebooking"
        if self._wrong_area(msg) and self._area_fits_budget(msg):
            return f"moved the stay to {msg.request.hotel_area} as asked"
        if h and h.get("nights") != msg.request.nights:
            return f"changed the stay to {msg.request.nights} nights to match the new dates"
        return None

    def propose(self, msg):
        p = msg.plan.model_copy(deep=True)
        if p.hotel and not self._wrong_city(msg) and not self._wrong_area(msg):   # dates changed: keep the hotel, fix the nights
            nights = msg.request.nights
            p.hotel = dict(p.hotel, nights=nights, total_usd=p.hotel["price_per_night_usd"] * nights)
            return p
        options = self._options(msg)
        if self._wrong_area(msg) and msg.request.budget_usd:
            # moving back into the asked-for area: the best-rated hotel there that keeps the budget
            area = msg.request.hotel_area.strip().lower()
            for o in options:
                trial = msg.plan.model_copy(deep=True)
                trial.hotel = o
                if o["area"].lower() == area and trial.total(msg.request.travelers) <= msg.request.budget_usd:
                    p.hotel = o
                    return p
        p.hotel = options[0] if options else None
        return p

    def satisfaction(self, msg): 
        if msg.plan.hotel is None:
            return 0.0 if self._options(msg) else 1.0   # nothing to book is not my failure
        return msg.plan.hotel["rating"] / 5

    def improve(self, msg):
        if "Budget" not in msg.objections or not msg.request.budget_usd or not msg.plan.hotel:
            return None
        over = msg.plan.total(msg.request.travelers) - msg.request.budget_usd
        if over <= 0:
            return None
        current = msg.plan.hotel
        options = cheaper_hotels(current, min_rating=self.threshold(msg.round) * 5, area=msg.request.hotel_area)
        if not options:
            return None
        nights = current.get("nights", msg.request.nights)
        rooms = msg.request.rooms
        # best-rated hotel that saves enough; otherwise the biggest saving available
        enough = [h for h in options
                if (current["price_per_night_usd"] - h["price_per_night_usd"]) * nights * rooms >= over]
        pick = max(enough, key=lambda h: h["rating"]) if enough else min(options, key=lambda h: h["price_per_night_usd"])
        p = msg.plan.model_copy(deep=True)
        p.hotel = dict(pick, nights=nights)
        saved = (current["price_per_night_usd"] - pick["price_per_night_usd"]) * nights * rooms
        moved = f", leaving {current['area']}" if pick["area"] != current["area"] else ""
        return p, f"switched to {pick['name']} (rated {pick['rating']}) to save ${saved:,.0f}{moved}"

    def _area_fits_budget(self, msg) -> bool:
        """Would the cheapest hotel in the asked-for area keep the trip within budget?
        If not, staying outside it (after a budget downgrade) is the right call."""
        r = msg.request
        if not r.budget_usd:
            return True
        in_area = [o for o in self._options(msg) if o["area"].lower() == r.hotel_area.strip().lower()]
        cheapest = min(in_area, key=lambda o: o["price_per_night_usd"])
        p = msg.plan.model_copy(deep=True)
        p.hotel = dict(cheapest, nights=r.nights)
        return p.total(r.travelers) <= r.budget_usd
