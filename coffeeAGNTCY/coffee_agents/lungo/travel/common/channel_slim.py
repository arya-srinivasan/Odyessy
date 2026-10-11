from uuid import uuid4
from a2a.types import Message, MessageSendParams, Part, Role, SendMessageRequest, TextPart
from agntcy_app_sdk import get_agent_identifier
from agntcy_app_sdk.semantic.a2a.client.factory import A2AClientFactory
from common.a2a_transport_config import build_a2a_client_config
from travel.common.a2a_server import make_card
from travel.common.models import ChannelMsg, END_TOKEN
from travel.common.protocol import start_message
import asyncio

_factory = A2AClientFactory(build_a2a_client_config(
    namespace="lungo", group="agents", agent_name="travel_gateway", include_nats=False))

async def run_slim(request, agent_names, plan=None, verbose=True, timeout=180):
    last = None
    try:
        async for m in run_slim_stream(request, agent_names, plan, timeout):
            last = m
            if verbose:
                l = m.history[-1]
                print(f"[r{l.round}] {l.sender:<10} {l.move:<7} v{l.version}  {l.note}", flush=True)
    except (TimeoutError, asyncio.TimeoutError):
        waiting = last.next if last else agent_names[0]
        print(f"Timed out waiting for {waiting} - check /tmp/agent_{waiting.lower()}.log")
    return last

async def run_slim_stream(request, agent_names, plan=None, timeout=180):
    """Yields each ChannelMsg as the agents speak; the last one is FINAL (or the last move before a timeout)."""
    agent_names = [n.strip().capitalize() for n in agent_names]
    cards = [make_card(n) for n in agent_names]
    start = start_message(request, agent_names, plan)
    client = await _factory.create(cards[0])
    init = SendMessageRequest(id=str(uuid4()), params=MessageSendParams(message=Message(
        messageId=str(uuid4()), role=Role.user, parts=[Part(TextPart(text=start.to_text()))])))
    async for r in client.start_streaming_groupchat(
            init_message=init, group_channel=f"travel-{uuid4().hex[:8]}",
            participants=[get_agent_identifier(c) for c in cards],
            end_message=END_TOKEN, timeout=timeout):
        result = getattr(r.root, "result", None)
        if result is not None and getattr(result, "parts", None):
            m = ChannelMsg.from_text(result.parts[0].root.text)
            if m:
                yield m

if __name__ == "__main__":
    import asyncio
    import sys

    import config.logging_config  # noqa: F401  (Lungo's logging setup)
    from travel.common.models import TripRequest

    # Agents to invite: pass names on the command line, or use these defaults.
    # Must match each agent's `name` and be running via `python -m travel.run <name>`.
    agents = sys.argv[1:] or ["Weather", "Flights", "Hotels", "Budget"]

    req = TripRequest(
        origin="SFO",
        destination="Tokyo",
        depart_date="2026-11-12",
        return_date="2026-11-16",
        budget_usd=2000,
        interests=["food", "temples"],
    )

    final = asyncio.run(run_slim(req, agents))
    if final is None:
        print("No messages came back - are the agents running and is SLIM up?")
    elif final.type != "FINAL":
        print(f"Channel ended without PLAN_FINAL (last move: {final.sender} {final.move})")
    else:
        print("FINAL:", final.plan.summary(final.request.travelers))