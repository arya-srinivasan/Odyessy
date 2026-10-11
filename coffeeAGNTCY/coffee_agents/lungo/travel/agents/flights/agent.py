from travel.common.negotiator import Negotiator
from travel.common.data import search_flights

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
        return msg.plan.flight is not None

    def propose(self, msg):
        r = msg.request
        p = msg.plan.model_copy(deep=True)
        out = search_flights(r.origin, r.destination, exclude_ids=r.cancelled_flights, limit=10)
        back = search_flights(r.destination, r.origin, exclude_ids=r.cancelled_flights, limit=10)
        p.flight = {
            "outbound": min(out, key=lambda f: f["duration_hours"]) if out else None,
            "return": min(back, key=lambda f: f["duration_hours"]) if back else None,
        }
        return p

    def _fastest_possible(self, msg) -> float:
        return _hours(self.propose(msg).flight)

    def satisfaction(self, msg): 
        current = _hours(msg.plan.flight)
        return self._fastest_possible(msg) / current if current else 0.0
    

    def improve(self, msg):
        if "Budget" not in msg.objections: 
            return None
        r, cur = msg.request, msg.plan.flight
        max_hours = self._fastest_possible(msg) / self.threshold(msg.round)
        def cheapest_ok(options, current):
            ok = [f for f in options
                  if f["price_usd"] < current["price_usd"] and f["duration_hours"] <= max_hours / 2]
            return min(ok, key=lambda f: f["price_usd"]) if ok else current
        out = cheapest_ok(search_flights(r.origin, r.destination, exclude_ids=r.cancelled_flights, limit=10), cur["outbound"])
        back = cheapest_ok(search_flights(r.destination, r.origin, exclude_ids=r.cancelled_flights, limit=10), cur["return"])
        saved = (cur["outbound"]["price_usd"] + cur["return"]["price_usd"]) - (out["price_usd"] + back["price_usd"])
        if saved <= 0:
            return None
        p = msg.plan.model_copy(deep=True)
        p.flight = {"outbound": out, "return": back}
        return p, f"switched to cheaper flights (saves ${saved * r.travelers:,.0f}, a bit longer)"

    def needs_redo(self, msg):
        f, cancelled = msg.plan.flight or {}, set(msg.request.cancelled_flights)
        for leg in ("outbound", "return"):
            if f.get(leg) and f[leg]["id"] in cancelled:
                return f"{leg} flight {f[leg]['flight_no']} was cancelled; rebooking"
        return None