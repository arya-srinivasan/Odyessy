from travel.common.negotiator import Negotiator
from travel.common.data import search_hotels, cheaper_hotels   # same for any other travel imports
from travel.common.models import Plan

class HotelsAgent(Negotiator):
    name = "Hotels"

    def has_my_part(self, msg):
        return msg.plan.hotel is not None

    def propose(self, msg):
        p = msg.plan.model_copy(deep=True)
        p.hotel = search_hotels(msg.request.destination, nights=msg.request.nights,
                                area=msg.request.hotel_area)[0]

        return p

    def satisfaction(self, msg): 
        return msg.plan.hotel["rating"] / 5

    def improve(self, msg):
        if "Budget" not in msg.objections or not msg.request.budget_usd:
            return None
        over = msg.plan.total(msg.request.travelers) - msg.request.budget_usd
        if over <= 0:
            return None
        current = msg.plan.hotel
        options = cheaper_hotels(current, min_rating=self.threshold(msg.round) * 5)
        if not options:
            return None
        nights = current.get("nights", msg.request.nights)
        # best-rated hotel that saves enough; otherwise the biggest saving available
        enough = [h for h in options
                if (current["price_per_night_usd"] - h["price_per_night_usd"]) * nights >= over]
        pick = max(enough, key=lambda h: h["rating"]) if enough else min(options, key=lambda h: h["price_per_night_usd"])
        p = msg.plan.model_copy(deep=True)
        p.hotel = dict(pick, nights=nights)
        saved = (current["price_per_night_usd"] - pick["price_per_night_usd"]) * nights
        return p, f"switched to {pick['name']} (rated {pick['rating']}) to save ${saved:,.0f}"
