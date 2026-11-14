import sys
from pathlib import Path

import pytest

ETL = Path(__file__).resolve().parents[1] / "etl"
if str(ETL) not in sys.path:
    sys.path.insert(0, str(ETL))


@pytest.fixture(scope="session")
def dev():
    import db
    try:
        conn = db.connect("dev")
    except Exception as e:  # SQL Server service stopped (Manual start)
        pytest.skip(f"cannot connect to the Dev database: {e}")
    yield conn
    conn.close()


def scalar(conn, sql, *params):
    return conn.cursor().execute(sql, *params).fetchone()[0]
