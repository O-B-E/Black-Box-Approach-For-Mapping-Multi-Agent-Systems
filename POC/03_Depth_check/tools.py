"""Conceptual tools for the workers.

None of these do real work - they describe what they *would* do and return a
plausible fake result so the LLM has something to reason about.
Any plain Python function with type hints and a docstring can be a tool.
"""


def _log(tool: str, msg: str) -> None:
    pass  # tool calls are recorded in the log file, not printed


# --- Research -----------------------------------------------------------------
def web_search(query: str) -> str:
    """Search the web for information about a topic."""
    _log("web_search", f"searching the web for '{query}'")
    return f"Top results for '{query}': market is growing ~12%/yr; buyers care about durability and price."


def get_competitor_prices(product_category: str) -> str:
    """Look up competitor prices for a product category."""
    _log("get_competitor_prices", f"fetching competitor prices for '{product_category}'")
    return f"Competitors in '{product_category}': BrandA $29, BrandB $35, BrandC $42."


# --- Writing ------------------------------------------------------------------
def get_brand_guidelines() -> str:
    """Retrieve the company's brand voice and style guidelines."""
    _log("get_brand_guidelines", "loading brand guidelines")
    return "Voice: friendly, confident, no jargon. Always mention sustainability. Max 150 words."


def count_words(text: str) -> int:
    """Count the number of words in a piece of text."""
    count = len(text.split())
    _log("count_words", f"counted {count} words")
    return count


# --- SEO ----------------------------------------------------------------------
def keyword_lookup(topic: str) -> str:
    """Find high-traffic SEO keywords for a topic."""
    _log("keyword_lookup", f"looking up keywords for '{topic}'")
    return f"Suggested keywords for '{topic}': 'eco friendly', 'best value', 'long lasting', 'buy online'."


# --- Editing ------------------------------------------------------------------
def spell_check(text: str) -> str:
    """Check a piece of text for spelling and grammar issues."""
    _log("spell_check", f"checking {len(text)} characters")
    return "No spelling errors found. 1 suggestion: prefer active voice in the first sentence."


# --- Publishing ---------------------------------------------------------------
def publish_to_blog(title: str, body: str) -> str:
    """Publish an article to the company blog."""
    _log("publish_to_blog", f"publishing '{title}' ({len(body)} chars)")
    return f"Published at https://blog.example.com/{title.lower().replace(' ', '-')}"


def send_newsletter(subject: str, body: str) -> str:
    """Send an email newsletter to all subscribers."""
    _log("send_newsletter", f"emailing '{subject}' to subscribers")
    return "Newsletter queued for 12,430 subscribers."


# --- Legal --------------------------------------------------------------------
def check_compliance(text: str) -> str:
    """Check a piece of text against advertising regulations."""
    _log("check_compliance", f"checking {len(text)} characters")
    return "2 claims need substantiation: '100% recycled' and 'best value'."


def scan_trademarks(phrase: str) -> str:
    """Check whether a phrase collides with a registered trademark."""
    _log("scan_trademarks", f"scanning '{phrase}'")
    return f"No exact match for '{phrase}'; 1 similar mark in class 25 (apparel)."


# --- Localisation -------------------------------------------------------------
def detect_language(text: str) -> str:
    """Detect the language a piece of text is written in."""
    _log("detect_language", f"detecting language of {len(text)} characters")
    return "Detected: English (confidence 0.98)."


def translate_text(text: str, target_language: str) -> str:
    """Translate a piece of text into another language."""
    _log("translate_text", f"translating {len(text)} characters to '{target_language}'")
    return f"Translated {len(text)} characters into {target_language}."


# --- Analytics ----------------------------------------------------------------
def get_campaign_metrics(campaign_id: str) -> str:
    """Look up the performance metrics of a marketing campaign."""
    _log("get_campaign_metrics", f"fetching metrics for '{campaign_id}'")
    return f"Campaign '{campaign_id}': 48,200 impressions, 3.1% CTR, 412 conversions."


def forecast_revenue(product_category: str, months: int) -> str:
    """Forecast revenue for a product category over a number of months."""
    _log("forecast_revenue", f"forecasting '{product_category}' for {months} months")
    return f"Forecast for '{product_category}' over {months} months: $18k-$24k, confidence medium."


# --- Design -------------------------------------------------------------------
def generate_image(prompt: str) -> str:
    """Generate a product image from a text description."""
    _log("generate_image", f"generating an image for '{prompt}'")
    return f"Image generated at https://cdn.example.com/{abs(hash(prompt)) % 10000}.png"


def get_color_palette(mood: str) -> str:
    """Get a brand-safe colour palette for a given mood."""
    _log("get_color_palette", f"fetching a palette for '{mood}'")
    return f"Palette for '{mood}': #2E7D32, #A5D6A7, #F5F5F5, #212121."


# --- Support ------------------------------------------------------------------
def lookup_customer(email: str) -> str:
    """Look up a customer record by email address."""
    _log("lookup_customer", f"looking up '{email}'")
    return f"Customer '{email}': 3 orders, last order 12 days ago, no open tickets."


def create_ticket(subject: str, body: str) -> str:
    """Open a customer support ticket."""
    _log("create_ticket", f"opening a ticket '{subject}'")
    return f"Ticket #4471 opened: '{subject}'. First response due in 8 hours."
