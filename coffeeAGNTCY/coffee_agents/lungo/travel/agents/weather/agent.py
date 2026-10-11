"""Weather Agent for TravelAgntcy.

Owns the forecast field in the plan. Evaluates outdoor activities
against rain days and objects to weather clashes without conceding.
"""

from travel.common.data import mock_forecast, outdoor_on_rain_days, resolve_city, trip_days
from travel.common.models import Plan
from travel.common.negotiator import Negotiator
from common.mcp_client import call_mcp_tool
from common.stable_agent_id import stable_agent_id_for_name
import asyncio

async def live_weather(city: dict) -> str | None:
    try:
        text = await asyncio.wait_for(call_mcp_tool(
            topic="lungo_weather_service", tool_name="get_forecast",
            target_stable_agent_id=stable_agent_id_for_name("Weather MCP Server"),
            arguments={"latitude": city["lat"], "longitude": city["lon"]},
            agent_id="Weather", source="travel_weather", extract_text=True), timeout=10)
        return text.split("\n")[0]                     # "Temperature: 18°C"
    except Exception:
        return None 


class WeatherAgent(Negotiator):
    name = "Weather"
    base_threshold = 1.0
    concession = 0.0  # Weather never concedes: rain clashes are strictly unacceptable
    floor = 1.0
    _live: str | None = None  # live reading for this turn, set in on_turn

    def _days(self, msg) -> list[str]:
        return trip_days(msg.request.depart_date, msg.request.return_date)

    def has_my_part(self, msg) -> bool:
        """Check if a forecast is already filled in the plan (nothing to forecast without dates)."""
        return bool(msg.plan.forecast) or not self._days(msg)

    def propose(self, msg) -> Plan:
        """Generate forecast for trip destination and dates."""
        p = msg.plan.model_copy(deep=True)
        dest = msg.request.destination
        p.forecast = mock_forecast(dest or "", self._days(msg), rain_days=msg.request.rain_days)
        return p

    def satisfaction(self, msg) -> float:
        """Return 1.0 if zero outdoor activities clash with rain, else 0.0."""
        if not msg.plan.forecast:
            return 0.0 if self._days(msg) else 1.0

        clashes = outdoor_on_rain_days(msg.plan.forecast, msg.plan.days)
        return 1.0 if clashes == 0 else 0.0

    def needs_redo(self, msg) -> str | None:
        """Redo my forecast if it no longer matches the trip (for example, rain was added)."""
        if msg.plan.forecast != self.propose(msg).forecast:
            return "updated forecast to reflect new weather conditions"
        return None

    def improve(self, msg) -> tuple[Plan, str] | None:
        """Weather can't move activities itself, so it returns None and OBJECTs to clashes."""
        return None

    async def on_turn(self, msg):
        self._live = None
        if not self.has_my_part(msg) or self.needs_redo(msg):   # only when posting a forecast
            city = resolve_city(msg.request.destination)
            self._live = await live_weather(city) if city else None
        return await super().on_turn(msg)

    def decide(self, msg):
        move, plan, note = super().decide(msg)
        if move == "CHANGE" and self._live:
            note += f" (live now in {msg.request.destination}: {self._live})"
        return move, plan, note