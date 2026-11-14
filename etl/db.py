"""Connection helper for the BI ETL: Windows authentication only, settings from bi/config.ini (gitignored).

No user name or password exists anywhere: the connection runs as the Windows user who starts the script
(or as the SQL Server Agent service account when the Agent job runs it).
"""
from __future__ import annotations

import re
from pathlib import Path

import pyodbc

from check_setup import connection_string, load_sql_settings

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def database_name(which: str) -> str:
    s, _ = load_sql_settings()
    return s["database_dev"] if which == "dev" else s["database_prod"]


def connect(which: str = "dev", autocommit: bool = False) -> pyodbc.Connection:
    s, _ = load_sql_settings()
    conn = pyodbc.connect(connection_string(s, database_name(which)), timeout=10, autocommit=autocommit)
    return conn


def batches(sql_text: str) -> list[str]:
    """Split a script on lines that contain only GO (what SSMS and sqlcmd do; GO is not T-SQL)."""
    parts = re.split(r"^\s*GO\s*;?\s*$", sql_text, flags=re.IGNORECASE | re.MULTILINE)
    return [p for p in (x.strip() for x in parts) if p and not _only_comments(p)]


def _only_comments(batch: str) -> bool:
    no_block = re.sub(r"/\*.*?\*/", "", batch, flags=re.S)
    return all(line.strip() == "" or line.strip().startswith("--") for line in no_block.splitlines())


def run_script(conn: pyodbc.Connection, path: Path) -> int:
    """Run every batch of a .sql file; returns the number of batches. Errors propagate (pyodbc raises)."""
    n = 0
    cur = conn.cursor()
    for b in batches(path.read_text(encoding="utf-8")):
        cur.execute(b)
        while cur.nextset():  # drain all result sets so errors in later statements surface
            pass
        n += 1
    conn.commit()
    return n
