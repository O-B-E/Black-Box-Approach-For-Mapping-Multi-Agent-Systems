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
