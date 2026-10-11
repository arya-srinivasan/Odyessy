from travel.common.data import outdoor_on_rain_days, trip_days
from travel.common.models import ChannelMsg, Plan, LogLine, TripRequest, TURN_ORDER, MAX_ROUNDS

def order(participants: list[str]) -> list[str]:
    """TURN_ORDER filtered to who's actually on the channel."""
    return [name for name in TURN_ORDER if name in participants]


def next_speaker(current: str, participants: list[str]) -> tuple[str, bool]:
    """Who speaks after `current`, and whether that starts a new round (wrapped around)."""
    ring = order(participants)
    if not ring:
        raise ValueError("no participants")
    if current not in ring:
        return ring[0], False
    i = ring.index(current)
    if i + 1 == len(ring):
        return ring[0], True
    return ring[i+1], False

def start_message(request, participants, plan: Plan | None = None) -> ChannelMsg:
    """type=START, sender=Gateway, next=first speaker, plan=given plan or empty Plan()."""
    plan = plan.model_copy(deep=True) if plan else Plan()
    first, _ = next_speaker("Gateway", participants)
    return ChannelMsg(
        type="START", 
        sender="Gateway", 
        next=first, 
        round=0, 
        participants=order(participants), 
        request=request, 
        plan=plan, 
        versions={str(plan.version): plan} if plan.version else {},
    )

def apply_move(msg: ChannelMsg, sender: str, move: str, note: str,
               new_plan: Plan | None = None, score: float | None = None) -> ChannelMsg:
    """Returns a NEW message (msg.model_copy(deep=True)):
       CHANGE -> plan = new_plan, plan.version += 1, accepts = [sender], save versions[str(v)]
       ACCEPT -> add sender to accepts
       OBJECT -> objections[sender] = note
       record scores[str(version)][sender] = score (if given)
       append a LogLine; set next + round via next_speaker
       if is_consensus -> type = FINAL
       elif round >= MAX_ROUNDS -> ranked_vote()"""
    
    out = msg.model_copy(deep=True)
    out.type, out.sender, out.move, out.note = "MOVE", sender, move, note

    if move == "CHANGE":
        if new_plan is None:
            raise ValueError("Change needs a new_plan")
        plan = new_plan.model_copy(deep=True)
        plan.version = msg.plan.version + 1
        out.plan = plan
        out.accepts = [sender]
        out.versions[str(plan.version)] = plan
        out.objections.pop(sender, None)
    elif move == "ACCEPT":
        if sender not in out.accepts:
            out.accepts.append(sender)
        out.objections.pop(sender, None)
    elif move == "OBJECT":
        out.objections[sender] = note
        out.accepts = [a for a in out.accepts if a != sender]

    if score is not None:
        out.scores.setdefault(str(out.plan.version), {})[sender] = round(score, 3)

    out.history.append(LogLine(round=msg.round, sender=sender, move=move, version=out.plan.version, note=note))

    out.next, wrapped = next_speaker(sender, out.participants)
    if wrapped:
        out.round = msg.round + 1

    if is_consensus(out):
        out.type, out.next = "FINAL", ""
        if warn := final_warnings(out):   # e.g. Budget agent wasn't on the channel
            out.note += f" ({warn})"
    elif out.round >= MAX_ROUNDS:
        out = ranked_vote(out)
    return out

def drop_participant(msg: ChannelMsg, name: str, reason: str = "stopped responding") -> ChannelMsg:
    """Take an agent off the channel mid-negotiation (e.g. its process died) and carry on:
       the plan, history, votes and round are kept; if it was its turn, the next agent speaks."""
    if name not in msg.participants:
        return msg
    out = msg.model_copy(deep=True)
    out.type = "MOVE"
    if out.next == name:
        out.next, wrapped = next_speaker(name, out.participants)
        if wrapped:
            out.round += 1
    out.participants = [p for p in out.participants if p != name]
    out.accepts = [a for a in out.accepts if a != name]
    out.objections.pop(name, None)
    out.sender, out.move = "Gateway", ""
    out.note = f"{name} {reason}; continuing without it"
    out.history.append(LogLine(round=out.round, sender="Gateway", move="", version=out.plan.version, note=out.note))
    if not out.participants:
        out.type, out.next = "FINAL", ""
    elif is_consensus(out):
        out.type, out.next = "FINAL", ""
        if warn := final_warnings(out):
            out.note += f" ({warn})"
    elif out.round >= MAX_ROUNDS:
        out = ranked_vote(out)
    return out


def is_consensus(msg: ChannelMsg) -> bool:
    """set(msg.accepts) == set(msg.participants)."""
    return set(msg.accepts) == set(msg.participants)

def _parts(plan: Plan) -> set[str]:
    return {k for k in ("forecast", "flight", "hotel", "days") if getattr(plan, k)}


def budget_note(msg: ChannelMsg) -> str:
    """'' when within budget, else a warning the traveler must see on the final plan."""
    over = msg.request.over_budget_by(msg.plan)
    if not over:
        return ""
    return (f"OVER BUDGET by ${over:,.0f}: total ${msg.plan.total(msg.request.travelers):,.0f} "
            f"vs ${msg.request.budget_usd:,.0f} for {msg.request.travelers} traveler(s), "
            f"no cheaper option was found")


def plan_problems(plan: Plan, request: TripRequest) -> list[str]:
    """Ways a saved version no longer fits the request (e.g. it was made before a
    flight was cancelled or the dates changed). Such versions never win the vote."""
    problems = []
    cancelled = set(request.cancelled_flights)
    for leg in ("outbound", "return"):
        f = (plan.flight or {}).get(leg)
        if f and f["id"] in cancelled:
            problems.append(f"{leg} flight {f['flight_no']} is cancelled")
    if plan.hotel and plan.hotel.get("nights") != request.nights:
        problems.append("hotel nights don't match the dates")
    dates = trip_days(request.depart_date, request.return_date)
    if plan.days and [d.get("date") for d in plan.days] != dates:
        problems.append("activities don't match the dates")
    if plan.forecast and [f.get("date") for f in plan.forecast] != dates:
        problems.append("forecast doesn't match the dates")
    if outdoor_on_rain_days(plan.forecast, plan.days):
        problems.append("outdoor plans on a rainy day")
    return problems


def final_warnings(msg: ChannelMsg) -> str:
    """Everything the traveler must know about the final plan: budget and missing pieces."""
    r, p, who = msg.request, msg.plan, set(msg.participants)
    warnings = [w for w in [budget_note(msg)] if w]
    f = p.flight or {}
    if "Flights" in who and r.origin and r.destination and not p.train:   # a booked train covers the trip
        missing = [leg for leg in ("outbound", "return") if not f.get(leg)]
        if missing:
            warnings.append(f"no {' or '.join(missing)} flight available between {r.origin} and {r.destination}")
    if "Hotels" in who and r.destination and not p.hotel:
        warnings.append(f"no hotel booked in {r.destination}")
    warnings += [x for x in plan_problems(p, r) if "flight" in x or "rainy" in x]
    return "; ".join(warnings)


def ranked_vote(msg: ChannelMsg) -> ChannelMsg:
    """Pick a saved, fully built version. Versions within budget come first (the budget is
       the traveler's hard limit); if none is, the cheapest wins. Among those, the highest
       *minimum* score across agents (the fairest compromise); ties -> highest version.
       An agent that never scored a version is assumed to feel as it did about the
       nearest earlier version it did score. Set plan to it, type = FINAL."""
    out = msg.model_copy(deep=True)
    if not out.versions:
        out.type, out.next = "FINAL", ""
        return out

    # Skip half-built versions (e.g. v1 has only a forecast and looks very cheap).
    req = out.request
    need = _parts(out.plan)
    built = [v for v, p in out.versions.items() if need <= _parts(p)] or list(out.versions)
    # Versions made before an event (cancelled flight, new dates, rain) can't win.
    built = [v for v in built if not plan_problems(out.versions[v], req)] or built

    within = [v for v in built if not req.over_budget_by(out.versions[v])]
    if within:
        candidates = within
    else:
        cheapest = min(out.versions[v].total(req.travelers) for v in built)
        candidates = [v for v in built if out.versions[v].total(req.travelers) <= cheapest]

    def score(agent: str, v: int) -> float:
        for earlier in range(v, -1, -1):
            if agent in out.scores.get(str(earlier), {}):
                return out.scores[str(earlier)][agent]
        return 0.5

    def fairness(v: str) -> tuple[float, int]:
        worst = min((score(a, int(v)) for a in out.participants), default=0.5)
        return (worst, int(v))

    best = max(candidates, key=fairness)
    out.plan = out.versions[best].model_copy(deep=True)
    out.type, out.next = "FINAL", ""
    out.note = f"No full agreement after {MAX_ROUNDS} rounds; ranked vote picked v{best}"
    if warn := final_warnings(out):
        out.note += f". {warn}"
    out.history.append(LogLine(round=out.round, sender="Vote", move="", version=int(best), note=out.note))
    return out
