"""Intake layer for turning conversation messages into structured TripRequests."""

from datetime import date, timedelta
import re
from typing import Any

from pydantic import BaseModel, Field

from travel.common.data import CITIES
from travel.common.llm import llm_structured
from travel.common.models import MAX_TRIP_NIGHTS, TripRequest

INTAKE_SYSTEM_PROMPT = """You are a travel planning assistant parsing user requests into a TripRequest schema.
Today's date is {today}. Dates without a year are the next time that date comes around.

Extract any travel parameters mentioned:
- origin (city or airport code)
- destination (city or airport code)
- depart_date (YYYY-MM-DD format)
- return_date (YYYY-MM-DD format)
- travelers (number of people going, including the user; "me and my wife" is 2)
- budget_usd (total budget for the whole group, as a number)
- interests (list of strings like ["food", "museum", "temple", "culture", "sightseeing"])
- hotel_area (a neighborhood to stay in, e.g. "Shibuya")

Only populate fields that are explicitly mentioned or clearly implied. Leave others null or empty.
"""

INTEREST_WORDS = {
    "food": ["food", "eat", "restaurant", "sushi", "ramen", "cuisine"],
    "museum": ["museum", "art", "gallery"],
    "sightseeing": ["sightseeing", "sights", "landmark"],
    "culture": ["culture", "cultural", "history", "historic"],
    "temple": ["temple", "shrine"],
    "park": ["park", "garden", "nature", "outdoors"],
    "nightlife": ["nightlife", "bar", "club", "jazz"],
    "shopping": ["shopping", "shop", "market"],
}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
CITY_ALIASES = {"NYC": "New York", "LA": "Los Angeles", "SF": "San Francisco"}  # matched in CAPS only
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


class IntakeFields(BaseModel):
    """What the LLM may fill in from a chat message. Events (rain, cancellations) and
    calendar data only come from the Gateway's own endpoints, never from free text."""

    origin: str | None = None
    destination: str | None = None
    depart_date: str | None = None
    return_date: str | None = None
    travelers: int | None = None
    budget_usd: float | None = None
    interests: list[str] = Field(default_factory=list)
    hotel_area: str | None = None


def _city_mentions(text: str) -> list[tuple[int, dict]]:
    """Every known city or airport code in the text, in order of appearance."""
    found = []
    for code, info in CITIES.items():
        aliases = {a for a, city in CITY_ALIASES.items() if city == info["city"]}
        for name in {code, info["city"]} | aliases:
            flags = 0 if name.isupper() else re.IGNORECASE  # codes and aliases must be CAPS
            for m in re.finditer(rf"\b{re.escape(name)}\b", text, flags):
                found.append((m.start(), info))
    found.sort(key=lambda f: f[0])
    seen, ordered = set(), []
    for pos, info in found:
        if info["city"] not in seen:
            seen.add(info["city"])
            ordered.append((pos, info))
    return ordered


def _name_at(text: str, pos: int) -> str:
    """The city name or code that starts at pos (the word(s) _city_mentions matched)."""
    for code, info in CITIES.items():
        for name in (code, info["city"], *[a for a, c in CITY_ALIASES.items() if c == info["city"]]):
            if text[pos:pos + len(name)].lower() == name.lower():
                return name
    return ""


def _next_occurrence(month: int, day: int, today: date) -> date | None:
    """The next time month/day comes around (this year, or next year if it has passed)."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def _extract_dates(lower: str, today: date) -> tuple[str | None, str | None]:
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", lower)
    if iso:
        return iso[0], iso[1] if len(iso) > 1 else None
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    m = re.search(rf"{mon}\s+(\d{{1,2}})(?:st|nd|rd|th)?"
                  rf"(?:\s*(?:to|-|until|through|thru)\s*(?:{mon}\s+)?(\d{{1,2}})(?:st|nd|rd|th)?)?", lower)
    if m:
        start = _next_occurrence(MONTHS[m.group(1)[:3]], int(m.group(2)), today)
        if not start:
            return None, None
        if not m.group(4):
            return start.isoformat(), None
        end_month = MONTHS[m.group(3)[:3]] if m.group(3) else start.month
        end_day = int(m.group(4))
        if not m.group(3) and end_day < start.day:   # "Nov 28 to 3" -> Dec 3
            end_month = start.month % 12 + 1
        end = _next_occurrence(end_month, end_day, start)
        return start.isoformat(), end.isoformat() if end else None
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{mon}", lower)   # "15th of November", "15 Nov"
    if m:
        start = _next_occurrence(MONTHS[m.group(2)[:3]], int(m.group(1)), today)
        return (start.isoformat() if start else None), None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:\s*(?:to|-)\s*(\d{1,2})/(\d{1,2}))?", lower)
    if m:
        start = _next_occurrence(int(m.group(1)), int(m.group(2)), today)
        end = _next_occurrence(int(m.group(3)), int(m.group(4)), start) if start and m.group(3) else None
        return (start.isoformat() if start else None), (end.isoformat() if end else None)
    return None, None


def _extract_travelers(lower: str) -> int | None:
    num = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"
    for pattern in (rf"\b{num}\s+(?:travel+ers?|people|persons|adults|guests|friends|of us)\b",
                    rf"\bfamily of {num}\b", rf"\bgroup of {num}\b", rf"\bparty of {num}\b",
                    rf"\bfor {num}\b(?!\s*(?:days?|nights?|weeks?|\$|dollars|k\b))"):
        m = re.search(pattern, lower)
        if m:
            n = m.group(1)
            return int(n) if n.isdigit() else NUMBER_WORDS[n]
    if re.search(r"\b(?:me|i) and my (?:wife|husband|partner|girlfriend|boyfriend|friend|mom|dad|son|daughter)\b", lower):
        return 2
    if re.search(r"\b(?:solo|alone|just me|by myself)\b", lower):
        return 1
    return None


def _extract_budget(lower: str) -> float | None:
    m = re.search(r"(?:budget(?:\s+(?:of|is))?\s*[:=]?\s*\$?|\$)\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", lower) \
        or re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*(?:usd|dollars)\b", lower)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * 1000 if m.group(2) else value


def _heuristic_extract(text: str, today: date | None = None) -> dict[str, Any]:
    """Fallback extraction when LLM is offline or disabled."""
    today = today or date.today()
    updates: dict[str, Any] = {}
    lower = text.lower()

    # Cities: "from X" is the origin; otherwise the first city mentioned is the origin
    # and the second the destination. A single city is the destination ("trip to Paris").
    cities = _city_mentions(text)
    origin = next((info for pos, info in cities if re.search(r"\bfrom\s*$", lower[:pos])), None)
    if origin is None and len(cities) == 1:   # "SFO to Atlantis": the one city we know is the origin
        pos, info = cities[0]
        if re.match(r"\s*(?:to|-)\s", lower[pos + len(_name_at(text, pos)):]):
            origin = info
    rest = [info for _, info in cities if info is not origin]
    if origin is None and len(rest) >= 2:
        origin = rest.pop(0)
    if origin:
        updates["origin"] = origin["airport"]
    if rest:
        updates["destination"] = rest[0]["city"]
    elif unknown := re.search(r"\b(?:to|visit|visiting)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", text):
        # A place we have no data for: keep it, so the Gateway can say so instead of asking again.
        words = []
        for w in unknown.group(1).split():
            if w.lower()[:3] in MONTHS:   # "Atlantis Nov 12" -> "Atlantis"
                break
            words.append(w)
        if words:
            updates["destination"] = " ".join(words)

    depart, ret = _extract_dates(lower, today)
    length = re.search(r"\bfor (?:a |an )?(\d+|" + "|".join(NUMBER_WORDS) + r"|week)\s*(days?|nights?|weeks?)?\b", lower)
    if depart and not ret and length and (length.group(2) or length.group(1) == "week"):
        n = NUMBER_WORDS.get(length.group(1)) or (1 if length.group(1) == "week" else int(length.group(1)))
        unit = length.group(2) or "week"
        nights = n * 7 if unit.startswith("week") else (n - 1 if unit.startswith("day") else n)
        ret = (date.fromisoformat(depart) + timedelta(days=max(1, nights))).isoformat()
    if depart:
        updates["depart_date"] = depart
    if ret:
        updates["return_date"] = ret

    if (budget := _extract_budget(lower)) is not None:
        updates["budget_usd"] = budget
    if (travelers := _extract_travelers(lower)) is not None:
        updates["travelers"] = travelers

    interests = [k for k, words in INTEREST_WORDS.items()
                 if any(re.search(rf"\b{w}s?\b", lower) for w in words)]
    if interests:
        updates["interests"] = interests
    return updates


def find_missing(req: TripRequest) -> list[str]:
    """Identify required trip parameters that remain unset."""
    missing = []
    if not req.origin:
        missing.append("origin")
    if not req.destination:
        missing.append("destination")
    if not req.depart_date:
        missing.append("departure date")
    if not req.return_date:
        missing.append("return date")
    return missing


def question_for(missing: list[str]) -> str:
    """Generate a clean natural-language clarification question for missing fields."""
    if not missing:
        return ""
    if missing == ["return date"]:
        return f"When are you coming back? (I can plan trips of up to {MAX_TRIP_NIGHTS} nights.)"
    if len(missing) == 1:
        return f"Could you provide your {missing[0]}?"
    if len(missing) == 2:
        return f"Could you share your {missing[0]} and {missing[1]}?"

    asks = {"origin": "where you're departing from", "destination": "where you'd like to go",
            "departure date": "when you're leaving", "return date": "when you're coming back"}
    parts = [asks.get(m, m) for m in missing]
    return f"To get started, could you tell me {', '.join(parts[:-1])}, and {parts[-1]}?"


async def update_request(current: TripRequest, user_message: str) -> TripRequest:
    """Extract trip parameters from user message and merge non-empty fields into current."""
    messages = [
        {"role": "system", "content": INTAKE_SYSTEM_PROMPT.format(today=date.today().isoformat())},
        {"role": "user", "content": user_message},
    ]

    extracted: IntakeFields | None = await llm_structured(
        schema=IntakeFields,
        messages=messages,
        fallback=None,
    )

    updated_data = current.model_dump()

    if extracted:
        extracted_data = extracted.model_dump(exclude_none=True)
    else:
        # Fall back to regex/heuristic extraction
        extracted_data = _heuristic_extract(user_message)

    # Answering "When are you coming back?" with one date: that date is the return, not a new departure.
    if (current.depart_date and not current.return_date
            and extracted_data.get("depart_date") and not extracted_data.get("return_date")):
        extracted_data["return_date"] = extracted_data.pop("depart_date")

    for key, value in extracted_data.items():
        if value is not None and value != "" and value != []:
            if key == "interests":
                existing = set(updated_data.get("interests") or [])
                existing.update(value)
                updated_data["interests"] = sorted(existing)
            else:
                updated_data[key] = value

    return TripRequest(**updated_data)
