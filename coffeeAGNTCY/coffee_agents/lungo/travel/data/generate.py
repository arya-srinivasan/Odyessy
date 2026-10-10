"""Regenerate the mock travel data in this folder.

    python travel/data/generate.py

Everything is deterministic (seeded), so the demo gives the same answers
every run. Edit CITIES / HOTEL_AREAS / ACTIVITIES below to add places.
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).parent
rng = random.Random(42)

# code -> (city, country, lat, lon, airport codes)
CITIES = {
    "SFO": ("San Francisco", "USA", 37.7749, -122.4194),
    "LAX": ("Los Angeles", "USA", 34.0522, -118.2437),
    "JFK": ("New York", "USA", 40.7128, -74.0060),
    "BOS": ("Boston", "USA", 42.3601, -71.0589),
    "HND": ("Tokyo", "Japan", 35.6762, 139.6503),
    "KIX": ("Kyoto", "Japan", 35.0116, 135.7681),
    "CDG": ("Paris", "France", 48.8566, 2.3522),
    "LHR": ("London", "UK", 51.5072, -0.1276),
}

AIRLINES = ["United", "ANA", "JAL", "Delta", "Air France", "British Airways", "Alaska", "JetBlue"]

# Rough one-way economy base fares in USD and flight hours between airports.
ROUTES = {
    ("SFO", "HND"): (780, 11.0), ("LAX", "HND"): (820, 11.5), ("JFK", "HND"): (1050, 14.0),
    ("SFO", "CDG"): (690, 10.5), ("JFK", "CDG"): (520, 7.5), ("LAX", "CDG"): (720, 11.0),
    ("SFO", "LHR"): (650, 10.0), ("JFK", "LHR"): (480, 7.0), ("BOS", "LHR"): (450, 6.5),
    ("SFO", "JFK"): (260, 5.5), ("LAX", "JFK"): (240, 5.3), ("SFO", "BOS"): (280, 5.8),
    ("SFO", "LAX"): (120, 1.4), ("HND", "KIX"): (110, 1.2), ("CDG", "LHR"): (130, 1.3),
    ("SFO", "KIX"): (850, 11.5),
}

HOTEL_AREAS = {
    "Tokyo": ["Shibuya", "Shinjuku", "Asakusa", "Ginza", "Ueno"],
    "Kyoto": ["Gion", "Kyoto Station", "Arashiyama", "Higashiyama"],
    "Paris": ["Le Marais", "Saint-Germain", "Montmartre", "Latin Quarter"],
    "London": ["Covent Garden", "Shoreditch", "Kensington", "South Bank"],
    "New York": ["Midtown", "SoHo", "Brooklyn", "Upper West Side"],
    "Boston": ["Back Bay", "Seaport", "Beacon Hill"],
    "San Francisco": ["Union Square", "Mission", "Fisherman's Wharf"],
    "Los Angeles": ["Santa Monica", "Downtown", "Hollywood"],
}
HOTEL_STYLES = [
    ("Hostel", 35, 3.6), ("Capsule", 45, 3.8), ("Budget Inn", 80, 3.7),
    ("Boutique", 180, 4.5), ("Business Hotel", 140, 4.1), ("Grand", 320, 4.7),
]

# city -> list of (name, category, indoor, hours, cost_usd)
ACTIVITIES = {
    "Tokyo": [
        ("Senso-ji Temple", "temple", False, 2, 0), ("Meiji Shrine", "temple", False, 1.5, 0),
        ("Tsukiji Outer Market food walk", "food", False, 2, 40), ("Ramen tasting in Shinjuku", "food", True, 1.5, 25),
        ("teamLab Planets", "museum", True, 2, 30), ("Tokyo National Museum", "museum", True, 2.5, 10),
        ("Shibuya Crossing & Sky", "sightseeing", False, 1.5, 20), ("Izakaya hopping in Golden Gai", "nightlife", True, 3, 60),
        ("Shinjuku Gyoen garden", "park", False, 2, 4), ("Akihabara arcades", "shopping", True, 2, 20),
        ("Sumo morning practice", "culture", True, 2, 50), ("Sushi-making class", "food", True, 2.5, 90),
    ],
    "Kyoto": [
        ("Fushimi Inari gates hike", "temple", False, 3, 0), ("Kinkaku-ji (Golden Pavilion)", "temple", False, 1.5, 4),
        ("Arashiyama bamboo grove", "park", False, 2, 0), ("Nishiki Market tasting", "food", True, 2, 30),
        ("Tea ceremony in Gion", "culture", True, 1.5, 45), ("Kyoto Railway Museum", "museum", True, 2, 12),
        ("Kaiseki dinner", "food", True, 2.5, 120), ("Kiyomizu-dera", "temple", False, 2, 4),
    ],
    "Paris": [
        ("Louvre highlights", "museum", True, 3, 22), ("Musee d'Orsay", "museum", True, 2.5, 16),
        ("Eiffel Tower summit", "sightseeing", False, 2, 35), ("Seine river cruise", "sightseeing", False, 1, 18),
        ("Le Marais food tour", "food", False, 3, 95), ("Montmartre walk", "sightseeing", False, 2, 0),
        ("Croissant baking class", "food", True, 3, 110), ("Luxembourg Gardens", "park", False, 1.5, 0),
        ("Sainte-Chapelle", "culture", True, 1, 13), ("Jazz club in Saint-Germain", "nightlife", True, 2.5, 40),
    ],
    "London": [
        ("British Museum", "museum", True, 3, 0), ("Tower of London", "culture", True, 2.5, 38),
        ("Borough Market food crawl", "food", False, 2, 45), ("Hyde Park walk", "park", False, 1.5, 0),
        ("West End show", "nightlife", True, 3, 90), ("Tate Modern", "museum", True, 2, 0),
        ("Thames sunset cruise", "sightseeing", False, 1.5, 25), ("Afternoon tea", "food", True, 2, 70),
    ],
    "New York": [
        ("The Met", "museum", True, 3, 30), ("Central Park bike ride", "park", False, 2, 25),
        ("Top of the Rock", "sightseeing", False, 1, 40), ("Pizza & bagel food tour", "food", False, 3, 85),
        ("Broadway show", "nightlife", True, 3, 150), ("MoMA", "museum", True, 2.5, 30),
        ("Brooklyn Bridge walk", "sightseeing", False, 1.5, 0), ("Chelsea Market tasting", "food", True, 2, 35),
    ],
    "Boston": [
        ("Freedom Trail", "culture", False, 2.5, 0), ("Museum of Fine Arts", "museum", True, 2.5, 27),
        ("Clam chowder at Quincy Market", "food", True, 1, 20), ("Harbor cruise", "sightseeing", False, 1.5, 35),
    ],
    "San Francisco": [
        ("Golden Gate Bridge walk", "sightseeing", False, 2, 0), ("Ferry Building food stalls", "food", True, 1.5, 30),
        ("Exploratorium", "museum", True, 2.5, 40), ("Alcatraz tour", "culture", False, 3, 45),
    ],
    "Los Angeles": [
        ("Getty Center", "museum", True, 3, 0), ("Santa Monica Pier", "sightseeing", False, 2, 0),
        ("Taco crawl in Boyle Heights", "food", False, 2.5, 35), ("Griffith Observatory", "sightseeing", False, 2, 0),
    ],
}

TRAINS = [
    # from, to, operator, hours, price_usd, departures
    ("Tokyo", "Kyoto", "JR Shinkansen Nozomi", 2.25, 105, ["07:00", "09:30", "12:00", "15:30", "18:00"]),
    ("Kyoto", "Tokyo", "JR Shinkansen Nozomi", 2.25, 105, ["08:00", "11:00", "14:30", "17:00", "20:00"]),
    ("Paris", "London", "Eurostar", 2.5, 120, ["07:15", "10:13", "13:31", "17:01", "19:31"]),
    ("London", "Paris", "Eurostar", 2.5, 120, ["06:50", "09:01", "12:31", "16:01", "19:01"]),
    ("New York", "Boston", "Amtrak Acela", 3.75, 95, ["06:00", "09:00", "12:00", "15:00", "18:00"]),
    ("Boston", "New York", "Amtrak Acela", 3.75, 95, ["07:00", "10:00", "13:00", "16:00", "19:00"]),
]

# Mock seasonal daily forecast pattern per city (cycled by day index).
FORECAST_PATTERN = {
    "Tokyo": ["sunny", "cloudy", "rain", "sunny", "sunny", "cloudy", "rain"],
    "Kyoto": ["sunny", "sunny", "cloudy", "rain", "sunny", "cloudy", "sunny"],
    "Paris": ["cloudy", "rain", "cloudy", "sunny", "rain", "cloudy", "sunny"],
    "London": ["rain", "cloudy", "rain", "cloudy", "sunny", "rain", "cloudy"],
    "New York": ["sunny", "sunny", "cloudy", "rain", "sunny", "sunny", "cloudy"],
    "Boston": ["cloudy", "sunny", "rain", "sunny", "cloudy", "sunny", "rain"],
    "San Francisco": ["cloudy", "sunny", "sunny", "cloudy", "sunny", "sunny", "cloudy"],
    "Los Angeles": ["sunny"] * 7,
}


def make_flights():
    flights = []
    n = 100
    for (a, b), (fare, hours) in ROUTES.items():
        for origin, dest in ((a, b), (b, a)):
            for _ in range(4):
                n += 1
                airline = rng.choice(AIRLINES)
                dep_h = rng.choice([6, 8, 10, 11, 13, 15, 17, 19, 22])
                dep_m = rng.choice([0, 15, 30, 45])
                stops = rng.choice([0, 0, 0, 1]) if hours > 3 else 0
                dur = round(hours + (2.5 if stops else 0) + rng.uniform(-0.3, 0.4), 1)
                price = round(fare * rng.uniform(0.8, 1.35) * (0.82 if stops else 1.0))
                co2 = round(dur * 90 * (1.15 if stops else 1.0))  # kg per passenger, rough
                flights.append({
                    "id": f"FL{n}",
                    "airline": airline,
                    "flight_no": f"{airline[:2].upper()}{rng.randint(10, 999)}",
                    "origin": origin,
                    "destination": dest,
                    "origin_city": CITIES[origin][0],
                    "destination_city": CITIES[dest][0],
                    "depart_time": f"{dep_h:02d}:{dep_m:02d}",
                    "duration_hours": dur,
                    "stops": stops,
                    "price_usd": price,
                    "co2_kg": co2,
                    "cabin": "economy",
                })
    return flights


def make_hotels():
    hotels = []
    n = 0
    for city, areas in HOTEL_AREAS.items():
        lat0, lon0 = next((v[2], v[3]) for v in CITIES.values() if v[0] == city)
        for area in areas:
            for style, base, rating in rng.sample(HOTEL_STYLES, 3):
                n += 1
                mult = 1.35 if city in ("London", "New York", "Paris") else 1.0
                hotels.append({
                    "id": f"HT{n:03d}",
                    "name": f"{area} {style}",
                    "city": city,
                    "area": area,
                    "style": style.lower(),
                    "price_per_night_usd": round(base * mult * rng.uniform(0.85, 1.25)),
                    "rating": round(min(5.0, rating + rng.uniform(-0.2, 0.2)), 1),
                    "eco_certified": rng.random() < 0.3,
                    "lat": round(lat0 + rng.uniform(-0.04, 0.04), 4),
                    "lon": round(lon0 + rng.uniform(-0.04, 0.04), 4),
                })
    return hotels


def make_activities():
    out = []
    n = 0
    for city, items in ACTIVITIES.items():
        for name, cat, indoor, hours, cost in items:
            n += 1
            out.append({
                "id": f"AC{n:03d}", "name": name, "city": city, "category": cat,
                "indoor": indoor, "duration_hours": hours, "cost_usd": cost,
                "rating": round(rng.uniform(4.2, 4.9), 1),
            })
    return out


def make_trains():
    out = []
    n = 0
    for frm, to, op, hours, price, deps in TRAINS:
        for d in deps:
            n += 1
            out.append({
                "id": f"TR{n:03d}", "from_city": frm, "to_city": to, "operator": op,
                "depart_time": d, "duration_hours": hours, "price_usd": price,
                "co2_kg": round(hours * 6),
            })
    return out


def make_cities():
    return {
        code: {"city": c, "country": k, "lat": lat, "lon": lon, "airport": code}
        for code, (c, k, lat, lon) in CITIES.items()
    }


def main():
    files = {
        "flights.json": make_flights(),
        "hotels.json": make_hotels(),
        "activities.json": make_activities(),
        "trains.json": make_trains(),
        "cities.json": make_cities(),
        "forecast_pattern.json": FORECAST_PATTERN,
    }
    for name, data in files.items():
        (HERE / name).write_text(json.dumps(data, indent=2))
        print(f"wrote {name} ({len(data)} entries)")


if __name__ == "__main__":
    main()