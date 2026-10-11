"""Trains Agent for TravelAgntcy.

Owns plan.train. Proposes direct rail or destination day-trips,
and concedes by dropping the rail leg if Budget objects.
"""

from travel.common.data import rail_alternatives
from travel.common.models import Plan
from travel.common.negotiator import Negotiator


class TrainsAgent(Negotiator):
    name = "Trains"
    base_threshold = 0.8
    concession = 0.15
    floor = 0.2

    def has_my_part(self, msg) -> bool:
        """Once I've spoken, my decision stands, even if that decision was 'no train'."""
        return any(line.sender == self.name for line in msg.history)

    def propose(self, msg) -> Plan:
        """Find direct train route or scenic day trips from destination."""
        p = msg.plan.model_copy(deep=True)
        origin = msg.request.origin
        dest = msg.request.destination

        options = rail_alternatives(origin, dest)
        if options:
            p.train = options[0]
        else:
            p.train = None
        return p

    def satisfaction(self, msg) -> float:
        """Return 1.0 if train is selected or none available; otherwise concession floor."""
        if msg.plan.train is not None:
            return 1.0

        # If no rail route exists from destination or origin, agent is satisfied
        dest = msg.request.destination
        origin = msg.request.origin
        options = rail_alternatives(origin, dest)
        if not options:
            return 1.0

        # If rail was dropped to save budget, score reflects concession round
        return self.threshold(msg.round)

    def improve(self, msg) -> tuple[Plan, str] | None:
        """Concede and drop the train leg if Budget objects to total trip cost."""
        if "Budget" in msg.objections and msg.plan.train is not None:
            p = msg.plan.model_copy(deep=True)
            dropped_id = p.train.get("id", "rail")
            p.train = None
            return p, f"dropped train leg {dropped_id} to reduce overall budget"
        return None