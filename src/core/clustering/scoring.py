import math
from typing import Optional

from src.config import get_settings


SEVERITY_WEIGHTS = {
    "fatal": 5.0,
    "critical": 5.0,
    "error": 4.0,
    "err": 4.0,
    "warn": 3.0,
    "warning": 3.0,
    "info": 1.0,
    "debug": 0.5,
    "trace": 0.5,
}


def get_severity_weight(levels_distribution: dict[str, int]) -> float:
    """Compute severity weight from a levels distribution dict."""
    if not levels_distribution:
        return 1.0

    total = sum(levels_distribution.values())
    if total == 0:
        return 1.0

    weighted = 0.0
    for level, count in levels_distribution.items():
        weight = SEVERITY_WEIGHTS.get(level.lower(), 1.0)
        weighted += weight * (count / total)

    return weighted


def compute_importance_score(
    count: int,
    levels_distribution: dict[str, int],
    change_ratio: float,
    services_count: int,
    is_trigger_correlated: bool = False,
) -> float:
    """
    Compute a composite importance score for a cluster.

    importance_score =
        severity_weight
        + log(count + 1)
        + change_ratio_weight
        + spread_weight
        + trigger_correlation_weight
    """
    severity = get_severity_weight(levels_distribution)
    log_count = math.log(count + 1)
    change_weight = math.log(change_ratio + 1)
    spread_weight = math.log(services_count + 1) * 0.5
    trigger_weight = 2.0 if is_trigger_correlated else 0.0

    return severity + log_count + change_weight + spread_weight + trigger_weight
