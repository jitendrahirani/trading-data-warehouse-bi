/* Phase 4: logging helpers used by every load procedure.
   The step row is written BEFORE the procedure opens its transaction, and closed AFTER commit or rollback, so a failed
   step is still visible in etl.StepLog with its error (requirement N6, KPI K11). */
SET NOCOUNT ON;
GO

CREATE OR ALTER PROCEDURE etl.usp_StepStart
    @EtlRunId int, @StepName varchar(100), @TargetTable varchar(128), @StepLogId int OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    INSERT etl.StepLog (EtlRunId, StepName, TargetTable) VALUES (@EtlRunId, @StepName, @TargetTable);
    SET @StepLogId = SCOPE_IDENTITY();
END;
GO

CREATE OR ALTER PROCEDURE etl.usp_StepEnd
    @StepLogId int, @Status varchar(10),
    @RowsRead int = NULL, @RowsInserted int = NULL, @RowsUpdated int = NULL, @RowsDeleted int = NULL, @RowsRejected int = NULL,
    @ErrorNumber int = NULL, @ErrorMessage nvarchar(4000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE etl.StepLog
       SET EndedAt = SYSDATETIME(), Status = @Status, RowsRead = @RowsRead, RowsInserted = @RowsInserted,
           RowsUpdated = @RowsUpdated, RowsDeleted = @RowsDeleted, RowsRejected = @RowsRejected,
           ErrorNumber = @ErrorNumber, ErrorMessage = @ErrorMessage
     WHERE StepLogId = @StepLogId;
END;
GO

/* Adds one reconciliation result. Status (PASS/BREAK) is a computed column of etl.ReconResult. */
CREATE OR ALTER PROCEDURE etl.usp_Recon
    @EtlRunId int, @CheckName varchar(100), @TableName varchar(128), @Layer varchar(20),
    @Expected decimal(19, 4), @Actual decimal(19, 4), @Tolerance decimal(19, 4) = 0, @Note nvarchar(400) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    INSERT etl.ReconResult (EtlRunId, CheckName, TableName, Layer, ExpectedValue, ActualValue, Tolerance, Note)
    VALUES (@EtlRunId, @CheckName, @TableName, @Layer, @Expected, @Actual, @Tolerance, @Note);
END;
GO
