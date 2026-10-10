from common.negotiator import Negotiator
from common.data import search_flights

class FlightsAgent(Negotiator):
    name = "Flight"

    def has_my_part(self, msg):
        return msg.plan.flight is not None

    def propose(self, msg):
        p = msg.plan.model_copy(deep=True)
        p.flight = search_flights(msg.request.destination)

        return p

    def satisfaction(self, msg): 
        return msg.plan.hotel["rating"] / 5

    def improve(self, msg):
        if "Budget" not in msg.objections: 
            return None
        options = cheaper_hotels(msg.plan.hotel, min_rating=self.threshold(msg.round) * 5)
        if not options:
            return None
        p = msg.plan.model_copy(deep=True)
        p.hotel = options[0]
        return p, f"switched to {p.hotel['name']} to save money"