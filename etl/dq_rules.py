"""DQ rules for the BI layer.

Reuses the production DQ gate (ml_pipeline/dq/gate.py) instead of re-implementing it, so the warehouse, the
morning report and the training gate can never disagree about what an "unresolved FAIL" or an excluded day is.
Read-only: nothing here writes anywhere.

The gate's rule, per FAIL in the resolved findings of a date:
  1. dataset not consumed by the model            -> ignored by the gate
  2. a contract exception added later demotes it   -> "by design now"
  3. a row in ml_pipeline/dq/decisions.csv         -> "accepted" or "excluded by decision" (day excluded)
  4. otherwise UNRESOLVED: minute-level checks with a minute range mask those minutes; anything else excludes the day
A date with no result for collector_a_minute_core is "unchecked". known_cause does NOT resolve a FAIL: on a FAIL it
records an incident (the severity is kept).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ml_pipeline.dq import gate  # noqa: E402  (production code, imported read-only)

FAIL_CLASSES = (
    "not consumed by the model",
    "by design now",
    "accepted by decision",
    "excluded by decision",
    "unresolved: minutes masked",
    "unresolved: day excluded",
)
CORE_DATASET = gate.CORE
CONSUMED = frozenset(gate.CONSUMED)
MINUTE_LEVEL_CHECKS = frozenset(gate.MINUTE_LEVEL_CHECKS)


def load_decisions(path: Path | None = None) -> pd.DataFrame:
    return gate.load_decisions() if path is None else gate.load_decisions(path)


def resolve(dq_root: Path, dates) -> pd.DataFrame:
    """Findings of every (date, dataset) from the latest run that evaluated the pair (production rule)."""
    return gate.resolve(dates, [dq_root])


def day_verdict(day: str, findings_of_day: pd.DataFrame, decisions: pd.DataFrame) -> dict:
    """{'status': ok | exclude | unchecked, 'reasons': [...], 'masks': [...], 'accepted': [...], 'runs': [...]}"""
    return gate.day_verdict(day, findings_of_day, decisions)


def classify_fail(row: pd.Series, day: str, decisions: pd.DataFrame) -> str:
    """Which branch of the gate a single FAIL row takes (same order as gate.day_verdict)."""
    if row["dataset"] not in CONSUMED:
        return FAIL_CLASSES[0]
    if gate._current_exception(row, day):
        return FAIL_CLASSES[1]
    d = gate._decision(decisions, day, row)
    if d is not None and d["action"] == "accept":
        return FAIL_CLASSES[2]
    if d is not None and d["action"] == "exclude_day":
        return FAIL_CLASSES[3]
    lo, hi = gate._minute(row.get("minute_from")), gate._minute(row.get("minute_to"))
    if row["check"] in MINUTE_LEVEL_CHECKS and lo is not None and hi is not None:
        return FAIL_CLASSES[4]
    return FAIL_CLASSES[5]
