"""Start the SQL Agent job and wait for it, then print its history (the same thing SSMS 'View History' shows).

    bi_env python bi/etl/run_agent_job.py                 # whole job
    bi_env python bi/etl/run_agent_job.py --step 2        # start at step 2 (the T-SQL load) - used for the forced-failure demo
"""
from __future__ import annotations

import argparse
import sys
import time

import pyodbc

from check_setup import connection_string, load_sql_settings
from create_agent_job import JOB

STEPS = {1: "1 Stage new and changed files (Python)", 2: "2 Load warehouse, reconcile, commit (T-SQL)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, choices=[1, 2], default=1)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait")
    a = ap.parse_args()
    s, _ = load_sql_settings()
    conn = pyodbc.connect(connection_string(s, "msdb"), timeout=10, autocommit=True)
    cur = conn.cursor()
    last = cur.execute("SELECT ISNULL(MAX(h.instance_id), 0) FROM dbo.sysjobhistory h JOIN dbo.sysjobs j ON j.job_id = h.job_id WHERE j.name = ?", JOB).fetchone()[0]
    cur.execute("EXEC dbo.sp_start_job @job_name = ?, @step_name = ?", JOB, STEPS[a.step])
    print(f"started '{JOB}' at step {a.step}")
    t0 = time.time()
    while time.time() - t0 < a.timeout:
        time.sleep(5)
        done = cur.execute("""SELECT COUNT(*) FROM dbo.sysjobhistory h JOIN dbo.sysjobs j ON j.job_id = h.job_id
                              WHERE j.name = ? AND h.step_id = 0 AND h.instance_id > ?""", JOB, last).fetchone()[0]
        if done:
            break
    else:
        print("still running after the timeout")
        return 2
    rows = cur.execute("""SELECT h.step_id, h.step_name, CASE h.run_status WHEN 0 THEN 'Failed' WHEN 1 THEN 'Succeeded' WHEN 2 THEN 'Retry'
                                 WHEN 3 THEN 'Canceled' ELSE 'In progress' END, h.run_duration, LEFT(h.message, 400)
                          FROM dbo.sysjobhistory h JOIN dbo.sysjobs j ON j.job_id = h.job_id
                          WHERE j.name = ? AND h.instance_id > ? ORDER BY h.instance_id""", JOB, last).fetchall()
    outcome = "Failed"
    for step_id, name, status, dur, msg in rows:
        d = f"{dur // 10000:02d}:{dur // 100 % 100:02d}:{dur % 100:02d}"
        print(f"\n[{'job outcome' if step_id == 0 else f'step {step_id}'}] {status}  ({d})  {name}\n   {' '.join(msg.split())}")
        if step_id == 0:
            outcome = status
    return 0 if outcome == "Succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
