"""Phase 5 step 5.2: run the six analytic queries (bi/sql/30_analytics.sql) and check each against pandas.

The pandas side is computed independently from the SOURCE files (findings CSVs, trade CSVs, the holiday seed) with the
production gate code for FAIL classes - it never reads the warehouse. Equal results mean the SQL (window frames, ranking,
gaps-and-islands) is right, not merely self-consistent.

    bi_env python bi/etl/analytics_check.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import db
import dq_rules
import extract

SQL_FILE = db.SQL_DIR / "30_analytics.sql"
UNRESOLVED = {dq_rules.FAIL_CLASSES[4], dq_rules.FAIL_CLASSES[5]}


def queries() -> dict[str, str]:
    text = SQL_FILE.read_text(encoding="utf-8")
    parts = re.split(r"^-- name: (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def run_sql(conn, sql: str) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


# ------------------------------------------------------------------------------------------------ pandas references
def _dq_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Resolved findings (latest run wins) with the gate class of every FAIL, and per-date totals."""
    _, findings = extract.read_dq()
    res = extract.latest_run_wins(findings.df)
    decisions = dq_rules.load_decisions()
    fails = res["severity"] == "fail"
    res["gate_class"] = ""
    res.loc[fails, "gate_class"] = [dq_rules.classify_fail(r, r["date"], decisions) for _, r in res[fails].iterrows()]
    res["unresolved"] = res["gate_class"].isin(UNRESOLVED).astype(int)
    daily = res.groupby("date").agg(unresolved=("unresolved", "sum"), checks=("severity", "size"),
                                    good=("severity", lambda s: s.isin(["pass", "note"]).sum())).reset_index()
    daily["DateKey"] = daily["date"].str.replace("-", "").astype(int)
    return res, daily.sort_values("DateKey").reset_index(drop=True)


def trading_index(dates: pd.Series) -> pd.Series:
    """Trading-day number from weekdays minus the NSE holiday seed (independent of DimDate)."""
    hol = pd.read_csv(extract.CALENDAR, dtype=str)
    hol = np.array(hol.loc[hol["market_status"] == "holiday", "holiday_date"].tolist(), dtype="datetime64[D]")
    d = np.array(pd.Series(dates).tolist(), dtype="datetime64[D]")
    return pd.Series(np.busday_count(np.datetime64("2026-04-01"), d, holidays=hol) + 1, index=dates.index)


def ref_q1(res, daily):
    out = daily[["DateKey", "unresolved", "checks"]].copy()
    out["unresolved_20d"] = out["unresolved"].rolling(20, min_periods=1).sum().astype(int)
    out["checks_20d"] = out["checks"].rolling(20, min_periods=1).sum().astype(int)
    out["fail_rate_20d"] = (out["unresolved_20d"] / out["checks_20d"]).round(8)
    return out


def ref_q2(res, daily):
    out = pd.DataFrame({"DateKey": daily["DateKey"], "pass_rate": (daily["good"] / daily["checks"])})
    out["change_vs_previous"] = out["pass_rate"].diff().round(8)
    out["pass_rate"] = out["pass_rate"].round(8)
    return out


def ref_q3(res, daily):
    p = res[res["severity"].isin(["warn", "fail"])].groupby("dataset").size().rename("warn_fail").reset_index()
    p["rnk"] = p["warn_fail"].rank(method="min", ascending=False).astype(int)
    p = p[p["rnk"] <= 10].rename(columns={"dataset": "DatasetCode"})
    return p.sort_values(["rnk", "DatasetCode"]).reset_index(drop=True)


def ref_q4(res, daily):
    t = extract.read_trades().df.copy()
    t["exit"] = pd.to_datetime(t["exit_ts"], utc=True)
    t["net"] = t["opt_net_rs"].astype(float)
    t["RunCode"] = (t["run_set"].str.replace("production ", "").str.replace("research ", "r") + ":" + t["label_mode"] + ":h"
                    + t["horizon_min"].astype(str) + "m")
    rows = []
    for code, g in t.sort_values(["exit", "SourceRowNo"]).groupby("RunCode", sort=True):
        cum = g["net"].cumsum().round(2)
        peak = cum.cummax().clip(lower=0)
        rows.append({"RunCode": code, "trades": len(g), "best_cum": cum.max(), "max_drawdown": round((cum - peak).min(), 2),
                     "final_net": round(g["net"].sum(), 2)})
    return pd.DataFrame(rows)


def ref_q5(res, daily):
    f = res[res["severity"] == "fail"][["dataset", "date"]].drop_duplicates()
    f["tdi"] = trading_index(f["date"])
    f = f.sort_values(["dataset", "tdi"])
    f["island"] = f["tdi"] - f.groupby("dataset").cumcount()
    f["DateKey"] = f["date"].str.replace("-", "").astype(int)
    out = f.groupby(["dataset", "island"]).agg(first_day=("DateKey", "min"), last_day=("DateKey", "max"),
                                                trading_days=("DateKey", "size")).reset_index()
    return out.rename(columns={"dataset": "DatasetCode"}).drop(columns="island").sort_values(["DatasetCode", "first_day"]).reset_index(drop=True)


def ref_q6(res, daily):
    d = daily[["DateKey", "unresolved", "date"]].copy()
    d["tdi"] = trading_index(d["date"])
    last = d["tdi"].where(d["unresolved"] > 0).ffill()
    d["days_since_unresolved"] = (d["tdi"] - last)
    return d[["DateKey", "unresolved", "days_since_unresolved"]]


REFS = {"q1_rolling_fail_rate": ref_q1, "q2_pass_rate_day_over_day": ref_q2, "q3_worst_datasets": ref_q3,
        "q4_drawdown": ref_q4, "q5_fail_islands": ref_q5, "q6_days_since_unresolved": ref_q6}


def compare(sql_df: pd.DataFrame, ref: pd.DataFrame) -> int:
    """Number of differing cells (after aligning columns, rounding numbers to 6 decimals)."""
    if list(sql_df.columns) != list(ref.columns) or len(sql_df) != len(ref):
        return max(len(sql_df), len(ref)) * max(len(sql_df.columns), len(ref.columns))
    a, b = sql_df.reset_index(drop=True), ref.reset_index(drop=True)
    diff = 0
    for c in a.columns:
        x, y = a[c], b[c]
        if pd.api.types.is_numeric_dtype(y) or x.map(lambda v: hasattr(v, "as_integer_ratio") or str(type(v)) == "<class 'decimal.Decimal'>").all():
            xf, yf = pd.to_numeric(x, errors="coerce").astype(float).round(6), pd.to_numeric(y, errors="coerce").astype(float).round(6)
            diff += int(((xf != yf) & ~(xf.isna() & yf.isna())).sum())
        else:
            diff += int((x.astype(str) != y.astype(str)).sum())
    return diff


def run_all(conn=None) -> list[dict]:
    conn = conn or db.connect("dev", autocommit=True)
    res, daily = _dq_frames()
    out = []
    for name, sql in queries().items():
        got = run_sql(conn, sql)
        ref = REFS[name](res, daily)
        out.append({"query": name, "sql_rows": len(got), "pandas_rows": len(ref), "differences": compare(got, ref),
                    "sql": sql, "result": got})
    return out


def main() -> int:
    bad = 0
    for r in run_all():
        status = "MATCH" if r["differences"] == 0 else "DIFFERENT"
        bad += r["differences"] != 0
        print(f"{r['query']:28s} SQL {r['sql_rows']:>4} rows | pandas {r['pandas_rows']:>4} rows | {status} ({r['differences']} differing cells)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
