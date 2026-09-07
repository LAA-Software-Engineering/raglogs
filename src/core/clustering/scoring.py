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


# How strongly earlier onset (within the incident window) boosts a cluster. A
# root cause tends to precede the cascade it triggers, so the cluster that
# appeared first is a better primary candidate than a louder but later one (#82).
_ONSET_WEIGHT = 3.0


def compute_importance_score(
    count: int,
    levels_distribution: dict[str, int],
    change_ratio: float,
    services_count: int,
    is_trigger_correlated: bool = False,
    onset_fraction: float = 0.5,
) -> float:
    """
    Compute a composite importance score for a cluster.

    importance_score =
        severity_weight
        + log(count + 1)
        + change_ratio_weight
        + spread_weight
        + trigger_correlation_weight
        + onset_weight

    ``onset_fraction`` is where the cluster's first log falls within the incident
    window (0 = at the start, 1 = at the end); earlier onset scores higher. The
    default 0.5 is neutral for callers that don't supply timing.
    """
    severity = get_severity_weight(levels_distribution)
    log_count = math.log(count + 1)
    change_weight = math.log(change_ratio + 1)
    spread_weight = math.log(services_count + 1) * 0.5
    trigger_weight = 2.0 if is_trigger_correlated else 0.0
    onset_weight = _ONSET_WEIGHT * (1.0 - _clamp01(onset_fraction))

    return severity + log_count + change_weight + spread_weight + trigger_weight + onset_weight


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x
