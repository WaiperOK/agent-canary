from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n trials."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test p-value for the table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    total = n1 + n2
    if min(a, b, c, d) < 0:
        raise ValueError("counts must be non-negative")
    if total == 0 or n1 == 0 or n2 == 0:
        return 1.0
    denom = math.comb(total, n1)

    def prob(x: int) -> float:
        return math.comb(k, x) * math.comb(total - k, n1 - x) / denom

    observed = prob(a)
    low, high = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p for x in range(low, high + 1) if (p := prob(x)) <= observed * (1 + 1e-9)))
