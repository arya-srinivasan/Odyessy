import asyncio, importlib, sys
import config.logging_config
from travel.common.a2a_server import serve

AGENTS = {
    "weather": ("travel.agents.weather.agent", "WeatherAgent"),
    "flights": ("travel.agents.flights.agent", "FlightsAgent"),
    "trains": ("travel.agents.trains.agent", "TrainsAgent"),
    "hotels": ("travel.agents.hotels.agent", "HotelsAgent"),
    "activities": ("travel.agents.activities.agent", "ActivitiesAgent"),
    "budget": ("travel.agents.budget.agent", "BudgetAgent"),
}

if __name__ == "__main__":
    module, cls = AGENTS[sys.argv[1]]
    agent = getattr(importlib.import_module(module), cls)()
    asyncio.run(serve(agent))