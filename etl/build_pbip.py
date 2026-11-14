"""Phase 6: generate the Power BI Project bi/powerbi/TradingBI.pbip (semantic model as TMDL) from bi/etl/semantic_model.py.

    bi_env python bi/etl/build_pbip.py
What it writes
  bi/powerbi/TradingBI.pbip                          the file you open in Power BI Desktop
  bi/powerbi/TradingBI.SemanticModel/definition/     the model as TMDL text: tables (Power Query source + typed columns),
                                                     relationships, measures, Threshold parameter, RLS roles, Date marked
  bi/powerbi/TradingBI.Report/                       an empty report (pages are Phase 7)
How it is checked
  The generated TMDL is parsed by Microsoft's own TmdlSerializer (Microsoft.AnalysisServices.Tabular 19.x, installed with
  DAX Studio) and written back through it, so the files on disk are in canonical TMDL that Power BI Desktop reads.
Column types come from the SQL Server catalog of the rpt views; each Power Query step sets them explicitly (Table.
TransformColumnTypes), so a refresh cannot change a type silently. Data is NOT in these files: open the .pbip and Refresh.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import db
import semantic_model as sm

BI = Path(__file__).resolve().parents[1]
PBI = BI / "powerbi"
NAME = "TradingBI"
SEM = PBI / f"{NAME}.SemanticModel"
REP = PBI / f"{NAME}.Report"
NS =uuid.UUID("6f1c2d5e-3b7a-4c1e-9a55-1f0f3b8a7c21")


def tag(*parts) -> str:
    """Deterministic lineage tag, so regenerating does not churn every line in git."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def q(name: str) -> str:
    """TMDL object name: quoted unless a plain identifier."""
    if name.replace("_", "a").isalnum() and not name[0].isdigit():
        return name
    return "'" + name.replace("'", "''") + "'"


def mstr(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


# SQL type -> (Power Query type, TMDL dataType, formatString, underlying date annotation)
TYPES = {
    "int": ("Int64.Type", "int64", "0", None), "smallint": ("Int64.Type", "int64", "0", None),
    "tinyint": ("Int64.Type", "int64", "0", None), "bigint": ("Int64.Type", "int64", "0", None),
    "float": ("type number", "double", None, None), "real": ("type number", "double", None, None),
    "decimal": ("type number", "double", None, None), "numeric": ("type number", "double", None, None),
    "bit": ("type logical", "boolean", '"""TRUE"";""TRUE"";""FALSE"""', None),
    "date": ("type date", "dateTime", "Long Date", "Date"), "datetime2": ("type datetime", "dateTime", "General Date", None),
    "datetime": ("type datetime", "dateTime", "General Date", None), "time": ("type time", "dateTime", "Long Time", "Time"),
    "char": ("type text", "string", None, None), "varchar": ("type text", "string", None, None),
    "nchar": ("type text", "string", None, None), "nvarchar": ("type text", "string", None, None),
}
FACTS = {"DQFinding", "DQDailyStatus", "DQDayVerdict", "ModelPrediction", "ModelMetric", "FeatureImportance", "Trade",
         "FeatureDrift", "FeatureValueDaily", "EtlRun", "EtlStep", "Reconciliation"}


def view_columns() -> dict[str, list[tuple[str, str]]]:
    cur = db.connect("dev", autocommit=True).cursor()
    cur.execute("""SELECT v.name, c.name, TYPE_NAME(c.user_type_id) FROM sys.views v JOIN sys.columns c ON c.object_id = v.object_id
                   WHERE SCHEMA_NAME(v.schema_id) = 'rpt' ORDER BY v.name, c.column_id""")
    out: dict[str, list] = {}
    for v, c, t in cur.fetchall():
        out.setdefault(v, []).append((c, t))
    return out


def table_tmdl(table: str, view: str, cols: list[tuple[str, str]]) -> str:
    schema, item = view.split(".", 1)
    sort_by = {c: by for t, c, by in sm.SORT_BY if t == table}
    L = [f"table {q(table)}", f"\tlineageTag: {tag('table', table)}"]
    if table == "Date":
        L.append("\tdataCategory: Time")
    if table == "UserDesk":
        L.append("\tisHidden")
    L.append("")
    for c, t in cols:
        mtype, dtype, fmt, under = TYPES[t]
        key = c.endswith("Key") or (c == "EtlRunId" and table != "EtlRun")
        L.append(f"\tcolumn {q(c)}")
        L.append(f"\t\tdataType: {dtype}")
        if table == "Date" and c == "Date":
            L.append("\t\tisKey")
        if fmt:
            L.append(f"\t\tformatString: {fmt}")
        if key:
            L.append("\t\tisHidden")
        L.append(f"\t\tlineageTag: {tag('column', table, c)}")
        numeric = dtype in ("int64", "double")
        summarize = "sum" if (numeric and table in FACTS and not key and not c.endswith(("Id", "No")) and c != "Rank"
                              and dtype == "double" or (numeric and table in FACTS and dtype == "int64" and not key
                                                        and not c.endswith(("Id", "No")) and c not in ("Rank",))) else "none"
        L.append(f"\t\tsummarizeBy: {summarize}")
        L.append(f"\t\tsourceColumn: {c}")
        if c in sort_by:
            L.append(f"\t\tsortByColumn: {q(sort_by[c])}")
        L.append("")
        if under:
            L.append(f"\t\tannotation UnderlyingDateTimeDataType = {under}")
            L.append("")
    types = ", ".join("{" + mstr(c) + ", " + TYPES[t][0] + "}" for c, t in cols)
    L += [f"\tpartition {q(table)} = m", "\t\tmode: import", "\t\tsource =", "\t\t\t\tlet",
          "\t\t\t\t    Source = Sql.Database(Server, Database),",
          f"\t\t\t\t    View = Source{{[Schema={mstr(schema)},Item={mstr(item)}]}}[Data],",
          f"\t\t\t\t    Typed = Table.TransformColumnTypes(View, {{{types}}})",
          "\t\t\t\tin", "\t\t\t\t    Typed", "", "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(L)


def measures_tmdl() -> str:
    L = ["table _Measures", f"\tlineageTag: {tag('table', '_Measures')}", ""]
    for m in sm.MEASURES:
        desc = m.desc + (f" ({m.kpi})" if m.kpi else "")
        L.append(f"\t/// {desc}")
        L.append(f"\tmeasure {q(m.name)} =")
        for line in m.dax.split("\n"):
            L.append("\t\t\t" + line)
        if m.fmt:
            L.append(f"\t\tformatString: {m.fmt}")
        L.append(f"\t\tdisplayFolder: {m.folder}")
        L.append(f"\t\tlineageTag: {tag('measure', m.name)}")
        L.append("")
    L += ["\tcolumn Column1", "\t\tdataType: string", "\t\tisHidden", f"\t\tlineageTag: {tag('column', '_Measures', 'Column1')}",
          "\t\tsummarizeBy: none", "\t\tsourceColumn: Column1", "",
          "\tpartition _Measures = m", "\t\tmode: import", "\t\tsource =",
          "\t\t\t\tlet", "\t\t\t\t    Source = #table(type table [Column1 = text], {})", "\t\t\t\tin", "\t\t\t\t    Source", "",
          "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(L)


def threshold_tmdl() -> str:
    return "\n".join([
        "table Threshold", f"\tlineageTag: {tag('table', 'Threshold')}", "",
        "\tmeasure 'Threshold Value' = SELECTEDVALUE ( 'Threshold'[Threshold], 0.5 )", "\t\tformatString: 0.00",
        f"\t\tlineageTag: {tag('measure', 'Threshold Value')}", "",
        "\tcolumn Threshold", "\t\tdataType: double", "\t\tformatString: 0.00", f"\t\tlineageTag: {tag('column', 'Threshold', 'Threshold')}",
        "\t\tsummarizeBy: none", "\t\tsourceColumn: [Value]", "",
        "\t\textendedProperty ParameterMetadata =", "\t\t\t\t{", '\t\t\t\t  "version": 0', "\t\t\t\t}", "",
        "\tpartition Threshold = calculated", "\t\tmode: import", "\t\tsource = GENERATESERIES ( 0.35, 0.70, 0.01 )", "",
        "\tannotation PBI_Id = " + tag("pbiid", "Threshold").replace("-", ""), ""])


def relationships_tmdl() -> str:
    L = []
    for a, b, c, d, act in sm.RELATIONSHIPS:
        L.append(f"relationship {tag('rel', a, b, c, d)}")
        if not act:
            L.append("\tisActive: false")
        L.append(f"\tfromColumn: {q(a)}.{q(b)}")
        L.append(f"\ttoColumn: {q(c)}.{q(d)}")
        L.append("")
    return "\n".join(L)


def roles_tmdl() -> dict[str, str]:
    return {r: "\n".join([f"role {q(r)}", "\tmodelPermission: read", "", f"\ttablePermission {q(t)} = {e}", ""])
            for r, t, e in sm.RLS_ROLES}


def model_tmdl(tables: list[str]) -> str:
    order = ["Server", "Database"] + tables
    L = ["model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3", "\tdiscourageImplicitMeasures",
         "\tsourceQueryCulture: en-IN", "\tdataAccessOptions", "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull", "",
         "annotation __PBI_TimeIntelligenceEnabled = 0", "", f"annotation PBI_QueryOrder = {json.dumps(order)}", ""]
    L += [f"ref table {q(t)}" for t in tables] + [""]
    return "\n".join(L)


def expressions_tmdl() -> str:
    out = []
    for name, value in (("Server", "localhost,1433"), ("Database", "TradingBI_Dev")):
        out += [f'expression {name} = {mstr(value)} meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
                f"\tlineageTag: {tag('expr', name)}", "", "\tannotation PBI_ResultType = Text", ""]
    return "\n".join(out)


def write_raw(raw: Path) -> list[str]:
    cols = view_columns()
    if raw.exists():
        shutil.rmtree(raw)
    (raw / "tables").mkdir(parents=True)
    (raw / "roles").mkdir()
    tables = list(sm.TABLES) + ["_Measures", "Threshold"]
    (raw / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1600\n\n", encoding="utf-8")
    (raw / "model.tmdl").write_text(model_tmdl(tables), encoding="utf-8")
    (raw / "expressions.tmdl").write_text(expressions_tmdl(), encoding="utf-8")
    (raw / "relationships.tmdl").write_text(relationships_tmdl(), encoding="utf-8")
    for t, v in sm.TABLES.items():
        (raw / "tables" / f"{t}.tmdl").write_text(table_tmdl(t, v, cols[v.split(".", 1)[1]]), encoding="utf-8")
    (raw / "tables" / "_Measures.tmdl").write_text(measures_tmdl(), encoding="utf-8")
    (raw / "tables" / "Threshold.tmdl").write_text(threshold_tmdl(), encoding="utf-8")
    for r, text in roles_tmdl().items():
        (raw / "roles" / f"{r}.tmdl").write_text(text, encoding="utf-8")
    return tables


def canonicalise(raw: Path, out: Path) -> str:
    """Parse with Microsoft's TmdlSerializer (fails on any structural error), then write canonical TMDL to `out`."""
    ps = Path(__file__).with_name("tmdl_roundtrip.ps1")
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps), "-In", str(raw), "-Out", str(out)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0 or "OK" not in p.stdout:
        raise SystemExit(f"TMDL check FAILED:\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def write_project(definition_ok: Path) -> None:
    if SEM.exists():
        shutil.rmtree(SEM)
    (SEM / "definition").mkdir(parents=True)
    for f in definition_ok.rglob("*"):
        if f.is_file():
            dst = SEM / "definition" / f.relative_to(definition_ok)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
    (SEM / "definition.pbism").write_text(json.dumps({"version": "4.0", "settings": {}}, indent=2), encoding="utf-8")
    # report: reuse the empty report definition Power BI Desktop itself wrote (in the 17 KB TradingBI.pbix), if present
    if REP.exists():
        shutil.rmtree(REP)
    REP.mkdir()
    pbix = PBI / f"{NAME}.pbix"
    with zipfile.ZipFile(pbix) as z:
        for e in z.namelist():
            if e.startswith("Report/"):
                dst = REP / e[len("Report/"):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(e))
    (REP / "definition.pbir").write_text(json.dumps(
        {"version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}}, indent=2), encoding="utf-8")
    (PBI / f"{NAME}.pbip").write_text(json.dumps(
        {"version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}},
        indent=2), encoding="utf-8")


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        raw, ok = Path(tmp) / "raw", Path(tmp) / "canonical"
        tables = write_raw(raw)
        print(canonicalise(raw, ok))
        write_project(ok)
    n = len(list((SEM / "definition").rglob("*.tmdl")))
    print(f"wrote {PBI.relative_to(BI.parent)}\\{NAME}.pbip: {len(tables)} tables, {len(sm.RELATIONSHIPS)} relationships, "
          f"{len(sm.MEASURES) + 1} measures, {len(sm.RLS_ROLES)} roles ({n} TMDL files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
