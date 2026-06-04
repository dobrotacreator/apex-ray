from decimal import Decimal

RATE_LIMIT = Decimal("1000.00")


def clamp_amount(amount: Decimal) -> Decimal:
    if amount > RATE_LIMIT:
        return RATE_LIMIT
    return amount


def quote_total(price: Decimal, quantity: int) -> Decimal:
    subtotal = price * quantity
    return clamp_amount(subtotal)
