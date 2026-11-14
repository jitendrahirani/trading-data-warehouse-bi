/* Phase 5 step 5.5: the rpt schema = the contract between the warehouse and Power BI / the paginated report.
   - Reports read ONLY these views, never dw.* directly: the warehouse can be refactored without breaking a report as long
     as the view keeps its columns.
   - Friendly column names, only the columns a report needs; no lineage columns (they stay in dw for audit).
   - rpt.Date has no Unknown (-1) row: Power BI's 'Mark as date table' needs one row per day, contiguous.
   - The class dimension is exposed twice (rpt.ActualClass, rpt.PredictedClass): a confusion matrix needs actual on rows
     AND predicted on columns at the same time, which one table with an inactive relationship cannot give.
   - rpt.FeatureValueDaily aggregates the 8.7-million-row fact to day x instrument x feature (about 23,000 rows): enough
     for the reports, and the columnstore answers it in batch mode.
   The line in /* */ above each view becomes its MS_Description (bi/etl/apply_ddl.py) and appears in the data dictionary.
*/
SET NOCOUNT ON;
GO

/* Calendar for every report page: one row per day 2026-04-01..2027-03-31 (no Unknown row), Indian fiscal year. */
CREATE OR ALTER VIEW rpt.[Date] AS
SELECT DateKey, [Date], [Year], CONCAT('Q', [Quarter]) AS [Quarter], [Month] AS [Month No], MonthName AS [Month], YearMonth AS [Year-Month],
       DayOfMonth AS [Day of Month], DayOfWeekNo AS [Weekday No], DayName AS [Weekday], IsoWeek AS [ISO Week], WeekStartDate AS [Week Start],
       IsWeekend AS [Is Weekend], IsHoliday AS [Is Holiday], HolidayName AS [Holiday], IsTradingDay AS [Is Trading Day],
       TradingDayIndex AS [Trading Day No], SessionEndTime AS [Session End], FiscalYear AS [Fiscal Year],
       CONCAT('FQ', FiscalQuarter) AS [Fiscal Quarter], FiscalMonthNo AS [Fiscal Month No]
FROM dw.DimDate
WHERE DateKey <> -1;
GO

/* Minute of the trading day (IST) with session buckets. */
CREATE OR ALTER VIEW rpt.[Time] AS
SELECT TimeKey, TimeOfDay AS [Time], HHMM AS [HH:MM], HourOfDay AS [Hour], SessionBucket AS [Session Bucket], SessionBucketSort AS [Session Bucket Order]
FROM dw.DimTime;
GO

/* Underlying index = trading desk; the row-level-security dimension. */
CREATE OR ALTER VIEW rpt.Instrument AS
SELECT InstrumentKey, InstrumentCode AS Instrument, InstrumentName AS [Instrument Name], DeskName AS Desk
FROM dw.DimInstrument;
GO

/* Dataset contract versions (SCD type 2); facts point at the version valid on their date. */
CREATE OR ALTER VIEW rpt.Dataset AS
SELECT DatasetKey, DatasetCode AS Dataset, DatasetGroup AS [Dataset Group], Publication, Cadence, IsIntraday AS [Is Intraday],
       IsConsumedByModel AS [Consumed by Model], SessionEndTime AS [Session End], ValidFrom AS [Valid From], ValidTo AS [Valid To],
       IsCurrent AS [Is Current Version]
FROM dw.DimDataset;
GO

/* DQ checks with their category (DQ dimension) and description. */
CREATE OR ALTER VIEW rpt.[Check] AS
SELECT CheckKey, CheckCode AS [Check], CheckCategory AS [Check Category], IsMinuteLevel AS [Minute Level], IsCrossDataset AS [Cross-Dataset],
       CheckDescription AS [Check Description]
FROM dw.DimCheck;
GO

/* Severity with its sort order and report colour. */
CREATE OR ALTER VIEW rpt.Severity AS
SELECT SeverityKey, SeverityLabel AS Severity, SeverityKey AS [Severity Order], IsProblem AS [Is Problem], ColorHex AS [Colour]
FROM dw.DimSeverity;
GO

/* Branch of the production DQ gate a finding takes (0 = not a FAIL). */
CREATE OR ALTER VIEW rpt.GateClass AS
SELECT GateClassKey, GateClass AS [Gate Class], IsFail AS [Is Fail], IsUnresolved AS [Is Unresolved], ExcludesDay AS [Excludes Day],
       MasksMinutes AS [Masks Minutes], SortOrder AS [Gate Class Order]
FROM dw.DimGateClass;
GO

/* Model run x label mode x horizon (production baseline and research runs). */
CREATE OR ALTER VIEW rpt.ModelRun AS
SELECT ModelRunKey, RunCode AS [Model Run], RunSet AS [Run Set], LabelMode AS [Label Mode], HorizonMin AS [Horizon (min)],
       CONCAT(RunSet, ' | ', LabelMode, ' | h', HorizonMin, 'm') AS Scenario, ModelName AS Model, TrainedAt AS [Trained At],
       TrainRows AS [Training Rows], FeatureCount AS Features, IsProduction AS [Is Production], IsHeadlineRun AS [Is Headline Run],
       HasOofPredictions AS [Has OOF Predictions]
FROM dw.DimModelRun;
GO

/* Actual class of a prediction (role-playing copy of the class dimension). */
CREATE OR ALTER VIEW rpt.ActualClass AS
SELECT ClassKey AS ActualClassKey, ClassName AS [Actual Class], SortOrder AS [Actual Class Order], IsDirectional AS [Actual Is Directional]
FROM dw.DimClass;
GO

/* Predicted class of a prediction (role-playing copy of the class dimension). */
CREATE OR ALTER VIEW rpt.PredictedClass AS
SELECT ClassKey AS PredictedClassKey, ClassName AS [Predicted Class], SortOrder AS [Predicted Class Order], IsDirectional AS [Predicted Is Directional]
FROM dw.DimClass;
GO

/* Model features with group and description from the feature workbook. */
CREATE OR ALTER VIEW rpt.Feature AS
SELECT FeatureKey, FeatureName AS Feature, FeatureGroup AS [Feature Group], ShortDescription AS [Description], ProductionStatus AS [Production Status]
FROM dw.DimFeature;
GO

/* Every DQ finding of every run (drill-through detail). */
CREATE OR ALTER VIEW rpt.DQFinding AS
SELECT FindingId, DateKey, DatasetKey, CheckKey, SeverityKey, GateClassKey, DQRunId AS [DQ Run], RunCreatedUtc AS [Run Created (UTC)],
       IsResolvedRun AS [Is Resolved Run], ColumnName AS [Column], Symbol, MinuteFrom AS [Minute From], MinuteTo AS [Minute To],
       ValueText AS [Value], ValueNum AS [Value (number)], ThresholdText AS Threshold, KnownCause AS [Known Cause], Detail,
       HasKnownCause AS [Has Known Cause]
FROM dw.FactDQFinding;
GO

/* One row per date x dataset from the latest run that evaluated it. */
CREATE OR ALTER VIEW rpt.DQDailyStatus AS
SELECT DateKey, DatasetKey, ResolvedRunId AS [Resolved Run], RunsEvaluated AS [Runs Evaluated], ChecksEvaluated AS [Checks Evaluated],
       PassCount AS [Pass], NoteCount AS [Note], WarnCount AS [Warn], FailCount AS [Fail], UnresolvedDayFails AS [Unresolved Day FAILs],
       UnresolvedMinuteFails AS [Unresolved Minute FAILs], WorstSeverityKey
FROM dw.FactDQDailyStatus;
GO

/* The production gate's verdict for each date. */
CREATE OR ALTER VIEW rpt.DQDayVerdict AS
SELECT DateKey, Verdict, ExcludeReasons AS [Exclude Reasons], MaskedRanges AS [Masked Ranges], AcceptedFails AS [Accepted FAILs], ReasonText AS Reasons
FROM dw.FactDQDayVerdict;
GO

/* Out-of-fold predictions of the production model (332,275 rows). */
CREATE OR ALTER VIEW rpt.ModelPrediction AS
SELECT ModelRunKey, InstrumentKey, DateKey, TimeKey, ActualClassKey, PredictedClassKey, ProbDown AS [Prob Down], ProbFlat AS [Prob Flat],
       ProbUp AS [Prob Up], MaxProb AS [Max Prob], IsCorrect AS [Is Correct]
FROM dw.FactModelPrediction;
GO

/* Metric values exactly as published in the frozen reports (for the reconciliation visual). */
CREATE OR ALTER VIEW rpt.ModelMetric AS
SELECT ModelRunKey, MetricCode AS Metric, ActualClassKey, PredictedClassKey, MetricValue AS [Value]
FROM dw.FactModelMetric;
GO

/* Top-15 permutation importances per production run. */
CREATE OR ALTER VIEW rpt.FeatureImportance AS
SELECT ModelRunKey, FeatureKey, ImportanceRank AS [Rank], Importance
FROM dw.FactFeatureImportance;
GO

/* Simulated trades with costs and P&L (20 scenarios). Exit date is the active date (P&L is booked on exit). */
CREATE OR ALTER VIEW rpt.Trade AS
SELECT TradeId, ModelRunKey, InstrumentKey, EntryDateKey, ExitDateKey, EntryTimeKey, ExitTimeKey, ExitTsUtc AS [Exit Time (UTC)],
       Side, Direction, HoldMin AS [Hold (min)], ExitReason AS [Exit Reason], TradingSymbol AS [Contract], OptionType AS [Option Type],
       Strike, ExpiryDate AS Expiry, Qty, SignalProb AS [Signal Prob], EntryPrice AS [Entry Price], ExitPrice AS [Exit Price],
       EntryCostRs AS [Entry Cost (Rs)], ExitCostRs AS [Exit Cost (Rs)], TotalCostRs AS [Total Cost (Rs)], OptGrossRs AS [Gross P&L (Rs)],
       OptNetRs AS [Net P&L (Rs)], IsWin AS [Is Win], FutNetRs AS [Futures Net (Rs)]
FROM dw.FactTrade;
GO

/* Weekly drift report lines (PSI of the listed features). */
CREATE OR ALTER VIEW rpt.FeatureDrift AS
SELECT ReportDateKey, ModelRunKey, FeatureKey, Section, Psi AS PSI, NEff AS [Effective N], InBandPct AS [In Band %], NullPctTrain AS [Null % Train],
       NullPctLive AS [Null % Live], DriftStatus AS [Status]
FROM dw.FactFeatureDrift;
GO

/* Daily summary of the 8.7-million-row feature fact: day x instrument x feature. */
CREATE OR ALTER VIEW rpt.FeatureValueDaily AS
SELECT DateKey, InstrumentKey, FeatureKey, COUNT_BIG(*) AS Minutes, AVG(FeatureValue) AS [Average], MIN(FeatureValue) AS [Min], MAX(FeatureValue) AS [Max]
FROM dw.FactFeatureValue
GROUP BY DateKey, InstrumentKey, FeatureKey;
GO

/* ETL runs (Pipeline Health page). */
CREATE OR ALTER VIEW rpt.EtlRun AS
SELECT EtlRunId, RunType AS [Run Type], StartedAt AS [Started], EndedAt AS [Ended], CONVERT(int, CONVERT(char(8), StartedAt, 112)) AS DateKey,
       Status, TriggeredBy AS [Triggered By], DurationSec AS [Duration (s)], GitCommit AS [Git Commit], ErrorMessage AS Error
FROM etl.RunLog;
GO

/* ETL steps with row counts and durations. */
CREATE OR ALTER VIEW rpt.EtlStep AS
SELECT StepLogId, EtlRunId, StepName AS Step, TargetTable AS [Target Table], Status, RowsRead AS [Rows Read], RowsInserted AS [Rows Inserted],
       RowsUpdated AS [Rows Updated], RowsDeleted AS [Rows Deleted], RowsRejected AS [Rows Rejected], DurationMs AS [Duration (ms)], ErrorMessage AS Error
FROM etl.StepLog;
GO

/* Reconciliation results of every ETL run (PASS / BREAK). */
CREATE OR ALTER VIEW rpt.Reconciliation AS
SELECT ReconId, EtlRunId, CheckName AS [Check], TableName AS [Table], Layer, ExpectedValue AS Expected, ActualValue AS Actual, Tolerance,
       Status, Note
FROM etl.ReconResult;
GO

/* Row-level security mapping: which login sees which desk (used by the dynamic RLS role). */
CREATE OR ALTER VIEW rpt.UserDesk AS
SELECT UserPrincipal AS [User], InstrumentKey
FROM sec.UserInstrument;
GO
