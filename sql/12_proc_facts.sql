/* Phase 4 steps 4.2 + 4.3: fact load procedures.
   Pattern: step row -> TRY { type the staged text (TRY_CONVERT), send rows that cannot be typed to etl.RejectRow with the
   reason and the whole row as JSON; BEGIN TRAN; delete the warehouse rows of every file in this batch; insert the batch
   with key lookups (not found -> -1 Unknown); COMMIT } CATCH { ROLLBACK; log; THROW }.
   Delete-by-file + insert makes every load idempotent: re-running a batch, or re-delivering a corrected file, replaces
   that file's rows instead of adding to them.
*/
SET NOCOUNT ON;
GO

/* run_created_utc 'yyyymmddThhmmssZ' -> datetime2(0) */
CREATE OR ALTER FUNCTION etl.ufn_RunTime (@s nvarchar(30))
RETURNS TABLE
AS RETURN (
    SELECT TRY_CONVERT(datetime2(0), STUFF(STUFF(STUFF(STUFF(STUFF(LEFT(@s, 15), 14, 0, ':'), 12, 0, ':'), 9, 1, ' '), 7, 0, '-'), 5, 0, '-')) AS t
);
GO

/* ------------------------------------------------------------------------------------------------ FactDQFinding */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactDQFinding @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0, @rej int = 0, @upd int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactDQFinding', 'dw.FactDQFinding', @step OUTPUT;
    BEGIN TRY
        IF NOT EXISTS (SELECT 1 FROM stg.DQFailClass) AND EXISTS (SELECT 1 FROM dw.FactDQFinding UNION ALL SELECT 1 FROM stg.DQFinding)
            THROW 50003, 'stg.DQFailClass is empty: the stage step must run first (gate classes are recomputed every run).', 1;

        SELECT f.*, TRY_CONVERT(date, f.[date]) AS d, rt.t AS created,
               CONCAT_WS('; ', IIF(TRY_CONVERT(date, f.[date]) IS NULL, 'date is not a date', NULL),
                               IIF(rt.t IS NULL, 'run_created_utc is not yyyymmddThhmmssZ', NULL),
                               IIF(NULLIF(f.minute_from, '') IS NOT NULL AND TRY_CONVERT(time(0), f.minute_from) IS NULL, 'minute_from is not a time', NULL),
                               IIF(NULLIF(f.minute_to, '') IS NOT NULL AND TRY_CONVERT(time(0), f.minute_to) IS NULL, 'minute_to is not a time', NULL)) AS reason
        INTO #t
        FROM stg.DQFinding f CROSS APPLY etl.ufn_RunTime(f.run_created_utc) rt;
        SET @read = @@ROWCOUNT;

        BEGIN TRANSACTION;
        INSERT etl.RejectRow (EtlRunId, StepLogId, TargetTable, SourceFile, SourceRowNo, Reason, RowJson)
        SELECT @EtlRunId, @step, 'dw.FactDQFinding', t.SourceFile, t.SourceRowNo, t.reason,
               (SELECT s.* FROM stg.DQFinding s WHERE s.SourceFile = t.SourceFile AND s.SourceRowNo = t.SourceRowNo FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
        FROM #t t WHERE t.reason <> '';
        SET @rej = @@ROWCOUNT;

        DELETE dw.FactDQFinding WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM #t);
        SET @del = @@ROWCOUNT;

        INSERT dw.FactDQFinding (DateKey, DatasetKey, CheckKey, SeverityKey, GateClassKey, DQRunId, RunCreatedUtc, IsResolvedRun,
                                 ColumnName, Symbol, MinuteFrom, MinuteTo, ValueText, ValueNum, ThresholdText, KnownCause, Detail,
                                 EtlRunId, SourceFile, SourceRowNo)
        SELECT ISNULL(dd.DateKey, -1), ISNULL(ds.DatasetKey, -1), ISNULL(c.CheckKey, -1), ISNULL(sv.SeverityKey, -1), 0,
               t.run_id, t.created, 0,
               NULLIF(t.[column], ''), NULLIF(t.symbol, ''), TRY_CONVERT(time(0), NULLIF(t.minute_from, '')), TRY_CONVERT(time(0), NULLIF(t.minute_to, '')),
               NULLIF(t.[value], ''), TRY_CONVERT(float, NULLIF(t.[value], '')), NULLIF(t.threshold, ''), NULLIF(t.known_cause, ''), NULLIF(t.detail, ''),
               @EtlRunId, t.SourceFile, t.SourceRowNo
        FROM #t t
        LEFT JOIN dw.DimDate dd ON dd.[Date] = t.d
        LEFT JOIN dw.DimDataset ds ON ds.DatasetCode = t.dataset AND t.d BETWEEN ds.ValidFrom AND ds.ValidTo   -- point-in-time SCD2 lookup
        LEFT JOIN dw.DimCheck c ON c.CheckCode = t.[check]
        LEFT JOIN dw.DimSeverity sv ON sv.SeverityCode = t.severity
        WHERE t.reason = '';
        SET @ins = @@ROWCOUNT;

        -- 'Latest run wins' over ALL runs (a new run can supersede an old one): the newest run that evaluated each
        -- date x dataset is the resolved one. Same rule as ml_pipeline/dq/gate.resolve; the reconciliation compares them.
        WITH runs AS (SELECT DISTINCT DateKey, DatasetKey, DQRunId, RunCreatedUtc FROM dw.FactDQFinding),
             ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY DateKey, DatasetKey ORDER BY RunCreatedUtc DESC, DQRunId DESC) AS rn FROM runs)
        UPDATE f SET IsResolvedRun = IIF(r.rn = 1, 1, 0)
        FROM dw.FactDQFinding f
        JOIN ranked r ON r.DateKey = f.DateKey AND r.DatasetKey = f.DatasetKey AND r.DQRunId = f.DQRunId
        WHERE f.IsResolvedRun <> IIF(r.rn = 1, 1, 0);
        SET @upd = @@ROWCOUNT;

        -- gate branch of each FAIL in a resolved run, as classified by the production gate code (stg.DQFailClass)
        UPDATE f SET GateClassKey = x.k
        FROM dw.FactDQFinding f
        JOIN dw.DimSeverity sv ON sv.SeverityKey = f.SeverityKey
        LEFT JOIN stg.DQFailClass fc ON fc.finding_file = f.SourceFile AND fc.finding_row_no = f.SourceRowNo
        LEFT JOIN dw.DimGateClass g ON g.GateClass = fc.gate_class
        CROSS APPLY (SELECT IIF(f.IsResolvedRun = 1 AND sv.SeverityCode = 'fail', ISNULL(g.GateClassKey, -1), 0) AS k) x
        WHERE f.GateClassKey <> x.k;
        SET @upd += @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, @upd, @del, @rej;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactDQDailyStatus */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactDQDailyStatus @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0, @del int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactDQDailyStatus', 'dw.FactDQDailyStatus', @step OUTPUT;
    BEGIN TRY
        -- rebuilt from the whole FactDQFinding (small), so the source is COMPLETE and NOT MATCHED BY SOURCE -> DELETE is safe here
        WITH allruns AS (SELECT DateKey, DatasetKey, COUNT(DISTINCT DQRunId) AS n FROM dw.FactDQFinding GROUP BY DateKey, DatasetKey),
             res AS (
                SELECT f.DateKey, f.DatasetKey, MAX(f.DQRunId) AS rid, MAX(f.RunCreatedUtc) AS rc, COUNT(*) AS chk,
                       SUM(IIF(sv.SeverityCode = 'pass', 1, 0)) AS p, SUM(IIF(sv.SeverityCode = 'note', 1, 0)) AS nt,
                       SUM(IIF(sv.SeverityCode = 'warn', 1, 0)) AS w, SUM(IIF(sv.SeverityCode = 'fail', 1, 0)) AS fl,
                       SUM(IIF(g.IsUnresolved = 1 AND g.ExcludesDay = 1, 1, 0)) AS uday,
                       SUM(IIF(g.IsUnresolved = 1 AND g.MasksMinutes = 1, 1, 0)) AS umin,
                       MAX(f.SeverityKey) AS worst
                FROM dw.FactDQFinding f
                JOIN dw.DimSeverity sv ON sv.SeverityKey = f.SeverityKey
                JOIN dw.DimGateClass g ON g.GateClassKey = f.GateClassKey
                WHERE f.IsResolvedRun = 1
                GROUP BY f.DateKey, f.DatasetKey)
        SELECT r.DateKey, r.DatasetKey, r.rid AS ResolvedRunId, r.rc AS ResolvedRunCreatedUtc, CAST(a.n AS tinyint) AS RunsEvaluated,
               r.chk AS ChecksEvaluated, r.p AS PassCount, r.nt AS NoteCount, r.w AS WarnCount, r.fl AS FailCount,
               r.uday AS UnresolvedDayFails, r.umin AS UnresolvedMinuteFails, CAST(r.worst AS smallint) AS WorstSeverityKey
        INTO #src
        FROM res r JOIN allruns a ON a.DateKey = r.DateKey AND a.DatasetKey = r.DatasetKey;

        BEGIN TRANSACTION;
        MERGE dw.FactDQDailyStatus AS t USING #src AS s ON t.DateKey = s.DateKey AND t.DatasetKey = s.DatasetKey
        WHEN MATCHED AND EXISTS (SELECT s.ResolvedRunId, s.ResolvedRunCreatedUtc, s.RunsEvaluated, s.ChecksEvaluated, s.PassCount, s.NoteCount,
                                        s.WarnCount, s.FailCount, s.UnresolvedDayFails, s.UnresolvedMinuteFails, s.WorstSeverityKey
                                 EXCEPT SELECT t.ResolvedRunId, t.ResolvedRunCreatedUtc, t.RunsEvaluated, t.ChecksEvaluated, t.PassCount, t.NoteCount,
                                        t.WarnCount, t.FailCount, t.UnresolvedDayFails, t.UnresolvedMinuteFails, t.WorstSeverityKey)
            THEN UPDATE SET ResolvedRunId = s.ResolvedRunId, ResolvedRunCreatedUtc = s.ResolvedRunCreatedUtc, RunsEvaluated = s.RunsEvaluated,
                            ChecksEvaluated = s.ChecksEvaluated, PassCount = s.PassCount, NoteCount = s.NoteCount, WarnCount = s.WarnCount,
                            FailCount = s.FailCount, UnresolvedDayFails = s.UnresolvedDayFails, UnresolvedMinuteFails = s.UnresolvedMinuteFails,
                            WorstSeverityKey = s.WorstSeverityKey, EtlRunId = @EtlRunId, LoadedAt = SYSDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (DateKey, DatasetKey, ResolvedRunId, ResolvedRunCreatedUtc, RunsEvaluated, ChecksEvaluated, PassCount, NoteCount, WarnCount,
                    FailCount, UnresolvedDayFails, UnresolvedMinuteFails, WorstSeverityKey, EtlRunId)
            VALUES (s.DateKey, s.DatasetKey, s.ResolvedRunId, s.ResolvedRunCreatedUtc, s.RunsEvaluated, s.ChecksEvaluated, s.PassCount, s.NoteCount,
                    s.WarnCount, s.FailCount, s.UnresolvedDayFails, s.UnresolvedMinuteFails, s.WorstSeverityKey, @EtlRunId)
        WHEN NOT MATCHED BY SOURCE THEN DELETE
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)), @del = SUM(IIF(a = 'DELETE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', NULL, @ins, @upd, @del, 0;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactDQDayVerdict */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactDQDayVerdict @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @upd int = 0, @del int = 0, @rej int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactDQDayVerdict', 'dw.FactDQDayVerdict', @step OUTPUT;
    BEGIN TRY
        -- stg.DQDayVerdict is recomputed in full by the production gate every run; an empty table would mean
        -- 'stage step not run', and the DELETE branch below would wipe every verdict, so refuse instead
        IF NOT EXISTS (SELECT 1 FROM stg.DQDayVerdict)
            THROW 50004, 'stg.DQDayVerdict is empty: the stage step must run first.', 1;
        SELECT v.*, dd.DateKey INTO #t FROM stg.DQDayVerdict v LEFT JOIN dw.DimDate dd ON dd.[Date] = TRY_CONVERT(date, v.[date]);
        SET @read = @@ROWCOUNT;

        BEGIN TRANSACTION;
        INSERT etl.RejectRow (EtlRunId, StepLogId, TargetTable, SourceFile, SourceRowNo, Reason, RowJson)
        SELECT @EtlRunId, @step, 'dw.FactDQDayVerdict', t.SourceFile, t.SourceRowNo, 'date not in DimDate or status not ok/exclude/unchecked',
               (SELECT t.[date], t.status FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
        FROM #t t WHERE t.DateKey IS NULL OR t.status NOT IN ('ok', 'exclude', 'unchecked');
        SET @rej = @@ROWCOUNT;

        MERGE dw.FactDQDayVerdict AS t
        USING (SELECT * FROM #t WHERE DateKey IS NOT NULL AND status IN ('ok', 'exclude', 'unchecked')) AS s ON t.DateKey = s.DateKey
        WHEN MATCHED AND EXISTS (SELECT s.status, s.reasons_n, s.masks_n, s.accepted_n, s.reason_text
                                 EXCEPT SELECT t.Verdict, t.ExcludeReasons, t.MaskedRanges, t.AcceptedFails, t.ReasonText)
            THEN UPDATE SET Verdict = s.status, ExcludeReasons = s.reasons_n, MaskedRanges = s.masks_n, AcceptedFails = s.accepted_n,
                            ReasonText = s.reason_text, EtlRunId = @EtlRunId, LoadedAt = SYSDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (DateKey, Verdict, ExcludeReasons, MaskedRanges, AcceptedFails, ReasonText, EtlRunId)
            VALUES (s.DateKey, s.status, s.reasons_n, s.masks_n, s.accepted_n, s.reason_text, @EtlRunId)
        WHEN NOT MATCHED BY SOURCE THEN DELETE
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)), @del = SUM(IIF(a = 'DELETE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, @upd, @del, @rej;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactModelPrediction */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactModelPrediction @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0, @rej int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactModelPrediction', 'dw.FactModelPrediction', @step OUTPUT;
    BEGIN TRY
        -- inst: 0 = NIFTY, 1 = BANKNIFTY (ml_pipeline/train_baseline.py; verified in Phase 3 against the panel's labels)
        SELECT p.*, x.ts, DATEADD(MINUTE, 330, x.ts) AS ist,
               CASE p.inst WHEN 0 THEN 'NIFTY' WHEN 1 THEN 'BANKNIFTY' END AS instrument,
               CONCAT_WS('; ', IIF(p.ts_ns % 1000000000 <> 0, 'ts is not a whole second', NULL),
                               IIF(p.inst NOT IN (0, 1), 'inst is not 0 or 1', NULL),
                               IIF(p.y NOT IN (0, 1, 2), 'y is not 0, 1 or 2', NULL)) AS reason
        INTO #t
        FROM stg.ModelPrediction p
        CROSS APPLY (SELECT DATEADD(SECOND, CAST(p.ts_ns / 1000000000 AS int), CAST('19700101' AS datetime2(0))) AS ts) x;
        SET @read = @@ROWCOUNT;

        BEGIN TRANSACTION;
        INSERT etl.RejectRow (EtlRunId, StepLogId, TargetTable, SourceFile, SourceRowNo, Reason, RowJson)
        SELECT @EtlRunId, @step, 'dw.FactModelPrediction', t.SourceFile, t.SourceRowNo, t.reason,
               (SELECT t.horizon_min, t.ts_ns, t.inst, t.y FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
        FROM #t t WHERE t.reason <> '';
        SET @rej = @@ROWCOUNT;

        DELETE dw.FactModelPrediction WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM #t);
        SET @del = @@ROWCOUNT;

        INSERT dw.FactModelPrediction WITH (TABLOCK)
               (ModelRunKey, InstrumentKey, TsUtc, DateKey, TimeKey, ActualClassKey, PredictedClassKey, ProbDown, ProbFlat, ProbUp, MaxProb,
                OofPos, EtlRunId, SourceFile, SourceRowNo)
        SELECT ISNULL(r.ModelRunKey, -1), ISNULL(i.InstrumentKey, -1), t.ts, ISNULL(dd.DateKey, -1),
               CAST(DATEPART(HOUR, t.ist) * 60 + DATEPART(MINUTE, t.ist) AS smallint), t.y,
               CASE WHEN t.proba_down >= t.proba_flat AND t.proba_down >= t.proba_up THEN 0   -- argmax, first maximum wins (as numpy)
                    WHEN t.proba_flat >= t.proba_up THEN 1 ELSE 2 END,
               t.proba_down, t.proba_flat, t.proba_up,
               (SELECT MAX(v) FROM (VALUES (t.proba_down), (t.proba_flat), (t.proba_up)) m(v)),
               t.pos, @EtlRunId, t.SourceFile, t.SourceRowNo
        FROM #t t
        LEFT JOIN dw.DimModelRun r ON r.RunCode = CONCAT('v0_2026-10:vol:h', t.horizon_min, 'm')
        LEFT JOIN dw.DimInstrument i ON i.InstrumentCode = t.instrument
        LEFT JOIN dw.DimDate dd ON dd.[Date] = CAST(t.ist AS date)
        WHERE t.reason = '';
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, @rej;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactModelMetric + FactFeatureImportance */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactModelMetric @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactModelMetric', 'dw.FactModelMetric', @step OUTPUT;
    BEGIN TRY
        SELECT @read = COUNT(*) FROM stg.ModelMetric;
        BEGIN TRANSACTION;
        DELETE dw.FactModelMetric WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM stg.ModelMetric);
        SET @del = @@ROWCOUNT;
        INSERT dw.FactModelMetric (ModelRunKey, MetricCode, ActualClassKey, PredictedClassKey, MetricValue, EtlRunId, SourceFile)
        SELECT ISNULL(r.ModelRunKey, -1), m.metric,
               IIF(m.actual_class IS NULL, -2, ISNULL(a.ClassKey, -1)), IIF(m.predicted_class IS NULL, -2, ISNULL(p.ClassKey, -1)),
               m.metric_value, @EtlRunId, m.SourceFile
        FROM stg.ModelMetric m
        LEFT JOIN dw.DimModelRun r ON r.RunCode = CONCAT('v0_2026-10:vol:h', m.horizon_min, 'm')
        LEFT JOIN dw.DimClass a ON a.ClassName = m.actual_class
        LEFT JOIN dw.DimClass p ON p.ClassName = m.predicted_class;
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, 0;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_Load_FactFeatureImportance @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactFeatureImportance', 'dw.FactFeatureImportance', @step OUTPUT;
    BEGIN TRY
        SELECT @read = COUNT(*) FROM stg.FeatureImportance;
        BEGIN TRANSACTION;
        DELETE dw.FactFeatureImportance WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM stg.FeatureImportance);
        SET @del = @@ROWCOUNT;
        INSERT dw.FactFeatureImportance (ModelRunKey, FeatureKey, ImportanceRank, Importance, EtlRunId, SourceFile)
        SELECT ISNULL(r.ModelRunKey, -1), ISNULL(f.FeatureKey, -1), fi.importance_rank, fi.importance, @EtlRunId, fi.SourceFile
        FROM stg.FeatureImportance fi
        LEFT JOIN dw.DimModelRun r ON r.RunCode = CONCAT('v0_2026-10:vol:h', fi.horizon_min, 'm')
        LEFT JOIN dw.DimFeature f ON f.FeatureName = fi.feature;
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, 0;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactTrade */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactTrade @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0, @rej int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactTrade', 'dw.FactTrade', @step OUTPUT;
    BEGIN TRY
        SELECT s.SourceFile, s.SourceRowNo, s.run_set, s.label_mode, s.horizon_min, s.instrument, s.dir, s.direction, s.exit_reason, s.tradingsymbol,
               TRY_CONVERT(datetimeoffset(0), s.decision_ts) AS dec_o, TRY_CONVERT(datetimeoffset(0), s.entry_ts) AS ent_o,
               TRY_CONVERT(datetimeoffset(0), s.exit_ts) AS ext_o,
               TRY_CONVERT(smallint, s.hold_min) AS hold_min, TRY_CONVERT(decimal(9, 1), s.strike) AS strike, TRY_CONVERT(date, s.expiry) AS expiry,
               TRY_CONVERT(smallint, s.lot_size) AS lot_size, TRY_CONVERT(smallint, s.qty) AS qty, TRY_CONVERT(float, s.signal_prob) AS signal_prob,
               TRY_CONVERT(decimal(10, 2), s.entry_price) AS entry_price, TRY_CONVERT(decimal(10, 2), s.exit_price) AS exit_price,
               TRY_CONVERT(decimal(10, 2), s.entry_cost) AS entry_cost, TRY_CONVERT(decimal(10, 2), s.exit_cost) AS exit_cost,
               TRY_CONVERT(decimal(10, 2), s.target_price) AS target_price, TRY_CONVERT(decimal(10, 2), s.stop_price) AS stop_price,
               TRY_CONVERT(decimal(6, 1), s.exit_quote_age_min) AS exit_quote_age_min, TRY_CONVERT(decimal(12, 2), s.opt_net_rs) AS opt_net_rs,
               TRY_CONVERT(decimal(10, 2), s.prem0) AS prem0, TRY_CONVERT(decimal(10, 1), s.entry_px) AS entry_px,
               TRY_CONVERT(decimal(10, 1), s.exit_px) AS exit_px, TRY_CONVERT(float, s.pts_net_slip) AS pts_net_slip,
               TRY_CONVERT(decimal(12, 2), s.fut_net_rs) AS fut_net_rs,
               CAST(CONCAT(REPLACE(REPLACE(s.run_set, 'production ', ''), 'research ', 'r'), ':', s.label_mode, ':h', s.horizon_min, 'm') AS varchar(40)) AS run_code
        INTO #t FROM stg.Trade s;
        SET @read = @@ROWCOUNT;

        ALTER TABLE #t ADD reason nvarchar(400);
        UPDATE #t SET reason = CONCAT_WS('; ',
            IIF(dec_o IS NULL OR ent_o IS NULL OR ext_o IS NULL, 'a timestamp is not a date-time with offset', NULL),
            IIF(ext_o < ent_o, 'exit before entry', NULL),
            IIF(dir NOT IN ('long', 'short'), 'dir is not long/short', NULL),
            IIF(direction NOT IN ('Up', 'Down'), 'direction is not Up/Down', NULL),
            IIF(hold_min IS NULL OR strike IS NULL OR expiry IS NULL OR lot_size IS NULL OR qty IS NULL OR signal_prob IS NULL
                OR entry_price IS NULL OR exit_price IS NULL OR entry_cost IS NULL OR exit_cost IS NULL OR target_price IS NULL
                OR stop_price IS NULL OR exit_quote_age_min IS NULL OR opt_net_rs IS NULL OR prem0 IS NULL OR entry_px IS NULL
                OR exit_px IS NULL OR pts_net_slip IS NULL OR fut_net_rs IS NULL, 'a numeric or date column did not convert', NULL));

        BEGIN TRANSACTION;
        INSERT etl.RejectRow (EtlRunId, StepLogId, TargetTable, SourceFile, SourceRowNo, Reason, RowJson)
        SELECT @EtlRunId, @step, 'dw.FactTrade', t.SourceFile, t.SourceRowNo, t.reason,
               (SELECT s.* FROM stg.Trade s WHERE s.SourceFile = t.SourceFile AND s.SourceRowNo = t.SourceRowNo FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
        FROM #t t WHERE t.reason <> '';
        SET @rej = @@ROWCOUNT;

        DELETE dw.FactTrade WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM #t);
        SET @del = @@ROWCOUNT;

        INSERT dw.FactTrade (ModelRunKey, InstrumentKey, EntryDateKey, ExitDateKey, EntryTimeKey, ExitTimeKey, DecisionTsUtc, EntryTsUtc, ExitTsUtc,
                             Side, Direction, HoldMin, ExitReason, TradingSymbol, Strike, ExpiryDate, LotSize, Qty, SignalProb, EntryPrice, ExitPrice,
                             EntryCostRs, ExitCostRs, TargetPrice, StopPrice, ExitQuoteAgeMin, OptNetRs, Prem0, FutEntryPx, FutExitPx, PtsNetSlip,
                             FutNetRs, EtlRunId, SourceFile, SourceRowNo)
        SELECT ISNULL(r.ModelRunKey, -1), ISNULL(i.InstrumentKey, -1), ISNULL(de.DateKey, -1), ISNULL(dx.DateKey, -1),
               CAST(DATEPART(HOUR, ei.v) * 60 + DATEPART(MINUTE, ei.v) AS smallint), CAST(DATEPART(HOUR, xi.v) * 60 + DATEPART(MINUTE, xi.v) AS smallint),
               CONVERT(datetime2(0), SWITCHOFFSET(t.dec_o, '+00:00')), CONVERT(datetime2(0), SWITCHOFFSET(t.ent_o, '+00:00')),
               CONVERT(datetime2(0), SWITCHOFFSET(t.ext_o, '+00:00')),
               t.dir, t.direction, t.hold_min, t.exit_reason, t.tradingsymbol, t.strike, t.expiry, t.lot_size, t.qty, t.signal_prob,
               t.entry_price, t.exit_price, t.entry_cost, t.exit_cost, t.target_price, t.stop_price, t.exit_quote_age_min, t.opt_net_rs,
               t.prem0, t.entry_px, t.exit_px, t.pts_net_slip, t.fut_net_rs, @EtlRunId, t.SourceFile, t.SourceRowNo
        FROM #t t
        CROSS APPLY (SELECT CONVERT(datetime2(0), SWITCHOFFSET(t.ent_o, '+05:30')) AS v) ei
        CROSS APPLY (SELECT CONVERT(datetime2(0), SWITCHOFFSET(t.ext_o, '+05:30')) AS v) xi
        LEFT JOIN dw.DimModelRun r ON r.RunCode = t.run_code
        LEFT JOIN dw.DimInstrument i ON i.InstrumentCode = t.instrument
        LEFT JOIN dw.DimDate de ON de.[Date] = CAST(ei.v AS date)
        LEFT JOIN dw.DimDate dx ON dx.[Date] = CAST(xi.v AS date)
        WHERE t.reason = '';
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, @rej;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactFeatureDrift */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactFeatureDrift @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0, @rej int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactFeatureDrift', 'dw.FactFeatureDrift', @step OUTPUT;
    BEGIN TRY
        SELECT s.*, TRY_CONVERT(date, s.report_date) AS rd,
               CASE WHEN s.section_title LIKE 'RANGE-SKEW%' THEN 'range-skew' WHEN s.section_title LIKE 'PSI-reliable%' THEN 'psi-reliable'
                    WHEN s.section_title LIKE 'Low effective-N%' THEN 'low-n' END AS section,
               TRY_CONVERT(float, s.psi) AS psi_f, TRY_CONVERT(int, s.n_eff) AS neff,
               TRY_CONVERT(decimal(5, 1), REPLACE(s.in_band, '%', '')) AS inband,
               IIF(CHARINDEX('->', s.null_train_live) > 0, TRY_CONVERT(decimal(5, 1), REPLACE(LTRIM(RTRIM(LEFT(s.null_train_live, CHARINDEX('->', s.null_train_live) - 1))), '%', '')), NULL) AS null_train,
               IIF(CHARINDEX('->', s.null_train_live) > 0, TRY_CONVERT(decimal(5, 1), REPLACE(LTRIM(RTRIM(SUBSTRING(s.null_train_live, CHARINDEX('->', s.null_train_live) + 2, 40))), '%', '')), NULL) AS null_live
        INTO #t FROM stg.FeatureDrift s;
        SET @read = @@ROWCOUNT;
        ALTER TABLE #t ADD reason nvarchar(400);
        UPDATE #t SET reason = CONCAT_WS('; ', IIF(rd IS NULL, 'report_date is not a date', NULL), IIF(section IS NULL, 'unknown section heading', NULL),
                                         IIF(psi_f IS NULL, 'PSI is not a number', NULL));

        BEGIN TRANSACTION;
        INSERT etl.RejectRow (EtlRunId, StepLogId, TargetTable, SourceFile, SourceRowNo, Reason, RowJson)
        SELECT @EtlRunId, @step, 'dw.FactFeatureDrift', t.SourceFile, t.SourceRowNo, t.reason,
               (SELECT t.report_date, t.section_title, t.feature, t.psi FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
        FROM #t t WHERE t.reason <> '';
        SET @rej = @@ROWCOUNT;
        DELETE dw.FactFeatureDrift WHERE SourceFile IN (SELECT DISTINCT SourceFile FROM #t);
        SET @del = @@ROWCOUNT;
        -- the weekly drift report compares live data with the training data of the production h10m model
        INSERT dw.FactFeatureDrift (ReportDateKey, ModelRunKey, FeatureKey, Section, Psi, NEff, InBandPct, NullPctTrain, NullPctLive,
                                    DriftStatus, EtlRunId, SourceFile)
        SELECT ISNULL(dd.DateKey, -1), ISNULL(r.ModelRunKey, -1), ISNULL(f.FeatureKey, -1), t.section, t.psi_f, t.neff, t.inband,
               t.null_train, t.null_live, LEFT(t.status, 16), @EtlRunId, t.SourceFile
        FROM #t t
        LEFT JOIN dw.DimDate dd ON dd.[Date] = t.rd
        LEFT JOIN dw.DimModelRun r ON r.RunCode = 'v0_2026-10:vol:h10m'
        LEFT JOIN dw.DimFeature f ON f.FeatureName = t.feature
        WHERE t.reason = '';
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, @rej;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ FactFeatureValue (unpivot) */
CREATE OR ALTER PROCEDURE dw.usp_Load_FactFeatureValue @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @read int, @ins int = 0, @del int = 0;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_FactFeatureValue', 'dw.FactFeatureValue', @step OUTPUT;
    BEGIN TRY
        SELECT @read = COUNT(*) FROM stg.PanelWide;
        IF @read = 0
        BEGIN
            EXEC etl.usp_StepEnd @step, 'Skipped', 0, 0, 0, 0, 0;   -- the panel file is not in this batch
            RETURN;
        END;
        -- Metadata-driven unpivot: the VALUES list is generated from the staging table's columns, so a feature added to
        -- the model (and to 08_stage_panel.sql) needs no change here.
        DECLARE @values nvarchar(max) = (
            SELECT STRING_AGG(CONVERT(nvarchar(max), CONCAT(N'(', QUOTENAME(c.name, ''''), N', p.', QUOTENAME(c.name), N')')), N', ')
                   WITHIN GROUP (ORDER BY c.column_id)
            FROM sys.columns c
            WHERE c.object_id = OBJECT_ID('stg.PanelWide')
              AND c.name NOT IN ('timestamp', 'instrument', 'date', 'EtlRunId', 'SourceFile', 'SourceRowNo', 'LoadedAt'));
        DECLARE @sql nvarchar(max) = N'
            INSERT dw.FactFeatureValue WITH (TABLOCK) (DateKey, TimeKey, InstrumentKey, FeatureKey, FeatureValue, EtlRunId)
            SELECT ISNULL(dd.DateKey, -1), CAST(DATEPART(HOUR, x.ist) * 60 + DATEPART(MINUTE, x.ist) AS smallint), ISNULL(i.InstrumentKey, -1),
                   ISNULL(f.FeatureKey, -1), v.val, @run
            FROM stg.PanelWide p
            CROSS APPLY (SELECT DATEADD(MINUTE, 330, p.[timestamp]) AS ist) x
            CROSS APPLY (VALUES ' + @values + N') v(feature, val)
            LEFT JOIN dw.DimDate dd ON dd.[Date] = TRY_CONVERT(date, p.[date])
            LEFT JOIN dw.DimInstrument i ON i.InstrumentCode = p.instrument
            LEFT JOIN dw.DimFeature f ON f.FeatureName = v.feature
            WHERE v.val IS NOT NULL
            OPTION (MAXDOP 1);';   -- a parallel TABLOCK insert into the columnstore deadlocked on its own threads
                                   -- ('lock | communication buffer resources', error 1205) on this 8-core, 1.5 GB-capped laptop

        BEGIN TRANSACTION;
        -- one panel file feeds the whole table, so a reload of that file replaces the table (TRUNCATE is minimally logged)
        SELECT @del = COUNT_BIG(*) FROM dw.FactFeatureValue;
        TRUNCATE TABLE dw.FactFeatureValue;
        EXEC sys.sp_executesql @sql, N'@run int', @run = @EtlRunId;
        SET @ins = @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @read, @ins, 0, @del, 0;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO
