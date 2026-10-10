from typing import TypedDict
from langgraph.graph import StateGraph, END
from travel.common.protocol import apply_move
from travel.common.llm import llm_text
from travel.common.models import ChannelMsg, Plan

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
        note = await llm_text(
            f"You are the {self.name} agent (goal: {self.goal}) planning a trip with other agents. "
            f"Your move is {move}. Reason: {s['note']}. Say it in one short sentence to the others.",
            fallback=s["note"])
        return {"out": apply_move(msg, self.name, move, note, new_plan=plan, score=score)}


    async def on_turn(self, msg: ChannelMsg) -> ChannelMsg:
       return (await self.graph.ainvoke({"msg": msg}))["out"]
    