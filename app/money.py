from decimal import Decimal, ROUND_HALF_UP


def cents(value) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def amount(value: int) -> float:
    return value / 100
