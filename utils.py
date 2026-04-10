"""Shared utility functions."""


def parse_price(raw: str) -> float:
    """Parse a price string like '$19.99' or '19.99' into a float."""
    cleaned = raw.strip().lstrip("$")
    return float(cleaned)


def format_currency(amount: float) -> str:
    """Format a float as a dollar string."""
    return f"${amount:.2f}"
