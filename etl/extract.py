"""Phase 3 step 3.1: one reader per source. Read-only: readers return DataFrames; nothing is written here.

Every reader returns a Batch: the rows for ONE staging table, with the lineage columns SourceFile (path relative to the
repo) and SourceRowNo (1-based), plus the list of files read with their SHA-256, size and row count. The loader
(load_stage.py) adds EtlRunId, decides which files are new or changed, and bulk-loads the rows.

Sources (codes as in bi/docs/Phase1_Source_Profile.xlsx -> Source inventory)
  S01  DQ runs: manifest.json + date=*/findings.csv      -> stg.DQRun, stg.DQFinding
  S01  derived with the production gate code             -> stg.DQFailClass, stg.DQDayVerdict
  S02  ml_pipeline/dq/decisions.csv                      -> stg.DQDecision
  S10  ml_pipeline/dq/contracts.py (dataset contracts)   -> stg.DatasetContract
  S03  out-of-fold predictions gbt_h{N}m.npz             -> stg.ModelPrediction
  S04  frozen reports baseline_h{N}m.md + model meta     -> stg.ModelMetric, stg.FeatureImportance, stg.ModelMeta
  S05  backtest trades (production 5 + research 15)      -> stg.Trade
  S06  weekly drift reports                              -> stg.FeatureDrift
  S07  production gold panel (Parquet, wide)             -> stg.PanelWide
  S08  model_feature_list.xlsx -> Features               -> stg.Feature
  S09  exchange_calendar_seed.csv                        -> stg.Holiday

    bi_env python bi/etl/extract.py                    # print what every reader returns (dry run)
    bi_env python bi/etl/extract.py --write-panel-ddl  # regenerate bi/sql/08_stage_panel.sql from the model's feature list
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import dq_rules
from profile_sources import (BASELINE, CALENDAR, CLASSES, DECISIONS, DRIFT, FEATURE_XLSX, HORIZONS, MANIFEST, REPO, SNAP,
                             file_sha256, parse_baseline_report, trade_files)

DQ_RUNS = SNAP / "dq" / "runs"
PANEL = BASELINE / "gold" / "index_fno_panel.parquet"
TRADE_COLUMNS = ["instrument", "date", "dir", "direction", "decision_ts", "entry_ts", "exit_ts", "hold_min", "exit_reason",
                 "tradingsymbol", "strike", "expiry", "lot_size", "qty", "signal_prob", "entry_price", "exit_price",
                 "entry_cost", "exit_cost", "target_price", "stop_price", "exit_quote_age_min", "opt_net_rs", "prem0",
                 "entry_px", "exit_px", "pts_net_slip", "fut_net_rs"]
FINDING_COLUMNS = ["date", "dataset", "check", "column", "symbol", "minute_from", "minute_to", "severity", "value",
                   "threshold", "known_cause", "detail"]
FEATURE_SHEET_COLUMNS = {"Sr No": "sr_no", "Feature exact name": "feature_name", "Group": "feature_group",
                         "Short description": "short_description", "Websocket data used or not": "websocket_used",
                         "Websocket data should be used or not": "websocket_should", "Status": "status",
                         "Research basis & usability": "research_basis", "Comment": "comment"}


@dataclass
class Batch:
    table: str                      # staging table, e.g. 'stg.DQFinding'
    source: str                     # source code S01..S10
    df: pd.DataFrame                # stg columns except EtlRunId / LoadedAt
    files: list = field(default_factory=list)   # [{'path', 'sha256', 'bytes', 'rows'}]
    refresh: str = "files"          # 'files' = incremental by new/changed file; 'always' = reloaded in full every run


def rel(p: Path) -> str:
    """Path relative to the repo (the lineage value); absolute for a file outside it (tests)."""
    try:
        return p.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def file_info(p: Path, rows: int | None) -> dict:
    return {"path": rel(p), "sha256": file_sha256(p), "bytes": p.stat().st_size, "rows": rows}


def model_features() -> list[str]:
    """The 124 production features, in the order the frozen manifest lists them."""
    return list(json.loads(MANIFEST.read_text())["models"]["features"])


# ------------------------------------------------------------------------------------------------ S01 DQ runs
def read_dq(root: Path = DQ_RUNS) -> tuple[Batch, Batch]:
    runs, frames, run_files, finding_files = [], [], [], []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        man_path = d / "manifest.json"
        man = json.loads(man_path.read_text()) if man_path.exists() else {}
        created = man.get("created_utc") or d.name[:16]
        files = sorted(d.glob("date=*/findings.csv"))
        for f in files:
            df = pd.read_csv(f, dtype=str, keep_default_na=False)
            missing = set(FINDING_COLUMNS) - set(df.columns)
            if missing:
                raise ValueError(f"{rel(f)}: missing columns {sorted(missing)}")
            df = df[FINDING_COLUMNS].copy()
            df["run_id"], df["run_created_utc"] = d.name, created
            df["SourceFile"], df["SourceRowNo"] = rel(f), np.arange(1, len(df) + 1)
            frames.append(df)
            finding_files.append(file_info(f, len(df)))
        src = man_path if man_path.exists() else d
        runs.append({"run_id": d.name, "created_utc": created, "manifest_rows": man.get("rows"),
                     "manifest_dates": len(man.get("dates", [])) if man else None, "date_files": len(files),
                     "SourceFile": rel(src), "SourceRowNo": 1})
        if man_path.exists():
            run_files.append(file_info(man_path, 1))
    findings = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=FINDING_COLUMNS)
    runs = pd.DataFrame(runs).astype({"manifest_rows": "Int64", "manifest_dates": "Int64"})   # nullable ints, not floats
    return (Batch("stg.DQRun", "S01", runs, run_files),
            Batch("stg.DQFinding", "S01", findings, finding_files))


def latest_run_wins(findings: pd.DataFrame) -> pd.DataFrame:
    """Rows of the newest run (by run_created_utc) that evaluated each (date, dataset): the production rule."""
    pairs = findings[["date", "dataset", "run_id", "run_created_utc"]].drop_duplicates()
    latest = pairs.sort_values(["run_created_utc", "run_id"]).groupby(["date", "dataset"], as_index=False).last()
    return findings.merge(latest[["date", "dataset", "run_id"]], on=["date", "dataset", "run_id"], how="inner")


def read_dq_derived(findings: pd.DataFrame, dq_root: Path = SNAP / "dq", decisions_path: Path = DECISIONS) -> tuple[Batch, Batch]:
    """Gate class of every FAIL in a resolved run, and the gate's verdict for every date - with the production code.
    Raises if this module's 'latest run wins' ever disagrees with the production gate.resolve."""
    decisions = dq_rules.load_decisions(decisions_path)
    resolved = latest_run_wins(findings)
    dates = sorted(findings["date"].unique())
    g = dq_rules.resolve(dq_root, dates)
    mine = set(map(tuple, resolved[["date", "dataset", "run_id"]].drop_duplicates().values))
    theirs = set(map(tuple, g[["date", "dataset", "run_id"]].drop_duplicates().values))
    if mine != theirs or len(resolved) != len(g):
        raise RuntimeError(f"latest-run-wins disagrees with gate.resolve: {len(resolved)} vs {len(g)} rows, "
                           f"{len(mine ^ theirs)} differing (date, dataset, run) triples")
    fails = resolved[resolved["severity"] == "fail"]
    cls = pd.DataFrame({
        "finding_file": fails["SourceFile"].values,
        "finding_row_no": fails["SourceRowNo"].values,
        "gate_class": [dq_rules.classify_fail(r, r["date"], decisions) for _, r in fails.iterrows()],
    })
    gate_src = "ml_pipeline/dq/gate.py"
    cls["SourceFile"], cls["SourceRowNo"] = gate_src, np.arange(1, len(cls) + 1)
    verdicts = []
    for d in dates:
        v = dq_rules.day_verdict(d, g[g["date"] == d], decisions)
        verdicts.append({"date": d, "status": v["status"], "reasons_n": len(v["reasons"]), "masks_n": len(v["masks"]),
                         "accepted_n": len(v["accepted"]), "reason_text": " ; ".join(v["reasons"])[:2000] or None,
                         "runs": ", ".join(v["runs"])[:1000] or None})
    ver = pd.DataFrame(verdicts)
    ver["SourceFile"], ver["SourceRowNo"] = gate_src, np.arange(1, len(ver) + 1)
    code_files = [file_info(REPO / "ml_pipeline" / "dq" / "gate.py", None), file_info(decisions_path, None)]
    return (Batch("stg.DQFailClass", "S01", cls, code_files, refresh="always"),
            Batch("stg.DQDayVerdict", "S01", ver, code_files, refresh="always"))


# ------------------------------------------------------------------------------------------------ S02 decisions
def read_decisions(path: Path = DECISIONS) -> Batch:
    """Parsed exactly as the gate parses it (gate.load_decisions), so the warehouse sees the same decisions."""
    d = dq_rules.load_decisions(path).reset_index(drop=True)
    d["SourceFile"], d["SourceRowNo"] = rel(path), np.arange(1, len(d) + 1)
    # always staged in full: the reconciliation re-derives the day verdicts from it on every run
    return Batch("stg.DQDecision", "S02", d, [file_info(path, len(d))], refresh="always")


# ------------------------------------------------------------------------------------------------ S10 contracts
def read_contracts() -> Batch:
    from ml_pipeline.dq import contracts as c
    from ml_pipeline.dq import gate
    rows = [{"dataset": name, "dataset_group": k.group, "publication": k.publication, "cadence": k.cadence}
            for name, k in c.CONTRACTS.items()]
    # cross_dataset is not a collector: it is the post-market cross-source reconciliation (checks X1-X8)
    rows.append({"dataset": "cross_dataset", "dataset_group": "Cross-source reconciliation (X1-X8)",
                 "publication": "same_day", "cadence": "daily"})
    df = pd.DataFrame(rows)
    df["is_consumed"] = df["dataset"].isin(gate.CONSUMED)
    df["session_end_before"] = c.hhmm(c.CLOSE_BEFORE)
    df["session_end_from"] = c.hhmm(c.CLOSE_FROM)
    df["session_end_change_date"] = c.CLOSE_EFFECTIVE_DATE.isoformat()
    path = REPO / "ml_pipeline" / "dq" / "contracts.py"
    df["SourceFile"], df["SourceRowNo"] = rel(path), np.arange(1, len(df) + 1)
    return Batch("stg.DatasetContract", "S10", df, [file_info(path, None)], refresh="always")


# ------------------------------------------------------------------------------------------------ S03 predictions
def read_oof() -> Batch:
    frames, files = [], []
    for h in HORIZONS:
        p = BASELINE / "gold" / "oof" / f"gbt_h{h}m.npz"
        z = np.load(p, allow_pickle=False)
        proba = z["proba"]
        df = pd.DataFrame({"horizon_min": h, "ts_ns": z["ts"].astype(np.int64), "inst": z["inst"].astype(np.int64),
                           "y": z["y"].astype(np.int64), "proba_down": proba[:, 0], "proba_flat": proba[:, 1],
                           "proba_up": proba[:, 2], "pos": z["pos"].astype(np.int64)})
        df["SourceFile"], df["SourceRowNo"] = rel(p), np.arange(1, len(df) + 1)
        frames.append(df)
        files.append(file_info(p, len(df)))
    return Batch("stg.ModelPrediction", "S03", pd.concat(frames, ignore_index=True), files)


# ------------------------------------------------------------------------------------------------ S04 reports + meta
def read_reports() -> tuple[Batch, Batch, Batch]:
    metrics, imps, metas, rep_files, meta_files = [], [], [], [], []
    for h in HORIZONS:
        p = BASELINE / "reports" / f"baseline_h{h}m.md"
        r = parse_baseline_report(p)
        rows = [("accuracy", None, None, r["accuracy"]), ("balanced_accuracy", None, None, r["balanced_accuracy"]),
                ("macro_f1", None, None, r["macro_f1"]), ("directional_precision", None, None, r["directional_precision"]),
                ("committed_pct", None, None, r["committed_pct"]), ("rows", None, None, r["rows"]),
                ("features", None, None, r["features"])]
        rows += [("confusion", CLASSES[a], CLASSES[b], r["confusion"][a][b]) for a in range(3) for b in range(3)]
        for i, (m, a, b, v) in enumerate(rows, 1):
            metrics.append({"horizon_min": h, "metric": m, "actual_class": a, "predicted_class": b,
                            "metric_value": float(v), "SourceFile": rel(p), "SourceRowNo": i})
        for i, (feat, imp) in enumerate(r["importances"], 1):
            imps.append({"horizon_min": h, "importance_rank": i, "feature": feat, "importance": imp,
                         "SourceFile": rel(p), "SourceRowNo": i})
        rep_files.append(file_info(p, len(rows)))
        mp = BASELINE / "models" / f"baseline_h{h}m_meta.json"
        m = json.loads(mp.read_text())
        metas.append({"horizon_min": m["horizon"], "trained_at": m.get("trained_at"), "n_rows": m.get("n_rows"),
                      "label_mode": m.get("label_mode"), "n_features": m.get("n_features"), "model_file": m.get("model_file"),
                      "balanced_accuracy_cv": m.get("balanced_accuracy_cv"),
                      "directional_precision_cv": m.get("directional_precision_cv"), "SourceFile": rel(mp), "SourceRowNo": 1})
        meta_files.append(file_info(mp, 1))
    return (Batch("stg.ModelMetric", "S04", pd.DataFrame(metrics), rep_files),
            Batch("stg.FeatureImportance", "S04", pd.DataFrame(imps), rep_files),
            Batch("stg.ModelMeta", "S04", pd.DataFrame(metas).astype({"n_rows": "Int64", "n_features": "Int64"}), meta_files))


# ------------------------------------------------------------------------------------------------ S05 trades
def read_trades() -> Batch:
    frames, files = [], []
    for run, label, h, p in trade_files():
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        if list(df.columns) != TRADE_COLUMNS:
            raise ValueError(f"{rel(p)}: columns differ from the expected 28 trade columns")
        df["run_set"], df["label_mode"], df["horizon_min"] = run, label, h
        df["SourceFile"], df["SourceRowNo"] = rel(p), np.arange(1, len(df) + 1)
        frames.append(df)
        files.append(file_info(p, len(df)))
    return Batch("stg.Trade", "S05", pd.concat(frames, ignore_index=True), files)


# ------------------------------------------------------------------------------------------------ S06 drift
DRIFT_ROW = re.compile(r"^\| ([a-z0-9_]+) \| (-?[0-9.]+) \|")


def parse_drift(p: Path) -> list[dict]:
    rows, section = [], ""
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
        if DRIFT_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 6:
                raise ValueError(f"{rel(p)} line {n}: expected 6 cells, got {len(cells)}")
            rows.append({"report_date": p.stem.replace("drift_report_", ""), "section_title": section,
                         "feature": cells[0], "psi": cells[1], "n_eff": cells[2], "in_band": cells[3],
                         "null_train_live": cells[4], "status": cells[5], "SourceFile": rel(p), "SourceRowNo": n})
    return rows


def read_drift(folder: Path = DRIFT) -> Batch:
    rows, files = [], []
    for p in sorted(folder.glob("drift_report_*.md")):
        r = parse_drift(p)
        rows += r
        files.append(file_info(p, len(r)))
    return Batch("stg.FeatureDrift", "S06", pd.DataFrame(rows), files)


# ------------------------------------------------------------------------------------------------ S07 panel
def read_panel(path: Path = PANEL) -> Batch:
    feats = model_features()
    df = pd.read_parquet(path, columns=["timestamp", "instrument", "date"] + feats)
    df["timestamp"] = df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)   # stored as UTC, no offset
    df["SourceFile"], df["SourceRowNo"] = rel(path), np.arange(1, len(df) + 1)
    return Batch("stg.PanelWide", "S07", df, [file_info(path, len(df))])


def panel_ddl(feats: list[str]) -> str:
    lines = [
        "/* Phase 3 step 3.2: GENERATED by bi/etl/extract.py --write-panel-ddl from the frozen model's feature list",
        "   (ml_pipeline/baselines/v0_2026-10/manifest.json). Do not edit by hand. */",
        "SET NOCOUNT ON;", "",
        "/* S07: the production gold panel, wide, as in the Parquet file (one row per minute x instrument, one column per feature). */",
        "CREATE TABLE stg.PanelWide (",
        "    [timestamp]      datetime2(0)  NOT NULL, -- Minute (UTC) | S07 timestamp | converted from UTC-aware to UTC without offset",
        "    instrument       nvarchar(20)  NOT NULL, -- Underlying | S07 instrument |",
        "    [date]           nvarchar(20)  NOT NULL, -- IST trading date | S07 date | equals the IST date of timestamp (checked in Phase 1)",
    ]
    for f in feats:
        lines.append(f"    [{f}] float NULL, -- Value of feature {f} at that minute | S07 {f} | NULL = missing (not loaded into FactFeatureValue)")
    lines += [
        "    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage",
        "    SourceFile       nvarchar(260) NOT NULL, -- Parquet path relative to the repo | extract.py | lineage",
        "    SourceRowNo      int           NOT NULL, -- 1-based row in the file | extract.py | lineage",
        "    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgPanelWide_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage",
        ");", "GO", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ S08, S09
def read_features(path: Path = FEATURE_XLSX) -> Batch:
    df = pd.read_excel(path, sheet_name="Features", dtype=str)
    missing = set(FEATURE_SHEET_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{rel(path)} -> Features: missing columns {sorted(missing)}")
    df = df[list(FEATURE_SHEET_COLUMNS)].rename(columns=FEATURE_SHEET_COLUMNS)
    df["SourceFile"], df["SourceRowNo"] = rel(path), np.arange(1, len(df) + 1)
    return Batch("stg.Feature", "S08", df, [file_info(path, len(df))])


def read_holidays(path: Path = CALENDAR) -> Batch:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["SourceFile"], df["SourceRowNo"] = rel(path), np.arange(1, len(df) + 1)
    return Batch("stg.Holiday", "S09", df, [file_info(path, len(df))])


# ------------------------------------------------------------------------------------------------ all
def read_all() -> list[Batch]:
    """Every staging batch, in load order."""
    dq_run, dq_finding = read_dq()
    fail_class, day_verdict = read_dq_derived(dq_finding.df)
    metric, importance, meta = read_reports()
    return [dq_run, dq_finding, fail_class, day_verdict, read_decisions(), read_contracts(), read_oof(), metric,
            importance, meta, read_trades(), read_drift(), read_panel(), read_features(), read_holidays()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write-panel-ddl", action="store_true")
    a = ap.parse_args()
    if a.write_panel_ddl:
        out = Path(__file__).resolve().parents[1] / "sql" / "08_stage_panel.sql"
        out.write_text(panel_ddl(model_features()), encoding="utf-8")
        print(f"wrote {rel(out)}")
        return 0
    for b in read_all():
        print(f"{b.table:24s} {b.source}  rows {len(b.df):>9,}  files {len(b.files):>3}  refresh={b.refresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
