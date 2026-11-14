"""Phase 1 (steps 1.2, 1.3, 1.6): profile every source, check integrity, pre-register reconciliation targets.

Read-only: it reads source files and writes two outputs, nothing else.
    bi/docs/Phase1_Source_Profile.xlsx   human-readable profile, integrity checks and targets
    bi/docs/preregistered_targets.json   the same targets for the load tests (Phase 3/4)

Run from the trading_bot folder:
    C:/Users/<you>/anaconda3/envs/bi_env/python.exe bi/etl/profile_sources.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import dq_rules  # same folder: the production DQ gate's rules

REPO = Path(__file__).resolve().parents[2]
BI = REPO / "bi"
SNAP = BI / "source_snapshot" / "gcs"
BASELINE = SNAP / "ml" / "baselines" / "v0_2026-10"
MANIFEST = REPO / "ml_pipeline" / "baselines" / "v0_2026-10" / "manifest.json"
LOCAL_DQ_HISTORY = REPO / "data" / "dq" / "runs" / "20261003b-history"
RESEARCH = REPO / "data" / "research_20260917"
DRIFT = SNAP / "ml" / "reports" / "drift_history"
DECISIONS = REPO / "ml_pipeline" / "dq" / "decisions.csv"
CALENDAR = REPO / "exchange_calendar_seed.csv"
FEATURE_XLSX = REPO / "model_feature_list.xlsx"
HORIZONS = (5, 10, 15, 20, 30)
CLASSES = ("Down", "Flat", "Up")
COPIED_PREFIXES = ("reports/", "gold/oof/", "gold/backtest/", "gold/index_fno_panel.parquet")  # what step 1.4 copied
HEADLINE_ROWS_H10 = 67_583
HEADLINE_FEATURES = 124


def rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- 1. integrity
def integrity_checks() -> list[dict]:
    """Copied baseline files vs the sha256 recorded when the baseline was frozen (2026-10-02)."""
    man = json.loads(MANIFEST.read_text())
    out = []
    for o in man["objects"]:
        relpath = o["destination"].split("v0_2026-10/", 1)[1]
        local = BASELINE / relpath
        wanted = relpath.startswith(COPIED_PREFIXES) or (relpath.startswith("models/") and relpath.endswith(".json"))
        if not wanted:
            continue  # not copied on purpose (option quotes, live scorecard, model binaries)
        if not local.exists():
            out.append({"file": relpath, "bytes_frozen": int(o["size"]), "bytes_local": None, "sha256_match": False})
            continue
        expected = o["sha256"]
        actual = file_sha256(local)
        if len(expected) != 64:  # base64 form
            actual = base64.b64encode(bytes.fromhex(actual)).decode()
        out.append({"file": relpath, "bytes_frozen": int(o["size"]), "bytes_local": local.stat().st_size,
                    "sha256_match": actual == expected})
    return out


# ---------------------------------------------------------------------------------------------- 2. DQ runs
def load_dq_runs() -> tuple[pd.DataFrame, list[dict]]:
    frames, runs = [], []
    for d in sorted((SNAP / "dq" / "runs").iterdir()):
        man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
        files = sorted(d.glob("date=*/findings.csv"))
        n = 0
        for f in files:
            df = pd.read_csv(f, dtype=str, keep_default_na=False)
            df["run_id"], df["run_created_utc"], df["source_file"] = d.name, man.get("created_utc", d.name[:16]), rel(f)
            frames.append(df)
            n += len(df)
        all_rows = None
        if (d / "all_findings.csv").exists():
            all_rows = len(pd.read_csv(d / "all_findings.csv", dtype=str, keep_default_na=False))
        runs.append({"run_id": d.name, "created_utc": man.get("created_utc", ""), "date_files": len(files),
                     "rows_per_date_files": n, "rows_all_findings_csv": all_rows,
                     "rows_in_manifest": man.get("rows"), "dates_in_manifest": len(man.get("dates", []))})
    return pd.concat(frames, ignore_index=True), runs


def history_parity() -> dict:
    """Step 1.3: the local history run vs the GCS copy of the same run, file by file."""
    gcs = SNAP / "dq" / "runs" / LOCAL_DQ_HISTORY.name
    local_files = {p.parent.name: p for p in LOCAL_DQ_HISTORY.glob("date=*/findings.csv")}
    gcs_files = {p.parent.name: p for p in gcs.glob("date=*/findings.csv")}
    same = sum(1 for k in local_files if k in gcs_files and file_sha256(local_files[k]) == file_sha256(gcs_files[k]))
    return {"local_files": len(local_files), "gcs_files": len(gcs_files), "identical_files": same,
            "only_local": sorted(set(local_files) - set(gcs_files)), "only_gcs": sorted(set(gcs_files) - set(local_files))}


def resolve_latest_run(f: pd.DataFrame) -> pd.DataFrame:
    """'Latest run wins' (same rule as ml_pipeline/dq/gate.py): for each (date, dataset) keep only the findings of the
    newest run (by manifest created_utc) that evaluated that pair."""
    pairs = f[["date", "dataset", "run_id", "run_created_utc"]].drop_duplicates()
    latest = pairs.sort_values("run_created_utc").groupby(["date", "dataset"], as_index=False).last()
    return f.merge(latest[["date", "dataset", "run_id"]], on=["date", "dataset", "run_id"], how="inner")


# ---------------------------------------------------------------------------------------------- 3. model
NUM = r"([0-9.]+)"


def parse_baseline_report(p: Path) -> dict:
    t = p.read_text(encoding="utf-8")
    g = lambda pat: (m.group(1) if (m := re.search(pat, t)) else None)
    rep = {
        "generated": g(r"_generated ([0-9: -]+)_"),
        "rows": int(g(r"rows=([0-9,]+)").replace(",", "")),
        "features": int(g(r"features=([0-9]+)")),
        "accuracy": float(g(r"- accuracy: \*\*" + NUM)),
        "balanced_accuracy": float(g(r"- balanced accuracy: \*\*" + NUM)),
        "macro_f1": float(g(r"- macro F1: \*\*" + NUM)),
        "directional_precision": float(g(r"directional precision \(Up/Down only\): \*\*" + NUM)),
        "committed_pct": float(g(r"on ([0-9.]+)% of rows committed")),
    }
    cm_block = t.split("Confusion matrix rows=actual cols=pred [Down,Flat,Up]:", 1)[1].split("```")[1]
    rep["confusion"] = [[int(x) for x in re.findall(r"-?\d+", line)] for line in cm_block.strip().splitlines()]
    imp = t.split("## Top permutation importances", 1)[1] if "## Top permutation importances" in t else ""
    rep["importances"] = [(a, float(b)) for a, b in re.findall(r"^- ([A-Za-z0-9_]+): (-?[0-9.]+)", imp, re.M)]
    return rep


def oof_metrics(npz: Path) -> dict:
    z = np.load(npz, allow_pickle=True)
    y, proba = z["y"].astype(int), z["proba"]
    pred = proba.argmax(1)
    cm = pd.crosstab(pd.Series(y, name="actual"), pd.Series(pred, name="pred")).reindex(index=[0, 1, 2], columns=[0, 1, 2], fill_value=0)
    recalls = [cm.loc[k, k] / cm.loc[k].sum() for k in range(3)]
    precisions = [cm.loc[k, k] / cm[k].sum() if cm[k].sum() else 0 for k in range(3)]
    f1 = [2 * p * r / (p + r) if p + r else 0 for p, r in zip(precisions, recalls)]
    directional = cm.loc[0, 0] + cm.loc[2, 2]
    committed = cm[0].sum() + cm[2].sum()
    ts = pd.to_datetime(z["ts"], utc=True)
    return {"rows": len(y), "accuracy": float((pred == y).mean()), "balanced_accuracy": float(np.mean(recalls)),
            "macro_f1": float(np.mean(f1)), "directional_precision": float(directional / committed),
            "committed_pct": float(100 * committed / len(y)), "confusion": cm.values.tolist(),
            "instruments": {int(k): int(v) for k, v in zip(*np.unique(z["inst"], return_counts=True))},
            "ts_min": str(ts.min()), "ts_max": str(ts.max()), "dates": int(ts.tz_convert("Asia/Kolkata").normalize().nunique()),
            "class_counts": {CLASSES[k]: int((y == k).sum()) for k in range(3)}}


# ---------------------------------------------------------------------------------------------- 4. trades
def trade_files() -> list[tuple[str, str, int, Path]]:
    out = [("production v0_2026-10", "vol", h, BASELINE / "gold" / "backtest" / f"trades_h{h}m.csv") for h in HORIZONS]
    for sub, label in (("gold/label_vol", "vol"), ("gold_cost/label_fut_cost", "fut_cost"), ("gold_cost/label_opt_pnl", "opt_pnl")):
        for h in HORIZONS:
            out.append(("research 2026-09-17", label, h, RESEARCH / sub / "backtest" / f"trades_h{h}m.csv"))
    return out


def backtest_cell_check(h: int, trades: pd.DataFrame) -> dict:
    """The production trade file should be the 'best' cell of the frozen backtest report."""
    bt = json.loads((BASELINE / "reports" / f"backtest_h{h}m.json").read_text())
    best = bt["best"]
    cell = next((c for c in bt["cells"] if all(c.get(k) == v for k, v in best.items())), None)
    if cell is None:
        return {"best": best, "found": False}
    return {"best": best, "found": True, "report_trades": cell["trades"], "file_trades": len(trades),
            "report_opt_net": round(cell["opt_net_total"], 2), "file_opt_net": round(float(trades["opt_net_rs"].sum()), 2)}


# ---------------------------------------------------------------------------------------------- 5. drift
def drift_rows() -> pd.DataFrame:
    rows = []
    for p in sorted(DRIFT.glob("drift_report_*.md")):
        week = p.stem.replace("drift_report_", "")
        section = ""
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            m = re.match(r"^\| ([a-z0-9_]+) \| (-?[0-9.]+) \|", line)
            if m:
                rows.append({"report_date": week, "section": section, "feature": m.group(1), "psi": float(m.group(2))})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------- 6. column profile
def column_profile(name: str, df: pd.DataFrame, limit_cols: int | None = None) -> list[dict]:
    out = []
    cols = list(df.columns)[:limit_cols] if limit_cols else list(df.columns)
    for c in cols:
        s = df[c]
        blank = s.isna() | (s.astype(str) == "")
        nonblank = s[~blank]
        out.append({"source": name, "column": c, "dtype": str(s.dtype), "rows": len(s),
                    "null_or_blank_pct": round(100 * blank.mean(), 2), "distinct": int(nonblank.nunique()),
                    "min": str(nonblank.min())[:40] if len(nonblank) else "", "max": str(nonblank.max())[:40] if len(nonblank) else "",
                    "example": str(nonblank.iloc[0])[:60] if len(nonblank) else ""})
    return out


# ---------------------------------------------------------------------------------------------- main
def main() -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    inventory, profile, checks = [], [], []

    def check(name: str, expected, actual, ok: bool | None = None, note: str = ""):
        ok = (expected == actual) if ok is None else ok
        checks.append({"check": name, "expected": str(expected), "actual": str(actual), "result": "PASS" if ok else "FAIL", "note": note})

    # 1. integrity
    integ = integrity_checks()
    check("Baseline copies match frozen sha256", len(integ), sum(r["sha256_match"] for r in integ), note=f"{len(integ)} files copied from GCS")

    # 2. DQ
    f, runs = load_dq_runs()
    for r in runs:
        if r["rows_all_findings_csv"] is not None:
            check(f"DQ run {r['run_id']}: per-date files = all_findings.csv", r["rows_all_findings_csv"], r["rows_per_date_files"])
        if r["rows_in_manifest"] is not None:
            check(f"DQ run {r['run_id']}: rows = manifest", r["rows_in_manifest"], r["rows_per_date_files"])
    par = history_parity()
    check("1.3 Local DQ history = GCS copy (identical files)", par["local_files"], par["identical_files"],
          ok=par["local_files"] == par["gcs_files"] == par["identical_files"], note=f"only local: {par['only_local']}, only GCS: {par['only_gcs']}")
    dups = int(f.duplicated(["run_id", "date", "dataset", "check", "column", "symbol", "minute_from", "minute_to", "severity", "value", "detail"]).sum())
    check("DQ findings: date column = date= folder", len(f), int((f.date == f.source_file.str.extract(r"date=([0-9-]+)")[0]).sum()))
    resolved = resolve_latest_run(f)
    dates = sorted(f.date.unique())
    g_res = dq_rules.resolve(SNAP / "dq", dates)
    mine = set(map(tuple, resolved[["date", "dataset", "run_id"]].drop_duplicates().values))
    theirs = set(map(tuple, g_res[["date", "dataset", "run_id"]].drop_duplicates().values))
    check("Latest run wins: my resolution = production gate.resolve (rows)", len(g_res), len(resolved))
    check("Latest run wins: same (date, dataset, run) pairs as gate.resolve", len(theirs), len(mine & theirs), ok=mine == theirs)
    decisions = dq_rules.load_decisions(DECISIONS)
    fails = g_res[g_res.severity == "fail"].copy()
    fails["gate_class"] = [dq_rules.classify_fail(r, r["date"], decisions) for _, r in fails.iterrows()]
    verdicts = {d: dq_rules.day_verdict(d, g_res[g_res.date == d], decisions) for d in dates}
    # the per-row classes must explain each day's verdict: a day is excluded iff it has a day-level class or a '*' decision
    day_level = set(fails.loc[fails.gate_class.isin([dq_rules.FAIL_CLASSES[3], dq_rules.FAIL_CLASSES[5]]), "date"])
    star_excl = set(decisions.loc[(decisions.action == "exclude_day") & (decisions.dataset == "*"), "date"])
    excluded = {d for d, v in verdicts.items() if v["status"] == "exclude"}
    check("Per-FAIL classes reproduce the gate's excluded days", sorted(excluded), sorted((day_level | star_excl) & set(dates)))
    masked = sum(len(v["masks"]) for v in verdicts.values())
    check("Per-FAIL classes reproduce the gate's minute masks", masked, int((fails.gate_class == dq_rules.FAIL_CLASSES[4]).sum()))
    inventory.append({"id": "S01", "source": "DQ findings (all runs)", "path": "bi/source_snapshot/gcs/dq/runs/*/date=*/findings.csv",
                      "format": "CSV", "files": f.source_file.nunique(), "rows": len(f), "columns": f.shape[1] - 3,
                      "date_min": f.date.min(), "date_max": f.date.max(),
                      "grain": "run × date × dataset × check × finding", "feeds": "FactDQFinding, FactDQDailyStatus",
                      "notes": f"{len(runs)} runs; {f.dataset.nunique()} datasets; {f.check.nunique()} checks; exact duplicate rows: {dups}"})
    inventory.append({"id": "S02", "source": "DQ decisions", "path": rel(DECISIONS), "format": "CSV (# comments)", "files": 1,
                      "rows": len(decisions), "columns": decisions.shape[1], "date_min": decisions.date.min(), "date_max": decisions.date.max(),
                      "grain": "one written decision", "feeds": "FactDQDailyStatus (exclude / accept)", "notes": "; ".join(decisions.action + " " + decisions.date)})
    profile += column_profile("S01 DQ findings", f.drop(columns=["source_file"]))

    # 3. model (production + research reports)
    model_rows, cm_rows, imp_rows = [], [], []
    for h in HORIZONS:
        rep = parse_baseline_report(BASELINE / "reports" / f"baseline_h{h}m.md")
        oof = oof_metrics(BASELINE / "gold" / "oof" / f"gbt_h{h}m.npz")
        meta = json.loads((BASELINE / "models" / f"baseline_h{h}m_meta.json").read_text())
        check(f"h{h}m: OOF rows = report rows", rep["rows"], oof["rows"])
        check(f"h{h}m: OOF rows = model meta n_rows", meta["n_rows"], oof["rows"])
        for k, tol in (("accuracy", 5e-4), ("balanced_accuracy", 5e-4), ("macro_f1", 5e-4), ("directional_precision", 5e-4), ("committed_pct", 0.05)):
            check(f"h{h}m: {k} recomputed from OOF = report", rep[k], round(oof[k], 4), ok=abs(rep[k] - oof[k]) <= tol, note=f"tolerance {tol} (report is rounded)")
        check(f"h{h}m: confusion matrix recomputed = report", rep["confusion"], oof["confusion"])
        model_rows.append({"run": "production v0_2026-10", "horizon_min": h, "report_generated": rep["generated"], "rows": oof["rows"],
                           "features": rep["features"], "dates": oof["dates"], "ts_min_utc": oof["ts_min"], "ts_max_utc": oof["ts_max"],
                           "accuracy": round(oof["accuracy"], 4), "balanced_accuracy": round(oof["balanced_accuracy"], 4), "macro_f1": round(oof["macro_f1"], 4),
                           "directional_precision": round(oof["directional_precision"], 4), "committed_pct": round(oof["committed_pct"], 2),
                           "Down/Flat/Up actual": "/".join(str(v) for v in oof["class_counts"].values()), "importances_listed": len(rep["importances"])})
        for a in range(3):
            for p in range(3):
                cm_rows.append({"horizon_min": h, "actual": CLASSES[a], "predicted": CLASSES[p], "count": oof["confusion"][a][p]})
        for rank, (feat, val) in enumerate(rep["importances"], start=1):
            imp_rows.append({"horizon_min": h, "rank": rank, "feature": feat, "importance": val})
    check("Headline run: production h10m OOF rows = 67,583", HEADLINE_ROWS_H10, next(m["rows"] for m in model_rows if m["horizon_min"] == 10))
    check("Headline run: features = 124", HEADLINE_FEATURES, next(m["features"] for m in model_rows if m["horizon_min"] == 10))
    inventory.append({"id": "S03", "source": "Production OOF predictions", "path": "…/v0_2026-10/gold/oof/gbt_h{5,10,15,20,30}m.npz",
                      "format": "NumPy npz (ts, y, proba[3], pos, inst)", "files": 5, "rows": sum(m["rows"] for m in model_rows), "columns": 7,
                      "date_min": min(m["ts_min_utc"] for m in model_rows)[:10], "date_max": max(m["ts_max_utc"] for m in model_rows)[:10],
                      "grain": "horizon × minute × instrument", "feeds": "FactModelPrediction", "notes": "y: 0=Down 1=Flat 2=Up; inst: 0/1"})
    inventory.append({"id": "S04", "source": "Production reports + model meta", "path": "…/v0_2026-10/reports/baseline_h*.md, models/*_meta.json",
                      "format": "Markdown + JSON", "files": 10, "rows": len(imp_rows), "columns": "", "date_min": "2026-09-17", "date_max": "2026-09-17",
                      "grain": "horizon × metric / × feature", "feeds": "FactModelMetric, FactFeatureImportance, DimModelRun", "notes": "importance = top 15, last fold"})

    # 4. trades
    trade_rows, all_trades = [], []
    for run, label, h, p in trade_files():
        t = pd.read_csv(p)
        t["scenario_run"], t["scenario_label"], t["horizon_min"] = run, label, h
        all_trades.append(t)
        row = {"run": run, "label_mode": label, "horizon_min": h, "file": rel(p), "trades": len(t),
               "opt_net_rs": round(float(t.opt_net_rs.sum()), 2), "fut_net_rs": round(float(t.fut_net_rs.sum()), 2),
               "costs_rs": round(float((t.entry_cost + t.exit_cost).sum()), 2), "date_min": t.date.min(), "date_max": t.date.max(),
               "dup_keys": int(t.duplicated(["instrument", "decision_ts"]).sum())}
        trade_rows.append(row)
        if run.startswith("production"):
            c = backtest_cell_check(h, t)
            if c["found"]:
                check(f"h{h}m: trade file = backtest 'best' cell (trades)", c["report_trades"], c["file_trades"], note=str(c["best"]))
                check(f"h{h}m: trade file = backtest 'best' cell (opt net Rs)", c["report_opt_net"], c["file_opt_net"],
                      ok=abs(c["report_opt_net"] - c["file_opt_net"]) < 1.0)
            else:
                check(f"h{h}m: backtest 'best' cell found", True, False, note=str(c["best"]))
    trades = pd.concat(all_trades, ignore_index=True)
    inventory.append({"id": "S05", "source": "Backtest trades (production 5 + research 15 files)", "path": "…/gold/backtest/trades_h*.csv; data/research_20260917/**/backtest/",
                      "format": "CSV", "files": len(trade_rows), "rows": len(trades), "columns": trades.shape[1] - 3,
                      "date_min": trades.date.min(), "date_max": trades.date.max(), "grain": "one simulated trade",
                      "feeds": "FactTrade, DimModelRun", "notes": f"duplicate (scenario, instrument, decision_ts): {int(trades.duplicated(['scenario_run','scenario_label','horizon_min','instrument','decision_ts']).sum())}"})
    profile += column_profile("S05 trades", trades.drop(columns=["scenario_run", "scenario_label", "horizon_min"]))

    # 5. drift
    dr = drift_rows()
    inventory.append({"id": "S06", "source": "Weekly drift reports", "path": "bi/source_snapshot/gcs/ml/reports/drift_history/*.md", "format": "Markdown tables",
                      "files": dr.report_date.nunique(), "rows": len(dr), "columns": 4, "date_min": dr.report_date.min(), "date_max": dr.report_date.max(),
                      "grain": "report × section × listed feature", "feeds": "FactFeatureDrift", "notes": "only the top-15 lists per section are published, not all 124 features"})

    # 6. panel + features
    panel_path = BASELINE / "gold" / "index_fno_panel.parquet"
    man = json.loads(MANIFEST.read_text())
    features = man["models"]["features"]
    pmeta = pq.ParquetFile(panel_path).metadata
    panel_cols = pq.ParquetFile(panel_path).schema_arrow.names
    missing_in_panel = [x for x in features if x not in panel_cols]
    panel = pd.read_parquet(panel_path, columns=["timestamp", "instrument", "date"] + [x for x in features if x in panel_cols])
    nonnull = int(panel[[x for x in features if x in panel_cols]].notna().sum().sum())
    check("Panel rows = frozen manifest rows", man["panel"]["rows"], pmeta.num_rows)
    check("All 124 model features present in the panel", 0, len(missing_in_panel), note=str(missing_in_panel[:5]))
    xl = pd.read_excel(FEATURE_XLSX, sheet_name="Features")
    names = set(xl["Feature exact name"].astype(str).str.strip())
    missing_in_xlsx = [x for x in features if x not in names]
    check("All 124 model features described in model_feature_list.xlsx", 0, len(missing_in_xlsx), ok=len(missing_in_xlsx) == 0, note=str(missing_in_xlsx[:8]))
    inventory.append({"id": "S07", "source": "Production gold panel", "path": rel(panel_path), "format": "Parquet", "files": 1, "rows": pmeta.num_rows,
                      "columns": pmeta.num_columns, "date_min": str(panel.date.min()), "date_max": str(panel.date.max()),
                      "grain": "minute × instrument (wide: 1 column per feature)", "feeds": "FactFeatureValue (unpivoted)",
                      "notes": f"{len(features)} model features; non-null feature values = {nonnull:,} of {pmeta.num_rows * len(features):,}"})
    inventory.append({"id": "S08", "source": "Feature descriptions", "path": "model_feature_list.xlsx → Features", "format": "Excel", "files": 1,
                      "rows": len(xl), "columns": xl.shape[1], "date_min": "", "date_max": "", "grain": "one feature", "feeds": "DimFeature",
                      "notes": f"{len(features) - len(missing_in_xlsx)} of 124 model features matched by exact name"})
    profile += column_profile("S07 panel (first 40 columns)", panel.iloc[:, :40])

    # 7. calendar
    cal = pd.read_csv(CALENDAR)
    inventory.append({"id": "S09", "source": "NSE holiday calendar", "path": rel(CALENDAR), "format": "CSV", "files": 1, "rows": len(cal),
                      "columns": cal.shape[1], "date_min": cal.iloc[:, 0].min(), "date_max": cal.iloc[:, 0].max(), "grain": "one holiday",
                      "feeds": "DimDate (holiday flags)", "notes": "2026 only; DimDate itself is generated"})

    # ------------------------------------------------------------------------------------------ targets
    sev = f.severity.value_counts().to_dict()
    rsev = g_res.severity.value_counts().to_dict()
    per_date = []
    for d in dates:
        g, v = g_res[g_res.date == d], verdicts[d]
        fc = fails[fails.date == d].gate_class.value_counts()
        per_date.append({"date": d, "verdict": v["status"], "datasets": g.dataset.nunique(), "checks": len(g),
                         "fail": int((g.severity == "fail").sum()), "warn": int((g.severity == "warn").sum()),
                         "unresolved_day": int(fc.get(dq_rules.FAIL_CLASSES[5], 0)), "unresolved_minutes": int(fc.get(dq_rules.FAIL_CLASSES[4], 0)),
                         "accepted_or_by_design": int(fc.get(dq_rules.FAIL_CLASSES[1], 0) + fc.get(dq_rules.FAIL_CLASSES[2], 0)),
                         "runs": ", ".join(v["runs"]), "reasons": " | ".join(v["reasons"])[:300]})
    per_date = pd.DataFrame(per_date)
    vcount = Counter(v["status"] for v in verdicts.values())
    all_dates = sorted(set(f.date) | set(trades.date.astype(str)) | {d[:10] for d in (m["ts_min_utc"] for m in model_rows)})
    targets = {
        "generated_utc": generated, "git_commit_at_generation": commit,
        "rule": "Pre-registered BEFORE any load. The warehouse must reproduce every number below exactly (or within the stated tolerance).",
        "FactDQFinding": {"rows": len(f), "runs": len(runs), "rows_by_run": f.run_id.value_counts().sort_index().to_dict(),
                          "rows_by_severity": sev, "datasets": int(f.dataset.nunique()), "checks": int(f.check.nunique())},
        "FactDQDailyStatus": {"date_dataset_pairs": int(g_res[["date", "dataset"]].drop_duplicates().shape[0]),
                              "dates": len(dates), "resolved_findings_rows": len(g_res), "resolved_rows_by_severity": rsev,
                              "day_verdicts": dict(vcount),
                              "excluded_dates": sorted(d for d, v in verdicts.items() if v["status"] == "exclude"),
                              "unchecked_dates": sorted(d for d, v in verdicts.items() if v["status"] == "unchecked"),
                              "fail_rows_by_gate_class": fails.gate_class.value_counts().to_dict(),
                              "minute_masks": masked,
                              "rule": "production gate (ml_pipeline/dq/gate.py) via bi/etl/dq_rules.py"},
        "FactModelPrediction": {"rows_by_horizon": {str(m["horizon_min"]): m["rows"] for m in model_rows},
                                "rows_total": sum(m["rows"] for m in model_rows),
                                "accuracy_by_horizon": {str(m["horizon_min"]): m["accuracy"] for m in model_rows}, "tolerance": 0.0005},
        "FactConfusion (DAX check)": {str(h): [r["count"] for r in cm_rows if r["horizon_min"] == h] for h in HORIZONS},
        "FactFeatureImportance": {"rows": len(imp_rows)},
        "FactTrade": {"rows": len(trades), "files": len(trade_rows),
                      "by_scenario": {f"{r['run']} | {r['label_mode']} | h{r['horizon_min']}m": {"trades": r["trades"], "opt_net_rs": r["opt_net_rs"], "costs_rs": r["costs_rs"]} for r in trade_rows}},
        "FactFeatureDrift": {"rows": len(dr), "reports": int(dr.report_date.nunique())},
        "FactFeatureValue": {"panel_rows": pmeta.num_rows, "features": len(features), "cells": pmeta.num_rows * len(features),
                             "non_null_rows_to_load": nonnull, "load_rule": "unpivot the 124 feature columns; load non-null values only"},
        "DimFeature": {"rows": len(features)},
        "DimDate": {"first_date": all_dates[0], "last_date": all_dates[-1]},
    }
    (BI / "docs" / "preregistered_targets.json").write_text(json.dumps(targets, indent=1, default=str))

    # ------------------------------------------------------------------------------------------ workbook
    write_workbook(generated, commit, inventory, checks, runs, per_date, model_rows, cm_rows, imp_rows, trade_rows, targets, profile, dr)
    fails = [c for c in checks if c["result"] == "FAIL"]
    print(f"checks: {len(checks)} run, {len(fails)} FAIL")
    for c in fails:
        print("  FAIL", c["check"], "| expected", c["expected"][:80], "| actual", c["actual"][:80], "|", c["note"][:100])
    print("targets:", json.dumps({k: v for k, v in targets.items() if k in ("FactDQFinding", "FactDQDailyStatus")}, default=str)[:600])


# ---------------------------------------------------------------------------------------------- workbook writer
FONT = "Arial"
NAVY = "1F3864"
HDR = (Font(name=FONT, size=10, bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY))
BODY = Font(name=FONT, size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
PASS_FILL, FAIL_FILL = PatternFill("solid", fgColor="E2EFDA"), PatternFill("solid", fgColor="F8CBAD")


def _lines(v, w):
    s = "" if v is None else str(v)
    return sum(max(1, math.ceil(len(p) / max(int(w * 1.15), 1))) for p in s.split("\n"))


def _sheet(wb, name, title, note, rows, widths=None, first=False):
    ws = wb.active if first else wb.create_sheet(name)
    ws.title = name
    ws.sheet_view.showGridLines = False
    ws["A1"] = title; ws["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    ws["A2"] = note; ws["A2"].font = Font(name=FONT, size=9, italic=True, color="595959")
    if not rows:
        return ws
    headers = list(rows[0].keys())
    widths = widths or [max(10, min(60, max(len(str(h)), *(len(str(r.get(h, ""))) for r in rows[:200])) + 2)) for h in headers]
    for j, h in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=4, column=j).column_letter].width = widths[j - 1]
        c = ws.cell(row=4, column=j, value=h)
        c.font, c.fill = HDR
        c.alignment, c.border = Alignment(wrap_text=True, vertical="top", horizontal="center"), BORDER
    for i, r in enumerate(rows, start=5):
        for j, h in enumerate(headers, start=1):
            v = r.get(h)
            v = json.dumps(v) if isinstance(v, (list, dict)) else v
            c = ws.cell(row=i, column=j, value=v)
            c.font, c.border, c.alignment = BODY, BORDER, Alignment(wrap_text=True, vertical="top")
            if h == "result":
                c.fill = PASS_FILL if v == "PASS" else FAIL_FILL
        ws.row_dimensions[i].height = 14 * max(_lines(r.get(h), widths[j - 1]) for j, h in enumerate(headers, start=1)) + 4
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:{ws.cell(row=4, column=len(headers)).column_letter}{4 + len(rows)}"
    return ws


def write_workbook(generated, commit, inventory, checks, runs, per_date, model_rows, cm_rows, imp_rows, trade_rows, targets, profile, dr):
    wb = Workbook()
    n_fail = sum(c["result"] == "FAIL" for c in checks)
    readme = [
        {"item": "What this is", "value": "Profile of every source feeding the BI warehouse (step 1.2), the local-vs-GCS DQ history comparison (1.3), "
                                          "integrity and consistency checks, and the PRE-REGISTERED reconciliation targets (1.6)."},
        {"item": "Generated", "value": f"{generated} by bi/etl/profile_sources.py at git commit {commit}"},
        {"item": "Checks", "value": f"{len(checks)} run · {len(checks) - n_fail} PASS · {n_fail} FAIL (see 'Checks')"},
        {"item": "Rule", "value": "Targets are frozen before any load. After loading, the warehouse must reproduce them exactly "
                                  "(or within the stated tolerance). The JSON copy is bi/docs/preregistered_targets.json."},
        {"item": "Read-only", "value": "Nothing in the trading system or GCS was changed. GCS files were copied to bi/source_snapshot/ (gitignored)."},
    ]
    _sheet(wb, "Read me", "Phase 1 — Source profile and pre-registered targets", "Generated file: do not edit by hand; re-run the script.", readme, [18, 120], first=True)
    _sheet(wb, "Source inventory", "Source inventory (step 1.2)", "One row per source.", inventory,
           [5, 30, 42, 16, 6, 10, 8, 11, 11, 26, 30, 48])
    _sheet(wb, "Checks", "Integrity and consistency checks", "Every check compares two independent sources or a copy against its frozen checksum.",
           checks, [52, 26, 26, 8, 56])
    tgt_rows = []
    for table, spec in targets.items():
        if isinstance(spec, dict):
            for k, v in spec.items():
                tgt_rows.append({"table": table, "measure": k, "target": json.dumps(v, default=str) if isinstance(v, (dict, list)) else v})
    _sheet(wb, "Targets (pre-registered)", "Pre-registered reconciliation targets (step 1.6)", f"Frozen {generated}, commit {commit}. Do not change after loading starts.",
           tgt_rows, [26, 30, 110])
    _sheet(wb, "DQ runs", "DQ runs", "One row per DQ run copied from GCS.", runs)
    _sheet(wb, "DQ by date (resolved)", "DQ by date after 'latest run wins'", "Target for FactDQDailyStatus per date.", per_date.to_dict("records"))
    _sheet(wb, "Model (production)", "Production model evaluation recomputed from out-of-fold predictions", "Must match the frozen reports (see Checks).", model_rows)
    _sheet(wb, "Confusion matrices", "Confusion matrices (actual × predicted)", "Target for the DAX confusion matrix.", cm_rows)
    _sheet(wb, "Feature importance", "Top permutation importances (last fold)", "From the frozen reports.", imp_rows)
    _sheet(wb, "Trades by scenario", "Backtest trade files", "Control totals per file = targets for FactTrade.", trade_rows)
    _sheet(wb, "Drift rows", "Drift report rows", "Parsed from the weekly drift reports.", dr.to_dict("records"))
    _sheet(wb, "Column profile", "Column profile", "Per column: type, null/blank %, distinct values, min, max, example.", profile,
           [26, 26, 14, 9, 10, 9, 22, 22, 30])
    out = BI / "docs" / "Phase1_Source_Profile.xlsx"
    wb.save(out)
    print("saved", rel(out))


if __name__ == "__main__":
    main()
