import config.config as cfg
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

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

from uuid import uuid4
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, TextPart
from travel.common.models import ChannelMsg

SILENT = "__SILENT__"

class TurnExecutor(AgentExecutor):
    def __init__(self, agent):
        self.agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        msg = ChannelMsg.from_text(context.get_user_input())
        if msg is None or msg.type == "FINAL" or msg.next != self.agent.name:
            text = SILENT
        else:
            text = (await self.agent.on_turn(msg)).to_text()
        await event_queue.enqueue_event(Message(
            message_id=str(uuid4()), role=Role.agent, 
            metadata={"name": self.agent.name}, parts=[Part(TextPart(text=text))]
        ))

    async def cancel(self, context, event_queue):
        pass

from agntcy_app_sdk.semantic.a2a.server.experimental_patterns import A2AExperimentalServer

_original = A2AExperimentalServer.handle_message

async def _quiet_handle_message(self, message, *, publish_fn=None):
    out = await _original(self, message, publish_fn=publish_fn)
    if out is not None and SILENT.encode() in (out.payload or b""):
        return None
    return out

A2AExperimentalServer.handle_message = _quiet_handle_message

import os
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from agntcy_app_sdk.factory import AgntcyFactory
from config.config import OTEL_SDK_DISABLED

async def serve(agent):
    card = make_card(agent.name)
    factory = AgntcyFactory(f"travel.{agent.name.lower()}", enable_tracing=not OTEL_SDK_DISABLED)
    handler = DefaultRequestHandler(agent_executor=TurnExecutor(agent), task_store=InMemoryTaskStore())
    session = factory.create_app_session()
    await session.add_a2a_card(card, handler).with_factory(factory).start(keep_alive=False)
    print(f"{agent.name} serving slim on {card.url} - ready", flush=True)
    await session.start_all_sessions(keep_alive=True)       # keeps the process alive

