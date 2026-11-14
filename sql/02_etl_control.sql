/* Phase 2 step 2.2: ETL control tables.
   Every load writes here, so any number on a report can be traced to the run, the step and the source file
   that produced it, and every reconciliation result is kept (requirements N2, N3, N6; KPIs K09-K11).
*/
SET NOCOUNT ON;

/* One row per ETL run: when, who, which code version, outcome. */
CREATE TABLE etl.RunLog (
    EtlRunId        int IDENTITY(1,1) NOT NULL CONSTRAINT PK_RunLog PRIMARY KEY, -- One ETL run | ETL | surrogate key
    RunType         varchar(12)    NOT NULL CONSTRAINT CK_RunLog_RunType CHECK (RunType IN ('full', 'incremental', 'test')), -- Kind of run | ETL | full, incremental, test
    StartedAt       datetime2(3)   NOT NULL CONSTRAINT DF_RunLog_StartedAt DEFAULT SYSDATETIME(), -- Run start (laptop local time) | ETL |
    EndedAt         datetime2(3)   NULL, -- Run end; NULL while running or if the process died | ETL |
    Status          varchar(10)    NOT NULL CONSTRAINT DF_RunLog_Status DEFAULT 'Running' -- Run outcome | ETL | Running, Succeeded, Failed
                                   CONSTRAINT CK_RunLog_Status CHECK (Status IN ('Running', 'Succeeded', 'Failed')),
    TriggeredBy     nvarchar(128)  NOT NULL CONSTRAINT DF_RunLog_TriggeredBy DEFAULT SUSER_SNAME(), -- Windows login that started the run | SUSER_SNAME() | SQL Agent runs show the Agent service account
    HostName        nvarchar(128)  NOT NULL CONSTRAINT DF_RunLog_HostName DEFAULT HOST_NAME(), -- Machine that started the run | HOST_NAME() |
    GitCommit       varchar(40)    NULL, -- git commit of the ETL code | ETL (git rev-parse) | ties every row to the code that produced it
    ErrorMessage    nvarchar(4000) NULL, -- First error, when Status = Failed | ETL |
    DurationSec     AS DATEDIFF(SECOND, StartedAt, EndedAt) -- EndedAt - StartedAt, seconds | computed | K11
);

/* One row per step of an ETL run: rows read/inserted/updated/deleted/rejected and duration. */
CREATE TABLE etl.StepLog (
    StepLogId       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_StepLog PRIMARY KEY, -- One step of a run | ETL | surrogate key
    EtlRunId        int            NOT NULL CONSTRAINT FK_StepLog_RunLog REFERENCES etl.RunLog (EtlRunId), -- Run the step belongs to | etl.RunLog |
    StepName        varchar(100)   NOT NULL, -- Step name, e.g. load_stage:findings | ETL |
    TargetTable     varchar(128)   NULL, -- Table the step writes | ETL |
    StartedAt       datetime2(3)   NOT NULL CONSTRAINT DF_StepLog_StartedAt DEFAULT SYSDATETIME(), -- Step start | ETL |
    EndedAt         datetime2(3)   NULL, -- Step end | ETL |
    Status          varchar(10)    NOT NULL CONSTRAINT DF_StepLog_Status DEFAULT 'Running' -- Step outcome | ETL | Running, Succeeded, Failed, Skipped
                                   CONSTRAINT CK_StepLog_Status CHECK (Status IN ('Running', 'Succeeded', 'Failed', 'Skipped')),
    RowsRead        int            NULL, -- Rows read from the source | ETL |
    RowsInserted    int            NULL, -- Rows inserted | ETL (@@ROWCOUNT / MERGE OUTPUT) |
    RowsUpdated     int            NULL, -- Rows updated | ETL |
    RowsDeleted     int            NULL, -- Rows deleted (late-arriving replacement) | ETL |
    RowsRejected    int            NULL, -- Rows sent to etl.RejectRow | ETL |
    ErrorNumber     int            NULL, -- ERROR_NUMBER() when the step failed | TRY...CATCH |
    ErrorMessage    nvarchar(4000) NULL, -- ERROR_MESSAGE() when the step failed | TRY...CATCH |
    DurationMs      AS DATEDIFF(MILLISECOND, StartedAt, EndedAt) -- EndedAt - StartedAt, milliseconds | computed | K11
);
CREATE INDEX IX_StepLog_EtlRunId ON etl.StepLog (EtlRunId);

/* One row per source row the ETL could not load, with the reason and the row as JSON. */
CREATE TABLE etl.RejectRow (
    RejectId        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_RejectRow PRIMARY KEY, -- One rejected source row | ETL | surrogate key
    EtlRunId        int            NOT NULL CONSTRAINT FK_RejectRow_RunLog REFERENCES etl.RunLog (EtlRunId), -- Run that rejected it | etl.RunLog |
    StepLogId       int            NULL     CONSTRAINT FK_RejectRow_StepLog REFERENCES etl.StepLog (StepLogId), -- Step that rejected it | etl.StepLog |
    TargetTable     varchar(128)   NOT NULL, -- Table the row was meant for | ETL |
    SourceFile      nvarchar(260)  NULL, -- File the row came from | ETL | lineage
    SourceRowNo     int            NULL, -- Row number in that file | ETL | lineage
    Reason          nvarchar(400)  NOT NULL, -- Why it was rejected | ETL | e.g. unparseable date
    RowJson         nvarchar(max)  NULL, -- The whole source row as JSON | ETL | for investigation
    RejectedAt      datetime2(3)   NOT NULL CONSTRAINT DF_RejectRow_RejectedAt DEFAULT SYSDATETIME() -- When it was rejected | ETL |
);

/* One row per reconciliation check of a run: expected vs actual, PASS or BREAK. */
CREATE TABLE etl.ReconResult (
    ReconId         int IDENTITY(1,1) NOT NULL CONSTRAINT PK_ReconResult PRIMARY KEY, -- One reconciliation check result | ETL | surrogate key
    EtlRunId        int            NOT NULL CONSTRAINT FK_ReconResult_RunLog REFERENCES etl.RunLog (EtlRunId), -- Run that performed the check | etl.RunLog |
    CheckName       varchar(100)   NOT NULL, -- What was compared, e.g. row count, sum of OptNetRs | etl.usp_Reconcile |
    TableName       varchar(128)   NOT NULL, -- Table checked | etl.usp_Reconcile |
    Layer           varchar(20)    NOT NULL CONSTRAINT CK_ReconResult_Layer CHECK (Layer IN ('source-stg', 'stg-dw', 'dw', 'target')), -- What was checked | load_stage.py + etl.usp_Reconcile | source-stg = files vs staging; stg-dw = this batch staged vs loaded; dw = whole-warehouse completeness, key integrity, rule equality; target = pre-registered values
    ExpectedValue   decimal(19, 4) NULL, -- Expected value (source count or pre-registered target) | bi/docs/preregistered_targets.json |
    ActualValue     decimal(19, 4) NULL, -- Value found in the warehouse | etl.usp_Reconcile |
    Tolerance       decimal(19, 4) NOT NULL CONSTRAINT DF_ReconResult_Tolerance DEFAULT 0, -- Allowed absolute difference | etl.usp_Reconcile | 0 for counts and rupee sums
    Status          AS CAST(CASE WHEN ExpectedValue IS NULL OR ActualValue IS NULL THEN 'BREAK' -- PASS when ABS(expected - actual) <= tolerance, else BREAK | computed | K09; a missing value is a BREAK
                                 WHEN ABS(ExpectedValue - ActualValue) <= Tolerance THEN 'PASS'
                                 ELSE 'BREAK' END AS varchar(5)) PERSISTED,
    Note            nvarchar(400)  NULL, -- Free-text note | etl.usp_Reconcile |
    CheckedAt       datetime2(3)   NOT NULL CONSTRAINT DF_ReconResult_CheckedAt DEFAULT SYSDATETIME() -- When the check ran | ETL |
);
CREATE INDEX IX_ReconResult_EtlRunId ON etl.ReconResult (EtlRunId);

/* One row per incremental source: the highest value already loaded. */
CREATE TABLE etl.Watermark (
    SourceName      varchar(60)    NOT NULL CONSTRAINT PK_Watermark PRIMARY KEY, -- Source the watermark belongs to | ETL | e.g. dq_runs
    LastValue       nvarchar(64)   NOT NULL, -- Highest value already loaded | ETL | for dq_runs: the newest run created_utc
    ValueMeaning    nvarchar(200)  NOT NULL, -- What LastValue means | ETL |
    EtlRunId        int            NULL     CONSTRAINT FK_Watermark_RunLog REFERENCES etl.RunLog (EtlRunId), -- Run that last moved the watermark | etl.RunLog |
    UpdatedAt       datetime2(3)   NOT NULL CONSTRAINT DF_Watermark_UpdatedAt DEFAULT SYSDATETIME() -- When it last moved | ETL |
);

/* One row per source file loaded: hash, size, rows, first and last loading run. */
CREATE TABLE etl.SourceFileLog (
    SourceFile      nvarchar(260)  NOT NULL CONSTRAINT PK_SourceFileLog PRIMARY KEY, -- Source file path relative to the repo | ETL | natural key
    SourceName      varchar(60)    NOT NULL, -- Which source (S01-S09) | ETL |
    Sha256          char(64)       NOT NULL, -- SHA-256 of the file at load time | ETL | a changed hash means the file must be reloaded
    Bytes           bigint         NOT NULL, -- File size | ETL |
    RowsInFile      int            NULL, -- Data rows in the file | ETL | source side of the reconciliation
    FirstLoadRunId  int            NOT NULL CONSTRAINT FK_SourceFileLog_First REFERENCES etl.RunLog (EtlRunId), -- Run that first loaded the file | etl.RunLog |
    LastLoadRunId   int            NOT NULL CONSTRAINT FK_SourceFileLog_Last  REFERENCES etl.RunLog (EtlRunId), -- Run that last (re)loaded the file | etl.RunLog |
    LastLoadedAt    datetime2(3)   NOT NULL CONSTRAINT DF_SourceFileLog_LastLoadedAt DEFAULT SYSDATETIME() -- When it was last loaded | ETL |
);
GO
