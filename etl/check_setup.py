"""Phase 0 readiness check for the BI showcase.

Run with the bi_env Python from the trading_bot folder:
    C:/Users/<you>/anaconda3/envs/bi_env/python.exe bi/etl/check_setup.py

Checks: Python packages, the ODBC driver, the SQL Server connection (Windows authentication),
the edition, the memory cap, the two databases and the SQL Server services. Read-only: it changes nothing.
"""
from __future__ import annotations

import configparser
import importlib
import sys
from pathlib import Path

BI_DIR = Path(__file__).resolve().parents[1]
PACKAGES = ["pandas", "pyarrow", "openpyxl", "pyodbc", "sqlalchemy", "pytest"]
EXPECTED_MAX_MEMORY_MB = 1536


def load_sql_settings() -> tuple[configparser.SectionProxy, Path]:
    path = BI_DIR / "config.ini"
    if not path.exists():
        path = BI_DIR / "config.example.ini"
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cfg.read(path)
    return cfg["sqlserver"], path


def connection_string(s: configparser.SectionProxy, database: str) -> str:
    trust = "yes" if s.get("trust_server_certificate", "no").lower() == "yes" else "no"
    return (
        f"DRIVER={{{s['driver']}}};SERVER={s['server']};DATABASE={database};"
        f"Trusted_Connection=yes;TrustServerCertificate={trust};"
    )


def main() -> int:
    problems: list[str] = []
    s, cfg_path = load_sql_settings()
    print(f"Settings file: {cfg_path.name}")

    print("\n1. Python packages")
    for name in PACKAGES:
        try:
            mod = importlib.import_module(name)
            print(f"   OK       {name} {getattr(mod, '__version__', '')}")
        except ImportError:
            print(f"   MISSING  {name}")
            problems.append(f"Python package {name} is missing")
    if "pyodbc" not in sys.modules:
        print("\nCannot continue without pyodbc.")
        return 1
    import pyodbc

    print("\n2. ODBC drivers")
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    for d in drivers:
        print(f"   found    {d}")
    if s["driver"] not in drivers:
        problems.append(f"'{s['driver']}' is not installed (install Microsoft ODBC Driver 18 for SQL Server)")
        print(f"   MISSING  {s['driver']}")

    print("\n3. SQL Server connection")
    try:
        conn = pyodbc.connect(connection_string(s, "master"), timeout=5)
    except pyodbc.Error as exc:
        print(f"   FAILED   {exc}")
        problems.append(
            "Cannot connect. Is the SQL Server (MSSQLSERVER) service started? With a Manual startup type it "
            "must be started each time (SQL Server Configuration Manager or services.msc)."
        )
        return report(problems)

    cur = conn.cursor()
    edition, version = cur.execute(
        "SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64))"
    ).fetchone()
    print(f"   OK       connected to {s['server']}: {edition}, version {version}")
    if "Developer" not in edition:
        problems.append(f"Edition is '{edition}', expected a Developer edition")

    max_mem = cur.execute(
        "SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = N'max server memory (MB)'"
    ).fetchone()[0]
    print(f"   {'OK      ' if max_mem == EXPECTED_MAX_MEMORY_MB else 'CHECK   '} max server memory = {max_mem} MB")
    if max_mem != EXPECTED_MAX_MEMORY_MB:
        problems.append(f"max server memory is {max_mem} MB, expected {EXPECTED_MAX_MEMORY_MB} (run bi/sql/00_setup_server.sql)")

    print("\n4. Databases")
    found = {row[0]: row[1] for row in cur.execute(
        "SELECT name, recovery_model_desc FROM sys.databases WHERE name LIKE N'TradingBI%'"
    ).fetchall()}
    for db in (s["database_dev"], s["database_prod"]):
        if db in found:
            print(f"   OK       {db} (recovery {found[db]})")
        else:
            print(f"   MISSING  {db}")
            problems.append(f"Database {db} does not exist (run bi/sql/00_setup_server.sql)")

    print("\n5. SQL Server services")
    try:
        for name, startup, status in cur.execute(
            "SELECT servicename, startup_type_desc, status_desc FROM sys.dm_server_services"
        ).fetchall():
            print(f"   {status:<9}{name}  (startup: {startup})")
    except pyodbc.Error as exc:
        print(f"   could not read services: {exc}")
    conn.close()
    return report(problems)


def report(problems: list[str]) -> int:
    print("\n" + ("READY" if not problems else f"NOT READY ({len(problems)} problem(s))"))
    for p in problems:
        print(f"   - {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
