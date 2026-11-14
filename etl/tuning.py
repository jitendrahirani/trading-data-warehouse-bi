"""Phase 5 (steps 5.2-5.4): SQL tuning experiments with ACTUAL execution plans, written to bi/docs/Phase5_SQL_Tuning.xlsx.

    bi_env python bi/etl/tuning.py              # all experiments (about 5-15 minutes on this laptop)
    bi_env python bi/etl/tuning.py --keep-copy  # keep the 8.7M-row rowstore copy (tune.FeatureValue_Rowstore) afterwards

How a query is measured: SET STATISTICS XML ON returns the actual plan with every run. From it we take logical reads
(summed over all operators and threads, including LOB reads of columnstore segments), CPU and elapsed time
(QueryTimeStats), estimated vs actual rows of the root operator, the physical operators and their execution mode
(Row or Batch), columnstore segments read/skipped, and SQL Server's own missing-index suggestion. Each query runs 3
times; times are the best of runs 2-3 (warm cache, compiled plan); reads are the same on every run.
LOGICAL READS ARE THE PRIMARY METRIC: they count the pages a query touched and do not depend on what else the laptop
is doing. Elapsed times on this 7.7 GB laptop vary with memory pressure (Edge), so they are reported but secondary.
The .sqlplan files in bi/docs/plans open in SSMS (graphical plan).
Touches only TradingBI_Dev: creates/drops the tuning index for the before/after, the tune.* demo objects, and switches
PARAMETER_SENSITIVE_PLAN_OPTIMIZATION off and back on for the parameter-sniffing demo.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

import analytics_check
import apply_ddl
import db

BI = Path(__file__).resolve().parents[1]
PLANS = BI / "docs" / "plans"
OUT = BI / "docs" / "Phase5_SQL_Tuning.xlsx"
NS = {"p": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}
SHOWPLAN_COL = "Microsoft SQL Server 2005 XML Showplan"


# ------------------------------------------------------------------------------------------------ measuring
def parse_plan(xml: str) -> dict:
    root = ET.fromstring(xml)
    stmts = [s for s in root.iter(f"{{{NS['p']}}}StmtSimple") if s.find("p:QueryPlan", NS) is not None]
    st = stmts[-1]
    qp = st.find("p:QueryPlan", NS)
    qts = qp.find("p:QueryTimeStats", NS)
    counters = list(qp.iter(f"{{{NS['p']}}}RunTimeCountersPerThread"))
    num = lambda e, a: int(float(e.get(a, 0) or 0))
    reads = sum(num(c, "ActualLogicalReads") + num(c, "ActualLobLogicalReads") for c in counters)
    # the top operator that has runtime counters (a Compute Scalar at the top often has none: it is evaluated lazily)
    rel = next(r for r in qp.iter(f"{{{NS['p']}}}RelOp") if r.find("p:RunTimeInformation", NS) is not None)
    act_root = sum(num(c, "ActualRows") for c in rel.find("p:RunTimeInformation", NS).findall("p:RunTimeCountersPerThread", NS))
    ops, modes = [], set()
    for r in qp.iter(f"{{{NS['p']}}}RelOp"):
        op = r.get("PhysicalOp")
        if op not in ("Compute Scalar", "Parallelism") and op not in ops:
            ops.append(op)
        ri = r.find("p:RunTimeInformation", NS)
        if ri is not None:
            modes |= {c.get("ActualExecutionMode") for c in ri.findall("p:RunTimeCountersPerThread", NS) if c.get("ActualExecutionMode")}
    mi = qp.find(".//p:MissingIndexGroup", NS)
    missing = None
    if mi is not None:
        cols = [f"{g.get('Usage')}: " + ", ".join(c.get("Name").strip("[]") for c in g.findall("p:Column", NS))
                for g in mi.iter(f"{{{NS['p']}}}ColumnGroup")]
        missing = f"impact {float(mi.get('Impact')):.0f}% - " + "; ".join(cols)
    return {"reads": reads, "cpu_ms": num(qts, "CpuTime") if qts is not None else None,
            "elapsed_ms": num(qts, "ElapsedTime") if qts is not None else None,
            "est_rows": round(float(rel.get("EstimateRows"))), "act_rows": act_root, "operators": " > ".join(ops),
            "mode": "/".join(sorted(modes)) or "", "segments_read": sum(num(c, "SegmentReads") for c in counters),
            "segments_skipped": sum(num(c, "SegmentSkips") for c in counters), "missing_index": missing,
            "psp_dispatcher": qp.find(".//p:Dispatcher", NS) is not None or st.get("QueryVariantID") is not None,
            "dop": num(qp, "DegreeOfParallelism")}


def io_stats(messages: list[str]) -> dict:
    """Sum of SET STATISTICS IO over every table of the statement (worktables included: they are real work).
    Columnstore segments are LOB pages, so 'lob logical reads' must be added: the plan XML reports 0 for them."""
    text = " ".join(messages)
    s = lambda pat: sum(int(x) for x in re.findall(pat, text))
    return {"io_logical": s(r"(?<!lob )logical reads (\d+)"), "io_lob_logical": s(r"lob logical reads (\d+)"),
            "io_segment_reads": s(r"Segment reads (\d+)"), "io_segment_skipped": s(r"segment skipped (\d+)")}


def execute_with_plan(conn, sql: str, params=()) -> tuple[int, str, list[str]]:
    cur = conn.cursor()
    cur.execute("SET STATISTICS XML ON; SET STATISTICS IO ON;")
    cur.execute(sql, *params)
    rows, plan, msgs = 0, None, []
    while True:
        msgs += [m[1] for m in (cur.messages or [])]
        if cur.description:
            if cur.description[0][0] == SHOWPLAN_COL:
                plan = cur.fetchone()[0]
            else:
                rows += len(cur.fetchall())
        if not cur.nextset():
            break
    msgs += [m[1] for m in (cur.messages or [])]
    cur.execute("SET STATISTICS XML OFF; SET STATISTICS IO OFF;")
    return rows, plan, msgs


def measure(conn, sql: str, name: str, params=(), runs: int = 3) -> dict:
    out = []
    for _ in range(runs):
        rows, plan, msgs = execute_with_plan(conn, sql, params)
        m = parse_plan(plan)
        io = io_stats(msgs)
        m["plan_reads"] = m.pop("reads")
        m["reads"] = io["io_logical"] + io["io_lob_logical"]          # the primary metric (as SSMS shows it)
        m.update(io)
        m["rows_returned"] = rows
        out.append((m, plan))
    warm = out[1:] if runs > 1 else out
    best = min(warm, key=lambda x: x[0]["elapsed_ms"] or 0)
    m, plan = best
    m["cpu_ms"] = min(x[0]["cpu_ms"] for x in warm)
    PLANS.mkdir(parents=True, exist_ok=True)
    (PLANS / f"{name}.sqlplan").write_text(plan, encoding="utf-8")
    m["plan_file"] = f"bi/docs/plans/{name}.sqlplan"
    print(f"   {name:34s} reads {m['reads']:>9,}  seg {m['io_segment_reads']}/{m['io_segment_skipped']}  cpu {m['cpu_ms']:>6} ms  elapsed {m['elapsed_ms']:>6} ms  "
          f"est/act rows {m['est_rows']:>7,}/{m['act_rows']:<7,}  {m['mode']:<9} {m['operators'][:70]}")
    return m


def scalar(conn, sql: str, *p):
    r = conn.cursor().execute(sql, *p).fetchone()
    return None if r is None else r[0]


# ------------------------------------------------------------------------------------------------ experiments
def e2_missing_index(conn) -> list[dict]:
    print("E2 missing index: one date's confusion matrix from 332,275 predictions")
    sql = ("SELECT ActualClassKey, PredictedClassKey, COUNT(*) AS n FROM dw.FactModelPrediction "
           "WHERE DateKey = 20260701 GROUP BY ActualClassKey, PredictedClassKey")
    conn.cursor().execute("DROP INDEX IF EXISTS IX_FactModelPrediction_Date ON dw.FactModelPrediction")
    before = measure(conn, sql, "E2_before_no_index")
    db.run_script(conn, db.SQL_DIR / "32_tuning.sql")
    after = measure(conn, sql, "E2_after_index_on_DateKey")
    return [dict(exp="E2", title="Missing index", variant="before: no index on DateKey (clustered index scan)", sql=sql, **before),
            dict(exp="E2", title="Missing index", variant="after: IX_FactModelPrediction_Date (DateKey) INCLUDE (...)", sql=sql, **after)]


def e1_sargable(conn) -> list[dict]:
    print("E1 non-SARGable predicate: one IST day of the headline model's NIFTY predictions")
    run = scalar(conn, "SELECT ModelRunKey FROM dw.DimModelRun WHERE IsHeadlineRun = 1")
    base = f"SELECT COUNT(*) AS n, AVG(CAST(IsCorrect AS float)) AS accuracy FROM dw.FactModelPrediction WHERE ModelRunKey = {run} AND InstrumentKey = 1 AND "
    variants = [
        ("E1_before_function_on_column", "before: CONVERT(date, DATEADD(MINUTE, 330, TsUtc)) = '2026-07-01' (function on the column)",
         base + "CONVERT(date, DATEADD(MINUTE, 330, TsUtc)) = '2026-07-01'"),
        ("E1_after_range_on_column", "after: TsUtc >= '2026-06-30 18:30' AND TsUtc < '2026-07-01 18:30' (bare column, range)",
         base + "TsUtc >= '2026-06-30T18:30:00' AND TsUtc < '2026-07-01T18:30:00'"),
        ("E1_after_datekey", "after (warehouse way): DateKey = 20260701 (the IST date key, indexed in E2)",
         base + "DateKey = 20260701"),
    ]
    return [dict(exp="E1", title="Non-SARGable predicate", variant=v, sql=s, **measure(conn, s, n)) for n, v, s in variants]


def e3_columnstore(conn, keep: bool) -> tuple[list[dict], list[dict]]:
    print("E3 rowstore vs columnstore: the same 8,749,765 rows twice")
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS tune.FeatureValue_Rowstore")
    cur.execute("SELECT DateKey, TimeKey, InstrumentKey, FeatureKey, FeatureValue, EtlRunId INTO tune.FeatureValue_Rowstore "
                "FROM dw.FactFeatureValue OPTION (MAXDOP 2)")
    cur.execute("CREATE UNIQUE CLUSTERED INDEX CX_FeatureValue_Rowstore ON tune.FeatureValue_Rowstore (DateKey, InstrumentKey, FeatureKey, TimeKey) "
                "WITH (MAXDOP = 2)")
    sizes = []
    for t in ("dw.FactFeatureValue", "tune.FeatureValue_Rowstore"):
        mb = scalar(conn, "SELECT SUM(a.total_pages) * 8 / 1024.0 FROM sys.partitions p JOIN sys.allocation_units a ON a.container_id = p.partition_id "
                          "WHERE p.object_id = OBJECT_ID(?)", t)
        sizes.append({"table": t, "storage": "clustered columnstore" if t.startswith("dw") else "rowstore, clustered index (DateKey, InstrumentKey, FeatureKey, TimeKey)",
                      "rows": scalar(conn, f"SELECT COUNT_BIG(*) FROM {t}"), "size_mb": round(float(mb), 1)})
    sizes.append({"table": "ratio", "storage": "rowstore / columnstore", "rows": None, "size_mb": round(sizes[1]["size_mb"] / sizes[0]["size_mb"], 1)})
    rows = []
    for qname, q in [("full", "SELECT FeatureKey, COUNT_BIG(*) AS n, AVG(FeatureValue) AS avg_v, MIN(FeatureValue) AS min_v, MAX(FeatureValue) AS max_v FROM {t} GROUP BY FeatureKey"),
                     ("week", "SELECT FeatureKey, AVG(FeatureValue) AS avg_v FROM {t} WHERE DateKey BETWEEN 20260701 AND 20260707 AND InstrumentKey = 1 GROUP BY FeatureKey")]:
        for label, t in (("columnstore", "dw.FactFeatureValue"), ("rowstore", "tune.FeatureValue_Rowstore")):
            s = q.format(t=t)
            m = measure(conn, s, f"E3_{qname}_{label}")
            what = "all 8.7M rows: per-feature count/avg/min/max" if qname == "full" else "one week, one instrument: per-feature average"
            rows.append(dict(exp="E3", title="Rowstore vs columnstore", variant=f"{label}: {what}", sql=s, **m))
    if not keep:
        cur.execute("DROP TABLE tune.FeatureValue_Rowstore")
    return rows, sizes


def e4_segment_elimination(conn) -> list[dict]:
    print("E4 columnstore segment elimination")
    fk = scalar(conn, "SELECT FeatureKey FROM dw.DimFeature WHERE FeatureName = 'atm_iv'")
    variants = [("E4_filter_on_DateKey", "filter on DateKey (row groups are in date order): most segments skipped",
                 "SELECT AVG(FeatureValue) AS avg_v FROM dw.FactFeatureValue WHERE DateKey = 20260701"),
                ("E4_filter_on_FeatureKey", "filter on FeatureKey (every row group holds every feature): no segment can be skipped",
                 f"SELECT AVG(FeatureValue) AS avg_v FROM dw.FactFeatureValue WHERE FeatureKey = {fk}")]
    return [dict(exp="E4", title="Segment elimination", variant=v, sql=s, **measure(conn, s, n)) for n, v, s in variants]


def e5_parameter_sniffing(conn) -> list[dict]:
    print("E5 parameter sniffing on tune.usp_FindingsBySeverity (pass = 24,194 rows, fail = 79)")
    cur = conn.cursor()
    rows = []

    def call(proc, key, label, scenario):
        m = measure(conn, f"EXEC {proc} @SeverityKey = ?", f"E5_{label}", params=(key,), runs=1)
        rows.append(dict(exp="E5", title="Parameter sniffing", variant=scenario, sql=f"EXEC {proc} @SeverityKey = {key}", **m))

    try:
        cur.execute("ALTER DATABASE SCOPED CONFIGURATION SET PARAMETER_SENSITIVE_PLAN_OPTIMIZATION = OFF")
        cur.execute("EXEC sp_recompile 'tune.usp_FindingsBySeverity'")
        call("tune.usp_FindingsBySeverity", 3, "a1_fail_first", "A. PSP off. 1st call with fail (79 rows) compiles the plan")
        call("tune.usp_FindingsBySeverity", 0, "a2_pass_reuses", "A. then pass (24,194 rows) REUSES the fail plan")
        cur.execute("EXEC sp_recompile 'tune.usp_FindingsBySeverity'")
        call("tune.usp_FindingsBySeverity", 0, "b1_pass_first", "B. PSP off. 1st call with pass compiles the plan")
        call("tune.usp_FindingsBySeverity", 3, "b2_fail_reuses", "B. then fail REUSES the pass plan")
        call("tune.usp_FindingsBySeverity_Recompile", 3, "c1_recompile_fail", "C. fix OPTION (RECOMPILE): fail")
        call("tune.usp_FindingsBySeverity_Recompile", 0, "c2_recompile_pass", "C. fix OPTION (RECOMPILE): pass")
        cur.execute("EXEC sp_recompile 'tune.usp_FindingsBySeverity_Unknown'")
        call("tune.usp_FindingsBySeverity_Unknown", 3, "d1_unknown_fail", "D. fix OPTIMIZE FOR UNKNOWN: fail")
        call("tune.usp_FindingsBySeverity_Unknown", 0, "d2_unknown_pass", "D. fix OPTIMIZE FOR UNKNOWN: pass")
    finally:
        cur.execute("ALTER DATABASE SCOPED CONFIGURATION SET PARAMETER_SENSITIVE_PLAN_OPTIMIZATION = ON")
    cur.execute("EXEC sp_recompile 'tune.usp_FindingsBySeverity'")
    call("tune.usp_FindingsBySeverity", 3, "e1_psp_fail", "E. PSP on (SQL Server 2022+ default): fail")
    call("tune.usp_FindingsBySeverity", 0, "e2_psp_pass", "E. PSP on: then pass")
    return rows


def statistics_health(conn) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute("""
        SELECT OBJECT_SCHEMA_NAME(s.object_id) + '.' + OBJECT_NAME(s.object_id) AS [table], s.name AS [statistics],
               IIF(s.auto_created = 1, 'auto-created (column)', 'index') AS kind, sp.rows, sp.rows_sampled,
               CAST(100.0 * sp.rows_sampled / NULLIF(sp.rows, 0) AS decimal(5, 1)) AS sampled_pct, sp.modification_counter,
               sp.last_updated
        FROM sys.stats s CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
        WHERE OBJECT_SCHEMA_NAME(s.object_id) = 'dw' AND OBJECT_NAME(s.object_id) LIKE 'Fact%' AND sp.rows > 10000
        ORDER BY sp.rows DESC, [statistics]""")
    cols = [c[0] for c in cur.description]
    return pd.DataFrame.from_records(cur.fetchall(), columns=cols)


# ------------------------------------------------------------------------------------------------ workbook
def write_workbook(log: list[dict], sizes: list[dict], stats: pd.DataFrame, analytics: list[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    lines = [
        ("Phase 5: SQL Server tuning log", apply_ddl.TITLE),
        (f"Generated {datetime.now():%Y-%m-%d %H:%M} by bi/etl/tuning.py on TradingBI_Dev (SQL Server 2025 Developer, compatibility level 170, "
         "max server memory 1,536 MB, 8 logical CPUs).", apply_ddl.NOTE),
        ("", apply_ddl.BODY),
        ("How to read it", Font(name="Arial", bold=True, size=11)),
        ("Reads = logical reads + LOB logical reads from SET STATISTICS IO (8 KB pages the query touched, as SSMS shows them). The primary metric: it does not "
         "depend on what else the laptop is doing. Columnstore segments are LOB pages; the plan XML reports 0 for them, so STATISTICS IO is used.", apply_ddl.BODY),
        ("CPU / elapsed ms = best of runs 2-3 (warm cache). Secondary: on this 7.7 GB laptop elapsed time moves with memory pressure.", apply_ddl.BODY),
        ("Est / actual rows = the optimiser's estimate vs what really came out of the top operator. Big gaps mean bad estimates and often bad plans.", apply_ddl.BODY),
        ("Mode = Row (one row at a time) or Batch (about 900 rows at a time, vectorised; columnstore and, from SQL 2019, rowstore too).", apply_ddl.BODY),
        ("Plan file = actual execution plan; open it in SSMS (File > Open) to see the graphical plan.", apply_ddl.BODY),
        ("", apply_ddl.BODY),
        ("Sheets", Font(name="Arial", bold=True, size=11)),
        ("Tuning log: every experiment, before and after. Columnstore: size comparison. Statistics: health of the statistics on the big facts.", apply_ddl.BODY),
        ("Analytic queries: the six CTE / window-function queries (step 5.2) and their check against pandas.", apply_ddl.BODY),
    ]
    for i, (text, font) in enumerate(lines, 1):
        c = ws.cell(row=i, column=1, value=text)
        c.font, c.alignment = font, Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 140

    df = pd.DataFrame(log)
    df["reads_vs_first"] = None
    for exp, g in df.groupby("exp", sort=False):
        if exp in ("E1", "E2"):
            first = g["reads"].iloc[0]
            df.loc[g.index, "reads_vs_first"] = [f"{first / r:,.1f}x fewer" if r and r < first else ("baseline" if i == 0 else f"{r / first:,.1f}x more")
                                                 for i, r in enumerate(g["reads"])]
    cols = ["exp", "title", "variant", "reads", "reads_vs_first", "io_logical", "io_lob_logical", "cpu_ms", "elapsed_ms", "est_rows", "act_rows", "rows_returned", "mode",
            "operators", "io_segment_reads", "io_segment_skipped", "dop", "missing_index", "psp_dispatcher", "plan_file", "sql"]
    apply_ddl._write(wb.create_sheet("Tuning log"), "Tuning log", "One row per measured query. Read the 'reads' column first.",
                     df[cols], {"exp": 6, "title": 18, "variant": 46, "reads": 11, "reads_vs_first": 13, "cpu_ms": 8, "elapsed_ms": 9,
                                "est_rows": 10, "act_rows": 10, "rows_returned": 9, "mode": 9, "operators": 44, "io_logical": 10, "io_lob_logical": 11, "io_segment_reads": 9,
                                "io_segment_skipped": 9, "dop": 5, "missing_index": 40, "psp_dispatcher": 9, "plan_file": 34, "sql": 70})
    apply_ddl._write(wb.create_sheet("Columnstore"), "Rowstore vs columnstore: size", "The same 8,749,765 rows stored two ways.",
                     pd.DataFrame(sizes), {"table": 30, "storage": 60, "rows": 12, "size_mb": 10})
    apply_ddl._write(wb.create_sheet("Statistics"), "Statistics health (facts over 10,000 rows)",
                     "rows_sampled = rows read to build the histogram; modification_counter = rows changed since. Auto-update fires after about sqrt(1000 x rows) changes.",
                     stats, {"table": 26, "statistics": 34, "kind": 20, "rows": 11, "rows_sampled": 12, "sampled_pct": 10,
                             "modification_counter": 12, "last_updated": 20})
    a = pd.DataFrame([{k: v for k, v in r.items() if k != "result"} for r in analytics])
    a["result"] = ["MATCH" if d == 0 else "DIFFERENT" for d in a["differences"]]
    apply_ddl._write(wb.create_sheet("Analytic queries"), "Analytic queries (step 5.2)",
                     "Each SQL result compared cell by cell with the same calculation in pandas from the source files (bi/etl/analytics_check.py).",
                     a[["query", "sql_rows", "pandas_rows", "differences", "result", "sql"]],
                     {"query": 28, "sql_rows": 9, "pandas_rows": 11, "differences": 11, "result": 10, "sql": 110})
    wb.save(OUT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-copy", action="store_true")
    a = ap.parse_args()
    conn = db.connect("dev", autocommit=True)
    db.run_script(conn, db.SQL_DIR / "32_tuning.sql")
    log = []
    log += e2_missing_index(conn)
    log += e1_sargable(conn)
    e3, sizes = e3_columnstore(conn, a.keep_copy)
    log += e3
    log += e4_segment_elimination(conn)
    log += e5_parameter_sniffing(conn)
    stats = statistics_health(conn)
    print("analytic queries vs pandas")
    analytics = analytics_check.run_all(conn)
    for r in analytics:
        print(f"   {r['query']:28s} {'MATCH' if r['differences'] == 0 else 'DIFFERENT'}")
    write_workbook(log, sizes, stats, analytics)
    print(f"wrote {OUT.relative_to(BI.parent)} and {len(list(PLANS.glob('*.sqlplan')))} plan files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
