import asyncio
from uuid import uuid4

from a2a.types import Message, MessageSendParams, Part, Role, SendMessageRequest, TextPart
from agntcy_app_sdk import get_agent_identifier
from agntcy_app_sdk.semantic.a2a.client.factory import A2AClientFactory
from common.a2a_transport_config import build_a2a_client_config

from travel.common.a2a_server import PING, make_card
from travel.common.models import END_TOKEN, TURN_ORDER, ChannelMsg
from travel.common.protocol import start_message

PING_TIMEOUT = 6  # seconds per agent health check

_factory = A2AClientFactory(build_a2a_client_config(
    namespace="lungo", group="agents", agent_name="travel_gateway", include_nats=False))


def _text_msg(text: str) -> Message:
    return Message(messageId=str(uuid4()), role=Role.user, parts=[Part(TextPart(text=text))])


async def _alive(card) -> bool:
    """True if the agent answers a PING within PING_TIMEOUT seconds."""
    async def ping() -> bool:
        client = await _factory.create(card)
        async for _ in client.send_message(_text_msg(PING)):
            return True
        return False
    for attempt in range(2):                       # one retry for a fast transient error
        try:
            return await asyncio.wait_for(ping(), timeout=PING_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            return False                           # really not answering - don't retry
        except Exception as e:
            if attempt == 1:
                print(f"  ping to {card.name} failed: {e!r}", flush=True)
            await asyncio.sleep(0.5)
    return False


async def live_agents(agent_names: list[str] | None = None, verbose: bool = True) -> list[str]:
    """Health-check agents (default: all of TURN_ORDER) and return the ones that answer."""
    names = list(dict.fromkeys(n.strip().capitalize() for n in (agent_names or TURN_ORDER)))
    alive = []
    for n in names:                                # one at a time: concurrent pings collide in the SDK
        if await _alive(make_card(n)):
            alive.append(n)
        elif verbose:
            print(f"Skipping {n}: not responding (check /tmp/agent_{n.lower()}.log)", flush=True)
    return alive


async def run_slim_stream(request, agent_names, plan=None, timeout=180):
    """Yields each ChannelMsg as the agents speak; the last one is FINAL
    (or the last move before a timeout). `agent_names` must already be live."""
    cards = [make_card(n) for n in agent_names]
    start = start_message(request, agent_names, plan)
    client = await _factory.create(cards[0])  # any card; recipients come from participants
    init = SendMessageRequest(id=str(uuid4()), params=MessageSendParams(message=_text_msg(start.to_text())))
    async for r in client.start_streaming_groupchat(
            init_message=init,
            group_channel=f"travel-{uuid4().hex[:8]}",  # fresh channel per trip
            participants=[get_agent_identifier(c) for c in cards],
            end_message=END_TOKEN,
            timeout=timeout):
        result = getattr(r.root, "result", None)
        if result is not None and getattr(result, "parts", None):
            m = ChannelMsg.from_text(result.parts[0].root.text)
            if m:
                yield m


async def run_slim(request, agent_names=None, plan=None, verbose=True, timeout=180):
    """Same contract as run_local: returns the last ChannelMsg (FINAL on success) or None.
    Every agent that is live and healthy joins (default: all of TURN_ORDER)."""
    names = await live_agents(agent_names, verbose)
    if not names:
        if verbose:
            print("No agents are responding - start them with: bash travel/start_agents.sh ...")
        return None
    last = None
    try:
        async for m in run_slim_stream(request, names, plan, timeout):
            last = m
            if verbose:
                l = m.history[-1]
                print(f"[r{l.round}] {l.sender:<10} {l.move:<7} v{l.version}  {l.note}", flush=True)
    except (TimeoutError, asyncio.TimeoutError):
        waiting = last.next if last else names[0]
        if verbose:
            print(f"Timed out waiting for {waiting} - check /tmp/agent_{waiting.lower()}.log")
    return last


if __name__ == "__main__":
    import sys

    import config.logging_config  # noqa: F401  (Lungo's logging setup)
    from travel.common.models import TripRequest

    # Agents to invite: names on the command line, or these defaults.
    agents = sys.argv[1:] or TURN_ORDER

    req = TripRequest(
        origin="SFO",
        destination="Tokyo",
        depart_date="2026-11-12",
        return_date="2026-11-16",
        budget_usd=2000,
        interests=["food", "temples"],
    )

    try:
        # Hard cap: nothing can wait longer than this, whatever goes wrong.
        final = asyncio.run(asyncio.wait_for(run_slim(req, agents), timeout=200))
    except (TimeoutError, asyncio.TimeoutError):
        final = None
        print("Gave up after 200 seconds.")

    if final is None:
        print("No plan came back - are the agents running and is SLIM up?")
    elif final.type != "FINAL":
        print(f"Channel ended without PLAN_FINAL (last move: {final.sender} {final.move})")
    else:
        print("FINAL:", final.plan.summary(final.request.travelers))
        print("Agents:", ", ".join(final.participants), "|", final.note)