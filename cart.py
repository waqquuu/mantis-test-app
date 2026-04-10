"""Shopping cart logic."""

from utils import parse_price, format_currency


def calculate_total(items: list[dict]) -> float:
    """Calculate the total price of all items in the cart."""
    total = 0.0
    for item in items:
        price = parse_price(item["price"])
        quantity = item.get("quantity", 1)
        total += price * quantity
    return total


def generate_receipt(items: list[dict], tax_rate: float = 0.08) -> str:
    """Generate a text receipt for the cart."""
    lines = ["=== RECEIPT ==="]
    subtotal = calculate_total(items)

    for item in items:
        price = parse_price(item["price"])
        qty = item.get("quantity", 1)
        lines.append(f"  {item['name']} x{qty}  {format_currency(price * qty)}")

    tax = subtotal * tax_rate
    total = subtotal + tax

    lines.append(f"  Subtotal: {format_currency(subtotal)}")
    lines.append(f"  Tax:      {format_currency(tax)}")
    lines.append(f"  Total:    {format_currency(total)}")
    lines.append("===============")
    return "\n".join(lines)


def apply_discount(items: list[dict], code: str) -> list[dict]:
    """Apply a discount code to cart items."""
    discounts = {
        "SAVE10": 0.10,
        "HALF": 0.50,
        "WELCOME": 0.15,
    }
    rate = discounts.get(code.upper(), 0.0)
    if rate == 0:
        return items

    result = []
    for item in items:
        price = parse_price(item["price"])
        new_price = price * (1 - rate)
        result.append({**item, "price": format_currency(new_price)})
    return result
