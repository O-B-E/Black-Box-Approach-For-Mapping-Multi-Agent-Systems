"""Conceptual tools for the branched network - one unique tool per worker.

None of these do real work - they return a plausible fake result so the LLM has
something to reason about. Any plain Python function with type hints and a
docstring can be a tool.
"""


# --- Branch A: travel desk ----------------------------------------------------
def search_flights(origin: str, destination: str, date: str) -> str:
    """Search for available flights between two cities on a given date."""
    return (
        f"Flights {origin}->{destination} on {date}: LY315 08:40 $410, "
        "AF2027 13:15 $385, BA164 19:50 $452."
    )


def search_hotels(city: str, nights: int) -> str:
    """Search for available hotels in a city for a number of nights."""
    return (
        f"Hotels in {city} for {nights} nights: Hotel Meridian $180/night (4.5), "
        "Casa Verde $125/night (4.2), The Old Mill $240/night (4.8)."
    )


# --- Branch B: local operations (the sub-orchestrator's own tool) -------------
def get_city_guide(city: str) -> str:
    """Retrieve the in-house city guide with districts and getting-around notes."""
    return (
        f"City guide for {city}: stay in the Old Town or Riverside; metro runs 05:30-00:30; "
        "museums closed on Mondays; tipping 10%."
    )


def quote_car_rental(city: str, days: int) -> str:
    """Get a car rental quote for a city and a number of days."""
    return (
        f"Car rental in {city} for {days} days: Economy $34/day, SUV $61/day, "
        "Electric $48/day. Full insurance +$12/day."
    )


def find_restaurants(city: str, cuisine: str) -> str:
    """Find restaurants in a city matching a cuisine."""
    return (
        f"{cuisine} restaurants in {city}: Olive & Ash $$ (4.6), Nonna Rosa $ (4.3), "
        "Atelier 12 $$$ (4.9, book 2 weeks ahead)."
    )


def get_forecast(city: str, days: int) -> str:
    """Get the weather forecast for a city over the next number of days."""
    return (
        f"Forecast for {city}, next {days} days: 19-26C, mostly sunny, "
        "rain likely on day 3 (70%)."
    )
