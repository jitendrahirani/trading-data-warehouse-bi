"""Phase 4 tests: dimension and fact procedures, SCD2, rejects, reconciliation, rule equality with the production gate.

Every test that changes data runs inside ONE outer transaction and rolls it back: the procedures' own BEGIN/COMMIT
nest inside it (a nested COMMIT only decrements @@TRANCOUNT), so nothing a test does is left in the warehouse.
Needs a successful full load first:  bi_env python bi/etl/load_stage.py --mode full
"""
import datetime as dt

import pytest

import dq_rules
import extract
import load_stage
from conftest import scalar


@pytest.fixture(scope="module")
def run_id(dev):
    rid = scalar(dev, "SELECT MAX(EtlRunId) FROM etl.RunLog WHERE RunType = 'full' AND Status = 'Succeeded'")
    if rid is None:
        pytest.skip("no successful full load yet")
    return rid


def _exec(conn, sql, *params):
    cur = conn.cursor()
    cur.execute(sql, *params)
    while cur.nextset():
        pass


# ------------------------------------------------------------------------------------------------ the load itself
def test_full_load_reconciled_with_every_target(dev, run_id):
    assert scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Status = 'BREAK'", run_id) == 0
    layers = {r[0] for r in dev.cursor().execute("SELECT DISTINCT Layer FROM etl.ReconResult WHERE EtlRunId = ?", run_id)}
    assert layers == {"source-stg", "stg-dw", "dw", "target"}
    n_targets = len(load_stage.target_rows())
    assert scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Layer = 'target' AND TableName LIKE 'dw.%' AND Status = 'PASS'", run_id) == n_targets   # + staging-level targets (stg.*)
    assert scalar(dev, "SELECT COUNT(*) FROM etl.StepLog WHERE EtlRunId = ? AND Status = 'Failed'", run_id) == 0


def test_headline_numbers(dev):
    """The figures quoted in the project summary: 124 features, 67,583 evaluation rows (production h10m)."""
    assert scalar(dev, "SELECT COUNT(*) FROM dw.DimFeature WHERE FeatureKey > 0") == 124
    assert scalar(dev, """SELECT COUNT(*) FROM dw.FactModelPrediction p JOIN dw.DimModelRun r ON r.ModelRunKey = p.ModelRunKey
                          WHERE r.IsHeadlineRun = 1""") == 67583


# ------------------------------------------------------------------------------------------------ dimensions
def test_dimdate_calendar_and_fiscal_year(dev):
    row = lambda d: dev.cursor().execute(
        "SELECT FiscalYear, FiscalQuarter, FiscalMonthNo, DayOfWeekNo, IsTradingDay, SessionEndTime FROM dw.DimDate WHERE [Date] = ?", d).fetchone()
    assert tuple(row("2026-04-01"))[:3] == ("FY2026-27", 1, 1)
    assert tuple(row("2027-03-31"))[:3] == ("FY2026-27", 4, 12)
    assert row("2026-11-14").DayOfWeekNo == 6 and row("2026-11-14").IsTradingDay is False      # a Saturday
    assert row("2026-07-31").SessionEndTime == dt.time(15, 30)
    assert row("2026-08-03").SessionEndTime == dt.time(15, 40)
    seed = scalar(dev, "SELECT COUNT(*) FROM dw.DimDate WHERE IsHoliday = 1")
    assert seed == scalar(dev, "SELECT COUNT(*) FROM dw.DimDate d WHERE d.IsHoliday = 1 AND d.IsTradingDay = 0")
    gaps = scalar(dev, """SELECT COUNT(*) FROM (SELECT TradingDayIndex - LAG(TradingDayIndex) OVER (ORDER BY [Date]) AS step
                          FROM dw.DimDate WHERE IsTradingDay = 1) x WHERE step <> 1""")
    assert gaps == 0


def test_minute_level_checks_equal_the_gate(dev):
    rows = {r[0] for r in dev.cursor().execute("SELECT CheckCode FROM dw.DimCheck WHERE IsMinuteLevel = 1")}
    assert rows == set(dq_rules.MINUTE_LEVEL_CHECKS)


def test_scd2_attribute_change_creates_a_new_version(dev, run_id):
    """Step 4.1 'done when': changing one attribute closes the current version and opens a new one."""
    code = "collector_a_minute_core"
    try:
        before = scalar(dev, "SELECT COUNT(*) FROM dw.DimDataset WHERE DatasetCode = ?", code)
        old_key = scalar(dev, "SELECT DatasetKey FROM dw.DimDataset WHERE DatasetCode = ? AND IsCurrent = 1", code)
        dev.cursor().execute("UPDATE stg.DatasetContract SET dataset_group = N'Index futures (regrouped)' WHERE dataset = ?", code)
        _exec(dev, "EXEC dw.usp_Load_DimDataset @EtlRunId = ?, @AsOfDate = '20261120'", run_id)
        assert scalar(dev, "SELECT COUNT(*) FROM dw.DimDataset WHERE DatasetCode = ?", code) == before + 1
        old = dev.cursor().execute("SELECT IsCurrent, ValidTo FROM dw.DimDataset WHERE DatasetKey = ?", old_key).fetchone()
        assert (old.IsCurrent, old.ValidTo) == (False, dt.date(2026, 11, 19))
        new = dev.cursor().execute("SELECT DatasetGroup, ValidFrom, ValidTo, SessionEndTime FROM dw.DimDataset "
                                   "WHERE DatasetCode = ? AND IsCurrent = 1", code).fetchone()
        assert (new.DatasetGroup, new.ValidFrom, new.ValidTo, new.SessionEndTime) == \
            ("Index futures (regrouped)", dt.date(2026, 11, 20), dt.date(9999, 12, 31), dt.time(15, 40))
        # a finding dated before the change still points at the version valid on ITS date (point-in-time)
        assert scalar(dev, """SELECT COUNT(*) FROM dw.FactDQFinding f JOIN dw.DimDataset d ON d.DatasetKey = f.DatasetKey
                              WHERE d.DatasetCode = ? AND d.IsCurrent = 1""", code) == 0
    finally:
        dev.rollback()


def test_scd2_unchanged_rerun_and_same_day_correction(dev, run_id):
    code = "collector_a_minute_core"
    try:
        before = scalar(dev, "SELECT COUNT(*) FROM dw.DimDataset")
        _exec(dev, "EXEC dw.usp_Load_DimDataset @EtlRunId = ?, @AsOfDate = '20261120'", run_id)
        assert scalar(dev, "SELECT COUNT(*) FROM dw.DimDataset") == before                     # nothing changed -> no version
        start = scalar(dev, "SELECT ValidFrom FROM dw.DimDataset WHERE DatasetCode = ? AND IsCurrent = 1", code)
        dev.cursor().execute("UPDATE stg.DatasetContract SET dataset_group = N'typo fixed' WHERE dataset = ?", code)
        _exec(dev, "EXEC dw.usp_Load_DimDataset @EtlRunId = ?, @AsOfDate = ?", run_id, start)   # change on the version's first day
        assert scalar(dev, "SELECT COUNT(*) FROM dw.DimDataset") == before                     # overwritten, not versioned
        assert scalar(dev, "SELECT DatasetGroup FROM dw.DimDataset WHERE DatasetCode = ? AND IsCurrent = 1", code) == "typo fixed"
    finally:
        dev.rollback()


# ------------------------------------------------------------------------------------------------ facts
def test_fact_reload_is_idempotent(dev, run_id):
    try:
        if not scalar(dev, "SELECT COUNT(*) FROM stg.Trade"):
            pytest.skip("stg.Trade is empty (last run was incremental)")
        before = dev.cursor().execute("SELECT COUNT(*), SUM(OptNetRs) FROM dw.FactTrade").fetchone()
        _exec(dev, "EXEC dw.usp_Load_FactTrade @EtlRunId = ?", run_id)
        _exec(dev, "EXEC dw.usp_Load_FactTrade @EtlRunId = ?", run_id)
        assert tuple(dev.cursor().execute("SELECT COUNT(*), SUM(OptNetRs) FROM dw.FactTrade").fetchone()) == tuple(before)
    finally:
        dev.rollback()


def test_bad_row_is_rejected_not_loaded(dev, run_id):
    try:
        if not scalar(dev, "SELECT COUNT(*) FROM stg.Trade"):
            pytest.skip("stg.Trade is empty (last run was incremental)")
        f = dev.cursor().execute("SELECT TOP 1 SourceFile FROM stg.Trade ORDER BY SourceFile").fetchone()[0]
        dev.cursor().execute("UPDATE stg.Trade SET entry_price = 'n/a' WHERE SourceFile = ? AND SourceRowNo = 1", f)
        rejects = scalar(dev, "SELECT COUNT(*) FROM etl.RejectRow")
        staged = scalar(dev, "SELECT COUNT(*) FROM stg.Trade WHERE SourceFile = ?", f)
        _exec(dev, "EXEC dw.usp_Load_FactTrade @EtlRunId = ?", run_id)
        assert scalar(dev, "SELECT COUNT(*) FROM etl.RejectRow") == rejects + 1
        r = dev.cursor().execute("SELECT TOP 1 Reason, RowJson FROM etl.RejectRow ORDER BY RejectId DESC").fetchone()
        assert "did not convert" in r.Reason and '"entry_price":"n\\/a"' in r.RowJson
        assert scalar(dev, "SELECT COUNT(*) FROM dw.FactTrade WHERE SourceFile = ?", f) == staged - 1
    finally:
        dev.rollback()


def test_day_verdicts_spot_check_against_the_python_gate(dev):
    """Step 4.3 'done when': dates checked against the production gate computed afresh in Python.
    One date per kind: unchecked, excluded by a written decision, excluded by an unresolved FAIL, minutes masked, ok."""
    from profile_sources import SNAP
    decisions = dq_rules.load_decisions()
    days = {"2026-04-17": "unchecked", "2026-05-29": "exclude", "2026-06-02": "exclude"}
    g_all = dq_rules.resolve(SNAP / "dq", sorted({r[0] for r in dev.cursor().execute(
        "SELECT CONVERT(char(10), d.[Date], 23) FROM dw.FactDQDayVerdict v JOIN dw.DimDate d ON d.DateKey = v.DateKey")}))
    masked = scalar(dev, "SELECT TOP 1 CONVERT(char(10), d.[Date], 23) FROM dw.FactDQDayVerdict v JOIN dw.DimDate d ON d.DateKey = v.DateKey "
                         "WHERE v.MaskedRanges > 0 AND v.Verdict = 'ok' ORDER BY d.[Date]")
    ok = scalar(dev, "SELECT TOP 1 CONVERT(char(10), d.[Date], 23) FROM dw.FactDQDayVerdict v JOIN dw.DimDate d ON d.DateKey = v.DateKey "
                     "WHERE v.MaskedRanges = 0 AND v.Verdict = 'ok' ORDER BY d.[Date] DESC")
    days.update({masked: "ok", ok: "ok"})
    assert len(days) == 5
    for day, expected in days.items():
        py = dq_rules.day_verdict(day, g_all[g_all["date"] == day], decisions)
        sql = dev.cursor().execute("SELECT v.Verdict, v.MaskedRanges FROM dw.FactDQDayVerdict v JOIN dw.DimDate d ON d.DateKey = v.DateKey "
                                   "WHERE d.[Date] = ?", day).fetchone()
        assert (sql.Verdict, sql.MaskedRanges) == (py["status"], len(py["masks"])) == (expected, sql.MaskedRanges), day


# ------------------------------------------------------------------------------------------------ reconciliation
def test_injected_break_is_detected(dev, run_id):
    """Step 4.4 'done when': an injected break shows BREAK (here: one trade deleted from the warehouse)."""
    try:
        dev.cursor().execute("DELETE TOP (1) dw.FactTrade")
        _exec(dev, "EXEC etl.usp_Reconcile @EtlRunId = ?, @Mode = 'full'", run_id)
        breaks = {r[0] for r in dev.cursor().execute(
            "SELECT CheckName FROM etl.ReconResult WHERE EtlRunId = ? AND Status = 'BREAK' AND TableName = 'dw.FactTrade'", run_id)}
        assert "rows = rows in all trade files" in breaks
        assert "FactTrade rows" in breaks
    finally:
        dev.rollback()


def test_unknown_key_is_a_break(dev, run_id):
    try:
        dev.cursor().execute("UPDATE TOP (1) dw.FactFeatureDrift SET FeatureKey = -1")
        _exec(dev, "EXEC etl.usp_Reconcile @EtlRunId = ?, @Mode = 'incremental'", run_id)
        assert scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Status = 'BREAK' "
                           "AND TableName = 'dw.FactFeatureDrift' AND CheckName LIKE 'rows with an Unknown%'", run_id) == 1
    finally:
        dev.rollback()


def test_every_target_name_has_an_actual(dev, run_id):
    """A target the SQL does not compute would silently never be checked; usp_Reconcile turns it into a BREAK.
    This test catches a typo in a name before a run does."""
    names = [r[0] for r in load_stage.target_rows()]
    assert len(names) == len(set(names))
    missing = scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Layer = 'target' AND ActualValue IS NULL", run_id)
    assert missing == 0
