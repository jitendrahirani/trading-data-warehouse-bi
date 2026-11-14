"""Phase 3 tests: readers, incremental selection, late-arriving data, bulk insert, reconciliation.

    bi_env python -m pytest bi/tests -q                 # all tests
    bi_env python -m pytest bi/tests -q -m "not slow"   # skip the end-to-end incremental scenarios (about 1 min each)

The slow scenarios simulate arrivals by editing only the ETL's own bookkeeping (etl.SourceFileLog / etl.Watermark) on
TradingBI_Dev: removing a file's log row makes it look new, changing its hash makes it look changed. The source files
are never touched. Each scenario ends with a successful incremental run, so the log is complete again afterwards.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

import db
import extract
import load_stage
from conftest import scalar

FINDING_HEADER = ",".join(extract.FINDING_COLUMNS)


def _write_run(root: Path, run_id: str, created: str, days: dict[str, list[str]]) -> None:
    d = root / run_id
    for day, lines in days.items():
        f = d / f"date={day}" / "findings.csv"
        f.parent.mkdir(parents=True)
        f.write_text("\n".join([FINDING_HEADER] + lines) + "\n")
    (d / "manifest.json").write_text(json.dumps({"created_utc": created, "rows": sum(len(v) for v in days.values()),
                                                 "dates": sorted(days)}))


def _row(day, dataset="collector_a_minute_core", check="rows", severity="pass"):
    return f"{day},{dataset},{check},,,,,{severity},1,1,,"


# ------------------------------------------------------------------------------------------------ unit (no database)
def test_read_dq_lineage_and_late_rerun(tmp_path):
    """A post-market run re-checks yesterday (T+1). Both runs are kept in staging; 'latest run wins' picks the re-check."""
    _write_run(tmp_path, "20261105T103000Z-postmarket", "20261105T103000Z",
               {"2026-11-05": [_row("2026-11-05", severity="fail"), _row("2026-11-05", check="gap")]})
    _write_run(tmp_path, "20261106T103000Z-postmarket", "20261106T103000Z",
               {"2026-11-06": [_row("2026-11-06")], "2026-11-05": [_row("2026-11-05")]})   # T+1 re-check: now pass
    runs, findings = extract.read_dq(tmp_path)
    assert len(runs.df) == 2 and len(findings.df) == 4
    assert findings.df.groupby("SourceFile")["SourceRowNo"].apply(list).tolist() == [[1, 2], [1], [1]]
    assert all(len(f["sha256"]) == 64 for f in findings.files)
    resolved = extract.latest_run_wins(findings.df)
    day5 = resolved[resolved["date"] == "2026-11-05"]
    assert day5["run_id"].unique().tolist() == ["20261106T103000Z-postmarket"]
    assert day5["severity"].tolist() == ["pass"]           # the earlier FAIL is superseded, not deleted from staging


def test_select_batch_full_incremental_always():
    df = pd.DataFrame({"SourceFile": ["a", "a", "b", "c"], "x": [1, 2, 3, 4]})
    files = [{"path": "a", "sha256": "1"}, {"path": "b", "sha256": "2"}, {"path": "c", "sha256": "3"}]
    b = extract.Batch("stg.X", "S0", df, files)
    known = {"a": "1", "b": "OLD"}                         # a unchanged, b changed, c new
    rows, chosen = load_stage.select_batch(b, "incremental", known)
    assert rows["SourceFile"].tolist() == ["b", "c"] and [f["path"] for f in chosen] == ["b", "c"]
    rows, chosen = load_stage.select_batch(b, "full", known)
    assert len(rows) == 4
    b.refresh = "always"
    rows, _ = load_stage.select_batch(b, "incremental", {f["path"]: f["sha256"] for f in files})
    assert len(rows) == 4                                  # derived tables are always recomputed


def test_parse_drift(tmp_path):
    p = tmp_path / "drift_report_2026-09-12.md"
    p.write_text("# title\n## PSI-reliable features\n| feature | PSI | n_eff | in-band | null% train -> live | status |\n"
                 "|---|---|---|---|---|---|\n| fut_spot_basis_bps | 5.070 | 151 | 100% | 0% -> 0% | drifted |\n"
                 "## Decision log summary\n| 2026-09-12 | something | x |\n")
    rows = extract.parse_drift(p)
    assert len(rows) == 1
    r = rows[0]
    assert (r["report_date"], r["section_title"], r["feature"], r["psi"], r["status"], r["SourceRowNo"]) == \
        ("2026-09-12", "PSI-reliable features", "fut_spot_basis_bps", "5.070", "drifted", 5)


def test_panel_ddl_file_matches_model_features():
    text = (db.SQL_DIR / "08_stage_panel.sql").read_text(encoding="utf-8")
    assert text == extract.panel_ddl(extract.model_features()), "re-run: extract.py --write-panel-ddl"
    assert len(extract.model_features()) == 124


def test_oof_instrument_code_mapping_documented():
    """0 = NIFTY, 1 = BANKNIFTY (ml_pipeline/train_baseline.py). Phase 3 verified it against the panel's labels."""
    assert "0=NIFTY 1=BANKNIFTY" in (extract.REPO / "ml_pipeline" / "train_baseline.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ database
def test_insert_df_nulls_and_types(dev):
    df = pd.DataFrame({"report_date": ["2026-09-12", "2026-09-12"], "section_title": ["s", "s"], "feature": ["f1", "f2"],
                       "psi": ["1.0", None], "n_eff": [None, "7"], "in_band": [float("nan"), "9%"],
                       "null_train_live": [None, None], "status": ["x", "y"], "SourceFile": ["t", "t"], "SourceRowNo": [1, 2]})
    try:
        n = load_stage.insert_df(dev, "stg.FeatureDrift", df, run_id=0)
        assert n == 2
        nulls = dev.cursor().execute("SELECT SUM(IIF(psi IS NULL, 1, 0)), SUM(IIF(n_eff IS NULL, 1, 0)), SUM(IIF(in_band IS NULL, 1, 0)), "
                                     "SUM(IIF(null_train_live IS NULL, 1, 0)) FROM stg.FeatureDrift WHERE SourceFile = 't'").fetchone()
        assert tuple(nulls) == (1, 1, 1, 2)                # None and NaN both arrive as NULL
    finally:
        dev.rollback()


def test_last_full_run_reconciled(dev):
    run = scalar(dev, "SELECT MAX(EtlRunId) FROM etl.RunLog WHERE RunType = 'full' AND Status = 'Succeeded'")
    assert run is not None, "run: load_stage.py --mode full"
    assert scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Status = 'BREAK'", run) == 0
    assert scalar(dev, "SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = ? AND Layer = 'target'", run) >= 60


def test_failed_run_is_logged(dev):
    """Run 1 failed on the first try (empty watermark): the failure must be visible in the log, not lost."""
    assert scalar(dev, "SELECT COUNT(*) FROM etl.RunLog WHERE Status = 'Failed' AND ErrorMessage IS NOT NULL") >= 1


# ------------------------------------------------------------------------------------------------ end to end (slow)
NEWEST = "20261109T103045Z-postmarket"
PREVIOUS_CREATED = "20261106T103059Z"


def _incremental(dev):
    assert load_stage.run("incremental") == 0
    dev.commit()


@pytest.mark.slow
def test_new_run_arrives(dev):
    """The newest DQ run (264 rows) arrives after the watermark: only its files are staged, watermark moves to it."""
    cur = dev.cursor()
    cur.execute("DELETE etl.SourceFileLog WHERE SourceFile LIKE ?", f"%/{NEWEST}/%")
    cur.execute("UPDATE etl.Watermark SET LastValue = ? WHERE SourceName = 'dq_runs'", PREVIOUS_CREATED)
    dev.commit()
    _incremental(dev)
    assert scalar(dev, "SELECT COUNT(*) FROM stg.DQFinding") == 264
    assert scalar(dev, "SELECT COUNT(DISTINCT run_id) FROM stg.DQFinding") == 1
    assert scalar(dev, "SELECT COUNT(*) FROM stg.DQRun") == 1
    assert scalar(dev, "SELECT COUNT(*) FROM stg.ModelPrediction") == 0
    assert scalar(dev, "SELECT LastValue FROM etl.Watermark WHERE SourceName = 'dq_runs'") == NEWEST[:16]


@pytest.mark.slow
def test_changed_file_is_reloaded(dev):
    """A source file whose content changed (different SHA-256) is staged again; nothing else is."""
    path, rows = dev.cursor().execute(
        "SELECT TOP 1 SourceFile, RowsInFile FROM etl.SourceFileLog WHERE SourceName = 'S05' ORDER BY SourceFile").fetchone()
    dev.cursor().execute("UPDATE etl.SourceFileLog SET Sha256 = REPLICATE('0', 64) WHERE SourceFile = ?", path)
    dev.commit()
    _incremental(dev)
    assert scalar(dev, "SELECT COUNT(*) FROM stg.Trade") == rows
    assert scalar(dev, "SELECT COUNT(DISTINCT SourceFile) FROM stg.Trade") == 1
    assert scalar(dev, "SELECT COUNT(*) FROM stg.DQFinding") == 0


@pytest.mark.slow
def test_old_run_arriving_late_is_still_loaded(dev, capsys):
    """A run older than the watermark that was never loaded is loaded anyway, and reported as late-arriving."""
    old = "20261012T103010Z-postmarket"
    dev.cursor().execute("DELETE etl.SourceFileLog WHERE SourceFile LIKE ?", f"%/{old}/%")
    dev.commit()
    _incremental(dev)
    assert scalar(dev, "SELECT COUNT(*) FROM stg.DQFinding") == 285
    assert "late-arriving" in capsys.readouterr().out
    assert scalar(dev, "SELECT LastValue FROM etl.Watermark WHERE SourceName = 'dq_runs'") == NEWEST[:16]   # unchanged


# ------------------------------------------------------------------------------------------------ corrupt input
def test_corrupt_findings_file_is_refused(tmp_path):
    """A findings.csv without its expected columns stops the read and names the file (no partial load)."""
    _write_run(tmp_path, "20261105T103000Z-postmarket", "20261105T103000Z", {"2026-11-05": [_row("2026-11-05")]})
    bad = tmp_path / "20261105T103000Z-postmarket" / "date=2026-11-05" / "findings.csv"
    bad.write_text("date,dataset,severity\n2026-11-05,x,pass\n")
    with pytest.raises(ValueError, match=r"date=2026-11-05/findings.csv: missing columns"):
        extract.read_dq(tmp_path)


def test_failed_run_logs_and_does_not_advance(dev, monkeypatch):
    """Any failure: RunLog = Failed with the error, and SourceFileLog / Watermark stay where they were,
    so the next run picks up exactly the same files again."""
    before = dev.cursor().execute("SELECT COUNT(*), MAX(LastLoadRunId) FROM etl.SourceFileLog").fetchone()
    wm = scalar(dev, "SELECT LastValue FROM etl.Watermark WHERE SourceName = 'dq_runs'")

    def broken():
        raise ValueError("bi/source_snapshot/x/findings.csv: missing columns ['severity']")
    monkeypatch.setattr(extract, "read_all", broken)
    assert load_stage.run("incremental") == 1
    dev.commit()
    status, err = dev.cursor().execute("SELECT TOP 1 Status, ErrorMessage FROM etl.RunLog ORDER BY EtlRunId DESC").fetchone()
    assert status == "Failed" and "missing columns" in err
    assert tuple(dev.cursor().execute("SELECT COUNT(*), MAX(LastLoadRunId) FROM etl.SourceFileLog").fetchone()) == tuple(before)
    assert scalar(dev, "SELECT LastValue FROM etl.Watermark WHERE SourceName = 'dq_runs'") == wm
