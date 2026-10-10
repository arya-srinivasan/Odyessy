from travel.common.models import ChannelMsg, Plan, LogLine, TURN_ORDER, MAX_ROUNDS

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
    elif out.round >= MAX_ROUNDS:
        out = ranked_vote(out)
    return out

def is_consensus(msg: ChannelMsg) -> bool:
    """set(msg.accepts) == set(msg.participants)."""
    return set(msg.accepts) == set(msg.participants)

def ranked_vote(msg: ChannelMsg) -> ChannelMsg:
    """Pick the saved version with the highest *minimum* score across agents
       (the fairest compromise); ties -> highest version. Set plan to it, type = FINAL,
       note = 'No full agreement after 4 rounds; ranked vote picked vN'."""
    out = msg.model_copy(deep=True)
    if not out.versions:
        out.type, out.next = "FINAL", ""
        return out

    def fairness(v: str) -> tuple[float, int]:
        s = out.scores.get(v, {})
        worst = min(s.get(a, 0.5) for a in out.participants)
        return (worst, int(v))                   

    best = max(out.versions, key=fairness)
    out.plan = out.versions[best].model_copy(deep=True)
    out.type, out.next = "FINAL", ""
    out.note = f"No full agreement after {MAX_ROUNDS} rounds; ranked vote picked v{best}"
    out.history.append(LogLine(round=out.round, sender="Vote", move="", version=int(best), note=out.note))
    return out