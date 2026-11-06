"""Small numeric helpers for the measurement reports."""

import math


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "0/0"


def nearest_rank(sorted_values: list, q: float):
    """Nearest-rank percentile (q in 0..1) of an ascending list."""
    if not sorted_values:
        return None
    rank = math.ceil(round(q * len(sorted_values), 9))
    return sorted_values[min(len(sorted_values), max(rank, 1)) - 1]


def wilson_interval(n: int, d: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval, as fractions, for n successes out of d trials."""
    if d <= 0:
        raise ValueError("the Wilson interval needs at least one trial")
    p = n / d
    centre = (p + z * z / (2 * d)) / (1 + z * z / d)
    half = z * ((p * (1 - p) / d + z * z / (4 * d * d)) ** 0.5) / (1 + z * z / d)
    return centre - half, centre + half
