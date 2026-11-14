"""Phase 4 step 4.5: create (or re-create) the SQL Server Agent job that runs the nightly load.

    bi_env python bi/etl/create_agent_job.py            # create / replace the job
    bi_env python bi/etl/create_agent_job.py --show     # print the T-SQL only

Job 'TradingBI - nightly load' (Mon-Fri 17:00, after the post-market DQ run at about 16:00 IST):
  step 1  Stage (Python, CmdExec)  load_stage.py --mode incremental --stage-only   on failure: quit, job fails
  step 2  Warehouse (T-SQL)        EXEC etl.usp_Run_All                            on failure: quit, job fails
Output of both steps goes to a file next to the SQL Server error log; a failed job also writes to the Windows event log.
Paths are taken from where this repo and the bi_env Python are on this machine, so nothing personal is hard-coded in git.

Prerequisites (yours, because they change Windows services / permissions):
  - the SQL Server Agent service must be running (startup type Manual on this laptop)
  - the CmdExec step runs as the Agent service account (NT SERVICE\\SQLSERVERAGENT): it needs READ access to the repo
    folder and the bi_env folder. It logs in to SQL Server with Windows authentication as that account.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyodbc

from check_setup import connection_string, load_sql_settings

JOB = "TradingBI - nightly load"
BI = Path(__file__).resolve().parents[1]


def job_sql(python: Path, script: Path, database: str) -> str:
    stage_cmd = f'"{python}" "{script}" --mode incremental --stage-only'
    q = lambda s: s.replace("'", "''")
    return f"""
SET NOCOUNT ON;
DECLARE @job sysname = N'{q(JOB)}', @job_id uniqueidentifier, @out nvarchar(4000);
SET @out = LEFT(CAST(SERVERPROPERTY('ErrorLogFileName') AS nvarchar(4000)),
                LEN(CAST(SERVERPROPERTY('ErrorLogFileName') AS nvarchar(4000))) - CHARINDEX('\\', REVERSE(CAST(SERVERPROPERTY('ErrorLogFileName') AS nvarchar(4000)))))
           + N'\\TradingBI_nightly_load.txt';

IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = @job)
    EXEC msdb.dbo.sp_delete_job @job_name = @job, @delete_unused_schedule = 1;

EXEC msdb.dbo.sp_add_job @job_name = @job, @enabled = 1, @owner_login_name = N'{q(load_owner())}',
     @notify_level_eventlog = 2,   -- write to the Windows application event log when the job fails
     @description = N'Nightly BI load: stage new/changed source files (Python), then dimensions, facts, reconciliation and commit (etl.usp_Run_All). See bi/README and etl.RunLog.',
     @job_id = @job_id OUTPUT;

EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_id = 1, @step_name = N'1 Stage new and changed files (Python)',
     @subsystem = N'CmdExec', @command = N'{q(stage_cmd)}',
     @on_success_action = 3,       -- go to the next step
     @on_fail_action = 2,          -- quit the job reporting failure
     @retry_attempts = 1, @retry_interval = 5, @output_file_name = @out, @flags = 0;

EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_id = 2, @step_name = N'2 Load warehouse, reconcile, commit (T-SQL)',
     @subsystem = N'TSQL', @database_name = N'{q(database)}', @command = N'EXEC etl.usp_Run_All;',
     @on_success_action = 1,       -- quit reporting success
     @on_fail_action = 2, @output_file_name = @out, @flags = 2;   -- 2 = append to the output file

EXEC msdb.dbo.sp_add_jobschedule @job_id = @job_id, @name = N'Weekdays 17:00',
     @freq_type = 8, @freq_interval = 62, @freq_recurrence_factor = 1,   -- weekly, Monday-Friday
     @active_start_time = 170000;

EXEC msdb.dbo.sp_add_jobserver @job_id = @job_id, @server_name = N'(local)';
SELECT @job_id AS job_id, @out AS output_file;
"""


def load_owner() -> str:
    s, _ = load_sql_settings()
    with pyodbc.connect(connection_string(s, "master"), timeout=10) as c:
        return c.cursor().execute("SELECT SUSER_SNAME()").fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    s, _ = load_sql_settings()
    sql = job_sql(Path(sys.executable), BI / "etl" / "load_stage.py", s["database_dev"])
    if a.show:
        print(sql)
        return 0
    with pyodbc.connect(connection_string(s, "msdb"), timeout=10, autocommit=True) as c:
        cur = c.cursor()
        cur.execute(sql)
        while cur.description is None and cur.nextset():
            pass
        job_id, out = cur.fetchone()
    print(f"created SQL Agent job '{JOB}' ({job_id}); step output file: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
