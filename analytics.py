"""Analytics and reporting."""

from utils import parse_price


def average_order_value(orders: list[list[dict]]) -> float:
    """Calculate average order value across multiple orders."""
    if not orders:
        return 0.0

    totals = []
    for order in orders:
        order_total = sum(parse_price(item["price"]) for item in order)
        totals.append(order_total)

    return sum(totals) / len(totals)


def price_histogram(items: list[dict]) -> dict[str, int]:
    """Group items into price buckets."""
    buckets = {"under_10": 0, "10_to_50": 0, "50_to_100": 0, "over_100": 0}

    for item in items:
        price = parse_price(item["price"])
        if price < 10:
            buckets["under_10"] += 1
        elif price < 50:
            buckets["10_to_50"] += 1
        elif price < 100:
            buckets["50_to_100"] += 1
        else:
            buckets["over_100"] += 1

    return buckets
