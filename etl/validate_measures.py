"""Phase 6 step 6.3 'done when': every measure validated against SQL for 3 filter combinations.

    bi_env python bi/etl/validate_measures.py            # Power BI Desktop must have the model open
    bi_env python bi/etl/validate_measures.py --port N   # if more than one Power BI file is open

Runs the DAX query of every validation case in YOUR open Power BI Desktop model (read-only, via bi/etl/run_dax.ps1),
recomputes the SQL twin from the rpt views at the same moment, compares them and writes
bi/docs/Phase6_Measure_Validation.xlsx. Exit code 0 only when every case matches.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

import apply_ddl
import db
import semantic_model as sm

BI = Path(__file__).resolve().parents[1]
OUT = BI / "docs" / "Phase6_Measure_Validation.xlsx"


def same(m: sm.Measure, dax, sql) -> bool:
    if m.name in sm.ZERO_WHEN_EMPTY and sql is None:        # SQL SUM over no rows is NULL; the measure returns 0 by design
        sql = 0
    if dax in (None, "") and sql in (None, ""):
        return True
    if dax in (None, "") or sql in (None, ""):
        return False
    if isinstance(sql, datetime) or m.fmt.startswith("yyyy"):
        return str(dax)[:19].replace("T", " ") == str(sql)[:19].replace("T", " ")
    if isinstance(sql, str):
        if m.name == "Worst Dataset":
            return set(str(dax).split(", ")) == set(sql.split(", "))
        return str(dax) == sql
    a, b = float(dax), float(sql if not isinstance(sql, Decimal) else float(sql))
    tol = 0.005 if m.fmt == sm.N2 else 1e-6 * max(1.0, abs(b))
    return abs(a - b) <= tol


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    cases = [(m, k, f) for m in sm.MEASURES for k, f in enumerate(sm.cases_for(m), 1)]
    with tempfile.TemporaryDirectory() as tmp:
        qf, rf = Path(tmp) / "q.json", Path(tmp) / "r.json"
        qf.write_text(json.dumps([{"id": i, "dax": sm.dax_query(m, f)} for i, (m, k, f) in enumerate(cases)]), encoding="utf-8")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("run_dax.ps1")),
                            "-QueryFile", str(qf), "-OutFile", str(rf), "-Port", str(a.port)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)   # PowerShell must not wait for console input
        if p.returncode != 0:
            print(p.stdout, p.stderr)
            return 2
        print(p.stdout.strip())
        res = {r["id"]: r for r in json.loads(rf.read_text(encoding="utf-8-sig"))}
    conn = db.connect("dev", autocommit=True)
    rows = []
    for i, (m, k, f) in enumerate(cases):
        r = conn.cursor().execute(sm.measure_sql(m, f)).fetchone()
        sql_v = None if r is None else r[0]
        d = res[i]
        ok = d["error"] is None and same(m, d["value"], sql_v)
        rows.append({"measure": m.name, "case": k, "filters": "; ".join(f"{t}[{c}] = {v}" for t, c, v in f) or "(no filter)",
                     "DAX value": d["value"], "SQL value": float(sql_v) if isinstance(sql_v, Decimal) else (str(sql_v) if isinstance(sql_v, datetime) else sql_v),
                     "result": "MATCH" if ok else "DIFFERENT", "DAX error": d["error"], "DAX query": sm.dax_query(m, f)})
    df = pd.DataFrame(rows)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    n_ok = int((df["result"] == "MATCH").sum())
    ws["A1"], ws["A1"].font = "Phase 6: measure validation (DAX in Power BI vs SQL)", apply_ddl.TITLE
    ws["A2"], ws["A2"].font = f"Run {datetime.now():%Y-%m-%d %H:%M}: {n_ok} of {len(df)} cases match ({df['measure'].nunique()} measures x 3 filter combinations).", apply_ddl.BODY
    ws.column_dimensions["A"].width = 120
    apply_ddl._write(wb.create_sheet("Cases"), "Every case", "Tolerance: 0.005 for rupee amounts, otherwise 1 part in a million.", df,
                     {"measure": 26, "case": 6, "filters": 48, "DAX value": 18, "SQL value": 18, "result": 11, "DAX error": 40, "DAX query": 80})
    wb.save(OUT)
    print(f"{n_ok} of {len(df)} cases match; wrote {OUT.relative_to(BI.parent)}")
    for r in df[df["result"] != "MATCH"].itertuples():
        print(f"   DIFFERENT  {r.measure} case {r.case}: DAX {r._4!r} vs SQL {r._5!r} {r._7 or ''}")
    return 0 if n_ok == len(df) else 1


if __name__ == "__main__":
    sys.exit(main())
