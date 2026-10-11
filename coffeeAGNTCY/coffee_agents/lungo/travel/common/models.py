"""Shared data shapes for the TravelAgntcy channel.

Three models travel on the SLIM group channel (or the local channel):

* TripRequest - what the traveler wants (+ events like rain or a cancelled flight)
* Plan        - the shared plan the agents build together, version by version
* ChannelMsg  - one message on the channel: who spoke, their move, the plan, whose turn is next

Every agent reads and writes the same ChannelMsg, so all six always see the
same plan. Messages go over A2A as plain text: "TRAVEL_CHANNEL {json}".
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# --- Channel constants ----------------------------------------------------

CHANNEL_PREFIX = "TRAVEL_CHANNEL "
END_TOKEN = "PLAN_FINAL"  # the Gateway closes the channel when it sees this
MAX_ROUNDS = 4  # after this many full rotations -> ranked vote
MAX_TRIP_NIGHTS = 14  # longest trip we plan (two weeks); longer -> Gateway asks for dates again
DEFAULT_TRIP_NIGHTS = 3  # assumed when there's no return date yet (e.g. a flight-only request)

# Speaking order. Weather goes first so everyone plans around the forecast.
# Agents that aren't on the channel are simply skipped.
TURN_ORDER = ["Weather", "Flights", "Trains", "Hotels", "Activities", "Budget"]

Move = Literal["ACCEPT", "CHANGE", "OBJECT", ""]
MsgType = Literal["START", "MOVE", "FINAL"]
Intent = Literal["flight", "hotel", "activities", "full_trip", "general"]


# --- What the traveler wants ----------------------------------------------

class BusySlot(BaseModel):
    """A busy block from Google Calendar free/busy (ISO datetimes)."""

    start: str
    end: str


class TripRequest(BaseModel):
    intent: Intent = "full_trip"
    origin: str | None = None
    destination: str | None = None
    depart_date: str | None = None  # YYYY-MM-DD
    return_date: str | None = None  # YYYY-MM-DD
    travelers: int = 1
    budget_usd: float | None = None
    interests: list[str] = Field(default_factory=list)  # e.g. ["food", "temples"]
    avoid: list[str] = Field(default_factory=list)  # activity categories to skip, e.g. ["museum"]
    must_do: list[str] = Field(default_factory=list)  # activity names to include, e.g. ["Louvre highlights"]
    activities_per_day: int | None = None  # pace: 1 (relaxed) to 3 (packed); None = 2
    hotel_area: str | None = None  # e.g. "Shibuya"

    # Constraints and events (Level 2 "changing conditions")
    busy: list[BusySlot] = Field(default_factory=list)  # from the Calendar MCP
    rain_days: list[int] = Field(default_factory=list)  # 0-based trip days forced to rain
    cancelled_flights: list[str] = Field(default_factory=list)  # flight ids

    @field_validator("travelers", mode="before")
    @classmethod
    def _at_least_one_traveler(cls, v):
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 1

    @field_validator("activities_per_day", mode="before")
    @classmethod
    def _sensible_pace(cls, v):
        try:
            return min(3, max(1, int(v))) if v is not None else None
        except (TypeError, ValueError):
            return None

    @field_validator("budget_usd", mode="before")
    @classmethod
    def _positive_budget(cls, v):
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    @field_validator("depart_date", "return_date", mode="before")
    @classmethod
    def _iso_date_or_none(cls, v):
        """Unparseable dates become None so the Gateway asks for them again."""
        try:
            return date.fromisoformat(str(v).strip()).isoformat() if v else None
        except ValueError:
            return None

    @model_validator(mode="after")
    def _return_after_depart(self):
        if self.depart_date and self.return_date:
            nights = (date.fromisoformat(self.return_date) - date.fromisoformat(self.depart_date)).days
            if nights < 0 or nights > MAX_TRIP_NIGHTS:
                self.return_date = None
        return self

    @property
    def rooms(self) -> int:
        """Hotel rooms needed: two travelers share a room."""
        return math.ceil(self.travelers / 2)

    def over_budget_by(self, plan: "Plan") -> float:
        """Dollars over budget (0 if within budget or no budget set)."""
        if not self.budget_usd:
            return 0.0
        return max(0.0, round(plan.total(self.travelers) - self.budget_usd, 2))

    @property
    def nights(self) -> int:
        try:
            n = (date.fromisoformat(self.return_date) - date.fromisoformat(self.depart_date)).days
            return max(1, n)
        except (TypeError, ValueError):
            return DEFAULT_TRIP_NIGHTS

    def missing_fields(self) -> list[str]:
        """What the Gateway still has to ask the traveler before opening the channel."""
        required = {
            "flight": ["origin", "destination", "depart_date"],
            "hotel": ["destination", "depart_date", "return_date"],
            "activities": ["destination", "depart_date", "return_date"],
            "full_trip": ["origin", "destination", "depart_date", "return_date"],
        }.get(self.intent, [])
        return [f for f in required if not getattr(self, f)]


# --- The shared plan --------------------------------------------------------

class Plan(BaseModel):
    """Each agent owns one part. A CHANGE replaces that part and bumps the version."""

    version: int = 0
    forecast: list[dict] = Field(default_factory=list)  # Weather:    [{"day", "date", "sky", "outdoor_ok"}]
    flight: dict | None = None  # Flights:    {"outbound": {...}, "return": {...}}
    train: dict | None = None  # Trains:     a rail leg, or None
    hotel: dict | None = None  # Hotels:     one hotel record + "nights"
    days: list[dict] = Field(default_factory=list)  # Activities: [{"day", "date", "activities": [...]}]

    def costs(self, travelers: int = 1) -> dict[str, float]:
        flight = 0.0
        if self.flight:
            for leg in ("outbound", "return"):
                if self.flight.get(leg):
                    flight += self.flight[leg]["price_usd"]
        train = sum(t["price_usd"] for t in self.train_legs())
        hotel = self.hotel["price_per_night_usd"] * self.hotel.get("nights", 1) if self.hotel else 0.0
        acts = sum(a["cost_usd"] for d in self.days for a in d.get("activities", []))
        # Hotel is per room (two travelers share); transport and activities are per person.
        return {
            "flights": flight * travelers,
            "train": train * travelers,
            "hotel": hotel * math.ceil(travelers / 2),
            "activities": acts * travelers,
        }

    def total(self, travelers: int = 1) -> float:
        return round(sum(self.costs(travelers).values()), 2)

    def co2_kg(self) -> float:
        co2 = 0.0
        if self.flight:
            for leg in ("outbound", "return"):
                if self.flight.get(leg):
                    co2 += self.flight[leg].get("co2_kg", 0)
        co2 += sum(t.get("co2_kg", 0) for t in self.train_legs())
        return co2

    def train_legs(self) -> list[dict]:
        """The outbound train, plus the return train if Trains booked one."""
        if not self.train:
            return []
        return [self.train] + ([self.train["return"]] if self.train.get("return") else [])

    def summary(self, travelers: int = 1) -> str:
        parts = []
        if self.flight and self.flight.get("outbound"):
            f = self.flight["outbound"]
            parts.append(f"{f['airline']} {f['flight_no']} ${f['price_usd']}")
        if self.train:
            parts.append(f"{self.train['operator']} ${self.train['price_usd']}")
        if self.hotel:
            parts.append(f"{self.hotel['name']} x{self.hotel.get('nights', 1)}")
        if self.days:
            n = sum(len(d.get("activities", [])) for d in self.days)
            parts.append(f"{n} activities")
        return f"v{self.version}: " + ", ".join(parts) + f" | total ${self.total(travelers):,.0f}"


# --- One message on the channel ----------------------------------------------

class LogLine(BaseModel):
    """One line of the negotiation log shown in the UI."""

    round: int
    sender: str
    move: str
    version: int
    note: str


class ChannelMsg(BaseModel):
    type: MsgType = "MOVE"
    sender: str = "Gateway"
    move: Move = ""
    note: str = ""  # plain-words reason, written by the agent (LLM or template)
    round: int = 0  # full rotations completed
    next: str = ""  # whose turn it is; everyone else stays silent
    participants: list[str] = Field(default_factory=list)  # agents on this channel

    request: TripRequest
    plan: Plan = Field(default_factory=Plan)
    accepts: list[str] = Field(default_factory=list)  # who accepted plan.version
    objections: dict[str, str] = Field(default_factory=dict)  # agent -> open objection

    # For the ranked vote after MAX_ROUNDS: every version seen + each agent's score of it.
    versions: dict[str, Plan] = Field(default_factory=dict)  # "3" -> Plan
    scores: dict[str, dict[str, float]] = Field(default_factory=dict)  # "3" -> {"Budget": 0.7, ...}

    history: list[LogLine] = Field(default_factory=list)

    # --- wire format ---
    def to_text(self) -> str:
        text = CHANNEL_PREFIX + self.model_dump_json()
        # The end token must appear in the payload so the Gateway can stop collecting.
        return text + f" {END_TOKEN}" if self.type == "FINAL" else text

    @classmethod
    def from_text(cls, text: str) -> "ChannelMsg | None":
        text = (text or "").strip()
        if not text.startswith(CHANNEL_PREFIX):
            return None
        body = text[len(CHANNEL_PREFIX):]
        if body.endswith(END_TOKEN):
            body = body[: -len(END_TOKEN)].rstrip()
        try:
            return cls.model_validate_json(body)
        except Exception:
            return None

    def log(self) -> str:
        return "\n".join(
            f"[r{l.round}] {l.sender:<10} {l.move:<7} v{l.version}  {l.note}" for l in self.history
        )


def to_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")