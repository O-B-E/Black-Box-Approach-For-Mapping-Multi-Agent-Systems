"""Worker definitions and the workflow order.

To add a worker: define a new `Worker(...)` below and put it in WORKFLOW.
To reorder the flow: reorder WORKFLOW. That's it - the wiring is automatic.
"""

from dataclasses import dataclass, field
from typing import Callable

import tools


@dataclass
class Worker:
    name: str  # also used as the agent type and topic type - must be unique, no spaces
    system_message: str
    tools: list[Callable] = field(default_factory=list)


RESEARCHER = Worker(
    name="Researcher",
    system_message=(
        "You are a market researcher. Given a product description, use your tools to research the market "
        "and competitors, then summarise key features, target audience and unique selling points."
    ),
    tools=[tools.web_search, tools.get_competitor_prices],
)

WRITER = Worker(
    name="Writer",
    system_message=(
        "You are a marketing copywriter. Fetch the brand guidelines, then turn the input into a short, "
        "compelling marketing copy (~150 words). Use the word counter to stay within limits. "
        "Output just the copy."
    ),
    tools=[tools.get_brand_guidelines, tools.count_words],
)

SEO_SPECIALIST = Worker(
    name="SEOSpecialist",
    system_message=(
        "You are an SEO specialist. Look up relevant keywords and weave them naturally into the given copy. "
        "Output just the improved copy."
    ),
    tools=[tools.keyword_lookup],
)

EDITOR = Worker(
    name="Editor",
    system_message=(
        "You are an editor. Spell-check the text, fix grammar, improve clarity and give it a polished format. "
        "Output just the final copy."
    ),
    tools=[tools.spell_check],
)

PUBLISHER = Worker(
    name="Publisher",
    system_message=(
        "You are a publisher. Publish the given copy to the blog and send it as a newsletter, "
        "then report what was published and where."
    ),
    tools=[tools.publish_to_blog, tools.send_newsletter],
)


# --- Spare workers ------------------------------------------------------------
# Defined but not wired into WORKFLOW. Drop any of them into the list below to
# lengthen the chain or change what it does.

LEGAL = Worker(
    name="Legal",
    system_message=(
        "You are a legal reviewer. Check the given copy against advertising regulations and scan its "
        "slogans for trademark collisions, then flag anything that has to change. Output just the copy, "
        "with your flags at the end."
    ),
    tools=[tools.check_compliance, tools.scan_trademarks],
)

LOCALIZER = Worker(
    name="Localizer",
    system_message=(
        "You are a localisation specialist. Detect the language of the given copy, then translate it "
        "into German. Output just the translated copy."
    ),
    tools=[tools.detect_language, tools.translate_text],
)

ANALYST = Worker(
    name="Analyst",
    system_message=(
        "You are a marketing analyst. Pull the metrics of the running campaign and forecast revenue for "
        "the category, then say in two sentences what the numbers mean for this copy."
    ),
    tools=[tools.get_campaign_metrics, tools.forecast_revenue],
)

DESIGNER = Worker(
    name="Designer",
    system_message=(
        "You are a visual designer. Pick a colour palette that matches the mood of the given copy and "
        "generate a product image to go with it, then report the asset links."
    ),
    tools=[tools.generate_image, tools.get_color_palette],
)

SUPPORT = Worker(
    name="Support",
    system_message=(
        "You are a customer support agent. Look up the customer the request concerns and open a ticket "
        "for anything that needs follow-up, then report what you filed."
    ),
    tools=[tools.lookup_customer, tools.create_ticket],
)


# The flow order. Each worker's output is handed to the next one.
WORKFLOW: list[Worker] = [
    RESEARCHER,
    WRITER,
    SEO_SPECIALIST,
    EDITOR,
    PUBLISHER,
]
