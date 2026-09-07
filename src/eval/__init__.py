"""Evaluation harness for raglogs explanation quality.

This package is the *harness*, not the data. It runs raglogs and a deliberately
trivial baseline arm over a directory of labeled cases and reports how often the
explanation is right — root-cause service, trigger, negative-case abstention,
and confidence calibration — plus raglogs' *lift* over the baseline. Corpora
arrive via separate download steps (``make eval-data``); this establishes the
contract they plug into. See ``tests/eval/README.md`` for the case format.
"""

from src.eval.case import EvalCase, RootCause, Trigger, load_cases
from src.eval.metrics import Prediction

__all__ = ["EvalCase", "RootCause", "Trigger", "load_cases", "Prediction"]
