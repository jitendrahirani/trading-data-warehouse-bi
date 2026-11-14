/* Phase 5 steps 5.3 + 5.4: objects created by the tuning work (re-runnable).
   - IX_FactModelPrediction_Date: found by experiment E2 (SQL Server's own missing-index suggestion in the 'before' plan):
     report pages filter predictions by date; without it every date filter scans the whole 82 MB clustered index.
   - IX_FactDQFinding_Severity: supports severity filters on the DQ pages, and is the index that makes parameter sniffing
     visible in experiment E5.
   - schema tune: demo objects for walkthroughs (parameter-sniffing procedures, the rowstore copy for E3).
*/
SET NOCOUNT ON;
IF SCHEMA_ID(N'tune') IS NULL EXEC (N'CREATE SCHEMA tune AUTHORIZATION dbo');
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dw.FactModelPrediction') AND name = 'IX_FactModelPrediction_Date')
    CREATE INDEX IX_FactModelPrediction_Date ON dw.FactModelPrediction (DateKey)
        INCLUDE (ModelRunKey, InstrumentKey, ActualClassKey, PredictedClassKey);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID('dw.FactDQFinding') AND name = 'IX_FactDQFinding_Severity')
    CREATE INDEX IX_FactDQFinding_Severity ON dw.FactDQFinding (SeverityKey);
GO

/* E5 parameter sniffing: the same query three ways. Severity is very skewed: pass 24,194 rows, fail 79. */
CREATE OR ALTER PROCEDURE tune.usp_FindingsBySeverity @SeverityKey smallint
AS
SELECT f.FindingId, f.DateKey, f.DatasetKey, f.CheckKey, f.DQRunId, f.Detail
FROM dw.FactDQFinding f
WHERE f.SeverityKey = @SeverityKey;
GO

CREATE OR ALTER PROCEDURE tune.usp_FindingsBySeverity_Recompile @SeverityKey smallint
AS
SELECT f.FindingId, f.DateKey, f.DatasetKey, f.CheckKey, f.DQRunId, f.Detail
FROM dw.FactDQFinding f
WHERE f.SeverityKey = @SeverityKey
OPTION (RECOMPILE);          -- a fresh plan for every call: optimal for each value, costs a compile each time
GO

CREATE OR ALTER PROCEDURE tune.usp_FindingsBySeverity_Unknown @SeverityKey smallint
AS
SELECT f.FindingId, f.DateKey, f.DatasetKey, f.CheckKey, f.DQRunId, f.Detail
FROM dw.FactDQFinding f
WHERE f.SeverityKey = @SeverityKey
OPTION (OPTIMIZE FOR (@SeverityKey UNKNOWN));   -- one plan from the average (density), whatever value comes first
GO
