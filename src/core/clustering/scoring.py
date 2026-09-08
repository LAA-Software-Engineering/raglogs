import math

from src.config import get_settings


def _severity_weights() -> dict[str, float]:
    """Build the level-name -> weight table from the configured severity_weight_* settings."""
    settings = get_settings()
    return {
        "fatal": settings.severity_weight_fatal,
        "critical": settings.severity_weight_fatal,
        "error": settings.severity_weight_error,
        "err": settings.severity_weight_error,
        "warn": settings.severity_weight_warn,
        "warning": settings.severity_weight_warn,
        "info": settings.severity_weight_info,
        "debug": settings.severity_weight_debug,
        "trace": settings.severity_weight_debug,
    }


def get_severity_weight(levels_distribution: dict[str, int]) -> float:
    """Compute severity weight from a levels distribution dict."""
    if not levels_distribution:
        return 1.0

    total = sum(levels_distribution.values())
    if total == 0:
        return 1.0

    weights = _severity_weights()
    weighted = 0.0
    for level, count in levels_distribution.items():
        weight = weights.get(level.lower(), 1.0)
        weighted += weight * (count / total)

    return weighted


# How strongly a cluster's anomaly against baseline (change_ratio) weighs in
# selection. A fault's error is *new* against a fair pre-incident baseline
# (change_ratio ~= count), while steady background noise sits near 1. At weight
# 1.0 this term was too weak to outvote raw log(count), so a louder steady
# cluster beat the anomalous one; #82 raises it so a genuine spike can win even
# when it is not the highest-volume cluster.
_CHANGE_WEIGHT = 3.0


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
        + change_ratio_weight   (weighted by _CHANGE_WEIGHT)
        + spread_weight
        + trigger_correlation_weight
    """
    severity = get_severity_weight(levels_distribution)
    log_count = math.log(count + 1)
    change_weight = math.log(change_ratio + 1) * _CHANGE_WEIGHT
    spread_weight = math.log(services_count + 1) * 0.5
    trigger_weight = 2.0 if is_trigger_correlated else 0.0

    return severity + log_count + change_weight + spread_weight + trigger_weight
