"""SEVERITY_WEIGHT_* settings must actually control cluster scoring.

get_severity_weight used a hardcoded module-level SEVERITY_WEIGHTS dict and
never read Settings, even though it imported get_settings. The five
severity_weight_* settings were parsed from the environment but had no
effect on cluster importance ranking - dead configuration.
"""

from unittest.mock import patch

from src.config.settings import Settings
from src.core.clustering.scoring import get_severity_weight


def _settings_with(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_severity_weight_error_setting_changes_score():
    default = get_severity_weight({"error": 10})

    custom = _settings_with(severity_weight_error=99.0)
    with patch("src.core.clustering.scoring.get_settings", return_value=custom):
        overridden = get_severity_weight({"error": 10})

    assert default == 4.0
    assert overridden == 99.0


def test_severity_weight_fatal_setting_changes_score():
    custom = _settings_with(severity_weight_fatal=42.0)
    with patch("src.core.clustering.scoring.get_settings", return_value=custom):
        assert get_severity_weight({"fatal": 1}) == 42.0


def test_alias_levels_track_their_canonical_setting():
    custom = _settings_with(
        severity_weight_fatal=42.0,
        severity_weight_error=41.0,
        severity_weight_warn=40.0,
        severity_weight_debug=39.0,
    )
    with patch("src.core.clustering.scoring.get_settings", return_value=custom):
        assert get_severity_weight({"critical": 1}) == 42.0
        assert get_severity_weight({"err": 1}) == 41.0
        assert get_severity_weight({"warning": 1}) == 40.0
        assert get_severity_weight({"trace": 1}) == 39.0


def test_default_weights_match_prior_hardcoded_values():
    with patch(
        "src.core.clustering.scoring.get_settings", return_value=_settings_with()
    ):
        assert get_severity_weight({"fatal": 1}) == 5.0
        assert get_severity_weight({"error": 1}) == 4.0
        assert get_severity_weight({"warn": 1}) == 3.0
        assert get_severity_weight({"info": 1}) == 1.0
        assert get_severity_weight({"debug": 1}) == 0.5


def test_unknown_level_falls_back_to_one():
    with patch(
        "src.core.clustering.scoring.get_settings", return_value=_settings_with()
    ):
        assert get_severity_weight({"notalevel": 1}) == 1.0
