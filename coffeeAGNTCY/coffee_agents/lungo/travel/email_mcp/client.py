import asyncio
import json
import logging
 
from travel.common.models import Plan, TripRequest
 
logger = logging.getLogger("travel.email_mcp.client")
 
TOPIC = "travel_email_service"
MCP_TIMEOUT = 10  # seconds
 
 
async def compose_trip_email(to: str, plan: Plan, request: TripRequest) -> dict:
    try:
        from common.mcp_client import call_mcp_tool
        from common.stable_agent_id import stable_agent_id_for_name
 
        text = await asyncio.wait_for(call_mcp_tool(
            topic=TOPIC,
            tool_name="compose_trip_email",
            target_stable_agent_id=stable_agent_id_for_name("Travel Email MCP"),
            arguments={"to": to, "plan_json": plan.model_dump_json(), "request_json": request.model_dump_json()},
            agent_id="Gateway",
            source="travel_gateway",
            message_timeout=MCP_TIMEOUT,
            extract_text=True,
        ), timeout=MCP_TIMEOUT + 2)
        result = json.loads(text)
        result["via"] = "mcp"
        return result
    except Exception as e:
        logger.warning("Email MCP unavailable (%r); composing locally", e)
        from travel.email_mcp.server import compose
 
        result = compose(to, plan, request)
        result["via"] = "local"
        return result
 
 
if __name__ == "__main__":
    import config.logging_config  # noqa: F401
    from travel.common.data import search_flights, search_hotels
 
    req = TripRequest(origin="SFO", destination="Tokyo", depart_date="2026-11-12",
                      return_date="2026-11-16", budget_usd=2000)
    plan = Plan(version=1, forecast=[{"day": 0, "date": "2026-11-12", "sky": "sunny", "outdoor_ok": True},
                                     {"day": 4, "date": "2026-11-16", "sky": "sunny", "outdoor_ok": True}],
                flight={"outbound": search_flights("SFO", "Tokyo")[0], "return": search_flights("Tokyo", "SFO")[0]},
                hotel=search_hotels("Tokyo", nights=4)[0])
    r = asyncio.run(compose_trip_email("traveler@example.com", plan, req))
    print("via:", r["via"])
    print(r["subject"])
    print(r["mailto"][:120] + "...")