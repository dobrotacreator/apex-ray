from decimal import Decimal

from pricing import quote_total


def preview_total() -> Decimal:
    return quote_total(Decimal("10.00"), 3)
