"""Phase 6: compare YOUR open Power BI model with the spec in semantic_model.py (read-only): tables, relationships,
auto date tables, date table, measures (with their error state).

    bi_env python bi/etl/check_model.py
The model is read through bi/etl/model_meta.ps1 (ADOMD.NET from DAX Studio, the local msmdsrv port of Power BI Desktop).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import semantic_model as sm

PS = Path(__file__).with_name("model_meta.ps1")
AUTO = ("LocalDateTable_", "DateTableTemplate_")


def read_model() -> dict | None:
    """The open model's metadata, or None when no Power BI Desktop model is open."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "m.json"
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS), "-OutFile", str(out)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if "NO MODEL" in p.stdout or not out.exists():
            return None
        return json.loads(out.read_text(encoding="utf-8-sig"))


def _list(x):
    return x if isinstance(x, list) else ([x] if x else [])


def relationship_status(m: dict) -> list[dict]:
    """One row per designed relationship (status + action), plus one row per extra relationship to delete."""
    tables = {t["ID"]: t["Name"] for t in _list(m["tables"])}
    cols = {c["ID"]: (tables.get(c["TableID"]), c["ExplicitName"] or c["InferredName"]) for c in _list(m["columns"])}
    spec = {(a, b, c, d): act for a, b, c, d, act in sm.RELATIONSHIPS}
    found, extra = {}, []
    for r in _list(m["rels"]):
        f, t = cols.get(r["FromColumnID"]), cols.get(r["ToColumnID"])
        if not f or not t or str(f[0]).startswith(AUTO) or str(t[0]).startswith(AUTO):
            continue
        info = {"active": bool(r["IsActive"]), "both": r["CrossFilteringBehavior"] == 2,
                "one_to_one": r["FromCardinality"] == 1 and r["ToCardinality"] == 1}
        key, rkey = (f[0], f[1], t[0], t[1]), (t[0], t[1], f[0], f[1])
        if key in spec:
            found[key] = info
        elif rkey in spec:
            found[rkey] = dict(info, reversed=True)
        else:
            extra.append((f"{f[0]}[{f[1]}]", f"{t[0]}[{t[1]}]"))
    rows = []
    for (a, b, c, d), act in spec.items():
        row = {"from (many)": f"{a}[{b}]", "to (one)": f"{c}[{d}]", "active": "yes" if act else "NO (inactive)"}
        i = found.get((a, b, c, d))
        if i is None:
            row["in your model now"] = "MISSING"
            row["what to do"] = ("Create it: Manage relationships > New; Many to one, Single"
                                 + ("; UNTICK 'Make this relationship active'" if not act else ""))
        else:
            fixes = []
            if i["one_to_one"]:
                fixes.append("Cardinality: Many to one (*:1)")
            if i["both"]:
                fixes.append("Cross-filter: Single")
            if i["active"] != act:
                fixes.append("tick 'Make this relationship active'" if act else "untick 'Make this relationship active'")
            if i.get("reversed"):
                fixes = ["Delete it and create it again with the fact table as 'From'"]
            row["in your model now"] = "FIX" if fixes else "OK (already there)"
            row["what to do"] = ("Manage relationships > select it > Edit: " + "; ".join(fixes)) if fixes and not i.get("reversed") else \
                (fixes[0] if fixes else "Nothing")
        rows.append(row)
    for f, t in extra:
        rows.append({"from (many)": f, "to (one)": t, "active": "-", "in your model now": "EXTRA (not in the design)",
                     "what to do": "Delete it: Manage relationships > select it > Delete"})
    return rows


def main() -> int:
    m = read_model()
    if m is None:
        sys.exit("No open Power BI model found. Open TradingBI.pbip in Power BI Desktop.")
    tables = {t["ID"]: t["Name"] for t in _list(m["tables"])}
    problems = 0
    auto = [n for n in tables.values() if n.startswith(AUTO)]
    print(f"Auto date tables: {len(auto)} {'(OK: none)' if not auto else '-> turn off File > Options > CURRENT FILE > Data Load > Auto date/time'}")
    problems += bool(auto)
    want, have = set(sm.TABLES), {n for n in tables.values() if not n.startswith(AUTO)}
    missing_t = sorted(want - have)
    print(f"Tables: {len(want & have)} of {len(want)} expected present" + (f"; MISSING {missing_t}" if missing_t else ""))
    problems += len(missing_t)
    rels = relationship_status(m)
    ok = sum(r["in your model now"].startswith("OK") for r in rels)
    print(f"Relationships: {ok} of {len(sm.RELATIONSHIPS)} correct")
    for r in rels:
        if not r["in your model now"].startswith("OK"):
            print(f"   {r['in your model now']:10s} {r['from (many)']} -> {r['to (one)']}: {r['what to do']}")
            problems += 1
    date_marked = any(t["Name"] == "Date" and t.get("DataCategory") == "Time" for t in _list(m["tables"]))
    print(f"Date marked as date table: {'yes' if date_marked else 'not yet (Part C2)'}")
    problems += not date_marked
    meas = _list(m["measures"])
    bad = [x["Name"] for x in meas if x.get("State") not in (None, 1)]
    print(f"Measures: {len(meas)} of {len(sm.MEASURES)}" + (f"; WITH ERRORS: {bad}" if bad else ""))
    problems += len(bad)
    print("RESULT:", "all good so far" if problems == 0 else f"{problems} thing(s) to fix")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
