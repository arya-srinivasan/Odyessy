import asyncio 
from travel.common.models import ChannelMsg, Plan, TripRequest
from travel.common.negotiator import take_turn
from travel.common.protocol import drop_participant, start_message
from travel.common.data import request_problems

async def run_local(request: TripRequest, agents: list, plan: Plan | None = None, verbose= True) -> ChannelMsg:
    problems = request_problems(request)
    if problems:
        raise ValueError(" ".join(problems))  # stop at the door, before any agent runs
    msg = start_message(request, [a.name for a in agents], plan)
    return await resume_local(msg, agents, verbose)


async def resume_local(msg: ChannelMsg, agents: list, verbose: bool = True) -> ChannelMsg:
    """Play the negotiation to the end in-process, from any point (a fresh start, or
    where a SLIM channel broke off). Participants with no agent here are dropped."""
    by_name = {a.name: a for a in agents}
    for name in [p for p in msg.participants if p not in by_name]:
        msg = drop_participant(msg, name, "isn't available")
    while msg.type != "FINAL":
        msg = await take_turn(by_name[msg.next], msg)
        if verbose:                                   # <- inside the loop
            l = msg.history[-1]
            print(f"[r{l.round}] {l.sender:<10} {l.move:<7} v{l.version}  {l.note}")
    if verbose:
        print("FINAL:", msg.plan.summary(msg.request.travelers))
    return msg

def default_agents() -> list:
    from travel.agents.hotels.agent import HotelsAgent
    from travel.agents.budget.agent import BudgetAgent
    from travel.agents.flights.agent import FlightsAgent
    from travel.agents.weather.agent import WeatherAgent
    from travel.agents.trains.agent import TrainsAgent
    from travel.agents.activities.agent import ActivitiesAgent
    return [HotelsAgent(), BudgetAgent(), FlightsAgent(), WeatherAgent(), TrainsAgent(), ActivitiesAgent()]

if __name__ == "__main__":
    req = TripRequest(origin="SFO", destination="Tokyo", depart_date="2026-11-12",
                  return_date="2026-11-16", budget_usd=2000)
    asyncio.run(run_local(req, default_agents()))

