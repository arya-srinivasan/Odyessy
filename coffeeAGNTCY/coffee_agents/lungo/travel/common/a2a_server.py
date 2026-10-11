import logging
from uuid import uuid4

import config.config as cfg
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Message, Part, Role, TextPart
from agntcy_app_sdk.factory import AgntcyFactory
from agntcy_app_sdk.semantic.a2a.server.experimental_patterns import A2AExperimentalServer
from config.config import OTEL_SDK_DISABLED

from travel.common.models import ChannelMsg
from travel.common.negotiator import take_turn

log = logging.getLogger("travel.a2a_server")

SILENT = "__SILENT__"
PING, PONG = "PING", "PONG"
TURN_TIMEOUT = 60  # seconds an agent may take for one turn

def make_card(name: str) -> AgentCard:
    agent_id = f"travel_{name.lower()}"
    url = f"slim://{cfg.SLIM_SERVER}/lungo/agents/{agent_id}"
    return AgentCard(
        name=name,
        description=f"TravelAgntcy {name} agent",
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id=agent_id, name=name, description=name, tags=["travel", name.lower()])],
        preferred_transport="slim",
        url=url,
        additional_interfaces=[AgentInterface(transport="slim", url=url)],
    )

class TurnExecutor(AgentExecutor):
    def __init__(self, agent):
        self.agent = agent

    async def _reply_text(self, incoming: str) -> str:
        if incoming == PING:
            return PONG  # health check from the Gateway
        msg = ChannelMsg.from_text(incoming)
        if msg is None or msg.type == "FINAL" or msg.next != self.agent.name:
            return SILENT  # not my turn
        out = await take_turn(self.agent, msg, TURN_TIMEOUT)
        return out.to_text()

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        text = await self._reply_text(context.get_user_input().strip())
        await event_queue.enqueue_event(Message(
            message_id=str(uuid4()), role=Role.agent,
            metadata={"name": self.agent.name}, parts=[Part(TextPart(text=text))]))

    async def cancel(self, context, event_queue):
        pass

_original = A2AExperimentalServer.handle_message


async def _quiet_handle_message(self, message, *, publish_fn=None):
    out = await _original(self, message, publish_fn=publish_fn)
    if out is None:
        return None
    payload = out.payload or b""
    if SILENT.encode() in payload:
        return None
    if b'"error"' in payload:
        log.warning("dropped an error reply instead of publishing it: %s", payload[:200])
        return None
    return out


A2AExperimentalServer.handle_message = _quiet_handle_message


async def serve(agent):
    card = make_card(agent.name)
    factory = AgntcyFactory(f"travel.{agent.name.lower()}", enable_tracing=not OTEL_SDK_DISABLED)
    handler = DefaultRequestHandler(agent_executor=TurnExecutor(agent), task_store=InMemoryTaskStore())
    session = factory.create_app_session()
    await session.add_a2a_card(card, handler).with_factory(factory).start(keep_alive=False)
    print(f"{agent.name} serving slim on {card.url} - ready", flush=True)
    await session.start_all_sessions(keep_alive=True)  # keeps the process alive

