"""TravelAgntcy Gateway: chat intake, the agent negotiation, trip events and the itinerary email.

Run from the lungo folder (agents and SLIM must be up for SLIM mode):
    PYTHONPATH=. SLIM_SERVER=localhost:46357 uv run uvicorn travel.gateway.main:app --port 8080
Set TRAVEL_LOCAL_MODE=true to run all six agents in-process instead of over SLIM.
"""

import asyncio
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from travel.common.data import request_problems, resolve_city, trip_days
from travel.common.models import TURN_ORDER, ChannelMsg, Plan, TripRequest
from travel.common.protocol import final_warnings
from travel.email_mcp.client import compose_trip_email
from travel.gateway.intake import find_missing, question_for, supported_cities, update_request_with_note

app = FastAPI(title="TravelAgntcy Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store
sessions: dict[str, dict[str, Any]] = {}
STATIC_DIR = Path(__file__).resolve().parent / "static"
NEGOTIATION_TIMEOUT = float(os.getenv("TRAVEL_NEGOTIATION_TIMEOUT", "180"))  # seconds


class ChatPayload(BaseModel):
    session_id: str
    message: str
    email: str | None = None  # where the itinerary email goes (optional)


class EventPayload(BaseModel):
    session_id: str
    type: Literal["rain", "cancel"]
    day: int | None = None
    flight_id: str | None = None


class CommitPayload(BaseModel):
    session_id: str
    email: str | None = None


class AnnouncePayload(BaseModel):
    agent_name: str
    endpoint: str | None = None


# Fallback agent discovery registry
AGENT_REGISTRY: dict[str, dict[str, Any]] = {name: {"status": "registered"} for name in TURN_ORDER}


def _local_mode() -> bool:
    return os.environ.get("TRAVEL_LOCAL_MODE", "").lower() == "true"


def _get_or_create_session(session_id: str) -> dict[str, Any]:
    if session_id not in sessions:
        sessions[session_id] = {
            "request": TripRequest(),
            "plan": None,       # set once a negotiation has finished
            "log": [],
            "email": None,
            "lock": asyncio.Lock(),  # one negotiation at a time per session
        }
    return sessions[session_id]


async def _run_channel(session_data: dict[str, Any]) -> tuple[ChannelMsg, str]:
    """Run the multi-agent negotiation. Returns the FINAL message and how it ran:
    "slim" (live agents over SLIM) or "local" (all six in-process)."""
    from travel.common.channel_local import default_agents, run_local

    request, plan = session_data["request"], session_data["plan"]
    if not _local_mode():
        from travel.common.channel_slim import run_slim

        final = await run_slim(request, plan=plan, verbose=False, timeout=NEGOTIATION_TIMEOUT)
        if final is not None and final.type == "FINAL":
            return final, "slim"
        # No agent answered (or the channel never finished): plan in-process instead.
    return await run_local(request, default_agents(), plan=plan, verbose=False), "local"


async def _negotiate(session: dict[str, Any]) -> dict[str, Any]:
    """Negotiate the session's request (from its current plan, if any) and package the result."""
    final, mode = await _run_channel(session)
    session["plan"] = final.plan
    session["log"].extend(line.model_dump() for line in final.history)
    request = session["request"]
    return {
        "plan": final.plan.model_dump(),
        "summary": final.plan.summary(request.travelers),
        "costs": final.plan.costs(request.travelers),
        "total_usd": final.plan.total(request.travelers),
        "note": final.note,
        "warnings": "; ".join(w for w in (final_warnings(final), _offline_warning(final.participants)) if w),
        "offline_agents": [name for name in TURN_ORDER if name not in final.participants],
        "agents": final.participants,
        "mode": mode,
        "log": session["log"],
        "request": request.model_dump(),
        "email": await compose_trip_email(session["email"] or "", final.plan, request),
    }


def _place_problems(request: TripRequest) -> list[str]:
    """Places the traveler named that we can't plan, reported even before the other questions."""
    problems = []
    origin, dest = resolve_city(request.origin or ""), resolve_city(request.destination or "")
    if request.destination and not dest:
        problems.append(f"Sorry, I don't have travel data for '{request.destination}' yet.")
    if request.origin and not origin:
        problems.append(f"Sorry, I don't have flights from '{request.origin}' yet.")
    if origin and dest and origin["city"] == dest["city"]:
        problems.append(f"You're starting in {dest['city']}; where would you like to go?")
    return problems


def _date_problems(request: TripRequest) -> list[str]:
    if request.depart_date and date.fromisoformat(request.depart_date) < date.today():
        return [f"Your departure date ({request.depart_date}) is in the past. When would you like to leave?"]
    return []


OWNS = {"Weather": "no forecast", "Flights": "no flights booked", "Trains": "no train options",
        "Hotels": "no hotel booked", "Activities": "no activities planned", "Budget": "the budget wasn't checked"}


def _offline_warning(participants: list[str]) -> str:
    """Agents that weren't on the channel, and what the traveler is missing because of it."""
    offline = [name for name in TURN_ORDER if name not in participants]
    return "; ".join(f"{name} agent is offline: {OWNS[name]}" for name in offline)


@app.get("/")
async def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "TravelAgntcy Gateway Running"}


@app.post("/chat")
async def handle_chat(payload: ChatPayload):
    session = _get_or_create_session(payload.session_id)
    async with session["lock"]:
        if payload.email:
            session["email"] = payload.email

        # 1. Update the structured request with information from user's message
        session["request"], date_issue, new_trip = await update_request_with_note(session["request"], payload.message)
        if new_trip:
            session["plan"], session["log"] = None, []   # plan the new trip from scratch

        # 2. Check for missing essential parameters, then for things we can't plan
        missing = find_missing(session["request"])
        problems = _place_problems(session["request"])
        if problems:
            missing = []      # sort out the place first; the other questions come after
        elif not missing:
            problems = request_problems(session["request"]) + _date_problems(session["request"])
        if missing or problems:
            return {
                "status": "needs_info",
                "message": " ".join(filter(None, [
                    date_issue,
                    ("When are you coming back?" if date_issue and missing == ["return date"] else question_for(missing))
                    if missing else " ".join(problems),
                    f"I can plan trips between {supported_cities()}." if "destination" in missing
                    or any("don't have" in p for p in problems) else "",
                ])),
                "missing": missing,
                "request": session["request"].model_dump(),
            }

        # 3. All required fields are present: execute the multi-agent negotiation
        result = await _negotiate(session)
        return {"status": "complete", **({"message": date_issue} if date_issue else {}), **result}


@app.post("/event")
async def handle_event(payload: EventPayload):
    session = sessions.get(payload.session_id)
    if not session or session["plan"] is None:
        raise HTTPException(status_code=404, detail="Plan a trip first, then add events to it")
    async with session["lock"]:
        request = session["request"]
        if payload.type == "rain":
            days = len(trip_days(request.depart_date, request.return_date))
            if payload.day is None or not 0 <= payload.day < days:
                raise HTTPException(status_code=422, detail=f"day must be between 0 and {days - 1}")
            update = {"rain_days": sorted(set(request.rain_days) | {payload.day})}
        else:
            plan: Plan = session["plan"]
            booked = {f["id"] for f in (plan.flight or {}).values() if f}
            if not payload.flight_id or payload.flight_id not in booked:
                raise HTTPException(status_code=422, detail=f"flight_id must be one of the booked flights: {sorted(booked)}")
            update = {"cancelled_flights": sorted(set(request.cancelled_flights) | {payload.flight_id})}
        session["request"] = TripRequest.model_validate({**request.model_dump(), **update})

        # Re-run negotiation starting from the existing plan state
        return {"status": "updated", "event": payload.model_dump(), **await _negotiate(session)}


@app.post("/calendar/commit")
async def commit_calendar(payload: CommitPayload):
    """The itinerary as an email (mailto link) and a calendar file (.ics), via the email MCP."""
    session = sessions.get(payload.session_id)
    if not session or session["plan"] is None:
        raise HTTPException(status_code=404, detail="No active plan found for this session")
    if payload.email:
        session["email"] = payload.email
    result = await compose_trip_email(session["email"] or "", session["plan"], session["request"])
    return {"status": "success", "via": result.get("via"), **result}


@app.get("/registry")
async def get_registry():
    """Which agents are registered, and which are live right now (pinged over SLIM)."""
    if _local_mode():
        return {name: {**info, "status": "local"} for name, info in AGENT_REGISTRY.items()}
    from travel.common.channel_slim import live_agents

    live = set(await live_agents(list(AGENT_REGISTRY), verbose=False))
    return {name: {**info, "status": "live" if name in live else "not responding"}
            for name, info in AGENT_REGISTRY.items()}


@app.post("/registry/announce")
async def announce_agent(payload: AnnouncePayload):
    AGENT_REGISTRY[payload.agent_name] = {
        "endpoint": payload.endpoint,
        "status": "active",
    }
    return {"status": "registered", "agent": payload.agent_name}
