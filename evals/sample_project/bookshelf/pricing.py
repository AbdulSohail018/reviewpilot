def apply_discount(price: float, percent: float) -> float:
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(price * (1 - percent / 100), 2)


def order_total(prices: list[float], member: bool = False) -> float:
    total = sum(prices)
    if member and total >= 50:
        total = apply_discount(total, 10)
    return round(total, 2)


def average_price(prices: list[float]) -> float:
    if not prices:
        return 0.0
    return sum(prices) / len(prices)
