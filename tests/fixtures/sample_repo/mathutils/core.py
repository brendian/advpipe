"""Core numeric helpers."""

from collections.abc import Sequence


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Return a / b, or ``default`` if b is zero."""
    if b == 0:
        return default
    return a / b


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)
