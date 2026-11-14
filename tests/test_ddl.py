"""Phase 2 tests: the warehouse structure on TradingBI_Dev matches the design.

    bi_env python -m pytest bi/tests -q
Every test that writes runs inside a transaction that is rolled back, so the database is left as it was.
"""
import pyodbc
import pytest

import db
import dq_rules
from conftest import scalar

DIMS = ["DimDate", "DimTime", "DimInstrument", "DimDataset", "DimCheck", "DimSeverity", "DimGateClass",
        "DimModelRun", "DimClass", "DimFeature"]
FACTS = ["FactDQFinding", "FactDQDailyStatus", "FactDQDayVerdict", "FactModelPrediction", "FactModelMetric",
         "FactFeatureImportance", "FactTrade", "FactFeatureDrift", "FactFeatureValue"]
ETL = ["RunLog", "StepLog", "RejectRow", "ReconResult", "Watermark", "SourceFileLog", "BatchFile", "Target"]
KEY = {"DimDate": "DateKey", "DimTime": "TimeKey", "DimInstrument": "InstrumentKey", "DimDataset": "DatasetKey",
       "DimCheck": "CheckKey", "DimSeverity": "SeverityKey", "DimGateClass": "GateClassKey",
       "DimModelRun": "ModelRunKey", "DimClass": "ClassKey", "DimFeature": "FeatureKey"}


def tables(conn, schema):
    return {r[0] for r in conn.cursor().execute("SELECT name FROM sys.tables WHERE SCHEMA_NAME(schema_id) = ?", schema)}


def test_all_tables_exist(dev):
    assert tables(dev, "dw") == set(DIMS) | set(FACTS)
    assert tables(dev, "etl") == set(ETL)
    assert tables(dev, "sec") == {"UserInstrument"}
    for s in ("stg", "dw", "etl", "rpt", "sec"):
        assert scalar(dev, "SELECT SCHEMA_ID(?)", s) is not None, s


@pytest.mark.parametrize("dim", DIMS)
def test_every_dimension_has_unknown_member(dev, dim):
    assert scalar(dev, f"SELECT COUNT(*) FROM dw.{dim} WHERE {KEY[dim]} = -1") == 1


def test_every_fact_key_column_has_a_foreign_key(dev):
    """Every *Key column of a fact (except its own surrogate key) must point at a dimension."""
    missing = dev.cursor().execute("""
        SELECT t.name + '.' + c.name FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
        WHERE SCHEMA_NAME(t.schema_id) = 'dw' AND t.name LIKE 'Fact%' AND c.name LIKE '%Key'
          AND NOT EXISTS (SELECT 1 FROM sys.foreign_key_columns f WHERE f.parent_object_id = c.object_id AND f.parent_column_id = c.column_id)
    """).fetchall()
    assert missing == []


def test_foreign_keys_trusted_and_enabled(dev):
    bad = dev.cursor().execute("SELECT name FROM sys.foreign_keys WHERE is_not_trusted = 1 OR is_disabled = 1").fetchall()
    assert bad == []


def test_every_column_and_table_is_described(dev):
    undocumented = dev.cursor().execute("""
        SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name + '.' + c.name
        FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
        WHERE SCHEMA_NAME(t.schema_id) IN ('dw', 'etl', 'sec')
          AND NOT EXISTS (SELECT 1 FROM sys.extended_properties p WHERE p.class = 1 AND p.major_id = c.object_id
                          AND p.minor_id = c.column_id AND p.name = 'MS_Description')
        UNION ALL
        SELECT SCHEMA_NAME(t.schema_id) + '.' + t.name FROM sys.tables t
        WHERE SCHEMA_NAME(t.schema_id) IN ('dw', 'etl', 'sec')
          AND NOT EXISTS (SELECT 1 FROM sys.extended_properties p WHERE p.class = 1 AND p.major_id = t.object_id
                          AND p.minor_id = 0 AND p.name = 'MS_Description')
    """).fetchall()
    assert undocumented == []


def test_gate_classes_match_production_gate_code(dev):
    rows = dev.cursor().execute("SELECT GateClass FROM dw.DimGateClass WHERE GateClassKey BETWEEN 1 AND 6 ORDER BY GateClassKey").fetchall()
    assert tuple(r[0] for r in rows) == dq_rules.FAIL_CLASSES
    flags = {r[0]: tuple(r[1:]) for r in dev.cursor().execute(
        "SELECT GateClass, IsUnresolved, ExcludesDay, MasksMinutes FROM dw.DimGateClass").fetchall()}
    assert flags["unresolved: day excluded"] == (True, True, False)
    assert flags["unresolved: minutes masked"] == (True, False, True)
    assert flags["excluded by decision"] == (False, True, False)


def test_severity_keys_are_the_production_ranks(dev):
    from ml_pipeline.dq.contracts import SEV_RANK
    rows = dict(dev.cursor().execute("SELECT SeverityCode, SeverityKey FROM dw.DimSeverity WHERE SeverityKey >= 0").fetchall())
    assert rows == SEV_RANK


def test_class_keys_are_the_model_label_codes(dev):
    rows = dict(dev.cursor().execute("SELECT ClassKey, ClassName FROM dw.DimClass WHERE ClassKey >= 0").fetchall())
    assert rows == {0: "Down", 1: "Flat", 2: "Up"}


def test_feature_value_is_clustered_columnstore(dev):
    assert scalar(dev, "SELECT type_desc FROM sys.indexes WHERE object_id = OBJECT_ID('dw.FactFeatureValue') AND index_id = 1") \
        == "CLUSTERED COLUMNSTORE"


def _new_run(cur):
    cur.execute("INSERT etl.RunLog (RunType) OUTPUT inserted.EtlRunId VALUES ('test')")
    return cur.fetchone()[0]


def test_orphan_fact_row_is_rejected(dev):
    cur = dev.cursor()
    try:
        run = _new_run(cur)
        with pytest.raises(pyodbc.IntegrityError, match="FOREIGN KEY"):
            cur.execute("INSERT dw.FactFeatureValue (DateKey, TimeKey, InstrumentKey, FeatureKey, FeatureValue, EtlRunId) "
                        "VALUES (20260505, 555, 99, -1, 1.0, ?)", run)  # InstrumentKey 99 does not exist
    finally:
        dev.rollback()


def test_unknown_member_lets_a_row_load(dev):
    cur = dev.cursor()
    try:
        run = _new_run(cur)
        cur.execute("INSERT dw.FactFeatureValue (DateKey, TimeKey, InstrumentKey, FeatureKey, FeatureValue, EtlRunId) "
                    "VALUES (-1, -1, -1, -1, 1.0, ?)", run)
        assert cur.rowcount == 1
    finally:
        dev.rollback()


def test_recon_status_is_computed(dev):
    cur = dev.cursor()
    try:
        run = _new_run(cur)
        cur.execute("""INSERT etl.ReconResult (EtlRunId, CheckName, TableName, Layer, ExpectedValue, ActualValue, Tolerance)
                       OUTPUT inserted.Status
                       VALUES (?, 'rows', 'x', 'target', 100, 100, 0), (?, 'rows', 'x', 'target', 100, 99, 0),
                              (?, 'sum', 'x', 'target', 10.5, 10.4, 0.2), (?, 'rows', 'x', 'target', 100, NULL, 0)""",
                    run, run, run, run)
        assert [r[0] for r in cur.fetchall()] == ["PASS", "BREAK", "PASS", "BREAK"]
    finally:
        dev.rollback()


def test_check_constraints_fire(dev):
    cur = dev.cursor()
    try:
        run = _new_run(cur)
        with pytest.raises(pyodbc.IntegrityError, match="CK_FactDQDayVerdict_Verdict"):
            cur.execute("INSERT dw.FactDQDayVerdict (DateKey, Verdict, ExcludeReasons, MaskedRanges, AcceptedFails, EtlRunId) "
                        "VALUES (-1, 'maybe', 0, 0, 0, ?)", run)
    finally:
        dev.rollback()


def test_scd2_allows_one_current_version_per_dataset(dev):
    cur = dev.cursor()
    try:
        cur.execute("""INSERT dw.DimDataset (DatasetCode, DatasetGroup, Publication, Cadence, IsIntraday, IsConsumedByModel,
                                             SessionEndTime, ValidFrom, ValidTo, IsCurrent, RowHash)
                       VALUES ('t', N'g', 'same_day', 'minute', 1, 1, '15:30', '20260417', '20260802', 0, 0x01),
                              ('t', N'g', 'same_day', 'minute', 1, 1, '15:40', '20260803', '99991231', 1, 0x02)""")
        with pytest.raises(pyodbc.IntegrityError, match="UX_DimDataset_Current"):
            cur.execute("""INSERT dw.DimDataset (DatasetCode, DatasetGroup, Publication, Cadence, IsIntraday, IsConsumedByModel,
                                                 ValidFrom, IsCurrent, RowHash)
                           VALUES ('t', N'g', 'same_day', 'minute', 1, 1, '20261101', 1, 0x03)""")
    finally:
        dev.rollback()


def test_seed_script_is_rerunnable(dev):
    before = [scalar(dev, f"SELECT COUNT(*) FROM dw.{d}") for d in DIMS]
    db.run_script(dev, db.SQL_DIR / "06_seed_static.sql")
    assert [scalar(dev, f"SELECT COUNT(*) FROM dw.{d}") for d in DIMS] == before


def test_drop_script_refuses_outside_dev():
    """90_drop_all.sql must refuse on Prod. TradingBI_Prod has no tables yet, so this is safe even if it failed."""
    conn = db.connect("prod")
    try:
        with pytest.raises(pyodbc.Error, match="refuses to run"):
            db.run_script(conn, db.SQL_DIR / "90_drop_all.sql")
    finally:
        conn.close()
