"""The agent network definition.

A node is an agent. A node with children is an orchestrator: it relays the
message it receives, unchanged, to all of its children at once. A node with
tools answers the message itself.

To change the shape of the network, change the `children` lists below - the
wiring, the topics and the subscriptions are all derived from this tree.
"""

from dataclasses import dataclass, field
from typing import Callable

import tools


@dataclass
class Node:
    name: str  # also the agent type and topic type - must be unique, no spaces
    system_message: str
    tools: list[Callable] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    @property
    def is_orchestrator(self) -> bool:
        return bool(self.children)

    @property
    def branch_topic(self) -> str:
        """The topic an orchestrator fans its message out on."""
        return f"{self.name}-branch"


# --- Leaves of branch A -------------------------------------------------------
FLIGHTS = Node(
    name="FlightsWorker",
    system_message=(
        "You are a flights specialist. Use your tool to look up flights and report the options. "
        "Only answer the parts of the request that concern flights."
    ),
    tools=[tools.search_flights],
)

HOTELS = Node(
    name="HotelsWorker",
    system_message=(
        "You are a hotels specialist. Use your tool to look up accommodation and report the options. "
        "Only answer the parts of the request that concern hotels."
    ),
    tools=[tools.search_hotels],
)

# --- Leaves of branch B -------------------------------------------------------
CAR_RENTAL = Node(
    name="CarRentalWorker",
    system_message=(
        "You are a car rental specialist. Use your tool to quote rentals and report the options. "
        "Only answer the parts of the request that concern renting a car."
    ),
    tools=[tools.quote_car_rental],
)

RESTAURANTS = Node(
    name="RestaurantWorker",
    system_message=(
        "You are a restaurant specialist. Use your tool to find places to eat and report them. "
        "Only answer the parts of the request that concern food."
    ),
    tools=[tools.find_restaurants],
)

WEATHER = Node(
    name="WeatherWorker",
    system_message=(
        "You are a weather specialist. Use your tool to get the forecast and report it. "
        "Only answer the parts of the request that concern the weather."
    ),
    tools=[tools.get_forecast],
)

# --- The sub-orchestrator: a worker with a tool AND three workers of its own ---
LOCAL_OPS = Node(
    name="LocalOpsOrchestrator",
    system_message=(
        "You are the local operations lead. Use your tool to pull the city guide and report what it says. "
        "Your own workers cover cars, food and weather, so do not answer those parts yourself."
    ),
    tools=[tools.get_city_guide],
    children=[CAR_RENTAL, RESTAURANTS, WEATHER],
)

# --- The root orchestrator: three workers, no tool of its own -----------------
CONCIERGE = Node(
    name="Concierge",
    system_message=(
        "You are the travel concierge and the entry point of the system. You have no tools of your own; "
        "your workers cover flights, hotels and local operations. Acknowledge the request in two or three "
        "sentences and say which of your workers are handling it."
    ),
    children=[FLIGHTS, HOTELS, LOCAL_OPS],
)


ROOT: Node = CONCIERGE


def walk(node: Node) -> list[Node]:
    """Every node in the tree, parents before children."""
    return [node] + [n for child in node.children for n in walk(child)]
