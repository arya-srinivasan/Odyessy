from travel.common.negotiator import Negotiator

class BudgetAgent(Negotiator):
    name = "Budget"

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
    