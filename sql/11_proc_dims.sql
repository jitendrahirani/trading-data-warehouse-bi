/* Phase 4 step 4.1: dimension load procedures.
   Pattern in every procedure: step row in etl.StepLog -> TRY { BEGIN TRAN; build the source set; MERGE; COMMIT }
   CATCH { ROLLBACK; close the step as Failed; THROW }.
   - Change detection with EXISTS (SELECT source columns EXCEPT SELECT target columns): NULL-safe, unlike a chain of <>.
   - No WHEN NOT MATCHED BY SOURCE ... DELETE: in an incremental run staging holds only a delta, so a missing row means
     'not in this batch', never 'deleted at source'.
   - Attributes that come from an optional source (e.g. model meta) are COALESCEd, so a batch without that file does not
     wipe them.
   - DimDataset is SCD type 2 (decision D5); all others are type 1 (overwrite).
*/
SET NOCOUNT ON;
GO

/* ------------------------------------------------------------------------------------------------ DimDate (generated) */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimDate
    @EtlRunId int, @From date = '20260401', @To date = '20270331'
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimDate', 'dw.DimDate', @step OUTPUT;
    BEGIN TRY
        -- the session-end rule comes from the production contract code (staged in full every run)
        DECLARE @change date, @endBefore time(0), @endFrom time(0);
        SELECT TOP (1) @change = TRY_CONVERT(date, session_end_change_date), @endBefore = TRY_CONVERT(time(0), session_end_before),
               @endFrom = TRY_CONVERT(time(0), session_end_from)
        FROM stg.DatasetContract;
        IF @change IS NULL OR @endBefore IS NULL OR @endFrom IS NULL
            THROW 50001, 'stg.DatasetContract has no session-end rule: run the stage step first.', 1;

        -- holidays: from staging when the calendar file is in this batch, otherwise keep what DimDate already holds
        CREATE TABLE #hol (d date PRIMARY KEY, name nvarchar(60));
        IF EXISTS (SELECT 1 FROM stg.Holiday)
            INSERT #hol (d, name)
            SELECT TRY_CONVERT(date, holiday_date), LEFT(MAX(holiday_name), 60)
            FROM stg.Holiday
            WHERE TRY_CONVERT(date, holiday_date) IS NOT NULL AND market_status = 'holiday'
            GROUP BY TRY_CONVERT(date, holiday_date);
        ELSE
            INSERT #hol (d, name) SELECT [Date], HolidayName FROM dw.DimDate WHERE IsHoliday = 1;

        WITH n AS (SELECT TOP (DATEDIFF(DAY, @From, @To) + 1) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS i
                   FROM sys.all_objects a CROSS JOIN sys.all_objects b),
             d AS (SELECT DATEADD(DAY, i, @From) AS dt FROM n),
             a AS (SELECT d.dt, (DATEPART(WEEKDAY, d.dt) + @@DATEFIRST - 2) % 7 + 1 AS iso, h.name,
                          IIF(h.d IS NULL, 0, 1) AS isHol,
                          IIF(YEAR(d.dt) IN (SELECT DISTINCT YEAR(d) FROM #hol), 1, 0) AS known
                   FROM d LEFT JOIN #hol h ON h.d = d.dt),
             b AS (SELECT *, IIF(iso >= 6, 1, 0) AS isWe, IIF(iso <= 5 AND isHol = 0, 1, 0) AS isTd,
                          YEAR(dt) - IIF(MONTH(dt) < 4, 1, 0) AS fys, (MONTH(dt) + 8) % 12 + 1 AS fmo FROM a)
        SELECT CONVERT(int, CONVERT(char(8), dt, 112)) AS DateKey, dt AS [Date], CAST(YEAR(dt) AS smallint) AS [Year],
               CAST(DATEPART(QUARTER, dt) AS tinyint) AS [Quarter], CAST(MONTH(dt) AS tinyint) AS [Month],
               CAST(CHOOSE(MONTH(dt), 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
                           'October', 'November', 'December') AS varchar(9)) AS MonthName,
               CAST(CONVERT(char(7), dt, 126) AS char(7)) AS YearMonth, CAST(DAY(dt) AS tinyint) AS DayOfMonth,
               CAST(iso AS tinyint) AS DayOfWeekNo,
               CAST(CHOOSE(iso, 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday') AS varchar(9)) AS DayName,
               CAST(DATEPART(ISO_WEEK, dt) AS tinyint) AS IsoWeek, DATEADD(DAY, 1 - iso, dt) AS WeekStartDate,
               CAST(isWe AS bit) AS IsWeekend, CAST(isHol AS bit) AS IsHoliday, name AS HolidayName,
               CAST(known AS bit) AS HolidayCalendarKnown, CAST(isTd AS bit) AS IsTradingDay,
               IIF(isTd = 1, SUM(isTd) OVER (ORDER BY dt ROWS UNBOUNDED PRECEDING), NULL) AS TradingDayIndex,
               IIF(isTd = 1, IIF(dt < @change, @endBefore, @endFrom), NULL) AS SessionEndTime,
               CAST(CONCAT('FY', fys, '-', RIGHT(fys + 1, 2)) AS char(9)) AS FiscalYear, CAST(fys AS smallint) AS FiscalYearStart,
               CAST((fmo - 1) / 3 + 1 AS tinyint) AS FiscalQuarter, CAST(fmo AS tinyint) AS FiscalMonthNo
        INTO #src FROM b;

        BEGIN TRANSACTION;
        MERGE dw.DimDate AS t
        USING #src AS s ON t.DateKey = s.DateKey
        WHEN MATCHED AND EXISTS (
                SELECT s.[Date], s.[Year], s.[Quarter], s.[Month], s.MonthName, s.YearMonth, s.DayOfMonth, s.DayOfWeekNo, s.DayName,
                       s.IsoWeek, s.WeekStartDate, s.IsWeekend, s.IsHoliday, s.HolidayName, s.HolidayCalendarKnown, s.IsTradingDay,
                       s.TradingDayIndex, s.SessionEndTime, s.FiscalYear, s.FiscalYearStart, s.FiscalQuarter, s.FiscalMonthNo
                EXCEPT
                SELECT t.[Date], t.[Year], t.[Quarter], t.[Month], t.MonthName, t.YearMonth, t.DayOfMonth, t.DayOfWeekNo, t.DayName,
                       t.IsoWeek, t.WeekStartDate, t.IsWeekend, t.IsHoliday, t.HolidayName, t.HolidayCalendarKnown, t.IsTradingDay,
                       t.TradingDayIndex, t.SessionEndTime, t.FiscalYear, t.FiscalYearStart, t.FiscalQuarter, t.FiscalMonthNo)
            THEN UPDATE SET [Date] = s.[Date], [Year] = s.[Year], [Quarter] = s.[Quarter], [Month] = s.[Month], MonthName = s.MonthName,
                 YearMonth = s.YearMonth, DayOfMonth = s.DayOfMonth, DayOfWeekNo = s.DayOfWeekNo, DayName = s.DayName, IsoWeek = s.IsoWeek,
                 WeekStartDate = s.WeekStartDate, IsWeekend = s.IsWeekend, IsHoliday = s.IsHoliday, HolidayName = s.HolidayName,
                 HolidayCalendarKnown = s.HolidayCalendarKnown, IsTradingDay = s.IsTradingDay, TradingDayIndex = s.TradingDayIndex,
                 SessionEndTime = s.SessionEndTime, FiscalYear = s.FiscalYear, FiscalYearStart = s.FiscalYearStart,
                 FiscalQuarter = s.FiscalQuarter, FiscalMonthNo = s.FiscalMonthNo
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (DateKey, [Date], [Year], [Quarter], [Month], MonthName, YearMonth, DayOfMonth, DayOfWeekNo, DayName, IsoWeek,
                    WeekStartDate, IsWeekend, IsHoliday, HolidayName, HolidayCalendarKnown, IsTradingDay, TradingDayIndex,
                    SessionEndTime, FiscalYear, FiscalYearStart, FiscalQuarter, FiscalMonthNo)
            VALUES (s.DateKey, s.[Date], s.[Year], s.[Quarter], s.[Month], s.MonthName, s.YearMonth, s.DayOfMonth, s.DayOfWeekNo,
                    s.DayName, s.IsoWeek, s.WeekStartDate, s.IsWeekend, s.IsHoliday, s.HolidayName, s.HolidayCalendarKnown,
                    s.IsTradingDay, s.TradingDayIndex, s.SessionEndTime, s.FiscalYear, s.FiscalYearStart, s.FiscalQuarter, s.FiscalMonthNo)
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsRead = NULL, @RowsInserted = @ins, @RowsUpdated = @upd;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ DimTime (generated) */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimTime @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimTime', 'dw.DimTime', @step OUTPUT;
    BEGIN TRY
        WITH n AS (SELECT TOP (1440) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS m FROM sys.all_objects a CROSS JOIN sys.all_objects b)
        SELECT CAST(m AS smallint) AS TimeKey, CAST(DATEADD(MINUTE, m, CAST('00:00' AS time(0))) AS time(0)) AS TimeOfDay,
               CAST(CONVERT(char(5), DATEADD(MINUTE, m, CAST('00:00' AS time(0))), 108) AS char(5)) AS HHMM,
               CAST(m / 60 AS tinyint) AS HourOfDay, CAST(m % 60 AS tinyint) AS MinuteOfHour,
               CAST(CASE WHEN m < 555 THEN 'Pre-open' WHEN m < 600 THEN 'Open 09:15-09:59' WHEN m < 720 THEN 'Morning 10:00-11:59'
                         WHEN m < 840 THEN 'Midday 12:00-13:59' WHEN m < 915 THEN 'Afternoon 14:00-15:14'
                         WHEN m <= 940 THEN 'Close 15:15-15:40' ELSE 'After hours' END AS varchar(24)) AS SessionBucket,
               CAST(CASE WHEN m < 555 THEN 1 WHEN m < 600 THEN 2 WHEN m < 720 THEN 3 WHEN m < 840 THEN 4 WHEN m < 915 THEN 5
                         WHEN m <= 940 THEN 6 ELSE 7 END AS tinyint) AS SessionBucketSort
        INTO #src FROM n;

        BEGIN TRANSACTION;
        MERGE dw.DimTime AS t USING #src AS s ON t.TimeKey = s.TimeKey
        WHEN MATCHED AND EXISTS (SELECT s.TimeOfDay, s.HHMM, s.HourOfDay, s.MinuteOfHour, s.SessionBucket, s.SessionBucketSort
                                 EXCEPT SELECT t.TimeOfDay, t.HHMM, t.HourOfDay, t.MinuteOfHour, t.SessionBucket, t.SessionBucketSort)
            THEN UPDATE SET TimeOfDay = s.TimeOfDay, HHMM = s.HHMM, HourOfDay = s.HourOfDay, MinuteOfHour = s.MinuteOfHour,
                            SessionBucket = s.SessionBucket, SessionBucketSort = s.SessionBucketSort
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (TimeKey, TimeOfDay, HHMM, HourOfDay, MinuteOfHour, SessionBucket, SessionBucketSort)
            VALUES (s.TimeKey, s.TimeOfDay, s.HHMM, s.HourOfDay, s.MinuteOfHour, s.SessionBucket, s.SessionBucketSort)
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsInserted = @ins, @RowsUpdated = @upd;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ DimDataset (SCD type 2) */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimDataset @EtlRunId int, @AsOfDate date = NULL
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    SET @AsOfDate = ISNULL(@AsOfDate, CAST(SYSDATETIME() AS date));
    DECLARE @step int, @new int = 0, @closed int = 0, @fixed int = 0, @read int;
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimDataset', 'dw.DimDataset', @step OUTPUT;
    BEGIN TRY
        -- the desired CURRENT attributes of every dataset, as the contract code defines them today
        SELECT c.dataset AS DatasetCode, c.dataset_group AS DatasetGroup, c.publication AS Publication, c.cadence AS Cadence,
               CAST(IIF(c.cadence IN ('minute', '5s', 'tick', '5min', '15min'), 1, 0) AS bit) AS IsIntraday,
               c.is_consumed AS IsConsumedByModel,
               TRY_CONVERT(date, c.session_end_change_date) AS ChangeDate,
               TRY_CONVERT(time(0), c.session_end_before) AS EndBefore, TRY_CONVERT(time(0), c.session_end_from) AS EndFrom
        INTO #c FROM stg.DatasetContract c;
        SET @read = @@ROWCOUNT;
        IF @read = 0 THROW 50002, 'stg.DatasetContract is empty: run the stage step first.', 1;

        SELECT *, IIF(IsIntraday = 1, IIF(@AsOfDate >= ChangeDate, EndFrom, EndBefore), NULL) AS SessionEndTime INTO #cur FROM #c;
        ALTER TABLE #cur ADD RowHash binary(32);
        UPDATE #cur SET RowHash = HASHBYTES('SHA2_256', CONCAT(DatasetGroup, N'|', Publication, N'|', Cadence, N'|', IsIntraday, N'|',
                                                              IsConsumedByModel, N'|', ISNULL(CONVERT(nchar(5), SessionEndTime, 108), N'~')));

        BEGIN TRANSACTION;
        -- 1. datasets never seen before: insert their full history (the session-end rule is effective-dated in the source)
        INSERT dw.DimDataset (DatasetCode, DatasetGroup, Publication, Cadence, IsIntraday, IsConsumedByModel, SessionEndTime,
                              ValidFrom, ValidTo, IsCurrent, RowHash, EtlRunId)
        SELECT v.DatasetCode, v.DatasetGroup, v.Publication, v.Cadence, v.IsIntraday, v.IsConsumedByModel, v.SessionEndTime,
               v.ValidFrom, v.ValidTo, v.IsCurrent,
               HASHBYTES('SHA2_256', CONCAT(v.DatasetGroup, N'|', v.Publication, N'|', v.Cadence, N'|', v.IsIntraday, N'|',
                                            v.IsConsumedByModel, N'|', ISNULL(CONVERT(nchar(5), v.SessionEndTime, 108), N'~'))), @EtlRunId
        FROM (
            SELECT c.DatasetCode, c.DatasetGroup, c.Publication, c.Cadence, c.IsIntraday, c.IsConsumedByModel, c.EndBefore AS SessionEndTime,
                   CAST('19000101' AS date) AS ValidFrom, DATEADD(DAY, -1, c.ChangeDate) AS ValidTo, CAST(0 AS bit) AS IsCurrent
            FROM #cur c WHERE c.IsIntraday = 1 AND @AsOfDate >= c.ChangeDate
            UNION ALL
            SELECT c.DatasetCode, c.DatasetGroup, c.Publication, c.Cadence, c.IsIntraday, c.IsConsumedByModel, c.SessionEndTime,
                   IIF(c.IsIntraday = 1 AND @AsOfDate >= c.ChangeDate, c.ChangeDate, CAST('19000101' AS date)), CAST('99991231' AS date), CAST(1 AS bit)
            FROM #cur c
        ) v
        WHERE NOT EXISTS (SELECT 1 FROM dw.DimDataset d WHERE d.DatasetCode = v.DatasetCode);
        SET @new = @@ROWCOUNT;

        -- 2. a changed attribute on a dataset whose current version started today: same-day correction, overwrite (type 1)
        UPDATE d SET DatasetGroup = c.DatasetGroup, Publication = c.Publication, Cadence = c.Cadence, IsIntraday = c.IsIntraday,
                     IsConsumedByModel = c.IsConsumedByModel, SessionEndTime = c.SessionEndTime, RowHash = c.RowHash, EtlRunId = @EtlRunId
        FROM dw.DimDataset d JOIN #cur c ON c.DatasetCode = d.DatasetCode
        WHERE d.IsCurrent = 1 AND d.RowHash <> c.RowHash AND d.ValidFrom >= @AsOfDate;
        SET @fixed = @@ROWCOUNT;

        -- 3. SCD2: close the current version (MERGE ... OUTPUT), then insert the new version from the OUTPUT rows
        CREATE TABLE #changed (DatasetCode varchar(64) PRIMARY KEY);
        MERGE dw.DimDataset AS t
        USING #cur AS s ON t.DatasetCode = s.DatasetCode AND t.IsCurrent = 1
        WHEN MATCHED AND t.RowHash <> s.RowHash AND t.ValidFrom < @AsOfDate
            THEN UPDATE SET IsCurrent = 0, ValidTo = DATEADD(DAY, -1, @AsOfDate), EtlRunId = @EtlRunId
        OUTPUT s.DatasetCode INTO #changed;
        SET @closed = @@ROWCOUNT;

        INSERT dw.DimDataset (DatasetCode, DatasetGroup, Publication, Cadence, IsIntraday, IsConsumedByModel, SessionEndTime,
                              ValidFrom, ValidTo, IsCurrent, RowHash, EtlRunId)
        SELECT c.DatasetCode, c.DatasetGroup, c.Publication, c.Cadence, c.IsIntraday, c.IsConsumedByModel, c.SessionEndTime,
               @AsOfDate, '99991231', 1, c.RowHash, @EtlRunId
        FROM #cur c JOIN #changed x ON x.DatasetCode = c.DatasetCode;
        SET @new += @@ROWCOUNT;
        COMMIT;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsRead = @read, @RowsInserted = @new, @RowsUpdated = @closed;
        IF @fixed > 0 PRINT CONCAT('DimDataset: ', @fixed, ' same-day correction(s) overwritten');
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ DimCheck (seed + findings) */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimCheck @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimCheck', 'dw.DimCheck', @step OUTPUT;
    BEGIN TRY
        -- Reference data kept in code (versioned in git). Category = the BI layer's classification by DQ dimension;
        -- IsMinuteLevel must equal ml_pipeline/dq/gate.py MINUTE_LEVEL_CHECKS (a test checks this).
        DECLARE @seed TABLE (CheckCode varchar(40) PRIMARY KEY, CheckCategory varchar(20), IsMinuteLevel bit, IsCrossDataset bit, CheckDescription nvarchar(300));
        INSERT @seed VALUES
         ('schema',          'Validity',       0, 0, N'Required columns are present with the expected types'),
         ('partition',       'Completeness',   0, 0, N'The day''s partition exists (or is legitimately absent / not yet published)'),
         ('partition_date',  'Validity',       0, 0, N'Dates inside the files match the partition date'),
         ('rows',            'Completeness',   0, 0, N'Row count against the rows expected for the session'),
         ('gap',             'Completeness',   1, 0, N'Gaps between consecutive records (missing minutes)'),
         ('nulls',           'Completeness',   0, 0, N'Share of nulls per column'),
         ('empty',           'Completeness',   0, 0, N'Columns or entities with no values at all'),
         ('frozen',          'Validity',       1, 0, N'Prices that stop changing for too long'),
         ('totals',          'Consistency',    1, 0, N'Cumulative totals (e.g. day volume) never decrease'),
         ('quotes',          'Validity',       1, 0, N'Share of two-sided bid/ask quotes during the session'),
         ('jumps',           'Validity',       1, 0, N'Implausible one-step price jumps'),
         ('dups',            'Uniqueness',     0, 0, N'Duplicate rows on the dataset''s key'),
         ('options',         'Validity',       0, 0, N'Option chain sanity: strikes, expiries, prices'),
         ('lag',             'Timeliness',     0, 0, N'Delay between exchange time and receive time'),
         ('calendar',        'Timeliness',     0, 0, N'The macro-event calendar still covers the coming weeks'),
         ('constituents',    'Completeness',   0, 0, N'Index constituent lists have the expected sizes'),
         ('weights',         'Validity',       0, 0, N'Index weights are present and add up'),
         ('factsheet',       'Completeness',   0, 0, N'Top-10 factsheet rows present for each index'),
         ('universe_review', 'Consistency',    0, 0, N'Stock-universe review statuses are consistent'),
         ('gcs_parity',      'Consistency',    0, 0, N'The local file equals its copy in Google Cloud Storage'),
         ('fut_core_vs_l1',  'Reconciliation', 0, 1, N'X1: minute futures price vs the last 5-second order-book row'),
         ('basis',           'Reconciliation', 0, 1, N'X2: future / spot - 1 within a band, no one-minute jump'),
         ('sector_vs_core',  'Reconciliation', 0, 1, N'X3: sector-bar quote vs the minute snapshot (NIFTY 50, NIFTY BANK, INDIA VIX)'),
         ('chain_vs_surface','Reconciliation', 0, 1, N'X4: option chain vs option surface mid, same minute and contract'),
         ('bars_vs_quotes_volume','Reconciliation', 0, 1, N'X5: T+1 stock bars day volume vs the quote sweep''s running total'),
         ('fut_core_vs_ticks','Reconciliation', 0, 1, N'X6: minute futures price vs the last websocket tick'),
         ('stock_list_vs_manifest','Reconciliation', 0, 1, N'X7: the stock websocket subscribed exactly that morning''s list'),
         ('stock_ticks_coverage','Reconciliation', 0, 1, N'X8: every subscribed stock has future and option ticks all session');

        SELECT s.CheckCode, s.CheckCategory, s.IsMinuteLevel, s.IsCrossDataset, s.CheckDescription INTO #src FROM @seed s
        UNION ALL   -- a check that appears in the findings but not in the seed still gets a row, visibly unclassified
        SELECT DISTINCT LEFT(f.[check], 40), 'Unclassified', 0, IIF(f.dataset = 'cross_dataset', 1, 0), N'Not in the seed list yet'
        FROM stg.DQFinding f WHERE NOT EXISTS (SELECT 1 FROM @seed s WHERE s.CheckCode = f.[check]);

        BEGIN TRANSACTION;
        MERGE dw.DimCheck AS t USING #src AS s ON t.CheckCode = s.CheckCode
        WHEN MATCHED AND EXISTS (SELECT s.CheckCategory, s.IsMinuteLevel, s.IsCrossDataset, s.CheckDescription
                                 EXCEPT SELECT t.CheckCategory, t.IsMinuteLevel, t.IsCrossDataset, t.CheckDescription)
            THEN UPDATE SET CheckCategory = s.CheckCategory, IsMinuteLevel = s.IsMinuteLevel, IsCrossDataset = s.IsCrossDataset,
                            CheckDescription = s.CheckDescription
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (CheckCode, CheckCategory, IsMinuteLevel, IsCrossDataset, CheckDescription)
            VALUES (s.CheckCode, s.CheckCategory, s.IsMinuteLevel, s.IsCrossDataset, s.CheckDescription)
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsInserted = @ins, @RowsUpdated = @upd;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ DimModelRun */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimModelRun @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    DECLARE @prod varchar(30) = 'production v0_2026-10';
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimModelRun', 'dw.DimModelRun', @step OUTPUT;
    BEGIN TRY
        -- every (run set, label mode, horizon) seen in any model source of this batch
        WITH runs AS (
            SELECT DISTINCT run_set, label_mode, horizon_min FROM stg.Trade
            UNION SELECT @prod, ISNULL(label_mode, 'vol'), horizon_min FROM stg.ModelMeta
            UNION SELECT @prod, 'vol', horizon_min FROM stg.ModelPrediction
            UNION SELECT @prod, 'vol', horizon_min FROM stg.ModelMetric)
        SELECT CAST(CONCAT(REPLACE(REPLACE(r.run_set, 'production ', ''), 'research ', 'r'), ':', r.label_mode, ':h', r.horizon_min, 'm') AS varchar(40)) AS RunCode,
               CAST(r.run_set AS varchar(30)) AS RunSet, CAST(r.label_mode AS varchar(12)) AS LabelMode, CAST(r.horizon_min AS tinyint) AS HorizonMin,
               CAST(LEFT(m.model_file, 80) AS varchar(80)) AS ModelName,
               TRY_CONVERT(datetime2(0), STUFF(STUFF(STUFF(STUFF(REPLACE(m.trained_at, '_', ' '), 14, 0, ':'), 12, 0, ':'), 7, 0, '-'), 5, 0, '-')) AS TrainedAt,
               m.n_rows AS TrainRows, CAST(m.n_features AS smallint) AS FeatureCount,
               CAST(IIF(r.run_set = @prod, 1, 0) AS bit) AS IsProduction,
               CAST(IIF(r.run_set = @prod AND r.label_mode = 'vol' AND r.horizon_min = 10, 1, 0) AS bit) AS IsHeadlineRun,
               CAST(IIF(r.run_set = @prod AND r.label_mode = 'vol', 1, 0) AS bit) AS HasOofPredictions
        INTO #src
        FROM runs r
        LEFT JOIN stg.ModelMeta m ON r.run_set = @prod AND m.horizon_min = r.horizon_min AND ISNULL(m.label_mode, 'vol') = r.label_mode;

        BEGIN TRANSACTION;
        MERGE dw.DimModelRun AS t USING #src AS s ON t.RunCode = s.RunCode
        WHEN MATCHED AND EXISTS (SELECT s.RunSet, s.LabelMode, s.HorizonMin, COALESCE(s.ModelName, t.ModelName), COALESCE(s.TrainedAt, t.TrainedAt),
                                        COALESCE(s.TrainRows, t.TrainRows), COALESCE(s.FeatureCount, t.FeatureCount), s.IsProduction, s.IsHeadlineRun, s.HasOofPredictions
                                 EXCEPT SELECT t.RunSet, t.LabelMode, t.HorizonMin, t.ModelName, t.TrainedAt, t.TrainRows, t.FeatureCount,
                                        t.IsProduction, t.IsHeadlineRun, t.HasOofPredictions)
            THEN UPDATE SET RunSet = s.RunSet, LabelMode = s.LabelMode, HorizonMin = s.HorizonMin,
                            ModelName = COALESCE(s.ModelName, t.ModelName), TrainedAt = COALESCE(s.TrainedAt, t.TrainedAt),
                            TrainRows = COALESCE(s.TrainRows, t.TrainRows), FeatureCount = COALESCE(s.FeatureCount, t.FeatureCount),
                            IsProduction = s.IsProduction, IsHeadlineRun = s.IsHeadlineRun, HasOofPredictions = s.HasOofPredictions
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (RunCode, RunSet, LabelMode, HorizonMin, ModelName, TrainedAt, TrainRows, FeatureCount, IsProduction, IsHeadlineRun, HasOofPredictions)
            VALUES (s.RunCode, s.RunSet, s.LabelMode, s.HorizonMin, s.ModelName, s.TrainedAt, s.TrainRows, s.FeatureCount, s.IsProduction,
                    s.IsHeadlineRun, s.HasOofPredictions)
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsInserted = @ins, @RowsUpdated = @upd;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO

/* ------------------------------------------------------------------------------------------------ DimFeature */
CREATE OR ALTER PROCEDURE dw.usp_Load_DimFeature @EtlRunId int
AS
BEGIN
    SET NOCOUNT, XACT_ABORT ON;
    DECLARE @step int, @ins int = 0, @upd int = 0;
    DECLARE @act TABLE (a nvarchar(10));
    EXEC etl.usp_StepStart @EtlRunId, 'dw.usp_Load_DimFeature', 'dw.DimFeature', @step OUTPUT;
    BEGIN TRY
        -- the model's features = the feature columns of stg.PanelWide (generated from the frozen manifest), so every model
        -- feature gets a row even if the workbook misses it; attributes come from the workbook when it is in the batch
        SELECT CAST(c.name AS varchar(64)) AS FeatureName INTO #model
        FROM sys.columns c
        WHERE c.object_id = OBJECT_ID('stg.PanelWide')
          AND c.name NOT IN ('timestamp', 'instrument', 'date', 'EtlRunId', 'SourceFile', 'SourceRowNo', 'LoadedAt');

        WITH x AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY feature_name ORDER BY TRY_CONVERT(int, sr_no)) AS rn FROM stg.Feature)
        SELECT m.FeatureName, CAST(x.feature_group AS nvarchar(60)) AS FeatureGroup, CAST(x.short_description AS nvarchar(2000)) AS ShortDescription,
               CAST(x.status AS nvarchar(400)) AS ProductionStatus, CAST(x.websocket_used AS nvarchar(1000)) AS WebsocketUse,
               TRY_CONVERT(smallint, x.sr_no) AS SrNo, CAST(1 AS bit) AS IsModelFeature, CAST(IIF(x.feature_name IS NULL, 0, 1) AS bit) AS InBatch
        INTO #src
        FROM #model m LEFT JOIN x ON x.feature_name = m.FeatureName AND x.rn = 1;

        BEGIN TRANSACTION;
        MERGE dw.DimFeature AS t USING #src AS s ON t.FeatureName = s.FeatureName
        WHEN MATCHED AND s.InBatch = 1 AND EXISTS (SELECT s.FeatureGroup, s.ShortDescription, s.ProductionStatus, s.WebsocketUse, s.SrNo, s.IsModelFeature
                                                   EXCEPT SELECT t.FeatureGroup, t.ShortDescription, t.ProductionStatus, t.WebsocketUse, t.SrNo, t.IsModelFeature)
            THEN UPDATE SET FeatureGroup = s.FeatureGroup, ShortDescription = s.ShortDescription, ProductionStatus = s.ProductionStatus,
                            WebsocketUse = s.WebsocketUse, SrNo = s.SrNo, IsModelFeature = s.IsModelFeature
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (FeatureName, FeatureGroup, ShortDescription, ProductionStatus, WebsocketUse, SrNo, IsModelFeature)
            VALUES (s.FeatureName, s.FeatureGroup, s.ShortDescription, s.ProductionStatus, s.WebsocketUse, s.SrNo, s.IsModelFeature)
        OUTPUT $action INTO @act;
        COMMIT;
        SELECT @ins = SUM(IIF(a = 'INSERT', 1, 0)), @upd = SUM(IIF(a = 'UPDATE', 1, 0)) FROM @act;
        EXEC etl.usp_StepEnd @step, 'Succeeded', @RowsInserted = @ins, @RowsUpdated = @upd;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        DECLARE @n int = ERROR_NUMBER(), @m nvarchar(4000) = ERROR_MESSAGE();
        EXEC etl.usp_StepEnd @step, 'Failed', @ErrorNumber = @n, @ErrorMessage = @m;
        THROW;
    END CATCH
END;
GO
