import asyncio 
from travel.common.models import ChannelMsg, Plan, TripRequest
from travel.common.protocol import start_message

async def run_local(request: TripRequest, agents: list, plan: Plan | None = None, verbose= True) -> ChannelMsg:
    msg = start_message(request, [a.name for a in agents], plan)
    by_name = {a.name : a for a in agents}
    while msg.type != "FINAL":
        msg = await by_name[msg.next].on_turn(msg)
        if verbose:                                   # <- inside the loop
            l = msg.history[-1]
            print(f"[r{l.round}] {l.sender:<10} {l.move:<7} v{l.version}  {l.note}")
    print("FINAL:", msg.plan.summary(msg.request.travelers))
    return msg

def default_agents() -> list:
    from travel.agents.hotels.agent import HotelsAgent
    from travel.agents.budget.agent import BudgetAgent
    return [HotelsAgent(), BudgetAgent()]

if __name__ == "__main__":
    req = TripRequest(origin="SFO", destination="Tokyo", depart_date="2026-11-12", return_date="2026-11-16", budget_usd=600, interests=["food"])
    asyncio.run(run_local(req, default_agents()))

