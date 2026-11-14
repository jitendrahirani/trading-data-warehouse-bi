/* Phase 2 steps 2.1 + 2.3: dimension tables.
   Conventions
   - Surrogate keys: int/smallint. Key -1 = 'Unknown' member in every dimension (05_seed_static.sql), so a fact row
     whose lookup fails still loads and stays visible instead of disappearing from totals. DimClass also has -2 =
     'Not applicable' (metrics that are not per class).
   - Smart keys only where the value itself is stable and meaningful: DateKey = yyyymmdd, TimeKey = minute of day,
     SeverityKey = severity rank, ClassKey = the label code used by the model (0 Down, 1 Flat, 2 Up).
   - SCD2 only on DimDataset (decision D5): the session end of intraday datasets changed on 2026-08-03 (15:30 -> 15:40).
   - Every column ends with a comment "meaning | source | rule". bi/etl/apply_ddl.py turns them into MS_Description
     extended properties, and the data dictionary is generated from the database, so the two cannot disagree.
*/
SET NOCOUNT ON;

/* One row per calendar date 2026-04-01 to 2027-03-31 = one whole Indian fiscal year (FY2026-27), so year-to-date measures see complete years; plus Unknown. */
CREATE TABLE dw.DimDate (
    DateKey          int          NOT NULL CONSTRAINT PK_DimDate PRIMARY KEY, -- Date as yyyymmdd; -1 = Unknown | generated | smart key
    [Date]           date         NOT NULL CONSTRAINT UQ_DimDate_Date UNIQUE, -- Calendar date | generated | 1900-01-01 for the Unknown row (hidden by the rpt view)
    [Year]           smallint     NOT NULL, -- Calendar year | generated |
    [Quarter]        tinyint      NOT NULL, -- Calendar quarter 1-4 | generated |
    [Month]          tinyint      NOT NULL, -- Calendar month 1-12 | generated |
    MonthName        varchar(9)   NOT NULL, -- Month name (January) | generated | sort by [Month]
    YearMonth        char(7)      NOT NULL, -- yyyy-mm | generated |
    DayOfMonth       tinyint      NOT NULL, -- Day of month 1-31 | generated |
    DayOfWeekNo      tinyint      NOT NULL, -- 1 = Monday ... 7 = Sunday (ISO) | generated | independent of SET DATEFIRST
    DayName          varchar(9)   NOT NULL, -- Day name (Monday) | generated | sort by DayOfWeekNo
    IsoWeek          tinyint      NOT NULL, -- ISO week number | generated |
    WeekStartDate    date         NOT NULL, -- Monday of the week | generated |
    IsWeekend        bit          NOT NULL, -- 1 on Saturday and Sunday | generated |
    IsHoliday        bit          NOT NULL, -- 1 on an NSE F&O trading holiday | S09 exchange_calendar_seed.csv | only 2026 holidays are known: see HolidayCalendarKnown
    HolidayName      nvarchar(60) NULL, -- Holiday name | S09 |
    HolidayCalendarKnown bit      NOT NULL, -- 1 when the holiday list covers this date (calendar year 2026) | S09 | IsTradingDay after 2026 is provisional
    IsTradingDay     bit          NOT NULL, -- 1 = NSE F&O session (not weekend, not holiday) | generated + S09 | weekday AND NOT holiday
    TradingDayIndex  int          NULL, -- Running number of trading days (1 = first trading day in the table) | generated | NULL on non-trading days; used for 'last N trading days' windows (K06, K07)
    SessionEndTime   time(0)      NULL, -- Scheduled end of the F&O session | ml_pipeline/dq/contracts.py session_end_min | 15:30 before 2026-08-03, 15:40 from then; NULL on non-trading days
    FiscalYear       char(9)      NOT NULL, -- Indian fiscal year, e.g. FY2026-27 | generated | FY starts 1 April
    FiscalYearStart  smallint     NOT NULL, -- Calendar year in which the fiscal year starts | generated |
    FiscalQuarter    tinyint      NOT NULL, -- Fiscal quarter 1-4 (Q1 = Apr-Jun) | generated |
    FiscalMonthNo    tinyint      NOT NULL, -- Fiscal month 1-12 (1 = April) | generated |
    CONSTRAINT CK_DimDate_DateKey CHECK (DateKey = -1 OR DateKey = CONVERT(int, CONVERT(char(8), [Date], 112)))
);

/* One row per minute of the day (IST) plus Unknown. */
CREATE TABLE dw.DimTime (
    TimeKey          smallint     NOT NULL CONSTRAINT PK_DimTime PRIMARY KEY, -- Minute of day 0-1439 (hh*60+mm, IST); -1 = Unknown | generated | smart key
    TimeOfDay        time(0)      NULL, -- Minute as a time (IST) | generated | NULL for Unknown
    HHMM             char(5)      NOT NULL, -- Minute as text hh:mm | generated |
    HourOfDay        tinyint      NULL, -- Hour 0-23 | generated |
    MinuteOfHour     tinyint      NULL, -- Minute 0-59 | generated |
    SessionBucket    varchar(24)  NOT NULL, -- Part of the trading day | generated | Pre-open <09:15; Open 09:15-09:59; Morning 10:00-11:59; Midday 12:00-13:59; Afternoon 14:00-15:14; Close 15:15-15:40 (cash closing auction from 15:15); After hours
    SessionBucketSort tinyint     NOT NULL, -- Sort order of SessionBucket | generated |
    CONSTRAINT CK_DimTime_TimeKey CHECK (TimeKey BETWEEN -1 AND 1439)
);

/* One row per underlying index = desk (NIFTY, BANKNIFTY) plus Unknown. */
CREATE TABLE dw.DimInstrument (
    InstrumentKey    smallint     NOT NULL CONSTRAINT PK_DimInstrument PRIMARY KEY, -- Surrogate key; -1 = Unknown | seeded | 1 NIFTY, 2 BANKNIFTY
    InstrumentCode   varchar(16)  NOT NULL CONSTRAINT UQ_DimInstrument_Code UNIQUE, -- Underlying index code | S03/S05/S07 instrument column | natural key
    InstrumentName   nvarchar(60) NOT NULL, -- Display name | seeded |
    DeskName         nvarchar(40) NOT NULL -- Trading desk that owns the instrument | role-played persona P2 | row-level security key (sec.UserInstrument)
);

/* One row per version of a dataset contract (SCD type 2) plus Unknown. */
CREATE TABLE dw.DimDataset (
    DatasetKey       int IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimDataset PRIMARY KEY, -- Surrogate key of one version of a dataset contract; -1 = Unknown | ETL | new key per SCD2 version
    DatasetCode      varchar(64)  NOT NULL, -- Dataset name as in the DQ findings | S01 dataset / contracts.py CONTRACTS | natural key (32 values incl. cross_dataset)
    DatasetGroup     nvarchar(60) NOT NULL, -- Business group of the collector | contracts.py Contract.group |
    Publication      varchar(12)  NOT NULL, -- When the data is published | contracts.py Contract.publication | same_day, t_plus_1, overnight, reference
    Cadence          varchar(8)   NOT NULL, -- Sampling frequency | contracts.py Contract.cadence | minute, 5s, tick, 5min, 15min, daily, events
    IsIntraday       bit          NOT NULL, -- 1 when the dataset is collected during the session | derived from Cadence | minute, 5s, tick, 5min, 15min
    IsConsumedByModel bit         NOT NULL, -- 1 when the production DQ gate counts this dataset's FAILs | ml_pipeline/dq/gate.py CONSUMED |
    SessionEndTime   time(0)      NULL, -- Session end the DQ checks expect for this version | contracts.py session_end_min | SCD2 attribute: 15:30 until 2026-08-02, 15:40 from 2026-08-03; NULL when not intraday
    ValidFrom        date         NOT NULL, -- First date this version applies to | ETL | inclusive
    ValidTo          date         NOT NULL CONSTRAINT DF_DimDataset_ValidTo DEFAULT '9999-12-31', -- Last date this version applies to | ETL | inclusive; 9999-12-31 = open
    IsCurrent        bit          NOT NULL, -- 1 for the version in force today | ETL | exactly one per DatasetCode
    RowHash          binary(32)   NOT NULL, -- SHA2_256 of the tracked attributes | ETL | a change in the hash creates a new version
    EtlRunId         int          NULL, -- ETL run that wrote this version | etl.RunLog | lineage
    CONSTRAINT UQ_DimDataset_Code_From UNIQUE (DatasetCode, ValidFrom),
    CONSTRAINT CK_DimDataset_Valid CHECK (ValidFrom <= ValidTo)
);
CREATE UNIQUE INDEX UX_DimDataset_Current ON dw.DimDataset (DatasetCode) WHERE IsCurrent = 1;

/* One row per DQ check plus Unknown. */
CREATE TABLE dw.DimCheck (
    CheckKey         smallint IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimCheck PRIMARY KEY, -- Surrogate key; -1 = Unknown | ETL |
    CheckCode        varchar(40)  NOT NULL CONSTRAINT UQ_DimCheck_Code UNIQUE, -- DQ check name | S01 check | natural key (28 values)
    CheckCategory    varchar(20)  NOT NULL, -- DQ dimension the check measures | bi/seed (Phase 4) | Completeness, Timeliness, Validity, Uniqueness, Consistency, Reconciliation
    IsMinuteLevel    bit          NOT NULL, -- 1 when an unresolved FAIL with a minute range only masks those minutes | gate.py MINUTE_LEVEL_CHECKS | gap, frozen, jumps, totals, quotes
    IsCrossDataset   bit          NOT NULL, -- 1 for the X1-X8 cross-source checks | S01 rows with dataset cross_dataset |
    CheckDescription nvarchar(300) NULL -- What the check tests, in plain words | bi/seed (Phase 4) |
);

/* One row per DQ severity (pass, note, warn, fail) plus Unknown. */
CREATE TABLE dw.DimSeverity (
    SeverityKey      smallint     NOT NULL CONSTRAINT PK_DimSeverity PRIMARY KEY, -- Severity rank 0-3; -1 = Unknown | contracts.py SEV_RANK | smart key: pass 0 < note 1 < warn 2 < fail 3
    SeverityCode     varchar(8)   NOT NULL CONSTRAINT UQ_DimSeverity_Code UNIQUE, -- Severity as in the findings | S01 severity |
    SeverityLabel    nvarchar(20) NOT NULL, -- Display label | seeded |
    IsProblem        bit          NOT NULL, -- 1 for warn and fail | seeded | K03 pass rate counts pass + note as good
    ColorHex         char(7)      NOT NULL -- Report colour for this severity | seeded | used by conditional formatting (N9)
);

/* One row per branch of the production DQ gate a finding can take (0 = not a FAIL) plus Unknown. */
CREATE TABLE dw.DimGateClass (
    GateClassKey     smallint     NOT NULL CONSTRAINT PK_DimGateClass PRIMARY KEY, -- 0 = not a FAIL, 1-6 = gate branch of a FAIL, -1 = Unknown | seeded |
    GateClass        varchar(40)  NOT NULL CONSTRAINT UQ_DimGateClass UNIQUE, -- Which branch of the production DQ gate a FAIL takes | bi/etl/dq_rules.py FAIL_CLASSES | same order as gate.day_verdict
    IsFail           bit          NOT NULL, -- 1 for the six FAIL classes | seeded |
    IsUnresolved     bit          NOT NULL, -- 1 = FAIL nobody has explained or decided (K04) | seeded | 'unresolved: minutes masked' and 'unresolved: day excluded'
    ExcludesDay      bit          NOT NULL, -- 1 when this class makes the gate exclude the whole day | seeded | 'excluded by decision', 'unresolved: day excluded'
    MasksMinutes     bit          NOT NULL, -- 1 when only the FAIL's minute range is masked | seeded |
    SortOrder        tinyint      NOT NULL -- Display order | seeded |
);

/* One row per model run x label mode x horizon (5 production + 15 research) plus Unknown. */
CREATE TABLE dw.DimModelRun (
    ModelRunKey      smallint IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimModelRun PRIMARY KEY, -- Surrogate key; -1 = Unknown | ETL |
    RunCode          varchar(40)  NOT NULL CONSTRAINT UQ_DimModelRun_Code UNIQUE, -- Run set + label mode + horizon, e.g. v0_2026-10:vol:h10m | ETL | natural key (20 rows)
    RunSet           varchar(30)  NOT NULL, -- Which batch of runs | S03/S05 folder | 'production v0_2026-10' or 'research 2026-09-17'
    LabelMode        varchar(12)  NOT NULL, -- How the target class was defined | S04 model meta label_mode / research folder | vol, fut_cost, opt_pnl
    HorizonMin       tinyint      NOT NULL, -- Prediction horizon in minutes | S04 meta horizon | 5, 10, 15, 20, 30
    ModelName        varchar(80)  NULL, -- Model bundle name | S04 meta model_file | production runs only
    TrainedAt        datetime2(0) NULL, -- When the model was trained | S04 meta trained_at |
    TrainRows        int          NULL, -- Rows the model was trained/evaluated on | S04 meta n_rows | 67,583 for the headline run (h10m production)
    FeatureCount     smallint     NULL, -- Model features | S04 meta n_features | 124
    IsProduction     bit          NOT NULL, -- 1 for the frozen production baseline v0_2026-10 | ETL |
    IsHeadlineRun      bit          NOT NULL, -- 1 for the run quoted in the project summary (production, h10m) | decision D7 | exactly one row
    HasOofPredictions bit         NOT NULL -- 1 when FactModelPrediction holds its out-of-fold predictions | ETL | production runs only
);
CREATE UNIQUE INDEX UX_DimModelRun_Headline ON dw.DimModelRun (IsHeadlineRun) WHERE IsHeadlineRun = 1;

/* One row per prediction class (Down, Flat, Up) plus Unknown and Not applicable. */
CREATE TABLE dw.DimClass (
    ClassKey         smallint     NOT NULL CONSTRAINT PK_DimClass PRIMARY KEY, -- Label code 0 Down, 1 Flat, 2 Up; -1 Unknown; -2 Not applicable | S03 y | smart key = the model's label code
    ClassName        varchar(16)  NOT NULL CONSTRAINT UQ_DimClass_Name UNIQUE, -- Class name | seeded |
    IsDirectional    bit          NOT NULL, -- 1 for Up and Down (a trade would be placed) | seeded | used by directional precision (K15)
    SortOrder        tinyint      NOT NULL -- Display order | seeded |
);

/* One row per model feature (124) plus Unknown. */
CREATE TABLE dw.DimFeature (
    FeatureKey       smallint IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimFeature PRIMARY KEY, -- Surrogate key; -1 = Unknown | ETL |
    FeatureName      varchar(64)  NOT NULL CONSTRAINT UQ_DimFeature_Name UNIQUE, -- Feature column name | S07 panel columns / S08 'Feature exact name' | natural key (124 model features)
    FeatureGroup     nvarchar(60) NULL, -- Feature family | S08 Group |
    ShortDescription nvarchar(2000) NULL, -- What the feature measures | S08 Short description | longest source text 1,129 characters
    ProductionStatus nvarchar(400) NULL, -- Status text from the feature workbook | S08 Status | longest source text 250 characters
    WebsocketUse     nvarchar(1000) NULL, -- Whether websocket data is used | S08 Websocket data used or not | longest source text 478 characters
    SrNo             smallint     NULL, -- Row number in the feature workbook | S08 Sr No | NOT the panel column position
    IsModelFeature   bit          NOT NULL -- 1 when the production model uses it | S04 meta / S07 | all 124 loaded rows
);
GO
