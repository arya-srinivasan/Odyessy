from travel.common.negotiator import Negotiator

class BudgetAgent(Negotiator):
    name = "Budget"
    goal = "keep the whole trip within the traveler's budget"

    def has_my_part(self, msg):
        return True

    def satisfaction(self, msg):
        b = msg.request.budget_usd
        if not b:
            return 1.0
        total = msg.plan.total(msg.request.travelers)
        return 1.0 if total <= b else max(0.0, b / total - 0.2)   # e.g. 20% over -> ~0.63

    def improve(self, msg):
        return None

    def object_reason(self, msg):
        r = msg.request
        over = r.over_budget_by(msg.plan)
        return (f"v{msg.plan.version} is ${over:,.0f} over the ${r.budget_usd:,.0f} budget "
                f"(total ${msg.plan.total(r.travelers):,.0f} for {r.travelers} traveler(s))")

    def decide(self, msg):
        # The budget is the traveler's hard limit: never concede on it, whatever the round.
        if msg.request.over_budget_by(msg.plan) > 0:
            return "OBJECT", None, self.object_reason(msg)
        return super().decide(msg)
