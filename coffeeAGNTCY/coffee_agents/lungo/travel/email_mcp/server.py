import config.logging_config 

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from travel.common.models import Plan, TripRequest

logger = logging.getLogger("travel.email_mcp")

TOPIC = "travel_email_service"
MAX_BODY_CHARS = 1800
ACTIVITY_SLOTS = {"10:00", "14:00", "17:00"}

mcp = FastMCP()

def _trip_dates(plan: Plan) -> list[str]:
    dates = [d["date"] for d in plan.forecast if d.get("date")]
    return dates or [d["date"] for d in plan.days if d.get("date")]

def _at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}")

def plan_events(plan: Plan, request: TripRequest) -> list[dict]:
    events: list[dict] = []
    dates = _trip_dates(plan)
    if not dates:
        return events
    first, last = dates[0], dates[-1]

    flight = plan.flight or {}
    for leg, day in (("outbound", first), ("return", last)):
        f = flight.get(leg)
        if f:
            start = _at(day, f["depart_time"])
            events.append({
                "title": f"Flight {f['airline']} {f['flight_no']}:  {f['origin']} to {f['destination']}",
                "start": start,
                "end": start + timedelta(hours=f["duration_hours"]),
                "all_day": False,
                "where": f"{f['origin']} airport",
            })

    if plan.hotel:
        h = plan.hotel
        check_in = date.fromisoformat(first)
        events.append({
            "title": f"Stay: {h['name']}",
            "start": check_in,
            "end": check_in + timedelta(days=h.get("nights", 1)),
            "all_day": True,
            "where": f"{h['area']}, {h['city']}",
        })

    if plan.train:
        t = plan.train
        day = dates[min(1, len(dates)-1)]
        start = _at(day, t["depart_time"])
        events.append({
            "title": f"Train {t['operator']}: {t['from_city']} to {t['to_city']}",
            "start": start,
            "end": start + timedelta(hours=t["duration_hours"]),
            "all_day": False,
            "where": f"{t['from_city']} station",
        })

    for d in plan.days:
        for slot, a in zip(ACTIVITY_SLOTS, d.get("activites", [])):
            start = _at(d["date"], slot)
            events.append({
                "title": a["name"],
                "start": start,
                "end": start + timedelta(hours=a.get("duration_hours", 2)),
                "all_day": False,
                "where": request.destination or a.get("city", "")
            })
    return events

def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
 
 
def to_ics(events: list[dict]) -> str:
    """A minimal iCalendar file any calendar app can import. Times are local (floating)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TravelAgntcy//EN", "CALSCALE:GREGORIAN"]
    for i, e in enumerate(events):
        lines += ["BEGIN:VEVENT", f"UID:travelagntcy-{stamp}-{i}@travelagntcy", f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            lines += [f"DTSTART;VALUE=DATE:{e['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{e['end']:%Y%m%d}"]
        else:
            lines += [f"DTSTART:{e['start']:%Y%m%dT%H%M%S}", f"DTEND:{e['end']:%Y%m%dT%H%M%S}"]
        lines += [f"SUMMARY:{_ics_text(e['title'])}", f"LOCATION:{_ics_text(e['where'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def render_text(plan: Plan, request: TripRequest) -> str:
    dates = _trip_dates(plan)
    sky = {d["day"]: d["sky"] for d in plan.forecast}
    where = request.destination or "your trip"
    lines = [f"Your trip to {where}" + (f", {dates[0]} to {dates[-1]}" if dates else ""), ""]
 
    flight = plan.flight or {}
    for label, leg in (("Outbound", "outbound"), ("Return", "return")):
        f = flight.get(leg)
        if f:
            stops = "nonstop" if not f.get("stops") else f"{f['stops']} stop"
            lines.append(f"{label} flight: {f['airline']} {f['flight_no']}, departs {f['depart_time']}, "
                         f"{f['duration_hours']}h {stops}, ${f['price_usd']:,}")
    if plan.train:
        t = plan.train
        lines.append(f"Train: {t['operator']} {t['from_city']} to {t['to_city']} at {t['depart_time']}, ${t['price_usd']:,}")
    if plan.hotel:
        h = plan.hotel
        lines.append(f"Hotel: {h['name']} ({h['area']}), rated {h['rating']}, "
                     f"{h.get('nights', 1)} nights at ${h['price_per_night_usd']:,}/night")
 
    if plan.days:
        lines += ["", "Day by day:"]
        for d in plan.days:
            names = ", ".join(a["name"] for a in d.get("activities", [])) or "Free day"
            weather = f" ({sky[d['day']]})" if d.get("day") in sky else ""
            lines.append(f"Day {d.get('day', 0) + 1}, {d.get('date', '')}{weather}: {names}")
 
    total = plan.total(request.travelers)
    budget = f" of your ${request.budget_usd:,.0f} budget" if request.budget_usd else ""
    lines += ["", f"Total: ${total:,.0f}{budget}",
              "Planned by six TravelAgntcy agents who negotiated it among themselves.",
              "Use Add to calendar on the trip page to put every flight, the hotel and each activity in your calendar."]
    return "\n".join(lines)
 
 
def compose(to: str, plan: Plan, request: TripRequest) -> dict:
    """Everything the page needs. Plain function so the Gateway can call it directly
    if the MCP server isn't reachable."""
    text = render_text(plan, request)
    body = text if len(text) <= MAX_BODY_CHARS else text[:MAX_BODY_CHARS] + "\n..."
    subject = f"Your TravelAgntcy trip to {request.destination or 'your destination'}"
    mailto = f"mailto:{quote(to or '', safe='@')}?subject={quote(subject)}&body={quote(body)}"
    return {"mailto": mailto, "ics": to_ics(plan_events(plan, request)), "text": text, "subject": subject}
 
 
# --- the MCP tool --------------------------------------------------------------------
 
@mcp.tool()
async def compose_trip_email(to: str, plan_json: str, request_json: str) -> str:
    """Build the itinerary email for the traveler. Returns JSON with keys
    mailto (opens their mail app pre-filled), ics (trip.ics text), text, subject."""
    plan = Plan.model_validate_json(plan_json)
    request = TripRequest.model_validate_json(request_json)
    logger.info("Composing trip email for %s (plan v%s)", request.destination, plan.version)
    return json.dumps(compose(to, plan, request))
 
 
# --- serving on SLIM (same steps as Lungo's weather_service.py) -----------------------
 
async def main():
    from agntcy_app_sdk.factory import AgntcyFactory
    from common.mcp_client import mcp_endpoint, mcp_transport
    from config.config import OTEL_SDK_DISABLED
 
    factory = AgntcyFactory("travel.email_mcp", enable_tracing=not OTEL_SDK_DISABLED)
    transport = factory.create_transport(
        mcp_transport,
        endpoint=mcp_endpoint,
        shared_secret_identity=os.getenv("SLIM_SHARED_SECRET"),
        name=f"default/default/{TOPIC}",
    )
    app_session = factory.create_app_session()
    app_session.add(mcp._mcp_server).with_transport(transport).with_topic(TOPIC).with_session_id(
        "default_session").build()
    await app_session.start_all_sessions(keep_alive=False)
    logger.info("Trip email MCP server ready on topic %s", TOPIC)
    print(f"Trip email MCP server ready on topic {TOPIC}", flush=True)
    await app_session.start_all_sessions(keep_alive=True)
 
 
def _selftest():
    """Build a sample plan from the mock data and show what the email looks like."""
    from travel.common.data import mock_forecast, search_activities, search_flights, search_hotels, trip_days
 
    req = TripRequest(origin="SFO", destination="Tokyo", depart_date="2026-11-12",
                      return_date="2026-11-16", budget_usd=2000, interests=["food", "temples"])
    days = trip_days(req.depart_date, req.return_date)
    acts = search_activities(req.destination, req.interests)
    plan = Plan(
        version=6,
        forecast=mock_forecast(req.destination, days, [1]),
        flight={"outbound": search_flights("SFO", "Tokyo")[0], "return": search_flights("Tokyo", "SFO")[0]},
        hotel=search_hotels("Tokyo", nights=req.nights)[0],
        days=[{"day": i, "date": d, "activities": acts[2 * i:2 * i + 2]} for i, d in enumerate(days)],
    )
    out = compose("traveler@example.com", plan, req)
    print(out["text"])
    print(f"\nmailto link: {len(out['mailto'])} characters")
    with open("/tmp/trip.ics", "w") as f:
        f.write(out["ics"])
    print(f"Wrote /tmp/trip.ics with {out['ics'].count('BEGIN:VEVENT')} events")
 
 
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        asyncio.run(main())
