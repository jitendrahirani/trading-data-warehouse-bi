"""Phase 2 (steps 2.2-2.4): build the warehouse structure and the data dictionary.

    bi_env python bi/etl/apply_ddl.py --rebuild              # Dev: drop everything, run 01..06, apply descriptions
    bi_env python bi/etl/apply_ddl.py --dictionary-only      # regenerate bi/docs/Phase2_Data_Dictionary.xlsx only

What it does
  1. Runs bi/sql/01..06 in order against TradingBI_Dev (Windows authentication, see db.py).
     --rebuild first runs 90_drop_all.sql, which refuses to run outside a *_Dev database. There is no option to run
     against Prod here: Prod is deployed later from the same scripts, deliberately, by the user.
  2. Reads the "-- meaning | source | rule" comment on every column and the /* ... */ line above every table and
     stores them as extended properties (MS_Description, Source, Rule): the documentation lives in the database.
  3. Generates the data dictionary workbook FROM the database catalog (sys.tables, sys.columns, sys.foreign_keys,
     extended properties), so it can never describe a column that does not exist or miss one that does.
Touches only the TradingBI_Dev database and bi/docs/Phase2_Data_Dictionary.xlsx.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import db

BI = Path(__file__).resolve().parents[1]
DOCS = BI / "docs"
SCRIPTS = ["01_schemas.sql", "02_etl_control.sql", "03_dimensions.sql", "04_facts.sql", "05_security.sql",
           "06_seed_static.sql", "07_staging.sql", "08_stage_panel.sql",
           "09_etl_batch.sql", "10_proc_logging.sql", "11_proc_dims.sql", "12_proc_facts.sql", "13_proc_reconcile.sql",
           "31_rpt_views.sql", "32_tuning.sql", "33_seed_rls_users.sql"]
# code objects (procedures, functions, views): CREATE OR ALTER, so --procs-only re-creates them without touching data
PROCS = ["10_proc_logging.sql", "11_proc_dims.sql", "12_proc_facts.sql", "13_proc_reconcile.sql", "31_rpt_views.sql"]
VIEW_SCRIPTS = ["31_rpt_views.sql"]
DESCRIBED = ["02_etl_control.sql", "03_dimensions.sql", "04_facts.sql", "05_security.sql", "07_staging.sql", "08_stage_panel.sql", "09_etl_batch.sql"]
SCHEMAS = ("dw", "etl", "sec", "stg")
DICTIONARY = DOCS / "Phase2_Data_Dictionary.xlsx"


# ------------------------------------------------------------------------------------------- descriptions
def parse_descriptions(sql_text: str) -> dict:
    """{'dw.DimDate': {'desc': str|None, 'columns': {'DateKey': (meaning, source, rule)}}}"""
    out, table, last_col, pending, block = {}, None, None, None, None
    for line in sql_text.splitlines():
        s = line.strip()
        if block is not None:                       # inside a multi-line /* ... */
            block.append(s.replace("*/", "").strip())
            if "*/" in s:
                pending, block = " ".join(x for x in block if x), None
            continue
        if s.startswith("/*"):
            body = s[2:]
            if "*/" in body:
                pending = body.split("*/")[0].strip()
            else:
                block = [body.strip()]
            continue
        m = re.match(r"CREATE TABLE (\w+)\.(\w+)", s)
        if m:
            table = f"{m.group(1)}.{m.group(2)}"
            out[table] = {"desc": pending, "columns": {}}
            pending, last_col = None, None
            continue
        if table is None:
            if s and not s.startswith("--"):
                pending = None                      # a statement between the comment and CREATE TABLE
            continue
        if s.startswith(");"):
            table = None
            continue
        m = re.match(r"^ {4}\[?(\w+)\]?\s", line)
        if m and m.group(1).upper() not in ("CONSTRAINT", "INDEX"):
            last_col = m.group(1)
        if "--" in line and last_col and last_col not in out[table]["columns"]:
            parts = [p.strip() for p in line.split("--", 1)[1].split("|")]
            if len(parts) != 3:
                raise ValueError(f"{table}.{last_col}: comment must be 'meaning | source | rule', got {parts}")
            out[table]["columns"][last_col] = tuple(parts)
    return out


def parse_view_descriptions(sql_text: str) -> dict:
    """{'rpt.Date': 'description'} from the one-line /* ... */ comment directly above each CREATE OR ALTER VIEW."""
    out, pending = {}, None
    for line in sql_text.splitlines():
        s = line.strip()
        m = re.match(r"/\*\s*(.*?)\s*\*/$", s)
        if m:
            pending = m.group(1)
            continue
        m = re.match(r"CREATE OR ALTER VIEW (\w+)\.\[?(\w+)\]?", s)
        if m:
            out[f"{m.group(1)}.{m.group(2)}"] = pending
        if s and not s.startswith("--"):
            pending = None if not m else pending
    return out


def apply_view_descriptions(conn, views: dict) -> int:
    cur, n = conn.cursor(), 0
    for full, desc in views.items():
        if not desc:
            continue
        schema, view = full.split(".")
        cur.execute("""
            IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 1 AND minor_id = 0 AND name = 'MS_Description'
                       AND major_id = OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?)))
                EXEC sys.sp_dropextendedproperty @name = 'MS_Description', @level0type = 'SCHEMA', @level0name = ?, @level1type = 'VIEW', @level1name = ?;
            EXEC sys.sp_addextendedproperty @name = 'MS_Description', @value = ?, @level0type = 'SCHEMA', @level0name = ?,
                 @level1type = 'VIEW', @level1name = ?;""", schema, view, schema, view, desc, schema, view)
        n += 1
    conn.commit()
    return n


def apply_descriptions(conn, described: dict) -> int:
    cur = conn.cursor()
    n = 0

    def put(name, value, schema, table, column=None):
        nonlocal n
        if not value:
            return
        lvl2 = ("COLUMN", column) if column else (None, None)
        args = (name, schema, table, lvl2[0], lvl2[1])
        cur.execute("""
            IF EXISTS (SELECT 1 FROM sys.extended_properties
                       WHERE class = 1 AND name = ? AND major_id = OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?))
                         AND minor_id = ISNULL(COLUMNPROPERTY(OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?)), ?, 'ColumnId'), 0))
                EXEC sys.sp_dropextendedproperty @name = ?, @level0type = 'SCHEMA', @level0name = ?,
                     @level1type = 'TABLE', @level1name = ?, @level2type = ?, @level2name = ?;
            EXEC sys.sp_addextendedproperty @name = ?, @value = ?, @level0type = 'SCHEMA', @level0name = ?,
                 @level1type = 'TABLE', @level1name = ?, @level2type = ?, @level2name = ?;""",
                    name, schema, table, schema, table, column or "", *args, name, value, schema, table, lvl2[0], lvl2[1])
        n += 1

    for full, d in described.items():
        schema, table = full.split(".")
        put("MS_Description", d["desc"], schema, table)
        for col, (meaning, source, rule) in d["columns"].items():
            put("MS_Description", meaning, schema, table, col)
            put("Source", source, schema, table, col)
            put("Rule", rule, schema, table, col)
    conn.commit()
    return n


# ------------------------------------------------------------------------------------------- dictionary
CATALOG_SQL = {
    "tables": """
        SELECT s.name AS [schema], t.name AS [table],
               CAST(ep.value AS nvarchar(400)) AS description,
               (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS [rows],
               (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS columns,
               CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type = 5) THEN 'clustered columnstore'
                    WHEN EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type = 1) THEN 'clustered rowstore'
                    ELSE 'heap' END AS storage,
               STUFF((SELECT ', ' + c.name FROM sys.index_columns ic JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                      JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                      WHERE i.object_id = t.object_id AND i.is_primary_key = 1 ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS primary_key
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        LEFT JOIN sys.extended_properties ep ON ep.class = 1 AND ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
        WHERE s.name IN ('dw', 'etl', 'sec', 'stg')""",
    "columns": """
        SELECT s.name AS [schema], t.name AS [table], c.column_id AS ordinal, c.name AS [column],
               ty.name + CASE WHEN ty.name IN ('varchar', 'char', 'binary', 'varbinary') THEN '(' + IIF(c.max_length = -1, 'max', CAST(c.max_length AS varchar)) + ')'
                              WHEN ty.name IN ('nvarchar', 'nchar') THEN '(' + IIF(c.max_length = -1, 'max', CAST(c.max_length / 2 AS varchar)) + ')'
                              WHEN ty.name = 'decimal' THEN '(' + CAST(c.precision AS varchar) + ',' + CAST(c.scale AS varchar) + ')'
                              WHEN ty.name IN ('datetime2', 'time') THEN '(' + CAST(c.scale AS varchar) + ')'
                              ELSE '' END AS data_type,
               IIF(c.is_nullable = 1, 'yes', 'no') AS nullable,
               IIF(c.is_identity = 1, 'identity', '') AS [identity],
               CAST(cc.definition AS nvarchar(400)) AS computed_as,
               CAST(dc.definition AS nvarchar(200)) AS default_value,
               IIF(EXISTS (SELECT 1 FROM sys.index_columns ic JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                           WHERE i.is_primary_key = 1 AND ic.object_id = c.object_id AND ic.column_id = c.column_id), 'PK', '') AS pk,
               (SELECT TOP 1 rs.name + '.' + rt.name + '.' + rc.name FROM sys.foreign_key_columns fkc
                  JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
                  JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
                 WHERE fkc.parent_object_id = c.object_id AND fkc.parent_column_id = c.column_id) AS references_,
               CAST(d.value AS nvarchar(1000)) AS meaning, CAST(src.value AS nvarchar(1000)) AS source, CAST(r.value AS nvarchar(1000)) AS [rule]
        FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
        LEFT JOIN sys.extended_properties d ON d.class = 1 AND d.major_id = c.object_id AND d.minor_id = c.column_id AND d.name = 'MS_Description'
        LEFT JOIN sys.extended_properties src ON src.class = 1 AND src.major_id = c.object_id AND src.minor_id = c.column_id AND src.name = 'Source'
        LEFT JOIN sys.extended_properties r ON r.class = 1 AND r.major_id = c.object_id AND r.minor_id = c.column_id AND r.name = 'Rule'
        WHERE s.name IN ('dw', 'etl', 'sec', 'stg')""",
    "relationships": """
        SELECT fk.name AS foreign_key, ps.name + '.' + pt.name AS from_table, pc.name AS from_column,
               rs.name + '.' + rt.name AS to_table, rc.name AS to_column,
               IIF(fk.is_not_trusted = 0, 'yes', 'NO') AS trusted, IIF(fk.is_disabled = 0, 'yes', 'NO') AS enabled
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables pt ON pt.object_id = fk.parent_object_id JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
        JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id""",
    "views": """
        SELECT s.name AS [schema], v.name AS [view], CAST(ep.value AS nvarchar(400)) AS description, c.column_id AS ordinal, c.name AS [column],
               ty.name + CASE WHEN ty.name IN ('varchar', 'char') THEN '(' + IIF(c.max_length = -1, 'max', CAST(c.max_length AS varchar)) + ')'
                              WHEN ty.name IN ('nvarchar', 'nchar') THEN '(' + IIF(c.max_length = -1, 'max', CAST(c.max_length / 2 AS varchar)) + ')'
                              WHEN ty.name = 'decimal' THEN '(' + CAST(c.precision AS varchar) + ',' + CAST(c.scale AS varchar) + ')'
                              ELSE '' END AS data_type,
               IIF(c.is_nullable = 1, 'yes', 'no') AS nullable
        FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
        JOIN sys.columns c ON c.object_id = v.object_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.extended_properties ep ON ep.class = 1 AND ep.major_id = v.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
        WHERE s.name = 'rpt'""",
    "constraints": """
        SELECT s.name + '.' + t.name AS [table], k.name AS [constraint], k.type_desc AS kind, CAST(k.definition AS nvarchar(800)) AS definition
        FROM (SELECT name, parent_object_id, type_desc, definition FROM sys.check_constraints
              UNION ALL
              SELECT kc.name, kc.parent_object_id, kc.type_desc,
                     STUFF((SELECT ', ' + c.name FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                            WHERE ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '')
              FROM sys.key_constraints kc) k
        JOIN sys.tables t ON t.object_id = k.parent_object_id JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name IN ('dw', 'etl', 'sec', 'stg')""",
}


def kind_of(schema: str, table: str) -> str:
    if schema == "dw":
        return "Dimension" if table.startswith("Dim") else "Fact"
    return {"etl": "ETL control", "sec": "Security", "stg": "Staging"}[schema]


def read_catalog(conn) -> dict:
    out = {}
    for k, q in CATALOG_SQL.items():
        cur = conn.cursor()
        cur.execute(q)
        cols = [c[0] for c in cur.description]
        out[k] = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
    order = {"Dimension": 0, "Fact": 1, "ETL control": 2, "Security": 3, "Staging": 4}
    t = out["tables"]
    t.insert(0, "kind", [kind_of(s, n) for s, n in zip(t["schema"], t["table"])])
    out["tables"] = t.assign(_o=t["kind"].map(order)).sort_values(["_o", "table"]).drop(columns="_o")
    out["columns"] = out["columns"].sort_values(["schema", "table", "ordinal"])
    out["relationships"] = out["relationships"].sort_values(["from_table", "from_column"])
    out["constraints"] = out["constraints"].sort_values(["table", "kind", "constraint"])
    out["views"] = out["views"].sort_values(["view", "ordinal"])
    return out


HDR_FILL = PatternFill("solid", fgColor="1F4E78")
HDR_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BODY = Font(name="Arial", size=10)
TITLE = Font(name="Arial", bold=True, size=14)
NOTE = Font(name="Arial", italic=True, size=10, color="595959")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")


def _write(ws, title: str, note: str, df: pd.DataFrame, widths: dict[str, int]):
    ws["A1"], ws["A1"].font = title, TITLE
    ws["A2"], ws["A2"].font = note, NOTE
    cols = list(df.columns)
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=4, column=j, value=c)
        cell.fill, cell.font, cell.alignment, cell.border = HDR_FILL, HDR_FONT, WRAP, BOX
        ws.column_dimensions[cell.column_letter].width = widths.get(c, 14)
    for i, row in enumerate(df.itertuples(index=False), 5):
        for j, v in enumerate(row, 1):
            v = None if (v is None or (isinstance(v, float) and pd.isna(v)) or v == "") else v
            cell = ws.cell(row=i, column=j, value=v)
            cell.font, cell.alignment, cell.border = BODY, WRAP, BOX
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:{ws.cell(row=4, column=len(cols)).column_letter}{max(4, 4 + len(df))}"


def physical_bus_matrix(cat: dict) -> pd.DataFrame:
    rel = cat["relationships"]
    facts = [f"dw.{t}" for t in cat["tables"].query("kind == 'Fact'")["table"]]
    dims = [f"dw.{t}" for t in cat["tables"].query("kind == 'Dimension'")["table"]]
    rows = []
    for f in facts:
        r = {"fact": f}
        for d in dims:
            c = rel[(rel.from_table == f) & (rel.to_table == d)]["from_column"].tolist()
            r[d.replace("dw.", "")] = " + ".join(c) + ("  (role-playing)" if len(c) > 1 else "") if c else None
        rows.append(r)
    return pd.DataFrame(rows)


def write_dictionary(cat: dict, path: Path, database: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    lines = [
        ("Data dictionary: TradingBI warehouse", TITLE),
        (f"Generated {datetime.now():%Y-%m-%d %H:%M} from the {database} catalog by bi/etl/apply_ddl.py. Do not edit by hand: "
         "change the comment in bi/sql/*.sql and re-run.", NOTE),
        ("", BODY),
        ("Sheets", Font(name="Arial", bold=True, size=11)),
        ("Tables: one row per table: kind, grain/description, storage, primary key, rows, columns.", BODY),
        ("Columns: one row per column: data type, nullability, keys, default/computed, meaning, source, rule.", BODY),
        ("Relationships: one row per foreign key column, with trusted/enabled flags (all must be 'yes').", BODY),
        ("Physical bus matrix: facts x dimensions, showing the actual key columns. Two columns to the same dimension = a role-playing dimension.", BODY),
        ("Report views: the rpt views Power BI reads (description, columns, types).", BODY),
        ("Constraints: primary keys, unique keys and check constraints.", BODY),
        ("", BODY),
        ("Conventions", Font(name="Arial", bold=True, size=11)),
        ("Key -1 = 'Unknown' member in every dimension (DimClass also -2 = 'Not applicable'). A failed lookup loads as -1 instead of disappearing.", BODY),
        ("Smart keys: DateKey = yyyymmdd, TimeKey = minute of day (IST), SeverityKey = severity rank, ClassKey = the model's label code.", BODY),
        ("Source codes S01-S09 refer to bi/docs/Phase1_Source_Profile.xlsx -> Source inventory.", BODY),
        ("Lineage: every fact row carries EtlRunId (-> etl.RunLog); file-based facts also carry SourceFile + SourceRowNo.", BODY),
        ("Timestamps are stored in UTC (Ts*Utc); DateKey/TimeKey are the IST calendar date and minute.", BODY),
    ]
    for i, (text, font) in enumerate(lines, 1):
        ws.cell(row=i, column=1, value=text).font = font
        ws.cell(row=i, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 130

    _write(wb.create_sheet("Tables"), "Tables", "Description = the grain or purpose of the table (from the /* */ line above CREATE TABLE).",
           cat["tables"], {"kind": 13, "schema": 10, "table": 24, "description": 70, "rows": 10, "columns": 10, "storage": 20, "primary_key": 34})
    cols = cat["columns"].rename(columns={"references_": "references"})
    _write(wb.create_sheet("Columns"), "Columns", "Meaning | Source | Rule come from the column comments in bi/sql/*.sql, stored as extended properties.",
           cols, {"schema": 9, "table": 22, "ordinal": 8, "column": 22, "data_type": 15, "nullable": 9, "identity": 9,
                  "computed_as": 30, "default_value": 16, "pk": 5, "references": 26, "meaning": 48, "source": 30, "rule": 40})
    _write(wb.create_sheet("Relationships"), "Relationships", "Every foreign key must be trusted and enabled (the tests check this).",
           cat["relationships"], {"foreign_key": 34, "from_table": 24, "from_column": 20, "to_table": 22, "to_column": 16, "trusted": 9, "enabled": 9})
    bm = physical_bus_matrix(cat)
    _write(wb.create_sheet("Physical bus matrix"), "Physical bus matrix", "Cell = the fact's key column(s) pointing at that dimension.",
           bm, {"fact": 26, **{c: 17 for c in bm.columns if c != "fact"}})
    v = cat["views"].copy()
    v.loc[v["view"].duplicated(), ["schema", "description"]] = None      # description once per view
    _write(wb.create_sheet("Report views"), "Report views (rpt schema)",
           "The contract with Power BI and the paginated report: reports read only these views. Description from the /* */ line above each view.",
           v, {"schema": 8, "view": 22, "description": 60, "ordinal": 8, "column": 26, "data_type": 16, "nullable": 9})
    _write(wb.create_sheet("Constraints"), "Constraints", "Primary keys, unique keys and check constraints.",
           cat["constraints"], {"table": 26, "constraint": 34, "kind": 24, "definition": 90})
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="Dev only: drop all stg/dw/etl/sec tables first")
    ap.add_argument("--dictionary-only", action="store_true", help="only regenerate the data dictionary")
    ap.add_argument("--procs-only", action="store_true",
                    help="(re)create the stored procedures only (CREATE OR ALTER); tables and data are untouched")
    ap.add_argument("--describe-only", action="store_true",
                    help="re-apply the column comments as extended properties and regenerate the dictionary; no DDL, no data loss")
    a = ap.parse_args()

    database = db.database_name("dev")
    if not database.endswith("_Dev"):
        print(f"Refusing: the dev database in config.ini is {database!r}, which does not end in _Dev.")
        return 2
    conn = db.connect("dev")
    described = {}
    for f in DESCRIBED:
        described.update(parse_descriptions((db.SQL_DIR / f).read_text(encoding="utf-8")))

    views = {}
    for f in VIEW_SCRIPTS:
        views.update(parse_view_descriptions((db.SQL_DIR / f).read_text(encoding="utf-8")))
    if a.procs_only:
        for s in PROCS:
            print(f"ran {s} ({db.run_script(conn, db.SQL_DIR / s)} batches)")
        print(f"described {apply_view_descriptions(conn, views)} views")
        cat = read_catalog(conn)
        write_dictionary(cat, DICTIONARY, database)
        print(f"data dictionary regenerated ({len(cat['views'].drop_duplicates('view'))} report views)")
        return 0
    if a.describe_only:
        print(f"applied {apply_descriptions(conn, described)} extended properties to {len(described)} tables")
        print(f"described {apply_view_descriptions(conn, views)} views")
    elif not a.dictionary_only:
        cur = conn.cursor()
        exists = cur.execute("SELECT OBJECT_ID('dw.DimDate')").fetchone()[0] is not None
        if exists and not a.rebuild:
            print("The warehouse tables already exist. Re-run with --rebuild (drops and recreates them on Dev).")
            return 1
        if a.rebuild:
            db.run_script(conn, db.SQL_DIR / "90_drop_all.sql")
            print("dropped existing stg/dw/etl/sec tables")
        for s in SCRIPTS:
            n = db.run_script(conn, db.SQL_DIR / s)
            print(f"ran {s} ({n} batch{'es' if n != 1 else ''})")
        props = apply_descriptions(conn, described) + apply_view_descriptions(conn, views)
        print(f"applied {props} extended properties to {len(described)} tables")

    cat = read_catalog(conn)
    write_dictionary(cat, DICTIONARY, database)
    missing = cat["columns"][cat["columns"]["meaning"].isna()]
    print(f"data dictionary: {DICTIONARY.relative_to(BI.parent)} ({len(cat['tables'])} tables, {len(cat['columns'])} columns, "
          f"{len(cat['relationships'])} foreign-key columns); columns without a description: {len(missing)}")
    if len(missing):
        print(missing[["schema", "table", "column"]].to_string(index=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
