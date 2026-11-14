"""Phase 3 (steps 3.2-3.4): load the staging tables, log every run and step, reconcile.

    bi_env python bi/etl/load_stage.py --mode full          # every file
    bi_env python bi/etl/load_stage.py --mode incremental   # only new or changed files (the nightly mode)

How a run works
  1. etl.RunLog gets a row (Running) with the git commit of the ETL code.
  2. For each staging table (etl.StepLog row per table):
       - the reader returns all rows + the files it read (with SHA-256);
       - incremental mode keeps only rows from files that are new or whose hash changed since the last successful run
         (etl.SourceFileLog). DQ runs arrive as new run folders, so a post-market run that re-checks yesterday (T+1)
         is simply a new file; the warehouse's 'latest run wins' rule then makes it replace yesterday's result.
         Derived tables (gate classes, day verdicts, contracts) are recomputed in full every run;
       - TRUNCATE + bulk insert (pyodbc fast_executemany) in one transaction: a failed step leaves the table empty,
         never half-loaded;
       - reconciliation source -> stg (rows read = rows in the table) into etl.ReconResult.
  3. Full mode also checks the staged data against the pre-registered targets (layer 'target').
  4. Only when every step and every check has passed: etl.SourceFileLog and etl.Watermark move forward and the run is
     marked Succeeded. If anything fails, they stay where they were, so the next incremental run picks the same files
     up again: nothing is skipped and nothing is loaded twice.
The log uses its own autocommit connection, so a failure's log rows survive the rollback of the data.
Touches only TradingBI_Dev.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

import db
import dq_rules
import extract

BI = Path(__file__).resolve().parents[1]
TARGETS = BI / "docs" / "preregistered_targets.json"


# ------------------------------------------------------------------------------------------------ logging
def git_commit() -> str | None:
    try:
        repo = BI.parent
        h = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "bi"], cwd=repo, capture_output=True, text=True).stdout.strip()
        return h + ("-dirty" if dirty else "")
    except Exception:
        return None


class EtlLog:
    """Writes etl.RunLog / StepLog / ReconResult on its own autocommit connection."""

    def __init__(self, which: str = "dev"):
        self.conn = db.connect(which, autocommit=True)
        self.run_id: int | None = None
        self.breaks = 0

    def start_run(self, run_type: str) -> int:
        cur = self.conn.cursor()
        cur.execute("INSERT etl.RunLog (RunType, GitCommit) OUTPUT inserted.EtlRunId VALUES (?, ?)", run_type, git_commit())
        self.run_id = int(cur.fetchone()[0])
        return self.run_id

    def end_run(self, status: str, error: str | None = None) -> None:
        self.conn.cursor().execute("UPDATE etl.RunLog SET EndedAt = SYSDATETIME(), Status = ?, ErrorMessage = ? WHERE EtlRunId = ?",
                                   status, (error or "")[:4000] or None, self.run_id)

    @contextmanager
    def step(self, name: str, table: str | None):
        cur = self.conn.cursor()
        cur.execute("INSERT etl.StepLog (EtlRunId, StepName, TargetTable) OUTPUT inserted.StepLogId VALUES (?, ?, ?)",
                    self.run_id, name, table)
        step_id = int(cur.fetchone()[0])
        counts = {"read": None, "inserted": None, "deleted": None, "status": "Succeeded"}
        try:
            yield counts
        except Exception as e:
            cur.execute("UPDATE etl.StepLog SET EndedAt = SYSDATETIME(), Status = 'Failed', ErrorMessage = ? WHERE StepLogId = ?",
                        f"{type(e).__name__}: {e}"[:4000], step_id)
            raise
        cur.execute("UPDATE etl.StepLog SET EndedAt = SYSDATETIME(), Status = ?, RowsRead = ?, RowsInserted = ?, RowsDeleted = ? "
                    "WHERE StepLogId = ?", counts["status"], counts["read"], counts["inserted"], counts["deleted"], step_id)

    def recon(self, check: str, table: str, layer: str, expected, actual, tolerance: float = 0, note: str | None = None) -> bool:
        cur = self.conn.cursor()
        cur.execute("INSERT etl.ReconResult (EtlRunId, CheckName, TableName, Layer, ExpectedValue, ActualValue, Tolerance, Note) "
                    "OUTPUT inserted.Status VALUES (?, ?, ?, ?, ?, ?, ?, ?)", self.run_id, check[:100], table, layer,
                    None if expected is None else float(expected), None if actual is None else float(actual), tolerance, note)
        ok = cur.fetchone()[0] == "PASS"
        if not ok:
            self.breaks += 1
            print(f"   BREAK  {table}: {check}: expected {expected}, actual {actual}")
        return ok


# ------------------------------------------------------------------------------------------------ data
def insert_df(conn, table: str, df: pd.DataFrame, run_id: int, chunk: int = 5000) -> int:
    """Bulk insert with fast_executemany (parameter arrays: one round trip per chunk instead of per row).
    NaN/NaT become NULL; numpy scalars become Python values."""
    if df.empty:
        return 0
    df = df.assign(EtlRunId=run_id)
    cols = list(df.columns)
    sql = f"INSERT INTO {table} ({', '.join(f'[{c}]' for c in cols)}) VALUES ({', '.join('?' * len(cols))})"
    cur = conn.cursor()
    cur.fast_executemany = True
    n = 0
    for start in range(0, len(df), chunk):
        part = df.iloc[start:start + chunk].astype(object)
        part = part.where(part.notna(), None)
        cur.executemany(sql, part.values.tolist())
        n += len(part)
    return n


def known_files(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.cursor().execute("SELECT SourceFile, Sha256 FROM etl.SourceFileLog").fetchall()}


def select_batch(batch: extract.Batch, mode: str, known: dict[str, str]) -> tuple[pd.DataFrame, list[dict]]:
    """Rows and files to load in this run. Full: everything. Incremental: rows of new or changed files only;
    'always' batches are recomputed in full."""
    if mode == "full" or batch.refresh == "always":
        return batch.df, batch.files
    changed = [f for f in batch.files if known.get(f["path"]) != f["sha256"]]
    paths = {f["path"] for f in changed}
    return batch.df[batch.df["SourceFile"].isin(paths)], changed


def scalar(conn, sql: str):
    row = conn.cursor().execute(sql).fetchone()
    return None if row is None else row[0]       # no row (e.g. no watermark before the first run) -> None


def target_checks(log: EtlLog, conn) -> None:
    """Full mode: the staged data against the numbers pre-registered in Phase 1 (committed before any load)."""
    t = json.loads(TARGETS.read_text())
    f, s, p, tr = t["FactDQFinding"], t["FactDQDailyStatus"], t["FactModelPrediction"], t["FactTrade"]
    q = lambda sql: scalar(conn, sql)
    log.recon("rows", "stg.DQFinding", "target", f["rows"], q("SELECT COUNT(*) FROM stg.DQFinding"))
    log.recon("DQ runs", "stg.DQFinding", "target", f["runs"], q("SELECT COUNT(DISTINCT run_id) FROM stg.DQFinding"))
    log.recon("datasets", "stg.DQFinding", "target", f["datasets"], q("SELECT COUNT(DISTINCT dataset) FROM stg.DQFinding"))
    log.recon("checks", "stg.DQFinding", "target", f["checks"], q("SELECT COUNT(DISTINCT [check]) FROM stg.DQFinding"))
    for sev, n in f["rows_by_severity"].items():
        log.recon(f"rows severity={sev}", "stg.DQFinding", "target", n, q(f"SELECT COUNT(*) FROM stg.DQFinding WHERE severity = '{sev}'"))
    for run, n in f["rows_by_run"].items():
        log.recon(f"rows run={run}", "stg.DQFinding", "target", n, q(f"SELECT COUNT(*) FROM stg.DQFinding WHERE run_id = '{run}'"))
    log.recon("runs", "stg.DQRun", "target", f["runs"], q("SELECT COUNT(*) FROM stg.DQRun"))
    log.recon("rows (FAILs in resolved runs)", "stg.DQFailClass", "target", sum(s["fail_rows_by_gate_class"].values()),
              q("SELECT COUNT(*) FROM stg.DQFailClass"))
    for cls, n in s["fail_rows_by_gate_class"].items():
        log.recon(f"class={cls}", "stg.DQFailClass", "target", n, q(f"SELECT COUNT(*) FROM stg.DQFailClass WHERE gate_class = '{cls}'"))
    log.recon("dates", "stg.DQDayVerdict", "target", s["dates"], q("SELECT COUNT(*) FROM stg.DQDayVerdict"))
    for v, n in s["day_verdicts"].items():
        log.recon(f"verdict={v}", "stg.DQDayVerdict", "target", n, q(f"SELECT COUNT(*) FROM stg.DQDayVerdict WHERE status = '{v}'"))
    excluded = ",".join(f"'{d}'" for d in s["excluded_dates"])
    log.recon("excluded dates = pre-registered list", "stg.DQDayVerdict", "target", len(s["excluded_dates"]),
              q(f"SELECT COUNT(*) FROM stg.DQDayVerdict WHERE status = 'exclude' AND [date] IN ({excluded})"))
    log.recon("datasets in findings without a contract", "stg.DatasetContract", "target", 0,
              q("SELECT COUNT(DISTINCT f.dataset) FROM stg.DQFinding f WHERE NOT EXISTS "
                "(SELECT 1 FROM stg.DatasetContract c WHERE c.dataset = f.dataset)"))
    log.recon("rows", "stg.ModelPrediction", "target", p["rows_total"], q("SELECT COUNT(*) FROM stg.ModelPrediction"))
    for h, n in p["rows_by_horizon"].items():
        log.recon(f"rows h{h}m", "stg.ModelPrediction", "target", n, q(f"SELECT COUNT(*) FROM stg.ModelPrediction WHERE horizon_min = {h}"))
    log.recon("rows", "stg.FeatureImportance", "target", t["FactFeatureImportance"]["rows"], q("SELECT COUNT(*) FROM stg.FeatureImportance"))
    log.recon("rows", "stg.Trade", "target", tr["rows"], q("SELECT COUNT(*) FROM stg.Trade"))
    log.recon("files", "stg.Trade", "target", tr["files"], q("SELECT COUNT(DISTINCT SourceFile) FROM stg.Trade"))
    total_net = round(sum(v["opt_net_rs"] for v in tr["by_scenario"].values()), 2)
    log.recon("sum opt_net_rs (Rs)", "stg.Trade", "target", total_net,
              q("SELECT SUM(TRY_CONVERT(decimal(12, 2), opt_net_rs)) FROM stg.Trade"), tolerance=0.005)
    total_cost = round(sum(v["costs_rs"] for v in tr["by_scenario"].values()), 2)
    log.recon("sum entry_cost + exit_cost (Rs)", "stg.Trade", "target", total_cost,
              q("SELECT SUM(TRY_CONVERT(decimal(12, 2), entry_cost) + TRY_CONVERT(decimal(12, 2), exit_cost)) FROM stg.Trade"),
              tolerance=0.005)
    log.recon("rows", "stg.FeatureDrift", "target", t["FactFeatureDrift"]["rows"], q("SELECT COUNT(*) FROM stg.FeatureDrift"))
    v = t["FactFeatureValue"]
    feats = extract.model_features()
    log.recon("rows", "stg.PanelWide", "target", v["panel_rows"], q("SELECT COUNT(*) FROM stg.PanelWide"))
    log.recon("non-null feature values", "stg.PanelWide", "target", v["non_null_rows_to_load"],
              q("SELECT CAST(" + " + ".join(f"COUNT_BIG([{c}])" for c in feats) + " AS bigint) FROM stg.PanelWide"))
    in_list = ",".join(f"'{c}'" for c in feats)
    log.recon("model features described", "stg.Feature", "target", t["DimFeature"]["rows"],
              q(f"SELECT COUNT(DISTINCT feature_name) FROM stg.Feature WHERE feature_name IN ({in_list})"))


def write_batch_files(conn, run_id: int, files: dict[str, tuple[str, dict]]) -> None:
    """Record the files staged by this run as PENDING (etl.BatchFile). etl.usp_CommitBatch moves them into
    etl.SourceFileLog only after the warehouse load and its reconciliation passed."""
    cur = conn.cursor()
    cur.fast_executemany = True
    rows = [(run_id, path, src, f["sha256"], f["bytes"], f["rows"]) for path, (src, f) in files.items()]
    if rows:
        cur.executemany("INSERT etl.BatchFile (EtlRunId, SourceFile, SourceName, Sha256, Bytes, RowsInFile) VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


def target_rows() -> list[tuple[str, str, float, float]]:
    """The pre-registered targets as (CheckName, TableName, Expected, Tolerance). The names are the contract with
    etl.usp_Reconcile, which computes an actual for each name; a target without an actual is a BREAK."""
    t = json.loads(TARGETS.read_text())
    f, s, p, tr = t["FactDQFinding"], t["FactDQDailyStatus"], t["FactModelPrediction"], t["FactTrade"]
    out = [("FactDQFinding rows", "dw.FactDQFinding", f["rows"], 0), ("FactDQFinding runs", "dw.FactDQFinding", f["runs"], 0),
           ("FactDQFinding datasets", "dw.FactDQFinding", f["datasets"], 0), ("FactDQFinding checks", "dw.FactDQFinding", f["checks"], 0),
           ("FactDQFinding resolved rows", "dw.FactDQFinding", s["resolved_findings_rows"], 0)]
    out += [(f"FactDQFinding rows severity={k}", "dw.FactDQFinding", v, 0) for k, v in f["rows_by_severity"].items()]
    out += [(f"FactDQFinding resolved severity={k}", "dw.FactDQFinding", v, 0) for k, v in s["resolved_rows_by_severity"].items()]
    out += [(f"FactDQFinding class={k}", "dw.FactDQFinding", s["fail_rows_by_gate_class"].get(k, 0), 0) for k in dq_rules.FAIL_CLASSES]
    out += [("FactDQDailyStatus pairs", "dw.FactDQDailyStatus", s["date_dataset_pairs"], 0),
            ("FactDQDailyStatus dates", "dw.FactDQDailyStatus", s["dates"], 0),
            ("FactDQDailyStatus checks evaluated", "dw.FactDQDailyStatus", s["resolved_findings_rows"], 0)]
    out += [(f"FactDQDayVerdict verdict={k}", "dw.FactDQDayVerdict", v, 0) for k, v in s["day_verdicts"].items()]
    out += [("FactModelPrediction rows", "dw.FactModelPrediction", p["rows_total"], 0)]
    out += [(f"FactModelPrediction rows h{h}", "dw.FactModelPrediction", v, 0) for h, v in p["rows_by_horizon"].items()]
    out += [(f"FactModelPrediction accuracy h{h}", "dw.FactModelPrediction", v, p["tolerance"]) for h, v in p["accuracy_by_horizon"].items()]
    for h, cells in t["FactConfusion (DAX check)"].items():
        out += [(f"FactModelPrediction confusion h{h} actual={extract.CLASSES[i // 3]} pred={extract.CLASSES[i % 3]}",
                 "dw.FactModelPrediction", v, 0) for i, v in enumerate(cells)]
    out += [("FactFeatureImportance rows", "dw.FactFeatureImportance", t["FactFeatureImportance"]["rows"], 0),
            ("FactTrade rows", "dw.FactTrade", tr["rows"], 0)]
    for scen, v in tr["by_scenario"].items():
        out += [(f"FactTrade [{scen}] trades", "dw.FactTrade", v["trades"], 0),
                (f"FactTrade [{scen}] opt_net_rs", "dw.FactTrade", v["opt_net_rs"], 0.005),
                (f"FactTrade [{scen}] costs_rs", "dw.FactTrade", v["costs_rs"], 0.005)]
    out += [("FactFeatureDrift rows", "dw.FactFeatureDrift", t["FactFeatureDrift"]["rows"], 0),
            ("FactFeatureValue rows", "dw.FactFeatureValue", t["FactFeatureValue"]["non_null_rows_to_load"], 0),
            ("DimFeature rows", "dw.DimFeature", t["DimFeature"]["rows"], 0),
            ("DimModelRun rows", "dw.DimModelRun", len(tr["by_scenario"]), 0),
            ("DimDataset current datasets", "dw.DimDataset", f["datasets"], 0),
            ("DimCheck rows", "dw.DimCheck", f["checks"], 0)]
    return out


def load_targets(conn) -> int:
    rows = target_rows()
    cur = conn.cursor()
    cur.execute("DELETE etl.Target")
    cur.fast_executemany = True
    cur.executemany("INSERT etl.Target (CheckName, TableName, ExpectedValue, Tolerance) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def run_warehouse(run_id: int) -> None:
    """EXEC etl.usp_Run_All: dimensions, facts, reconcile, commit. It marks the run Succeeded or Failed itself.
    AUTOCOMMIT on purpose: with pyodbc's default (implicit transaction) every procedure's COMMIT would only decrement
    @@TRANCOUNT, so nothing - not even the step log - would really commit until the very end, and one failure would
    roll back every step. With autocommit each procedure owns its transaction, exactly as under SQL Agent."""
    conn = db.connect("dev", autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute("EXEC etl.usp_Run_All @EtlRunId = ?", run_id)
        while cur.nextset():      # drain informational messages so a late error is raised here
            pass
    finally:
        conn.close()


def run(mode: str, stage_only: bool = False) -> int:
    log = EtlLog("dev")
    run_id = log.start_run(mode)
    print(f"ETL run {run_id} ({mode}{', stage only' if stage_only else ''}) started")
    conn = db.connect("dev")
    t0 = time.time()
    try:
        with log.step("extract:read_all", None) as s:
            batches = extract.read_all()
            s["read"] = sum(len(b.df) for b in batches)
        with log.step("targets:load", "etl.Target") as s:
            s["inserted"] = load_targets(conn)
        known = known_files(conn)
        pending: dict[str, tuple[str, dict]] = {}
        watermark = scalar(conn, "SELECT LastValue FROM etl.Watermark WHERE SourceName = 'dq_runs'")
        for b in batches:
            with log.step(f"stage:{b.table}", b.table) as s:
                df, files = select_batch(b, mode, known)
                cur = conn.cursor()
                deleted = scalar(conn, f"SELECT COUNT_BIG(*) FROM {b.table}")
                cur.execute(f"TRUNCATE TABLE {b.table}")
                n = insert_df(conn, b.table, df, run_id)
                conn.commit()
                s["read"], s["inserted"], s["deleted"] = len(df), n, int(deleted)
                actual = scalar(conn, f"SELECT COUNT_BIG(*) FROM {b.table}")
                log.recon("rows read = rows staged", b.table, "source-stg", len(df), actual,
                          note=f"{len(files)} file(s) in this batch; refresh={b.refresh}")
                if b.refresh == "files":
                    expected_rows = sum(f["rows"] or 0 for f in files)
                    if b.table not in ("stg.ModelMetric", "stg.FeatureImportance"):   # report files hold both
                        log.recon("rows per file list = rows staged", b.table, "source-stg", expected_rows, actual)
                    for f in files:
                        pending[f["path"]] = (b.source, f)
                print(f"   {b.table:24s} {n:>9,} rows  ({len(files)} file(s))")
        new_runs = batches[0].df
        if mode == "incremental" and watermark:
            late = new_runs[(new_runs["created_utc"] <= watermark) & new_runs["SourceFile"].isin(pending)]
            for r in late.itertuples():
                print(f"   NOTE   run {r.run_id} is older than the watermark {watermark} but new or changed: loaded as late-arriving")
        if mode == "full":
            with log.step("reconcile:stage_targets", None):
                target_checks(log, conn)
        if log.breaks:
            raise RuntimeError(f"{log.breaks} reconciliation break(s) in staging: see etl.ReconResult for run {run_id}")
        with log.step("batch:pending_files", "etl.BatchFile") as s:
            write_batch_files(conn, run_id, pending)
            s["inserted"] = len(pending)
        print(f"   staged in {time.time() - t0:.0f} s; {len(pending)} file(s) pending commit")
        if stage_only:
            print(f"ETL run {run_id}: staging done; the warehouse step (etl.usp_Run_All) runs next")
            return 0
        t1 = time.time()
        run_warehouse(run_id)
        status = scalar(conn, f"SELECT Status FROM etl.RunLog WHERE EtlRunId = {run_id}")
        breaks = scalar(conn, f"SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = {run_id} AND Status = 'BREAK'")
        checks = scalar(conn, f"SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = {run_id}")
        print(f"ETL run {run_id} {status}: warehouse {time.time() - t1:.0f} s, total {time.time() - t0:.0f} s; "
              f"{checks} reconciliation checks, {breaks} breaks")
        return 0 if status == "Succeeded" else 1
    except Exception as e:
        conn.rollback()
        if scalar(conn, f"SELECT Status FROM etl.RunLog WHERE EtlRunId = {run_id}") == "Running":
            log.end_run("Failed", f"{type(e).__name__}: {e}")
        traceback.print_exc()
        print(f"ETL run {run_id} FAILED: {e}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["full", "incremental"], required=True)
    ap.add_argument("--stage-only", action="store_true",
                    help="stop after staging (the SQL Agent job runs etl.usp_Run_All as its next step)")
    a = ap.parse_args()
    return run(a.mode, a.stage_only)


if __name__ == "__main__":
    sys.exit(main())
