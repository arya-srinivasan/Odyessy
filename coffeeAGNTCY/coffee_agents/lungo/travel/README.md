# Odyssey

**Six agents plan your trip together. Nobody's in charge.**

Odyssey is a trip planner where six AI agents (Weather, Flights, Trains, Hotels,
Activities and Budget) negotiate with each other over a shared channel until they
all agree on one plan. You chat with it in plain language, watch the agents argue,
and get a trip that respects your budget.

Built for the Outshift by Cisco hackathon on top of the
[coffeeAgntcy](https://github.com/agntcy/coffeeAgntcy) Lungo demo.

## Why we built it

We all love to travel, whether that is going home to family or exploring somewhere
new. But planning a trip means jumping between booking.com, Expedia, the Amtrak app
and more, trying to find a mix of flights, trains, hotels and things to do that is
also affordable. It is stressful and it takes a long time, and time is valuable.

Odyssey makes that simpler, so you can focus on enjoying the trip.

## What it does

- **Chat to plan.** Type something like `SFO to Tokyo, Nov 12 to 16, budget 2000, I like temples`.
  If something is missing (dates, destination), Odyssey asks.
- **Agents negotiate.** Each agent fills in its part, then they take turns scoring the
  plan. If one is unhappy, it objects, and the others try to fix it. For example,
  Budget objects that the trip is $1,011 over, and Hotels switches to a cheaper
  hotel to save $1,088.
- **It adapts.** Click "Make it rain on day 2" or "Cancel my outbound flight" and the
  agents renegotiate around the change.
- **Trains vs planes.** On routes with a direct train both ways (London to Paris,
  Tokyo to Kyoto), Trains books rail and Flights steps aside. If the train is too
  expensive for the budget, Flights books flights instead.
- **Honest results.** If no plan fits the budget after four rounds, the agents vote
  and Odyssey tells you how far over it is instead of hiding it.
- **You stay in control.** Odyssey never books anything. "Email me this trip" opens
  your own mail app with the plan and a calendar file filled in; you press send.

## How it works

Most travel assistants use a supervisor agent that routes the conversation to
sub-agents one after another. That works, but the user's constraints (like the
budget) can get lost along the way. Odyssey uses an **undirected** design instead:
no agent is in charge, and every agent can push back on every other agent.

```
Browser ──► Gateway (FastAPI) ──► SLIM channel ◄──► Weather, Flights, Trains,
   ▲            │                                    Hotels, Activities, Budget
   │            └──► Email MCP (FastMCP, over SLIM)
   └──── plan, costs, agent log, email
```

- **Agents (LangGraph).** Each agent has a goal it optimizes for, a way to improve
  the plan when others object, and a way to object when it is not satisfied. Every
  agent concedes a little each round (except Weather, which never accepts outdoor
  plans on a rainy day).
- **SLIM + A2A.** Each agent runs on its own and registers on SLIM as an A2A server.
  For every trip, the Gateway opens a SLIM channel, invites the agents that are live,
  and they take turns until all of them accept the same version of the plan, or four
  rounds pass and they vote.
- **LLM.** An LLM reads the chat message into a structured trip request, and writes
  each agent's message in plain English. Every LLM call has a timeout and a fallback,
  so the app still works if the LLM is slow or off.
- **Data.** Flights, trains, hotels, activities and the day-by-day forecast come from
  a small sample dataset in `data/`. Weather also asks Lungo's existing weather MCP
  for a live reading.
- **Email MCP.** A small MCP server built with FastMCP composes the email subject,
  body and calendar file for the finished trip.

## Challenges

- **One stuck agent froze everything.** If an agent crashed or hung, the whole
  negotiation stalled. We added timeouts, and a failing agent now passes its turn
  with an objection instead.
- **Too much noise on the channel.** Every agent received every message and replied
  to all of them. We added strict turn-taking so only one agent talks at a time.
- **Edge cases from real typing.** Past dates, a $0 budget, 100 travelers, a hotel
  area in the wrong city, a plan that booked both a flight and a train. We tested
  these by hand and the app now explains what it changed.

## What we learned

How different multi-agent structures (supervisor vs undirected) change what the
system can do, hands-on work with A2A and Cisco's SLIM, how to design decision
making between agents, and building small reasoning agents with LangGraph.

## What's next

- Real APIs for flights, hotels, weather and activities instead of sample data,
  covering many more destinations.
- Trip history, so Odyssey learns your preferences over time.

## Running it

All commands run from the `lungo` folder (`coffeeAGNTCY/coffee_agents/lungo`).
You need [uv](https://docs.astral.sh/uv/) and Python 3.13.

### 1. Quick start on a laptop (no LLM, no SLIM)

Everything runs in one process with sample text instead of the LLM.

```bash
TRAVEL_DISABLE_LLM=true TRAVEL_LOCAL_MODE=true PYTHONPATH=. \
  uv run uvicorn travel.gateway.main:app --port 8090
```

Open http://localhost:8090.

### 2. With the LLM (and the email MCP over SLIM)

Put your LLM settings in `.env` in the `lungo` folder. Never commit this file.

```
LLM_MODEL=...
LITELLM_PROXY_BASE_URL=...
LITELLM_PROXY_API_KEY=...
```

You also need a SLIM server listening on `localhost:46357`
(for example `docker compose up -d slim`). Check with `ss -ltn | grep 46357`.

Terminal 1, the email MCP server:

```bash
set -a; source .env; set +a
SLIM_SERVER=localhost:46357 PYTHONPATH=. uv run python -m travel.email_mcp.server
```

Terminal 2, the Gateway (agents run inside it):

```bash
pkill -f "uvicorn travel.gateway"
unset TRAVEL_DISABLE_LLM
set -a; source .env; set +a
TRAVEL_LOCAL_MODE=true SLIM_SERVER=localhost:46357 PYTHONPATH=. \
  uv run uvicorn travel.gateway.main:app --host 0.0.0.0 --port 3000
```

Open http://localhost:3000, or your lab's public URL for that port.

### 3. Full SLIM mode (each agent is its own process)

```bash
bash travel/start_agents.sh          # starts all six agents, logs in /tmp/agent_*.log
set -a; source .env; set +a
SLIM_SERVER=localhost:46357 PYTHONPATH=. \
  uv run uvicorn travel.gateway.main:app --host 0.0.0.0 --port 3000
```

Leave out `TRAVEL_LOCAL_MODE` so the Gateway talks to the agents over SLIM.

### Tests

```bash
TRAVEL_DISABLE_LLM=true uv run --extra dev pytest travel/tests -q
```

### Settings

| Variable | What it does |
|---|---|
| `TRAVEL_DISABLE_LLM=true` | Skip the LLM and use the built-in text |
| `TRAVEL_LOCAL_MODE=true` | Run the six agents inside the Gateway instead of over SLIM |
| `TRAVEL_LLM_TIMEOUT` | Seconds to wait for each LLM call before falling back (default 20) |
| `SLIM_SERVER` | Where SLIM is, usually `localhost:46357` |

## Try these

| Prompt | What to watch |
|---|---|
| `SFO to Tokyo, Nov 12 to 16, budget 2000, I like temples` | Budget objects, Hotels gives up a 4.8 star hotel to save $1,088 |
| Then click **Make it rain on day 2** | Activities move outdoor plans off the rainy days |
| `London to Paris, Nov 12 to 14` | Trains books the Eurostar both ways, no flights needed |

## Project layout

```
travel/
  agents/       the six agents (one folder each)
  common/       negotiation rules, channel (local and SLIM), data search, LLM helpers
  data/         sample flights, trains, hotels, activities, forecast (generate.py builds them)
  email_mcp/    FastMCP server that composes the trip email
  gateway/      FastAPI app, chat intake, and the web page (static/index.html)
  tests/        pytest suite
```

## Built with

Python, LangGraph, SLIM, A2A, FastMCP, FastAPI, LiteLLM, uv.
