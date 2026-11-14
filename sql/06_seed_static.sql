/* Phase 2 step 2.3: the Unknown (-1) member of every dimension, plus the small static reference dimensions
   (severity, class, gate class, instrument). Re-runnable: inserts only what is missing.
   The data-driven dimensions (DimDate, DimTime, DimDataset, DimCheck, DimModelRun, DimFeature) are loaded by the
   Phase 4 procedures.
*/
SET NOCOUNT ON;
SET XACT_ABORT ON;
BEGIN TRANSACTION;

-- Unknown members ---------------------------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM dw.DimDate WHERE DateKey = -1)
    INSERT dw.DimDate (DateKey, [Date], [Year], [Quarter], [Month], MonthName, YearMonth, DayOfMonth, DayOfWeekNo, DayName,
                       IsoWeek, WeekStartDate, IsWeekend, IsHoliday, HolidayName, HolidayCalendarKnown, IsTradingDay,
                       TradingDayIndex, SessionEndTime, FiscalYear, FiscalYearStart, FiscalQuarter, FiscalMonthNo)
    VALUES (-1, '19000101', 1900, 1, 1, 'Unknown', 'Unknown', 1, 1, 'Unknown', 1, '19000101', 0, 0, NULL, 0, 0,
            NULL, NULL, 'Unknown', 1900, 1, 1);

IF NOT EXISTS (SELECT 1 FROM dw.DimTime WHERE TimeKey = -1)
    INSERT dw.DimTime (TimeKey, TimeOfDay, HHMM, HourOfDay, MinuteOfHour, SessionBucket, SessionBucketSort)
    VALUES (-1, NULL, '--:--', NULL, NULL, 'Unknown', 99);

IF NOT EXISTS (SELECT 1 FROM dw.DimDataset WHERE DatasetKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimDataset ON;
    INSERT dw.DimDataset (DatasetKey, DatasetCode, DatasetGroup, Publication, Cadence, IsIntraday, IsConsumedByModel,
                          SessionEndTime, ValidFrom, ValidTo, IsCurrent, RowHash)
    VALUES (-1, 'Unknown', N'Unknown', 'unknown', 'unknown', 0, 0, NULL, '19000101', '99991231', 1, 0x00);
    SET IDENTITY_INSERT dw.DimDataset OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimCheck WHERE CheckKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimCheck ON;
    INSERT dw.DimCheck (CheckKey, CheckCode, CheckCategory, IsMinuteLevel, IsCrossDataset, CheckDescription)
    VALUES (-1, 'Unknown', 'Unknown', 0, 0, N'Check not found in DimCheck');
    SET IDENTITY_INSERT dw.DimCheck OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimModelRun WHERE ModelRunKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimModelRun ON;
    INSERT dw.DimModelRun (ModelRunKey, RunCode, RunSet, LabelMode, HorizonMin, IsProduction, IsHeadlineRun, HasOofPredictions)
    VALUES (-1, 'Unknown', 'Unknown', 'unknown', 0, 0, 0, 0);
    SET IDENTITY_INSERT dw.DimModelRun OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimFeature WHERE FeatureKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimFeature ON;
    INSERT dw.DimFeature (FeatureKey, FeatureName, FeatureGroup, IsModelFeature)
    VALUES (-1, 'Unknown', N'Unknown', 0);
    SET IDENTITY_INSERT dw.DimFeature OFF;
END;

-- Static reference dimensions (merge by key, so a changed label is corrected on re-run) ------------------------
MERGE dw.DimInstrument AS t
USING (VALUES (-1, 'Unknown',   N'Unknown',     N'Unknown'),
              ( 1, 'NIFTY',     N'Nifty 50',    N'NIFTY desk'),
              ( 2, 'BANKNIFTY', N'Nifty Bank',  N'BANKNIFTY desk')) AS s (InstrumentKey, InstrumentCode, InstrumentName, DeskName)
ON t.InstrumentKey = s.InstrumentKey
WHEN MATCHED AND (t.InstrumentCode <> s.InstrumentCode OR t.InstrumentName <> s.InstrumentName OR t.DeskName <> s.DeskName)
    THEN UPDATE SET InstrumentCode = s.InstrumentCode, InstrumentName = s.InstrumentName, DeskName = s.DeskName
WHEN NOT MATCHED THEN INSERT (InstrumentKey, InstrumentCode, InstrumentName, DeskName)
    VALUES (s.InstrumentKey, s.InstrumentCode, s.InstrumentName, s.DeskName);

MERGE dw.DimSeverity AS t
USING (VALUES (-1, 'unknown', N'Unknown', 0, '#9E9E9E'),
              ( 0, 'pass',    N'Pass',    0, '#2E7D32'),
              ( 1, 'note',    N'Note',    0, '#1565C0'),
              ( 2, 'warn',    N'Warning', 1, '#EF6C00'),
              ( 3, 'fail',    N'Fail',    1, '#C62828')) AS s (SeverityKey, SeverityCode, SeverityLabel, IsProblem, ColorHex)
ON t.SeverityKey = s.SeverityKey
WHEN MATCHED AND (t.SeverityCode <> s.SeverityCode OR t.SeverityLabel <> s.SeverityLabel OR t.IsProblem <> s.IsProblem OR t.ColorHex <> s.ColorHex)
    THEN UPDATE SET SeverityCode = s.SeverityCode, SeverityLabel = s.SeverityLabel, IsProblem = s.IsProblem, ColorHex = s.ColorHex
WHEN NOT MATCHED THEN INSERT (SeverityKey, SeverityCode, SeverityLabel, IsProblem, ColorHex)
    VALUES (s.SeverityKey, s.SeverityCode, s.SeverityLabel, s.IsProblem, s.ColorHex);

MERGE dw.DimClass AS t
USING (VALUES (-2, 'Not applicable', 0, 98),
              (-1, 'Unknown',        0, 99),
              ( 0, 'Down',           1, 1),
              ( 1, 'Flat',           0, 2),
              ( 2, 'Up',             1, 3)) AS s (ClassKey, ClassName, IsDirectional, SortOrder)
ON t.ClassKey = s.ClassKey
WHEN MATCHED AND (t.ClassName <> s.ClassName OR t.IsDirectional <> s.IsDirectional OR t.SortOrder <> s.SortOrder)
    THEN UPDATE SET ClassName = s.ClassName, IsDirectional = s.IsDirectional, SortOrder = s.SortOrder
WHEN NOT MATCHED THEN INSERT (ClassKey, ClassName, IsDirectional, SortOrder)
    VALUES (s.ClassKey, s.ClassName, s.IsDirectional, s.SortOrder);

-- Gate classes: same names and order as bi/etl/dq_rules.py FAIL_CLASSES (the Phase 4 load checks this).
MERGE dw.DimGateClass AS t
USING (VALUES (-1, 'Unknown',                    0, 0, 0, 0, 99),
              ( 0, 'not a FAIL',                 0, 0, 0, 0, 0),
              ( 1, 'not consumed by the model',  1, 0, 0, 0, 1),
              ( 2, 'by design now',              1, 0, 0, 0, 2),
              ( 3, 'accepted by decision',       1, 0, 0, 0, 3),
              ( 4, 'excluded by decision',       1, 0, 1, 0, 4),
              ( 5, 'unresolved: minutes masked', 1, 1, 0, 1, 5),
              ( 6, 'unresolved: day excluded',   1, 1, 1, 0, 6))
      AS s (GateClassKey, GateClass, IsFail, IsUnresolved, ExcludesDay, MasksMinutes, SortOrder)
ON t.GateClassKey = s.GateClassKey
WHEN MATCHED AND (t.GateClass <> s.GateClass OR t.IsFail <> s.IsFail OR t.IsUnresolved <> s.IsUnresolved
                  OR t.ExcludesDay <> s.ExcludesDay OR t.MasksMinutes <> s.MasksMinutes OR t.SortOrder <> s.SortOrder)
    THEN UPDATE SET GateClass = s.GateClass, IsFail = s.IsFail, IsUnresolved = s.IsUnresolved, ExcludesDay = s.ExcludesDay,
                    MasksMinutes = s.MasksMinutes, SortOrder = s.SortOrder
WHEN NOT MATCHED THEN INSERT (GateClassKey, GateClass, IsFail, IsUnresolved, ExcludesDay, MasksMinutes, SortOrder)
    VALUES (s.GateClassKey, s.GateClass, s.IsFail, s.IsUnresolved, s.ExcludesDay, s.MasksMinutes, s.SortOrder);

COMMIT TRANSACTION;
GO
