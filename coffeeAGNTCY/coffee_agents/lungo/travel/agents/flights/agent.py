from travel.common.negotiator import Negotiator
from travel.common.data import resolve_city, search_flights

def _hours(flight: dict | None) -> float:
    if not flight:
        return 0.0
    return sum(f["duration_hours"] for f in (flight.get("outbound"), flight.get("return")) if f)



class FlightsAgent(Negotiator):
    name = "Flights"
    goal = "shortest travel time"
    base_threshold= 0.9
    concession = 0.15

    def has_my_part(self, msg):
        # A booked train covers the journey, so there is nothing for me to add.
        return msg.plan.flight is not None or msg.plan.train is not None

    def _search(self, frm, to, msg):
        if not frm or not to:
            return []
        return search_flights(frm, to, exclude_ids=msg.request.cancelled_flights, limit=10)

    def propose(self, msg):
        r = msg.request
        p = msg.plan.model_copy(deep=True)
        out = self._search(r.origin, r.destination, msg)
        back = self._search(r.destination, r.origin, msg)
        p.flight = {
            "outbound": min(out, key=lambda f: f["duration_hours"]) if out else None,
            "return": min(back, key=lambda f: f["duration_hours"]) if back else None,
        }
        return p

    def _fastest_possible(self, msg) -> float:
        return _hours(self.propose(msg).flight)

    def satisfaction(self, msg):
        if msg.plan.train is not None:
            return 1.0   # rail covers the trip; I step aside
        current, fastest = _hours(msg.plan.flight), self._fastest_possible(msg)
        if not fastest:
            return 1.0   # no flights exist for this route: nothing better I could book
        return min(1.0, fastest / current) if current else 0.0
    

    def improve(self, msg):
        if "Budget" not in msg.objections: 
            return None
        r, cur = msg.request, msg.plan.flight
        if not cur or not cur.get("outbound") or not cur.get("return"):
            return None
        max_hours = self._fastest_possible(msg) / self.threshold(msg.round)
        def cheapest_ok(options, current):
            ok = [f for f in options
                  if f["price_usd"] < current["price_usd"] and f["duration_hours"] <= max_hours / 2]
            return min(ok, key=lambda f: f["price_usd"]) if ok else current
        out = cheapest_ok(self._search(r.origin, r.destination, msg), cur["outbound"])
        back = cheapest_ok(self._search(r.destination, r.origin, msg), cur["return"])
        saved = (cur["outbound"]["price_usd"] + cur["return"]["price_usd"]) - (out["price_usd"] + back["price_usd"])
        if saved <= 0:
            return None
        p = msg.plan.model_copy(deep=True)
        p.flight = {"outbound": out, "return": back}
        return p, f"switched to cheaper flights (saves ${saved * r.travelers:,.0f}, a bit longer)"

    def needs_redo(self, msg):
        f, cancelled = msg.plan.flight or {}, set(msg.request.cancelled_flights)
        r = msg.request
        want = {"outbound": (r.origin, r.destination), "return": (r.destination, r.origin)}
        for leg, (frm, to) in want.items():
            a, b = resolve_city(frm or ""), resolve_city(to or "")
            if f.get(leg) and a and b and (f[leg]["origin_city"], f[leg]["destination_city"]) != (a["city"], b["city"]):
                return f"the trip is now {r.origin} to {r.destination}; rebooking flights"
        for leg in ("outbound", "return"):
            if f.get(leg) and f[leg]["id"] in cancelled:
                return f"{leg} flight {f[leg]['flight_no']} was cancelled; rebooking"
        return None