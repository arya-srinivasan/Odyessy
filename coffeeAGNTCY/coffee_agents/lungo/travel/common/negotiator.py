from typing import TypedDict
from langgraph.graph import StateGraph, END
import asyncio
import logging
import re

from travel.common.protocol import apply_move
from travel.common.llm import llm_text
from travel.common.models import ChannelMsg, Plan

MOVE_WORDS = {"ACCEPT": "agreed to the plan", "CHANGE": "changed the plan", "OBJECT": "objected to the plan"}


def my_part(name: str, plan: Plan) -> str:
    """A few concrete facts about this agent's part of the plan, so the LLM can name them."""
    if name == "Flights" and plan.flight and plan.flight.get("outbound"):
        f = plan.flight["outbound"]
        return f"{f['airline']} {f['flight_no']}, ${f['price_usd']}, {f['duration_hours']}h"
    if name == "Trains" and plan.train:
        return f"{plan.train['operator']} {plan.train['from_city']} to {plan.train['to_city']}, ${plan.train['price_usd']}"
    if name == "Hotels" and plan.hotel:
        h = plan.hotel
        return f"{h['name']} in {h['area']}, rated {h['rating']}, ${h['price_per_night_usd']}/night"
    if name == "Weather" and plan.forecast:
        rain = [str(d["day"] + 1) for d in plan.forecast if d.get("sky") == "rain"]
        return f"rain on day {', '.join(rain)}" if rain else "no rain expected"
    if name == "Activities" and plan.days:
        names = [a["name"] for d in plan.days for a in d.get("activities", [])]
        return f"{len(names)} activities, including {', '.join(names[:3])}"
    return ""


def clean_note(text: str) -> str:
    """Drop a leading move label ("ACCEPT:", "OBJECT -") and swap dashes for commas."""
    text = re.sub(r"^\s*(ACCEPT|CHANGE|OBJECT)\b\s*[:\-\u2013\u2014]?\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"\s*[\u2013\u2014]\s*", ", ", text)
    if re.match(r"v\d", text):          # "v4 costs..." reads fine as is
        return text
    return text[:1].upper() + text[1:] if text else text


class TurnState(TypedDict, total=False):
    msg: ChannelMsg
    move: str
    plan: Plan | None
    note: str
    out: ChannelMsg

class Negotiator:
    name = "Base"             # must match TURN_ORDER
    goal = "my goal"
    base_threshold = 0.8      # how picky it starts (0..1)
    concession = 0.15         # how much the bar drops each round
    floor = 0.3               # never accept below this

    def threshold(self, round_: int) -> float:
        return max(self.floor, self.base_threshold - self.concession * round_)

    def has_my_part(self, msg) -> bool: raise NotImplementedError
    def propose(self, msg) -> Plan: raise NotImplementedError                 # plan with my part filled in
    def satisfaction(self, msg) -> float: raise NotImplementedError           # 0..1, how happy my goal is with msg.plan
    def improve(self, msg) -> tuple[Plan, str] | None: return None            # better plan for the group, or None
    def needs_redo(self, msg) -> str | None: return None

    def object_reason(self, msg) -> str:
        return f"v{msg.plan.version} misses my goal ({self.goal})"

    def decide(self, msg) -> tuple[str, Plan | None, str]:
        if not self.has_my_part(msg):
            return "CHANGE", self.propose(msg), "added my part"
        if (why := self.needs_redo(msg)):
            return "CHANGE", self.propose(msg), why
        score = self.satisfaction(msg)
        others_object = any(a != self.name for a in msg.objections)
        if score >= self.threshold(msg.round) and not others_object:
            return "ACCEPT", None, f"happy with v{msg.plan.version}"
        better = self.improve(msg)
        if better:
            return "CHANGE", better[0], better[1]
        if score >= self.threshold(msg.round):
            return "ACCEPT", None, "fine for me; can't do more"
        return "OBJECT", None, self.object_reason(msg)

    def __init__(self):
        g = StateGraph(TurnState)
        g.add_node("decide", self._decide_node)
        g.add_node("explain", self._explain_node)
        g.set_entry_point("decide")
        g.add_edge("decide", "explain")
        g.add_edge("explain", END)
        self.graph = g.compile()

    async def _decide_node(self, s):
        move, plan, note = self.decide(s["msg"])
        return {"move": move, "plan": plan, "note": note}

    async def _explain_node(self, s):
        msg, move, plan = s["msg"], s["move"], s["plan"]
        after = msg.model_copy(update={"plan": plan}) if plan else msg   # score what I'm leaving behind
        score = self.satisfaction(after)
        facts = s["note"]
        if plan is not None and (part := my_part(self.name, plan)):
            facts += f" (what I put in the plan: {part})"
        note = await llm_text(
            f"You are the {self.name} agent (goal: {self.goal}), negotiating one trip plan with other agents. "
            f"You just {MOVE_WORDS.get(move, move)}. Details: {facts}. "
            "Tell the other agents in one short, plain sentence. Keep the specific names, numbers and prices. "
            "Don't start with a label like ACCEPT, CHANGE or OBJECT, and don't use dashes.",
            fallback=s["note"])
        if note != s["note"]:              # only tidy what the LLM wrote; LLM-off text stays as is
            note = clean_note(note) or s["note"]
        return {"out": apply_move(msg, self.name, move, note, new_plan=plan, score=score)}


    async def on_turn(self, msg: ChannelMsg) -> ChannelMsg:
        return (await self.graph.ainvoke({"msg": msg}))["out"]


async def take_turn(agent: "Negotiator", msg: ChannelMsg, timeout: float = 60) -> ChannelMsg:
    """One turn that can't take the channel down: if the agent crashes or hangs,
    it objects with a plain note instead, and the negotiation moves on."""
    try:
        return await asyncio.wait_for(agent.on_turn(msg), timeout)
    except Exception:
        logging.getLogger("travel.negotiator").exception("%s failed on its turn; passing", agent.name)
        return apply_move(msg, agent.name, "OBJECT", "I hit an error and am skipping this turn")
