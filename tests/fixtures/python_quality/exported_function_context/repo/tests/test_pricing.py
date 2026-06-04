from decimal import Decimal

from pricing import quote_total


def test_quote_total_uses_quantity() -> None:
    assert quote_total(Decimal("10.00"), 3) == Decimal("30.00")
