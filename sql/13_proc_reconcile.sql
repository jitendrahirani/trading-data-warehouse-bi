/* Phase 4 steps 4.4 + 4.5: reconciliation, batch commit and the master procedure.

   etl.usp_Reconcile writes every result to etl.ReconResult (Status PASS/BREAK is a computed column):
     stg-dw  this batch: rows staged = rows loaded + rows rejected, per fact; rupee control totals for trades
     dw      the whole warehouse: row counts = rows in the source files (file log); no -1 (Unknown) keys;
             SQL 'latest run wins' + gate classes agree with the production gate's own output; SQL day verdicts agree
     target  full runs only: every pre-registered value in etl.Target (from bi/docs/preregistered_targets.json)
   etl.usp_Run_All: dimensions -> facts -> reconcile -> (no BREAK) commit batch -> RunLog Succeeded. Any error or BREAK:
     RunLog Failed, the file log and watermark do not move, and the error is re-thrown (the SQL Agent job step fails).
*/
SET NOCOUNT ON;
GO

CREATE OR ALTER PROCEDURE etl.usp_Reconcile @EtlRunId int, @Mode varchar(12)
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @e decimal(19, 4), @a decimal(19, 4), @rej int;
    EXEC etl.usp_StepStart @EtlRunId, 'etl.usp_Reconcile', 'etl.ReconResult', @step OUTPUT;
    BEGIN TRY
        /* ---------------------------------------------------------------- stg-dw: this batch */
        DECLARE @b TABLE (tbl varchar(128), stg_rows int, dw_rows int, rejected int);
        INSERT @b SELECT 'dw.FactDQFinding', (SELECT COUNT(*) FROM stg.DQFinding),
                         (SELECT COUNT(*) FROM dw.FactDQFinding WHERE SourceFile IN (SELECT SourceFile FROM stg.DQFinding)), 0;
        INSERT @b SELECT 'dw.FactModelPrediction', (SELECT COUNT(*) FROM stg.ModelPrediction),
                         (SELECT COUNT(*) FROM dw.FactModelPrediction WHERE SourceFile IN (SELECT SourceFile FROM stg.ModelPrediction)), 0;
        INSERT @b SELECT 'dw.FactTrade', (SELECT COUNT(*) FROM stg.Trade),
                         (SELECT COUNT(*) FROM dw.FactTrade WHERE SourceFile IN (SELECT SourceFile FROM stg.Trade)), 0;
        INSERT @b SELECT 'dw.FactFeatureDrift', (SELECT COUNT(*) FROM stg.FeatureDrift),
                         (SELECT COUNT(*) FROM dw.FactFeatureDrift WHERE SourceFile IN (SELECT SourceFile FROM stg.FeatureDrift)), 0;
        INSERT @b SELECT 'dw.FactModelMetric', (SELECT COUNT(*) FROM stg.ModelMetric),
                         (SELECT COUNT(*) FROM dw.FactModelMetric WHERE SourceFile IN (SELECT SourceFile FROM stg.ModelMetric)), 0;
        INSERT @b SELECT 'dw.FactFeatureImportance', (SELECT COUNT(*) FROM stg.FeatureImportance),
                         (SELECT COUNT(*) FROM dw.FactFeatureImportance WHERE SourceFile IN (SELECT SourceFile FROM stg.FeatureImportance)), 0;
        INSERT @b SELECT 'dw.FactDQDayVerdict', (SELECT COUNT(*) FROM stg.DQDayVerdict), (SELECT COUNT(*) FROM dw.FactDQDayVerdict), 0;
        UPDATE b SET rejected = (SELECT COUNT(*) FROM etl.RejectRow r WHERE r.EtlRunId = @EtlRunId AND r.TargetTable = b.tbl) FROM @b b;

        DECLARE @tbl varchar(128), @s int, @d int, @r int;
        DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT tbl, stg_rows, dw_rows, rejected FROM @b WHERE stg_rows > 0;
        OPEN c; FETCH NEXT FROM c INTO @tbl, @s, @d, @r;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @e = @s; SET @a = @d + @r;
            EXEC etl.usp_Recon @EtlRunId, 'rows staged = rows loaded + rows rejected', @tbl, 'stg-dw', @e, @a,
                 @Note = 'a rejected row is listed in etl.RejectRow and also breaks the dw completeness check until fixed';
            FETCH NEXT FROM c INTO @tbl, @s, @d, @r;
        END;
        CLOSE c; DEALLOCATE c;

        IF EXISTS (SELECT 1 FROM stg.Trade)
        BEGIN
            SELECT @e = SUM(TRY_CONVERT(decimal(12, 2), opt_net_rs)) FROM stg.Trade;
            SELECT @a = SUM(OptNetRs) FROM dw.FactTrade WHERE SourceFile IN (SELECT SourceFile FROM stg.Trade);
            EXEC etl.usp_Recon @EtlRunId, 'control total: sum of option net P&L (Rs)', 'dw.FactTrade', 'stg-dw', @e, @a, 0.005;
            SELECT @e = SUM(TRY_CONVERT(decimal(12, 2), entry_cost) + TRY_CONVERT(decimal(12, 2), exit_cost)) FROM stg.Trade;
            SELECT @a = SUM(TotalCostRs) FROM dw.FactTrade WHERE SourceFile IN (SELECT SourceFile FROM stg.Trade);
            EXEC etl.usp_Recon @EtlRunId, 'control total: sum of costs (Rs)', 'dw.FactTrade', 'stg-dw', @e, @a, 0.005;
        END;

        IF EXISTS (SELECT 1 FROM stg.PanelWide)
        BEGIN
            DECLARE @cnt nvarchar(max) = (SELECT STRING_AGG(CONVERT(nvarchar(max), CONCAT(N'COUNT_BIG(', QUOTENAME(name), N')')), N' + ')
                                          FROM sys.columns WHERE object_id = OBJECT_ID('stg.PanelWide')
                                            AND name NOT IN ('timestamp', 'instrument', 'date', 'EtlRunId', 'SourceFile', 'SourceRowNo', 'LoadedAt'));
            DECLARE @nn bigint, @q nvarchar(max) = N'SELECT @x = ' + @cnt + N' FROM stg.PanelWide';
            EXEC sys.sp_executesql @q, N'@x bigint OUTPUT', @x = @nn OUTPUT;
            SET @e = @nn; SELECT @a = COUNT_BIG(*) FROM dw.FactFeatureValue;
            EXEC etl.usp_Recon @EtlRunId, 'non-null panel cells staged = rows loaded (unpivot)', 'dw.FactFeatureValue', 'stg-dw', @e, @a;
        END;

        /* ---------------------------------------------------------------- dw: the whole warehouse */
        -- the current state of every source file: this batch's version, else the last committed one
        SELECT b.SourceFile, b.SourceName, b.RowsInFile INTO #files FROM etl.BatchFile b WHERE b.EtlRunId = @EtlRunId
        UNION ALL
        SELECT l.SourceFile, l.SourceName, l.RowsInFile FROM etl.SourceFileLog l
        WHERE NOT EXISTS (SELECT 1 FROM etl.BatchFile b WHERE b.EtlRunId = @EtlRunId AND b.SourceFile = l.SourceFile);

        SELECT @e = SUM(RowsInFile) FROM #files WHERE SourceFile LIKE '%/findings.csv';
        SELECT @a = COUNT(*) FROM dw.FactDQFinding;
        EXEC etl.usp_Recon @EtlRunId, 'rows = rows in all findings files', 'dw.FactDQFinding', 'dw', @e, @a;
        SELECT @e = SUM(RowsInFile) FROM #files WHERE SourceFile LIKE '%.npz';
        SELECT @a = COUNT(*) FROM dw.FactModelPrediction;
        EXEC etl.usp_Recon @EtlRunId, 'rows = rows in all prediction files', 'dw.FactModelPrediction', 'dw', @e, @a;
        SELECT @e = SUM(RowsInFile) FROM #files WHERE SourceName = 'S05';
        SELECT @a = COUNT(*) FROM dw.FactTrade;
        EXEC etl.usp_Recon @EtlRunId, 'rows = rows in all trade files', 'dw.FactTrade', 'dw', @e, @a;
        SELECT @e = SUM(RowsInFile) FROM #files WHERE SourceName = 'S06';
        SELECT @a = COUNT(*) FROM dw.FactFeatureDrift;
        EXEC etl.usp_Recon @EtlRunId, 'rows = rows in all drift reports', 'dw.FactFeatureDrift', 'dw', @e, @a;

        -- Unknown (-1) keys: a failed lookup loads (so totals stay complete) but must be zero to pass
        DECLARE @u TABLE (tbl varchar(128), n bigint);
        INSERT @u SELECT 'dw.FactDQFinding', COUNT(*) FROM dw.FactDQFinding WHERE -1 IN (DateKey, DatasetKey, CheckKey, SeverityKey, GateClassKey);
        INSERT @u SELECT 'dw.FactDQDailyStatus', COUNT(*) FROM dw.FactDQDailyStatus WHERE -1 IN (DateKey, DatasetKey, WorstSeverityKey);
        INSERT @u SELECT 'dw.FactModelPrediction', COUNT(*) FROM dw.FactModelPrediction WHERE -1 IN (ModelRunKey, InstrumentKey, DateKey, ActualClassKey);
        INSERT @u SELECT 'dw.FactModelMetric', COUNT(*) FROM dw.FactModelMetric WHERE -1 IN (ModelRunKey, ActualClassKey, PredictedClassKey);
        INSERT @u SELECT 'dw.FactFeatureImportance', COUNT(*) FROM dw.FactFeatureImportance WHERE -1 IN (ModelRunKey, FeatureKey);
        INSERT @u SELECT 'dw.FactTrade', COUNT(*) FROM dw.FactTrade WHERE -1 IN (ModelRunKey, InstrumentKey, EntryDateKey, ExitDateKey);
        INSERT @u SELECT 'dw.FactFeatureDrift', COUNT(*) FROM dw.FactFeatureDrift WHERE -1 IN (ReportDateKey, ModelRunKey, FeatureKey);
        INSERT @u SELECT 'dw.FactFeatureValue', COUNT_BIG(*) FROM dw.FactFeatureValue WHERE -1 IN (DateKey, InstrumentKey, FeatureKey);
        DECLARE u CURSOR LOCAL FAST_FORWARD FOR SELECT tbl, n FROM @u;
        DECLARE @un bigint;
        OPEN u; FETCH NEXT FROM u INTO @tbl, @un;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @a = @un;
            EXEC etl.usp_Recon @EtlRunId, 'rows with an Unknown (-1) key', @tbl, 'dw', 0, @a;
            FETCH NEXT FROM u INTO @tbl, @un;
        END;
        CLOSE u; DEALLOCATE u;

        -- SQL 'latest run wins' vs the production gate: every gate-classified FAIL must be a resolved FAIL here, and back
        SELECT @a = COUNT(*) FROM stg.DQFailClass fc
        WHERE NOT EXISTS (SELECT 1 FROM dw.FactDQFinding f JOIN dw.DimSeverity sv ON sv.SeverityKey = f.SeverityKey
                          WHERE f.SourceFile = fc.finding_file AND f.SourceRowNo = fc.finding_row_no AND f.IsResolvedRun = 1 AND sv.SeverityCode = 'fail');
        EXEC etl.usp_Recon @EtlRunId, 'gate-classified FAILs not resolved FAILs in SQL', 'dw.FactDQFinding', 'dw', 0, @a;
        SELECT @a = COUNT(*) FROM dw.FactDQFinding f JOIN dw.DimSeverity sv ON sv.SeverityKey = f.SeverityKey
        WHERE f.IsResolvedRun = 1 AND sv.SeverityCode = 'fail'
          AND NOT EXISTS (SELECT 1 FROM stg.DQFailClass fc WHERE fc.finding_file = f.SourceFile AND fc.finding_row_no = f.SourceRowNo);
        EXEC etl.usp_Recon @EtlRunId, 'resolved FAILs in SQL the gate did not classify', 'dw.FactDQFinding', 'dw', 0, @a;

        -- the day verdict re-derived in SQL from the warehouse vs the production gate's verdict, for every date
        WITH sqlv AS (
            SELECT d.DateKey,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM dw.FactDQFinding f JOIN dw.DimDataset ds ON ds.DatasetKey = f.DatasetKey
                                         WHERE f.DateKey = d.DateKey AND f.IsResolvedRun = 1 AND ds.DatasetCode = 'collector_a_minute_core') THEN 'unchecked'
                        WHEN EXISTS (SELECT 1 FROM dw.FactDQFinding f JOIN dw.DimGateClass g ON g.GateClassKey = f.GateClassKey
                                     WHERE f.DateKey = d.DateKey AND g.ExcludesDay = 1)
                          OR EXISTS (SELECT 1 FROM stg.DQDecision x JOIN dw.DimDate dd ON dd.[Date] = TRY_CONVERT(date, x.[date])
                                     WHERE dd.DateKey = d.DateKey AND x.action = 'exclude_day' AND x.dataset = '*') THEN 'exclude'
                        ELSE 'ok' END AS v
            FROM (SELECT DISTINCT DateKey FROM dw.FactDQFinding) d)
        SELECT @a = COUNT(*) FROM sqlv s FULL JOIN dw.FactDQDayVerdict v ON v.DateKey = s.DateKey
        WHERE s.v IS NULL OR v.Verdict IS NULL OR s.v <> v.Verdict;
        EXEC etl.usp_Recon @EtlRunId, 'dates where the SQL verdict differs from the gate', 'dw.FactDQDayVerdict', 'dw', 0, @a;

        /* ---------------------------------------------------------------- target: pre-registered values (full runs) */
        IF @Mode = 'full'
        BEGIN
            CREATE TABLE #act (CheckName varchar(100) PRIMARY KEY, Actual decimal(19, 4));
            INSERT #act SELECT 'FactDQFinding rows', COUNT(*) FROM dw.FactDQFinding;
            INSERT #act SELECT 'FactDQFinding runs', COUNT(DISTINCT DQRunId) FROM dw.FactDQFinding;
            INSERT #act SELECT 'FactDQFinding datasets', COUNT(DISTINCT ds.DatasetCode) FROM dw.FactDQFinding f JOIN dw.DimDataset ds ON ds.DatasetKey = f.DatasetKey;
            INSERT #act SELECT 'FactDQFinding checks', COUNT(DISTINCT CheckKey) FROM dw.FactDQFinding;
            INSERT #act SELECT 'FactDQFinding resolved rows', COUNT(*) FROM dw.FactDQFinding WHERE IsResolvedRun = 1;
            INSERT #act SELECT CONCAT('FactDQFinding rows severity=', sv.SeverityCode), COUNT(f.FindingId)
                FROM dw.DimSeverity sv LEFT JOIN dw.FactDQFinding f ON f.SeverityKey = sv.SeverityKey WHERE sv.SeverityKey >= 0 GROUP BY sv.SeverityCode;
            INSERT #act SELECT CONCAT('FactDQFinding resolved severity=', sv.SeverityCode), COUNT(f.FindingId)
                FROM dw.DimSeverity sv LEFT JOIN dw.FactDQFinding f ON f.SeverityKey = sv.SeverityKey AND f.IsResolvedRun = 1 WHERE sv.SeverityKey >= 0 GROUP BY sv.SeverityCode;
            INSERT #act SELECT CONCAT('FactDQFinding class=', g.GateClass), COUNT(f.FindingId)
                FROM dw.DimGateClass g LEFT JOIN dw.FactDQFinding f ON f.GateClassKey = g.GateClassKey WHERE g.IsFail = 1 GROUP BY g.GateClass;
            INSERT #act SELECT 'FactDQDailyStatus pairs', COUNT(*) FROM dw.FactDQDailyStatus;
            INSERT #act SELECT 'FactDQDailyStatus dates', COUNT(DISTINCT DateKey) FROM dw.FactDQDailyStatus;
            INSERT #act SELECT 'FactDQDailyStatus checks evaluated', SUM(ChecksEvaluated) FROM dw.FactDQDailyStatus;
            INSERT #act SELECT CONCAT('FactDQDayVerdict verdict=', v.Verdict), COUNT(*) FROM dw.FactDQDayVerdict v GROUP BY v.Verdict;
            INSERT #act SELECT 'FactModelPrediction rows', COUNT(*) FROM dw.FactModelPrediction;
            INSERT #act SELECT CONCAT('FactModelPrediction rows h', r.HorizonMin), COUNT(*)
                FROM dw.FactModelPrediction p JOIN dw.DimModelRun r ON r.ModelRunKey = p.ModelRunKey GROUP BY r.HorizonMin;
            INSERT #act SELECT CONCAT('FactModelPrediction accuracy h', r.HorizonMin), AVG(CAST(p.IsCorrect AS decimal(19, 6)))
                FROM dw.FactModelPrediction p JOIN dw.DimModelRun r ON r.ModelRunKey = p.ModelRunKey GROUP BY r.HorizonMin;
            INSERT #act SELECT CONCAT('FactModelPrediction confusion h', r.HorizonMin, ' actual=', a.ClassName, ' pred=', pc.ClassName), COUNT(p.TsUtc)
                FROM dw.DimModelRun r CROSS JOIN dw.DimClass a CROSS JOIN dw.DimClass pc
                LEFT JOIN dw.FactModelPrediction p ON p.ModelRunKey = r.ModelRunKey AND p.ActualClassKey = a.ClassKey AND p.PredictedClassKey = pc.ClassKey
                WHERE r.HasOofPredictions = 1 AND a.ClassKey >= 0 AND pc.ClassKey >= 0
                GROUP BY r.HorizonMin, a.ClassName, pc.ClassName;
            INSERT #act SELECT 'FactFeatureImportance rows', COUNT(*) FROM dw.FactFeatureImportance;
            INSERT #act SELECT 'FactTrade rows', COUNT(*) FROM dw.FactTrade;
            INSERT #act SELECT CONCAT('FactTrade [', r.RunSet, ' | ', r.LabelMode, ' | h', r.HorizonMin, 'm] trades'), COUNT(t.TradeId)
                FROM dw.DimModelRun r LEFT JOIN dw.FactTrade t ON t.ModelRunKey = r.ModelRunKey WHERE r.ModelRunKey > 0 GROUP BY r.RunSet, r.LabelMode, r.HorizonMin;
            INSERT #act SELECT CONCAT('FactTrade [', r.RunSet, ' | ', r.LabelMode, ' | h', r.HorizonMin, 'm] opt_net_rs'), ISNULL(SUM(t.OptNetRs), 0)
                FROM dw.DimModelRun r LEFT JOIN dw.FactTrade t ON t.ModelRunKey = r.ModelRunKey WHERE r.ModelRunKey > 0 GROUP BY r.RunSet, r.LabelMode, r.HorizonMin;
            INSERT #act SELECT CONCAT('FactTrade [', r.RunSet, ' | ', r.LabelMode, ' | h', r.HorizonMin, 'm] costs_rs'), ISNULL(SUM(t.TotalCostRs), 0)
                FROM dw.DimModelRun r LEFT JOIN dw.FactTrade t ON t.ModelRunKey = r.ModelRunKey WHERE r.ModelRunKey > 0 GROUP BY r.RunSet, r.LabelMode, r.HorizonMin;
            INSERT #act SELECT 'FactFeatureDrift rows', COUNT(*) FROM dw.FactFeatureDrift;
            INSERT #act SELECT 'FactFeatureValue rows', COUNT_BIG(*) FROM dw.FactFeatureValue;
            INSERT #act SELECT 'DimFeature rows', COUNT(*) FROM dw.DimFeature WHERE FeatureKey > 0;
            INSERT #act SELECT 'DimModelRun rows', COUNT(*) FROM dw.DimModelRun WHERE ModelRunKey > 0;
            INSERT #act SELECT 'DimDataset current datasets', COUNT(*) FROM dw.DimDataset WHERE IsCurrent = 1 AND DatasetKey > 0;
            INSERT #act SELECT 'DimCheck rows', COUNT(*) FROM dw.DimCheck WHERE CheckKey > 0;

            -- every target must have an actual (a NULL actual is a BREAK), and the names are the contract
            INSERT etl.ReconResult (EtlRunId, CheckName, TableName, Layer, ExpectedValue, ActualValue, Tolerance, Note)
            SELECT @EtlRunId, t.CheckName, t.TableName, 'target', t.ExpectedValue, a.Actual, t.Tolerance,
                   IIF(a.CheckName IS NULL, 'no actual computed for this target name', NULL)
            FROM etl.Target t LEFT JOIN #act a ON a.CheckName = t.CheckName;
        END;
        EXEC etl.usp_StepEnd @step, 'Succeeded';
    END TRY
    BEGIN CATCH
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* Moves this run's staged files into etl.SourceFileLog and the DQ-run watermark forward. Called only after a clean reconcile. */
CREATE OR ALTER PROCEDURE etl.usp_CommitBatch @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @n int;
    EXEC etl.usp_StepStart @EtlRunId, 'etl.usp_CommitBatch', 'etl.SourceFileLog', @step OUTPUT;
    BEGIN TRY
        BEGIN TRANSACTION;
        MERGE etl.SourceFileLog AS t
        USING (SELECT * FROM etl.BatchFile WHERE EtlRunId = @EtlRunId) AS s ON t.SourceFile = s.SourceFile
        WHEN MATCHED THEN UPDATE SET SourceName = s.SourceName, Sha256 = s.Sha256, Bytes = s.Bytes, RowsInFile = s.RowsInFile,
                                     LastLoadRunId = @EtlRunId, LastLoadedAt = SYSDATETIME()
        WHEN NOT MATCHED BY TARGET THEN INSERT (SourceFile, SourceName, Sha256, Bytes, RowsInFile, FirstLoadRunId, LastLoadRunId)
             VALUES (s.SourceFile, s.SourceName, s.Sha256, s.Bytes, s.RowsInFile, @EtlRunId, @EtlRunId);
        SET @n = @@ROWCOUNT;

        DECLARE @wm nvarchar(64) = (SELECT MAX(created_utc) FROM stg.DQRun);
        IF @wm IS NOT NULL
            MERGE etl.Watermark AS t USING (SELECT 'dq_runs' AS SourceName) AS s ON t.SourceName = s.SourceName
            WHEN MATCHED AND t.LastValue < @wm THEN UPDATE SET LastValue = @wm, EtlRunId = @EtlRunId, UpdatedAt = SYSDATETIME()
            WHEN NOT MATCHED THEN INSERT (SourceName, LastValue, ValueMeaning, EtlRunId)
                 VALUES ('dq_runs', @wm, N'created_utc of the newest DQ run loaded (yyyymmddThhmmssZ)', @EtlRunId);
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsInserted = @n;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @en int = ERROR_NUMBER(), @em nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @en, @ErrorMessage = @em;
        THROW;
    END CATCH
END;
GO

/* The master procedure: everything after staging. @EtlRunId NULL = the newest run still 'Running' (the SQL Agent job's
   T-SQL step after its Python stage step). */
CREATE OR ALTER PROCEDURE etl.usp_Run_All @EtlRunId int = NULL, @AsOfDate date = NULL
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    IF @EtlRunId IS NULL
        SELECT TOP (1) @EtlRunId = EtlRunId FROM etl.RunLog WHERE Status = 'Running' ORDER BY EtlRunId DESC;
    IF @EtlRunId IS NULL
        THROW 50010, 'No ETL run is waiting (Status = Running): run the stage step first.', 1;
    DECLARE @mode varchar(12) = (SELECT RunType FROM etl.RunLog WHERE EtlRunId = @EtlRunId);
    BEGIN TRY
        IF EXISTS (SELECT 1 FROM etl.StepLog WHERE EtlRunId = @EtlRunId AND Status = 'Failed')
            THROW 50011, 'A stage step of this run failed: the warehouse load will not start.', 1;
        EXEC dw.usp_Load_DimDate @EtlRunId;
        EXEC dw.usp_Load_DimTime @EtlRunId;
        EXEC dw.usp_Load_DimDataset @EtlRunId, @AsOfDate;
        EXEC dw.usp_Load_DimCheck @EtlRunId;
        EXEC dw.usp_Load_DimModelRun @EtlRunId;
        EXEC dw.usp_Load_DimFeature @EtlRunId;
        EXEC dw.usp_Load_FactDQFinding @EtlRunId;
        EXEC dw.usp_Load_FactDQDailyStatus @EtlRunId;
        EXEC dw.usp_Load_FactDQDayVerdict @EtlRunId;
        EXEC dw.usp_Load_FactModelPrediction @EtlRunId;
        EXEC dw.usp_Load_FactModelMetric @EtlRunId;
        EXEC dw.usp_Load_FactFeatureImportance @EtlRunId;
        EXEC dw.usp_Load_FactTrade @EtlRunId;
        EXEC dw.usp_Load_FactFeatureDrift @EtlRunId;
        EXEC dw.usp_Load_FactFeatureValue @EtlRunId;
        EXEC etl.usp_Reconcile @EtlRunId, @mode;
        DECLARE @breaks int = (SELECT COUNT(*) FROM etl.ReconResult WHERE EtlRunId = @EtlRunId AND Status = 'BREAK');
        IF @breaks > 0
        BEGIN
            DECLARE @msg nvarchar(400) = CONCAT(@breaks, ' reconciliation break(s) in run ', @EtlRunId, ': see etl.ReconResult. The file log was not advanced.');
            THROW 50012, @msg, 1;
        END;
        EXEC etl.usp_CommitBatch @EtlRunId;
        UPDATE etl.RunLog SET Status = 'Succeeded', EndedAt = SYSDATETIME() WHERE EtlRunId = @EtlRunId;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        UPDATE etl.RunLog SET Status = 'Failed', EndedAt = SYSDATETIME(), ErrorMessage = LEFT(ERROR_MESSAGE(), 4000) WHERE EtlRunId = @EtlRunId;
        THROW;
    END CATCH
END;
GO
