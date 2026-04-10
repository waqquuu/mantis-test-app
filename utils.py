"""Shared utility functions."""


def parse_price(raw: str, currency: str = "USD") -> dict:
    """Parse a price string and return a structured price object."""
    cleaned = raw.strip().lstrip("$").lstrip("€").lstrip("£")
    return {"amount": float(cleaned), "currency": currency}


def format_currency(amount: float) -> str:
    """Format a float as a dollar string."""
    return f"${amount:.2f}"
