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

    def needs_redo(self, msg):
        h = msg.plan.hotel
        if self._wrong_city(msg):
            return f"{h['name']} is in {h['city']}, not {msg.request.destination}; rebooking"
        if h and h.get("nights") != msg.request.nights:
            return f"changed the stay to {msg.request.nights} nights to match the new dates"
        return None

    def propose(self, msg):
        p = msg.plan.model_copy(deep=True)
        if p.hotel and not self._wrong_city(msg):   # dates changed: keep the hotel we agreed on, just fix the nights
            nights = msg.request.nights
            p.hotel = dict(p.hotel, nights=nights, total_usd=p.hotel["price_per_night_usd"] * nights)
            return p
        options = self._options(msg)
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
        options = cheaper_hotels(current, min_rating=self.threshold(msg.round) * 5)
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
        return p, f"switched to {pick['name']} (rated {pick['rating']}) to save ${saved:,.0f}"
