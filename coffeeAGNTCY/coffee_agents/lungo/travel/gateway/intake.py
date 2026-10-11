"""Intake layer for turning conversation messages into structured TripRequests."""

from datetime import date, timedelta
import re
from typing import Any

from pydantic import BaseModel, Field

from travel.common.data import CITIES, COUNTRY_ALIASES, hotel_areas, resolve_city
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
- interests (categories they want more of, from: food, museum, sightseeing, culture, temple, park, nightlife, shopping)
- avoid (categories from the same list they don't want, e.g. "no museums" -> ["museum"])
- activities_per_day (1 for a relaxed pace, 3 for packed, 2 for normal; only if they mention pace)
- hotel_area (a neighborhood to stay in, e.g. "Shibuya")

Never invent a city, flight, hotel or activity. Write places exactly as the traveler named them.

Only populate fields that are explicitly mentioned or clearly implied. Leave others null or empty.
"""

INTEREST_WORDS = {
    "food": ["food", "foodie", "eat", "eating", "restaurant", "sushi", "ramen", "cuisine", "cooking", "tasting"],
    "museum": ["museum", "art", "gallery", "galleries", "exhibit"],
    "sightseeing": ["sightseeing", "sights", "landmark", "views", "view", "skyline", "beach", "beaches", "cruise"],
    "culture": ["culture", "cultural", "history", "historic", "historical", "tradition", "traditional"],
    "temple": ["temple", "shrine"],
    "park": ["park", "garden", "nature", "outdoors", "outdoor", "hiking", "hike", "walking"],
    "nightlife": ["nightlife", "bar", "club", "jazz", "drinks", "show", "theater", "theatre"],
    "shopping": ["shopping", "shop", "market", "arcade", "anime", "games", "gaming"],
}
NEGATION = (r"\b(?:no|not into|not a fan of|not big on|don'?t (?:like|love|want|care for|enjoy)|do not like|"
            r"skip(?:ping)?|avoid|hate|without|not interested in|nothing|none of the|less|fewer|tired of)\s+"
            r"(?:any\s+|the\s+|more\s+|big\s+)?")
# Things travelers ask for that no city in our data offers: said out loud instead of dropped.
UNSUPPORTED_INTERESTS = {"ski": "skiing", "skiing": "skiing", "snowboarding": "snowboarding",
                         "scuba": "diving", "diving": "diving", "safari": "safaris",
                         "casino": "casinos", "theme park": "theme parks"}
MAX_TRAVELERS = 9
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
MONTH_WORDS = {"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
               "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov",
               "november", "dec", "december"}


def _is_month(word: str) -> bool:
    return word.lower().strip(".,!?") in MONTH_WORDS


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
    avoid: list[str] = Field(default_factory=list)
    activities_per_day: int | None = None
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
        # "sfo to tokyo", "sf to la": a lowercase code or alias counts when it sits in a route
        # ("from sfo", "sfo to", "to sfo").
        for short in [code, *aliases]:
            for m in re.finditer(rf"(?:\b(?:from|to)\s+)({short})\b|\b({short})(?=\s+(?:to|-)\s)", text, re.IGNORECASE):
                found.append((m.start(1) if m.group(1) else m.start(2), info))
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
            if text[pos:pos + len(name)].lower() == name.lower() and not text[pos + len(name):pos + len(name) + 1].isalpha():
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
        # Same year unless the range wraps past December ("Dec 28 to Jan 4"). An end before
        # the start in the same year is kept as given, so the traveler is told about it.
        try:
            end = date(start.year + (end_month < start.month), end_month, end_day)
        except ValueError:
            end = None
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


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _relative_dates(lower: str, today: date) -> tuple[date | None, date | None]:
    """'tomorrow', 'next week', 'this weekend', 'in 3 days', 'on Friday', 'back Sunday'."""
    depart = ret = None
    if re.search(r"\btoday\b", lower):
        depart = today
    elif re.search(r"\btomorrow\b", lower):
        depart = today + timedelta(days=1)
    elif re.search(r"\bnext week\b", lower):
        depart = today + timedelta(days=7 - today.weekday())               # next Monday
    elif m := re.search(r"\b(this|next) weekend\b", lower):
        coming_friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        if today.weekday() >= 4:   # already the weekend: "this" is now, "next" is the coming one
            depart = today if m.group(1) == "this" else coming_friday
        else:
            depart = coming_friday + timedelta(days=7 if m.group(1) == "next" else 0)
        ret = depart + timedelta(days=(6 - depart.weekday()) or 1)   # through Sunday
    elif m := re.search(r"\bin (\d+|" + "|".join(NUMBER_WORDS) + r"|a) (days?|weeks?)\b", lower):
        n = 1 if m.group(1) == "a" else NUMBER_WORDS.get(m.group(1)) or int(m.group(1))
        depart = today + timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    for m in re.finditer(r"\b(?:(back|return|returning|until|till|through)\s+(?:on\s+)?|on\s+|leaving\s+)?(" + "|".join(WEEKDAYS) + r")\b", lower):
        is_return = bool(m.group(1))
        base = depart if (is_return and depart) else today
        delta = (WEEKDAYS.index(m.group(2)) - base.weekday()) % 7 or 7
        if is_return:
            ret = base + timedelta(days=delta)
        elif depart is None:
            depart = base + timedelta(days=delta)
    return depart, ret


# Words in activity names too generic to name one place ("Museum", "Market", "walk")
GENERIC_NAME_WORDS = {"museum", "market", "temple", "tower", "garden", "gardens", "park", "walk", "tour", "class",
                      "show", "cruise", "tasting", "food", "highlights", "summit", "bridge", "crawl", "dinner",
                      "ceremony", "practice", "morning", "sunset", "river", "trail", "center", "pier", "hike",
                      "gates", "grove", "stalls", "station", "making", "baking", "hopping", "crossing", "afternoon",
                      "london", "tokyo", "kyoto", "paris", "boston", "york", "brooklyn", "shinjuku", "gion",
                      "shibuya", "arashiyama", "saint-germain", "quincy", "chelsea", "golden", "building", "pizza",
                      "bagel", "jazz", "club", "ferry", "fine", "arts", "national", "railway", "west", "end"}


def _must_sees(lower: str) -> list[tuple[str, str]]:
    """Activities named in the text: [(activity name, city)]. A name counts by its full name
    or one of its own distinctive words ("Louvre", "teamLab", "Alcatraz", "Senso-ji")."""
    from travel.common.data import ACTIVITIES
    # A neighborhood or city on its own ("stay in Montmartre") is a place, not an activity.
    place_words = {w for name in [*hotel_areas(), *(c["city"] for c in CITIES.values())]
                   for w in name.lower().split()}
    found = []
    for a in ACTIVITIES:
        name = a["name"].lower()
        words = {w.strip("()&,'") for w in name.split()} - GENERIC_NAME_WORDS - place_words
        keys = {name} | {w for w in words if len(w) >= 4}
        if any(re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", lower) for k in keys):
            found.append((a["name"], a["city"]))
    return found


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


GREETINGS = {"hi", "hello", "hey", "yes", "no", "yeah", "nope", "ok", "okay", "sure", "thanks", "thank",
             "please", "help", "what", "how", "why", "when", "where", "who", "can", "could", "i", "we",
             "the", "a", "an", "and", "or", "next", "this", "tomorrow", "today", "actually", "planning"}
# Words that follow "to"/"from" without being a place ("to go", "from home")
NOT_PLACES = GREETINGS | {"go", "plan", "book", "see", "visit", "travel", "fly", "be", "do", "get", "have", "make",
                          "take", "stay", "find", "know", "help", "try", "start", "leave", "return", "come",
                          "spend", "eat", "explore", "check", "home", "work", "here", "there", "me", "us", "you"}
WORD = r"[^\W\d_][^\W\d_'.-]*"           # a word of letters, any alphabet (São, Zürich)
PLACE = rf"{WORD}(?:\s+{WORD}){{0,2}}"     # up to three words


def _trim_place(raw: str, capitalized: bool = True) -> str | None:
    """Cut a captured place down to its name: stop at a month, a lowercase word or a filler word."""
    words = []
    for w in raw.split():
        if _is_month(w) or w.lower() in GREETINGS or w.lower() in {"for", "on", "in", "with", "from", "to", "and"}:
            break
        if capitalized and not w[0].isupper():
            break
        words.append(w.strip(".,!?"))
    return " ".join(words) or None


def _extract_places(text: str) -> tuple[str | None, str | None, list[str]]:
    """(origin, destination, other destinations mentioned) from free text.

    Known cities come back as an airport code (origin) or city name (destination); places
    we have no data for come back as written, so the Gateway can name them. Corrections
    ("from Boston? no wait, from New York") take the last "from"."""
    lower = text.lower()
    mentions = []   # (position, end, value-as-origin, value-as-destination, known?)
    for pos, info in _city_mentions(text):
        name = _name_at(text, pos)
        end = pos + len(name)
        qualifier = re.match(rf",\s*(?:the\s+)?({PLACE})", text[end:])
        q = _trim_place(qualifier.group(1)) if qualifier else None
        if q and q.lower() not in COUNTRY_ALIASES.get(info["country"], set()) and not resolve_city(q):
            label = f"{text[pos:end]}, {q}"          # "Paris, Texas" is not Paris, France
            mentions.append((pos, end, label, label, False))
        else:
            mentions.append((pos, end, info["airport"], info["city"], True))
    # Places we have no data for, after a route word ("to Rome", "from Narnia", "to the UK")
    for m in re.finditer(rf"\b(from|to|visit|visiting)\s+(?:the\s+)?({PLACE})", text):
        if any(p <= m.start(2) < e for p, e, *_ in mentions):
            continue
        place = _trim_place(m.group(2))
        if place and not resolve_city(place):
            mentions.append((m.start(2), m.start(2) + len(place), place, place, False))
    # lowercase unknowns only after a travel verb ("go to sydney"), to avoid "to plan" etc.
    for m in re.finditer(rf"\b(?:go|fly|flying|travel|traveling|travelling|trip|visit|going|heading|head)\s+to\s+({WORD})", lower):
        if any(p <= m.start(1) < e for p, e, *_ in mentions):
            continue
        place = _trim_place(text[m.start(1):m.end(1)], capitalized=False)
        if place and not resolve_city(place):
            mentions.append((m.start(1), m.end(1), place.title(), place.title(), False))
    # "Narnia to Tokyo": an unknown first word before a route arrow
    lead = re.match(rf"\s*({PLACE})\s+(?:to|-)\s", text)
    if lead and not any(p == lead.start(1) for p, *_ in mentions):
        place = _trim_place(lead.group(1))
        if place and not resolve_city(place):
            mentions.append((lead.start(1), lead.start(1) + len(place), place, place, False))
    # all lowercase, in a route with a city we know: "sf to chicago", "chicago to sf"
    if any(known for *_, known in mentions):
        for m in re.finditer(rf"\b(?:from|to)\s+({WORD})|^\s*({WORD})(?=\s+(?:to|-)\s)", lower):
            start = m.start(1) if m.group(1) else m.start(2)
            word = m.group(1) or m.group(2)
            if any(p <= start < e for p, e, *_ in mentions) or word in NOT_PLACES or _is_month(word):
                continue
            if not resolve_city(word):
                mentions.append((start, start + len(word), word.title(), word.title(), False))
    mentions.sort()
    if not mentions:
        bare = _trim_place(text.strip(" .!?"))
        if bare and bare == text.strip(" .!?") and len(bare.split()) <= 3:
            return None, bare, []           # a reply that is just a place name ("Rome")
        return None, None, []

    def after(word_re, pos):
        return re.search(rf"\b(?:{word_re})\s+(?:the\s+)?$", lower[:pos])

    froms = [m for m in mentions if after("from", m[0])]
    origin = froms[-1] if froms else None
    if origin is None:
        followed = [m for m in mentions if re.match(r"\s*(?:to|-)\s", lower[m[1]:])]
        if followed:
            origin = followed[0]
        elif len(mentions) >= 2:
            origin = mentions[0]
    dests = [m for m in mentions if m is not origin and m not in froms]
    destination = dests[0] if dests else None
    if destination is None and origin and origin[4]:
        same = re.search(rf"\bto\s+({PLACE})", text[origin[1]:])     # "Tokyo to Tokyo"
        city = resolve_city(_trim_place(same.group(1)) or "") if same else None
        if city and city["airport"] == origin[2]:
            return origin[2], city["city"], []
    others = [m[3] for m in dests[1:] if m[3] != destination[3]]
    return (origin[2] if origin else None), (destination[3] if destination else None), others


def _heuristic_extract(text: str, today: date | None = None) -> dict[str, Any]:
    """Fallback extraction when LLM is offline or disabled."""
    today = today or date.today()
    updates: dict[str, Any] = {}
    lower = text.lower()

    origin, destination, others = _extract_places(text)
    if origin:
        updates["origin"] = origin
    if destination:
        updates["destination"] = destination
    if others:
        updates["_other_destinations"] = others

    depart, ret = _extract_dates(lower, today)
    rel_depart, rel_ret = _relative_dates(lower, today)
    depart = depart or (rel_depart.isoformat() if rel_depart else None)
    ret = ret or (rel_ret.isoformat() if rel_ret else None)
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

    # Where to stay: any neighborhood we have hotels in, plus whatever follows "stay in".
    areas = [(area, city) for area, city in hotel_areas().items()
             if re.search(rf"(?<![\w']){re.escape(area.lower())}(?![\w'])", lower)]
    if areas:
        updates["_areas"] = areas
    stay = re.search(rf"\bstay(?:ing)?\s+(?:somewhere\s+)?(?:in|near|around|at|close to|by)\s+(?:the\s+)?({PLACE})", text)
    if stay and (place := _trim_place(stay.group(1), capitalized=False)):
        updates["_stay_place"] = place

    if (budget := _extract_budget(lower)) is not None:
        updates["budget_usd"] = budget
    if (travelers := _extract_travelers(lower)) is not None:
        updates["travelers"] = travelers
    else:
        companions = r"(?:wife|husband|partner|girlfriend|boyfriend|friend|mom|dad|mother|father|son|daughter|kid|brother|sister|boss|colleague)"
        joining = re.findall(rf"\b(?:my|our) {companions}s? (?:is|are|will be) (?:coming|joining)|\b(?:bringing|plus|with) my {companions}\b", lower)
        if joining:
            updates["_extra_travelers"] = len(joining)

    # Likes and dislikes: "no museums", "not into nightlife" are things to skip, not interests.
    # A must-see's own name doesn't count ("British Museum" isn't a wish for more museums).
    must = _must_sees(lower)
    for name, _ in must:
        lower = lower.replace(name.lower(), " ")
        for word in name.lower().split():
            if word not in GENERIC_NAME_WORDS and len(word) >= 4:
                lower = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])\s*(?:\w+)?", " ", lower)
    avoid = [k for k, words in INTEREST_WORDS.items()
             if any(re.search(NEGATION + rf"{w}s?\b", lower) for w in words)]
    interests = [k for k, words in INTEREST_WORDS.items()
                 if k not in avoid and any(re.search(rf"\b{w}s?\b", lower) for w in words)]
    if interests:
        updates["interests"] = interests
    if avoid:
        updates["avoid"] = avoid
    if must:
        updates["_must_sees"] = must

    if re.search(r"\b(?:relax(?:ed|ing)?|slow|chill|easy|laid[- ]back|low[- ]key)\b(?:\s+\w+)?\s*(?:pace|trip|days?)?"
                 r"|\b(?:one|1) (?:thing|activity|activities)(?: a| per) day\b", lower):
        updates["activities_per_day"] = 1
    elif re.search(r"\b(?:packed|busy|action[- ]packed|lots to do|as much as (?:possible|we can)|jam[- ]packed)\b"
                   r"|\b(?:three|3) (?:things|activities)(?: a| per) day\b", lower):
        updates["activities_per_day"] = 3
    elif re.search(r"\b(?:two|2) (?:things|activities)(?: a| per) day\b|\bnormal pace\b", lower):
        updates["activities_per_day"] = 2
    return updates


def supported_cities() -> str:
    names = [info["city"] for info in CITIES.values()]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


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


def _choose_must_sees(data: dict, must_sees: list[tuple[str, str]]) -> str:
    """Add named activities in the trip's city to data['must_do']; note the ones elsewhere."""
    if not must_sees:
        return ""
    dest = resolve_city(data.get("destination") or "")
    here = [name for name, city in must_sees if not dest or city == dest["city"]]
    data["must_do"] = sorted(set(data.get("must_do") or []) | set(here))
    away = [(name, city) for name, city in must_sees if dest and city != dest["city"]]
    if away:
        return "; ".join(f"{name} is in {city}, not {dest['city']}" for name, city in away) + "."
    return ""


def _choose_area(data: dict, areas: list[tuple[str, str]], stay: str | None, others: list[str]) -> str:
    """Set data['hotel_area'] from what the traveler asked for; return a note when it can't be used."""
    if not areas and not stay:
        return ""
    dest = resolve_city(data.get("destination") or "")
    if dest:
        here = [a for a, city in areas if city == dest["city"]]
        if here:
            data["hotel_area"] = here[0]
            return ""
        if data.get("hotel_area") and hotel_areas(dest["city"]).get(data["hotel_area"]) is None:
            data["hotel_area"] = None          # left over from another city
    elif areas and stay:
        data["hotel_area"] = areas[0][0]       # no destination yet; checked again once there is one
        return ""
    known_here = ", ".join(hotel_areas(dest["city"])) if dest else ""
    # Another city's neighborhood counts only when it's where they asked to stay ("stay in Le Marais"),
    # not a stray word that happens to share a name ("our mission is fun").
    elsewhere = [(a, city) for a, city in areas
                 if (not dest or city != dest["city"]) and stay and stay.lower().startswith(a.lower())]
    if elsewhere and dest:
        a, city = elsewhere[0]
        return f"{a} is in {city}, not {dest['city']}, so I picked the best area in {dest['city']} ({known_here})."
    if stay and dest:
        city = resolve_city(stay)
        if city and city["city"] == dest["city"]:
            return ""                          # "stay in Tokyo" on a Tokyo trip
        if city and city["city"] in others:
            return ""                          # already told: one destination at a time
        if city:
            return f"I plan one city per trip, so you'll stay in {dest['city']}."
        return f"I don't have hotels listed in {stay}; the areas I know in {dest['city']} are {known_here}."
    return ""


def date_note(depart: str | None, ret: str | None) -> str:
    """Why a pair of dates the traveler gave can't be used ('' when they're fine)."""
    try:
        nights = (date.fromisoformat(ret) - date.fromisoformat(depart)).days
    except (TypeError, ValueError):
        return ""
    if nights < 0:
        return f"Your return date ({ret}) is before your departure date ({depart})."
    if nights > MAX_TRIP_NIGHTS:
        return f"That's {nights} nights; I can plan trips of up to {MAX_TRIP_NIGHTS} nights."
    return ""


def _extra_notes(extracted: dict, updated: dict, current: TripRequest, message: str) -> list[str]:
    """Things the traveler asked for that we changed or couldn't do, said out loud (edits updated in place)."""
    notes, lower, today = [], message.lower(), date.today()

    # A date that already passed this year was moved to next year: say so.
    depart = extracted.get("depart_date")
    if depart and depart != current.depart_date:
        d = date.fromisoformat(depart)
        if d.year > today.year and d.replace(year=today.year) < today:
            notes.append(f"{d:%b} {d.day} has already passed this year, so I planned it for {d.year}.")

    # Budgets of $0 or less can't be planned for.
    if (extracted.get("budget_usd") is not None and extracted["budget_usd"] <= 0) or \
            re.search(r"\bbudget\D{0,10}-\s*\$?\d", lower):
        updated["budget_usd"] = current.budget_usd
        notes.append("A budget has to be more than $0, so I planned without that one.")

    # Groups bigger than we plan for.
    if (updated.get("travelers") or 1) > MAX_TRAVELERS:
        updated["travelers"] = current.travelers
        notes.append(f"I can plan for up to {MAX_TRAVELERS} travelers, so I kept it at {current.travelers}.")

    # Interests we have nothing for.
    unsupported = sorted({label for word, label in UNSUPPORTED_INTERESTS.items() if re.search(rf"\b{word}\b", lower)})
    if unsupported:
        notes.append(f"I don't have {' or '.join(unsupported)} yet, so I picked the best-rated activities instead.")
    return notes


async def update_request(current: TripRequest, user_message: str) -> TripRequest:
    """Extract trip parameters from user message and merge non-empty fields into current."""
    return (await update_request_with_note(current, user_message))[0]


async def update_request_with_note(current: TripRequest, user_message: str) -> tuple[TripRequest, str, bool]:
    """update_request, plus a note on anything the traveler said that couldn't be used as
    given (dates that end before they start, a trip that is too long, a second destination),
    and whether the message started a new trip rather than changing the current one."""
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

    # The rule-based reading always runs: it finds the neighborhoods, must-sees and places we
    # have no data for. The LLM, when on, fills in or overrides the plain fields on top.
    extracted_data = _heuristic_extract(user_message)
    if extracted:
        for key, value in extracted.model_dump(exclude_none=True).items():
            if value not in ("", [], 0):
                extracted_data[key] = value

    notes = []
    lower = user_message.lower()
    changing = re.search(r"\b(?:actually|instead|change|switch|rather|make it|let'?s do|how about|what about)\b", lower)

    # A full route restated after a trip was already set up ("SFO to Paris Dec 1 to 5") is a
    # new trip: nothing from the last one (budget, travelers, interests, rain, cancellations)
    # carries over. Edits ("actually Paris instead", "keep it under $1,500") still change this one.
    new_trip = bool(
        re.search(r"\b(?:new|another|next|different) trip\b", lower)
        or (current.origin and current.destination and not changing
            and extracted_data.get("origin") and extracted_data.get("destination")))
    if new_trip and current != TripRequest():
        carried = []
        if current.budget_usd and "budget_usd" not in extracted_data:
            carried.append(f"the ${current.budget_usd:,.0f} budget")
        if current.travelers > 1 and "travelers" not in extracted_data:
            carried.append(f"{current.travelers} travelers")
        if carried:
            notes.append(f"New trip, so I didn't carry over {' or '.join(carried)} from the last one.")
        current = TripRequest()
        updated_data = current.model_dump()

    others = extracted_data.pop("_other_destinations", [])
    areas = extracted_data.pop("_areas", [])
    must_sees = extracted_data.pop("_must_sees", [])
    stay = extracted_data.pop("_stay_place", None)
    extra = extracted_data.pop("_extra_travelers", 0)
    if extra and "travelers" not in extracted_data:
        extracted_data["travelers"] = current.travelers + extra   # "my wife is coming too"

    # Answering "where are you departing from?" with just a city: that's the origin, not a new
    # destination, unless they say they're changing it ("actually Paris", "make it London").
    new_dest = extracted_data.get("destination")
    if (new_dest and not extracted_data.get("origin") and current.destination and not current.origin
            and new_dest != current.destination and not changing and not re.search(r"\bto\b", lower)):
        known = resolve_city(new_dest)
        extracted_data["origin"] = known["airport"] if known else new_dest
        del extracted_data["destination"]

    # Answering "When are you coming back?" with one date: that date is the return, not a new departure.
    if (current.depart_date and not current.return_date
            and extracted_data.get("depart_date") and not extracted_data.get("return_date")):
        extracted_data["return_date"] = extracted_data.pop("depart_date")

    for key, value in extracted_data.items():
        if value is not None and value != "" and value != []:
            if key in ("interests", "avoid"):
                existing = set(updated_data.get(key) or [])
                existing.update(value)
                updated_data[key] = sorted(existing)
            else:
                updated_data[key] = value

    if extracted_data.get("avoid"):   # a newly disliked category stops being an interest
        updated_data["interests"] = [i for i in updated_data["interests"] if i not in extracted_data["avoid"]]
    if extracted_data.get("interests"):   # and a newly liked one stops being avoided
        updated_data["avoid"] = [a for a in updated_data["avoid"] if a not in extracted_data["interests"]]
    notes.append(_choose_must_sees(updated_data, must_sees))
    notes.append(_choose_area(updated_data, areas, stay, others))
    notes += _extra_notes(extracted_data, updated_data, current, user_message)
    notes.append(date_note(updated_data.get("depart_date"), updated_data.get("return_date")))
    if others:
        notes.append(f"I can plan one destination at a time, so this plan is for {updated_data['destination']}; "
                     f"ask me about {' and '.join(others)} next.")
    return TripRequest(**updated_data), " ".join(n for n in notes if n), new_trip
