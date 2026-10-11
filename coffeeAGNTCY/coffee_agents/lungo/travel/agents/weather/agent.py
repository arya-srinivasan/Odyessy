"""Weather Agent for TravelAgntcy.

Owns the forecast field in the plan. Evaluates outdoor activities
against rain days and objects to weather clashes without conceding.
"""

from travel.common.data import mock_forecast, outdoor_on_rain_days, trip_days
from travel.common.models import Plan
from travel.common.negotiator import Negotiator


class WeatherAgent(Negotiator):
    name = "Weather"
    base_threshold = 1.0
    concession = 0.0  # Weather never concedes: rain clashes are strictly unacceptable
    floor = 1.0

    def has_my_part(self, msg) -> bool:
        """Check if a forecast is already filled in the plan."""
        return bool(msg.plan.forecast)

    def propose(self, msg) -> Plan:
        """Generate forecast for trip destination and dates."""
        p = msg.plan.model_copy(deep=True)
        dest = msg.request.destination
        days = trip_days(msg.request.depart_date, msg.request.return_date)
        p.forecast = mock_forecast(dest, days, rain_days=msg.request.rain_days)
        return p

    def satisfaction(self, msg) -> float:
        """Return 1.0 if zero outdoor activities clash with rain, else 0.0."""
        if not msg.plan.forecast:
            return 0.0

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