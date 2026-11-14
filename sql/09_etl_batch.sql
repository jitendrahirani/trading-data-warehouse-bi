/* Phase 4: two more ETL control tables.
   etl.BatchFile - the files staged by a run, waiting to be committed to etl.SourceFileLog. The Python stage step writes
                   it; etl.usp_CommitBatch moves it to SourceFileLog only after the warehouse load AND the reconciliation
                   passed. So the file log (what is 'already loaded') can never run ahead of the warehouse.
   etl.Target    - the pre-registered targets from bi/docs/preregistered_targets.json, as rows, so the reconciliation
                   procedure can compare the warehouse with them in SQL. The JSON in git stays the source of truth.
*/
SET NOCOUNT ON;

/* One row per file staged by an ETL run (pending until the run commits). */
CREATE TABLE etl.BatchFile (
    EtlRunId         int           NOT NULL CONSTRAINT FK_BatchFile_RunLog REFERENCES etl.RunLog (EtlRunId), -- Run that staged the file | load_stage.py |
    SourceFile       nvarchar(260) NOT NULL, -- File path relative to the repo | extract.py |
    SourceName       varchar(60)   NOT NULL, -- Source code S01-S10 | extract.py |
    Sha256           char(64)      NOT NULL, -- SHA-256 of the file when staged | extract.py |
    Bytes            bigint        NOT NULL, -- File size | extract.py |
    RowsInFile       int           NULL, -- Data rows the reader found | extract.py | expected side of the completeness checks
    CONSTRAINT PK_BatchFile PRIMARY KEY (EtlRunId, SourceFile)
);

/* One row per pre-registered target (expected value) the full-load reconciliation checks. */
CREATE TABLE etl.Target (
    CheckName        varchar(100)  NOT NULL CONSTRAINT PK_Target PRIMARY KEY, -- Name of the check, e.g. FactTrade rows | load_stage.py | from preregistered_targets.json
    TableName        varchar(128)  NOT NULL, -- Warehouse table the target is for | load_stage.py |
    ExpectedValue    decimal(19, 4) NOT NULL, -- Pre-registered value | bi/docs/preregistered_targets.json | committed to git before any load
    Tolerance        decimal(19, 4) NOT NULL, -- Allowed absolute difference | preregistered_targets.json | 0 for counts; 0.005 for rupee sums; 0.0005 for accuracy
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_Target_LoadedAt DEFAULT SYSDATETIME() -- When the targets were (re)read from the JSON | SQL Server |
);
GO
