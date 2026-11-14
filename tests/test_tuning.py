"""Phase 5 tests: analytic queries equal pandas, the comparison can actually fail, report views, tuning objects, plan parsing."""
import pandas as pd
import pytest

import analytics_check
import tuning
from conftest import scalar


@pytest.fixture(scope="module")
def analytics(dev):
    return analytics_check.run_all(dev)


def test_six_analytic_queries_match_pandas(analytics):
    assert [r["query"] for r in analytics] == list(analytics_check.REFS)
    assert {r["query"]: r["differences"] for r in analytics} == {q: 0 for q in analytics_check.REFS}


def test_comparison_detects_a_difference(analytics):
    """Guard against a comparison that can never fail: change one cell, expect exactly one difference."""
    r = analytics[3]                                   # q4_drawdown
    ref = r["result"].copy()
    ref["max_drawdown"] = ref["max_drawdown"].astype(float)
    ref.loc[0, "max_drawdown"] = ref.loc[0, "max_drawdown"] - 0.01
    assert analytics_check.compare(r["result"], ref) == 1
    assert analytics_check.compare(r["result"], ref.iloc[:-1]) > 0          # a missing row is caught too


def test_rpt_date_is_contiguous_without_unknown(dev):
    n, lo, hi, unknown = dev.cursor().execute(
        "SELECT COUNT(*), MIN([Date]), MAX([Date]), SUM(IIF(DateKey = -1, 1, 0)) FROM rpt.[Date]").fetchone()
    assert unknown == 0 and n == (hi - lo).days + 1 == 365


def test_every_rpt_view_selects_and_is_described(dev):
    views = [r[0] for r in dev.cursor().execute("SELECT name FROM sys.views WHERE SCHEMA_NAME(schema_id) = 'rpt'")]
    assert len(views) == 24
    for v in views:
        dev.cursor().execute(f"SELECT TOP (1) * FROM rpt.[{v}]").fetchall()
    undescribed = scalar(dev, """SELECT COUNT(*) FROM sys.views v WHERE SCHEMA_NAME(v.schema_id) = 'rpt' AND NOT EXISTS
                                 (SELECT 1 FROM sys.extended_properties p WHERE p.major_id = v.object_id AND p.minor_id = 0 AND p.name = 'MS_Description')""")
    assert undescribed == 0


def test_rpt_views_hide_lineage(dev):
    lineage = scalar(dev, """SELECT COUNT(*) FROM sys.columns c JOIN sys.views v ON v.object_id = c.object_id
                             WHERE SCHEMA_NAME(v.schema_id) = 'rpt' AND c.name IN ('SourceFile', 'SourceRowNo', 'LoadedAt')""")
    assert lineage == 0


def test_feature_value_daily_reconciles_to_the_fact(dev):
    assert scalar(dev, "SELECT SUM(Minutes) FROM rpt.FeatureValueDaily") == scalar(dev, "SELECT COUNT_BIG(*) FROM dw.FactFeatureValue")


def test_tuning_index_exists(dev):
    assert scalar(dev, "SELECT COUNT(*) FROM sys.indexes WHERE name IN ('IX_FactModelPrediction_Date', 'IX_FactDQFinding_Severity')") == 2


def test_plan_parser_reads_an_actual_plan(dev):
    rows, plan, _ = tuning.execute_with_plan(dev, "SELECT COUNT(*) FROM dw.FactDQFinding WHERE SeverityKey = 3")
    m = tuning.parse_plan(plan)
    assert rows == 1 and m["reads"] > 0 and m["act_rows"] == 1 and m["operators"]
    dev.commit()
