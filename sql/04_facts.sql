/* Phase 2 steps 2.1 + 2.3: fact tables.
   Grain (one row per ...) is stated above each table and in Phase1_Requirements.xlsx -> Bus matrix.
   - Every foreign key is declared and trusted, so orphans are impossible and the optimiser can rely on them.
   - Lineage on every fact: EtlRunId (-> etl.RunLog), SourceFile + SourceRowNo where the source is a file of rows,
     LoadedAt. The (SourceFile, SourceRowNo) or natural-key UNIQUE constraint is what makes reloads idempotent.
   - Money in rupees: decimal(12,2) (every source value has at most 2 decimals, so sums reconcile exactly).
     Model probabilities and PSI: float (the predicted class must come from full-precision probabilities).
   - Timestamps are stored in UTC (TsUtc); DateKey/TimeKey are the IST calendar date and minute of day.
*/
SET NOCOUNT ON;

/* One row per DQ run x date x dataset x check x finding (transaction fact, every run kept). */
CREATE TABLE dw.FactDQFinding (
    FindingId        bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactDQFinding PRIMARY KEY, -- Surrogate key | ETL |
    DateKey          int          NOT NULL CONSTRAINT FK_FactDQFinding_Date REFERENCES dw.DimDate (DateKey), -- Trading date the finding is about | S01 date |
    DatasetKey       int          NOT NULL CONSTRAINT FK_FactDQFinding_Dataset REFERENCES dw.DimDataset (DatasetKey), -- Dataset version valid on DateKey | S01 dataset | point-in-time SCD2 lookup: ValidFrom <= date <= ValidTo
    CheckKey         smallint     NOT NULL CONSTRAINT FK_FactDQFinding_Check REFERENCES dw.DimCheck (CheckKey), -- DQ check | S01 check |
    SeverityKey      smallint     NOT NULL CONSTRAINT FK_FactDQFinding_Severity REFERENCES dw.DimSeverity (SeverityKey), -- Severity | S01 severity |
    GateClassKey     smallint     NOT NULL CONSTRAINT FK_FactDQFinding_GateClass REFERENCES dw.DimGateClass (GateClassKey), -- Gate branch for a FAIL in the resolved run, 0 otherwise | bi/etl/dq_rules.py classify_fail | FAILs in superseded runs are 0: the gate never sees them
    DQRunId          varchar(40)  NOT NULL, -- DQ run folder name | S01 run_id | degenerate dimension
    RunCreatedUtc    datetime2(0) NOT NULL, -- When the DQ run was created (UTC) | S01 manifest.json created_utc | orders runs for 'latest run wins'
    IsResolvedRun    bit          NOT NULL, -- 1 when this run is the latest one that evaluated this date x dataset | ETL (gate.resolve rule) | 27,511 rows pre-registered
    ColumnName       varchar(64)  NULL, -- Column the finding is about | S01 column | blank -> NULL
    Symbol           nvarchar(40) NULL, -- Symbol or entity the finding is about | S01 symbol | blank -> NULL
    MinuteFrom       time(0)      NULL, -- Start of the affected range (IST) | S01 minute_from | hh:mm or hh:mm:ss (tick outages carry seconds); used by minute-level masks
    MinuteTo         time(0)      NULL, -- End of the affected range (IST) | S01 minute_to | hh:mm or hh:mm:ss
    ValueText        nvarchar(40) NULL, -- Measured value as written by the check | S01 value | text: some values are words
    ValueNum         float        NULL, -- Measured value when it is a number | S01 value | TRY_CONVERT; NULL when not numeric
    ThresholdText    nvarchar(20) NULL, -- Threshold the value was compared with | S01 threshold |
    KnownCause       nvarchar(400) NULL, -- Recorded explanation (by-design behaviour or incident) | S01 known_cause | does NOT resolve a FAIL (K05)
    Detail           nvarchar(400) NULL, -- Free-text detail from the check | S01 detail |
    HasKnownCause    AS CAST(CASE WHEN KnownCause IS NULL THEN 0 ELSE 1 END AS bit) PERSISTED, -- 1 when KnownCause is filled | computed | K05
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactDQFinding_Run REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog | lineage (N6)
    SourceFile       nvarchar(260) NOT NULL, -- Source findings.csv, relative to the repo | ETL | lineage
    SourceRowNo      int          NOT NULL, -- 1-based data row number in SourceFile | ETL | with SourceFile = idempotency key; keeps the 1 exact duplicate row
    LoadedAt         datetime2(3) NOT NULL CONSTRAINT DF_FactDQFinding_LoadedAt DEFAULT SYSDATETIME(), -- When the row was loaded | ETL |
    CONSTRAINT UQ_FactDQFinding_Source UNIQUE (SourceFile, SourceRowNo)
);
CREATE INDEX IX_FactDQFinding_Date_Dataset ON dw.FactDQFinding (DateKey, DatasetKey) INCLUDE (SeverityKey, GateClassKey, IsResolvedRun);

/* One row per date x dataset: the summary of the latest run that evaluated the pair. */
CREATE TABLE dw.FactDQDailyStatus (
    DateKey          int          NOT NULL CONSTRAINT FK_FactDQDailyStatus_Date REFERENCES dw.DimDate (DateKey), -- Trading date | S01 date |
    DatasetKey       int          NOT NULL CONSTRAINT FK_FactDQDailyStatus_Dataset REFERENCES dw.DimDataset (DatasetKey), -- Dataset version valid on DateKey | S01 dataset |
    ResolvedRunId    varchar(40)  NOT NULL, -- The run whose findings count for this pair | ETL 'latest run wins' | ROW_NUMBER over RunCreatedUtc DESC
    ResolvedRunCreatedUtc datetime2(0) NOT NULL, -- Creation time of ResolvedRunId (UTC) | S01 manifest |
    RunsEvaluated    tinyint      NOT NULL, -- How many runs evaluated this pair (>1 = a later re-check replaced an earlier one) | ETL | late-arriving data (N3)
    ChecksEvaluated  int          NOT NULL, -- Findings in the resolved run | ETL | K02; sum over all rows = 27,511 pre-registered
    PassCount        int          NOT NULL, -- Findings with severity pass | ETL |
    NoteCount        int          NOT NULL, -- Findings with severity note | ETL |
    WarnCount        int          NOT NULL, -- Findings with severity warn | ETL |
    FailCount        int          NOT NULL, -- Findings with severity fail | ETL |
    UnresolvedDayFails int        NOT NULL, -- FAILs of class 'unresolved: day excluded' | ETL (dq_rules) | K04
    UnresolvedMinuteFails int     NOT NULL, -- FAILs of class 'unresolved: minutes masked' | ETL (dq_rules) | K04
    WorstSeverityKey smallint     NOT NULL CONSTRAINT FK_FactDQDailyStatus_Severity REFERENCES dw.DimSeverity (SeverityKey), -- Highest severity among the resolved findings | ETL |
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactDQDailyStatus_Run REFERENCES etl.RunLog (EtlRunId), -- ETL run that wrote the row | etl.RunLog |
    LoadedAt         datetime2(3) NOT NULL CONSTRAINT DF_FactDQDailyStatus_LoadedAt DEFAULT SYSDATETIME(), -- When the row was loaded | ETL |
    CONSTRAINT PK_FactDQDailyStatus PRIMARY KEY (DateKey, DatasetKey),
    CONSTRAINT CK_FactDQDailyStatus_Counts CHECK (ChecksEvaluated = PassCount + NoteCount + WarnCount + FailCount)
);

/* One row per trading date: the production gate's verdict for the day. */
CREATE TABLE dw.FactDQDayVerdict (
    DateKey          int          NOT NULL CONSTRAINT PK_FactDQDayVerdict PRIMARY KEY
                                  CONSTRAINT FK_FactDQDayVerdict_Date REFERENCES dw.DimDate (DateKey), -- Trading date | S01 date |
    Verdict          varchar(10)  NOT NULL CONSTRAINT CK_FactDQDayVerdict_Verdict CHECK (Verdict IN ('ok', 'exclude', 'unchecked')), -- Gate verdict | gate.day_verdict via dq_rules | K01; pre-registered 131 ok / 9 exclude / 1 unchecked
    ExcludeReasons   smallint     NOT NULL, -- Reasons that exclude the day | gate.day_verdict reasons |
    MaskedRanges     smallint     NOT NULL, -- Minute ranges masked by unresolved minute-level FAILs | gate.day_verdict masks |
    AcceptedFails    smallint     NOT NULL, -- FAILs accepted by a written decision | gate.day_verdict accepted |
    ReasonText       nvarchar(2000) NULL, -- The gate's reasons, joined | gate.day_verdict reasons | for the drill-through tooltip
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactDQDayVerdict_Run REFERENCES etl.RunLog (EtlRunId), -- ETL run that wrote the row | etl.RunLog |
    LoadedAt         datetime2(3) NOT NULL CONSTRAINT DF_FactDQDayVerdict_LoadedAt DEFAULT SYSDATETIME() -- When the row was loaded | ETL |
);

/* One row per model run x instrument x minute: an out-of-fold prediction (production runs only). */
CREATE TABLE dw.FactModelPrediction (
    ModelRunKey      smallint     NOT NULL CONSTRAINT FK_FactModelPrediction_Run REFERENCES dw.DimModelRun (ModelRunKey), -- Model run (one per horizon) | S03 file name gbt_h{N}m.npz |
    InstrumentKey    smallint     NOT NULL CONSTRAINT FK_FactModelPrediction_Instr REFERENCES dw.DimInstrument (InstrumentKey), -- Underlying | S03 inst (code mapped in Phase 3) |
    TsUtc            datetime2(0) NOT NULL, -- Decision minute (UTC) | S03 ts (ns since epoch) |
    DateKey          int          NOT NULL CONSTRAINT FK_FactModelPrediction_Date REFERENCES dw.DimDate (DateKey), -- IST date of TsUtc | derived |
    TimeKey          smallint     NOT NULL CONSTRAINT FK_FactModelPrediction_Time REFERENCES dw.DimTime (TimeKey), -- IST minute of day of TsUtc | derived |
    ActualClassKey   smallint     NOT NULL CONSTRAINT FK_FactModelPrediction_Actual REFERENCES dw.DimClass (ClassKey), -- What happened (label) | S03 y | role-playing DimClass (active relationship)
    PredictedClassKey smallint    NOT NULL CONSTRAINT FK_FactModelPrediction_Pred REFERENCES dw.DimClass (ClassKey), -- Class with the highest probability | S03 argmax(proba) | computed in dw.usp_Load_FactModelPrediction from the full-precision probabilities, first maximum wins (as numpy; 0 ties in the data); role-playing DimClass (inactive, USERELATIONSHIP)
    ProbDown         float        NOT NULL, -- Predicted probability of Down | S03 proba[:,0] |
    ProbFlat         float        NOT NULL, -- Predicted probability of Flat | S03 proba[:,1] |
    ProbUp           float        NOT NULL, -- Predicted probability of Up | S03 proba[:,2] |
    MaxProb          float        NOT NULL, -- Highest of the three probabilities | derived | the threshold in K15 is applied to this
    IsCorrect        AS CAST(CASE WHEN ActualClassKey = PredictedClassKey THEN 1 ELSE 0 END AS bit) PERSISTED, -- 1 when predicted = actual | computed | K12 accuracy = AVG
    OofPos           int          NOT NULL, -- Row position in the label-filtered, time-sorted frame the model was trained on | S03 pos | NOT a row of the panel file; join to the panel on (TsUtc, instrument) instead
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactModelPrediction_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    SourceFile       nvarchar(260) NOT NULL, -- Source npz file | ETL | lineage
    SourceRowNo      int          NOT NULL, -- 1-based row number in SourceFile | ETL | lineage
    CONSTRAINT PK_FactModelPrediction PRIMARY KEY (ModelRunKey, InstrumentKey, TsUtc),
    CONSTRAINT CK_FactModelPrediction_Prob CHECK (ProbDown BETWEEN 0 AND 1 AND ProbFlat BETWEEN 0 AND 1 AND ProbUp BETWEEN 0 AND 1)
);

/* One row per model run x metric (x actual class x predicted class where the metric is per class or a matrix cell). */
CREATE TABLE dw.FactModelMetric (
    ModelRunKey      smallint     NOT NULL CONSTRAINT FK_FactModelMetric_Run REFERENCES dw.DimModelRun (ModelRunKey), -- Model run | S04 report file name |
    MetricCode       varchar(40)  NOT NULL, -- Metric name | S04 report | accuracy, balanced_accuracy, macro_f1, directional_precision, committed_pct, confusion
    ActualClassKey   smallint     NOT NULL CONSTRAINT DF_FactModelMetric_Actual DEFAULT -2
                                  CONSTRAINT FK_FactModelMetric_Actual REFERENCES dw.DimClass (ClassKey), -- Actual class for confusion cells, else -2 | S04 confusion rows |
    PredictedClassKey smallint    NOT NULL CONSTRAINT DF_FactModelMetric_Pred DEFAULT -2
                                  CONSTRAINT FK_FactModelMetric_Pred REFERENCES dw.DimClass (ClassKey), -- Predicted class for confusion cells, else -2 | S04 confusion columns |
    MetricValue      float        NOT NULL, -- Value exactly as published in the frozen report | S04 | the DAX measures must reproduce it (K12-K14)
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactModelMetric_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    SourceFile       nvarchar(260) NOT NULL, -- Source report | ETL | lineage
    CONSTRAINT PK_FactModelMetric PRIMARY KEY (ModelRunKey, MetricCode, ActualClassKey, PredictedClassKey)
);

/* One row per model run x feature in the published top 15 (permutation importance, last fold). */
CREATE TABLE dw.FactFeatureImportance (
    ModelRunKey      smallint     NOT NULL CONSTRAINT FK_FactFeatureImportance_Run REFERENCES dw.DimModelRun (ModelRunKey), -- Model run | S04 report |
    FeatureKey       smallint     NOT NULL CONSTRAINT FK_FactFeatureImportance_Feature REFERENCES dw.DimFeature (FeatureKey), -- Feature | S04 report |
    ImportanceRank   tinyint      NOT NULL, -- 1 = most important | S04 list order |
    Importance       float        NOT NULL, -- Drop in score when the feature is shuffled | S04 report | can be negative (noise)
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactFeatureImportance_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    SourceFile       nvarchar(260) NOT NULL, -- Source report | ETL | lineage
    CONSTRAINT PK_FactFeatureImportance PRIMARY KEY (ModelRunKey, FeatureKey),
    CONSTRAINT UQ_FactFeatureImportance_Rank UNIQUE (ModelRunKey, ImportanceRank)
);

/* One row per simulated trade (backtest; entry and exit are role-playing dates and times). */
CREATE TABLE dw.FactTrade (
    TradeId          int IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactTrade PRIMARY KEY, -- Surrogate key | ETL |
    ModelRunKey      smallint     NOT NULL CONSTRAINT FK_FactTrade_Run REFERENCES dw.DimModelRun (ModelRunKey), -- Scenario = model run (run set, label mode, horizon) | S05 folder + file name |
    InstrumentKey    smallint     NOT NULL CONSTRAINT FK_FactTrade_Instr REFERENCES dw.DimInstrument (InstrumentKey), -- Underlying | S05 instrument |
    EntryDateKey     int          NOT NULL CONSTRAINT FK_FactTrade_EntryDate REFERENCES dw.DimDate (DateKey), -- IST date of entry | S05 entry_ts | role-playing DimDate (inactive, USERELATIONSHIP)
    ExitDateKey      int          NOT NULL CONSTRAINT FK_FactTrade_ExitDate REFERENCES dw.DimDate (DateKey), -- IST date of exit | S05 exit_ts | role-playing DimDate (active: P&L is booked on exit)
    EntryTimeKey     smallint     NOT NULL CONSTRAINT FK_FactTrade_EntryTime REFERENCES dw.DimTime (TimeKey), -- IST minute of entry | S05 entry_ts |
    ExitTimeKey      smallint     NOT NULL CONSTRAINT FK_FactTrade_ExitTime REFERENCES dw.DimTime (TimeKey), -- IST minute of exit | S05 exit_ts |
    DecisionTsUtc    datetime2(0) NOT NULL, -- When the model gave the signal (UTC) | S05 decision_ts |
    EntryTsUtc       datetime2(0) NOT NULL, -- Entry time (UTC) | S05 entry_ts |
    ExitTsUtc        datetime2(0) NOT NULL, -- Exit time (UTC) | S05 exit_ts |
    Side             varchar(5)   NOT NULL CONSTRAINT CK_FactTrade_Side CHECK (Side IN ('long', 'short')), -- Position side | S05 dir |
    Direction        varchar(4)   NOT NULL CONSTRAINT CK_FactTrade_Direction CHECK (Direction IN ('Up', 'Down')), -- Predicted direction | S05 direction |
    HoldMin          smallint     NOT NULL, -- Minutes held | S05 hold_min |
    ExitReason       varchar(10)  NOT NULL, -- Why the trade closed | S05 exit_reason | TIME, TARGET, STOP, OPPOSITE, SQUAREOFF
    TradingSymbol    varchar(32)  NOT NULL, -- Option contract traded | S05 tradingsymbol |
    OptionType       AS CAST(RIGHT(TradingSymbol, 2) AS char(2)) PERSISTED, -- CE (call) or PE (put) | computed |
    Strike           decimal(9, 1) NOT NULL, -- Option strike | S05 strike |
    ExpiryDate       date         NOT NULL, -- Option expiry | S05 expiry |
    LotSize          smallint     NOT NULL, -- Exchange lot size at the time | S05 lot_size |
    Qty              smallint     NOT NULL, -- Quantity traded (one lot) | S05 qty |
    SignalProb       float        NOT NULL, -- Model probability behind the signal | S05 signal_prob |
    EntryPrice       decimal(10, 2) NOT NULL, -- Option entry price, Rs | S05 entry_price |
    ExitPrice        decimal(10, 2) NOT NULL, -- Option exit price, Rs | S05 exit_price |
    EntryCostRs      decimal(10, 2) NOT NULL, -- Costs on entry (brokerage, taxes, slippage), Rs | S05 entry_cost |
    ExitCostRs       decimal(10, 2) NOT NULL, -- Costs on exit, Rs | S05 exit_cost |
    TotalCostRs      AS (EntryCostRs + ExitCostRs) PERSISTED, -- Entry + exit costs, Rs | computed | K19
    TargetPrice      decimal(10, 2) NOT NULL, -- Take-profit level, Rs | S05 target_price |
    StopPrice        decimal(10, 2) NOT NULL, -- Stop-loss level, Rs | S05 stop_price |
    ExitQuoteAgeMin  decimal(6, 1) NOT NULL, -- Age of the quote used for the exit, minutes | S05 exit_quote_age_min |
    OptNetRs         decimal(12, 2) NOT NULL, -- Option P&L after costs, Rs | S05 opt_net_rs | K18; control total = file sum
    OptGrossRs       AS (OptNetRs + EntryCostRs + ExitCostRs) PERSISTED, -- Option P&L before costs, Rs | computed | defined as net + costs, so gross - costs = net exactly
    Prem0            decimal(10, 2) NOT NULL, -- Option premium at the decision | S05 prem0 |
    FutEntryPx       decimal(10, 1) NOT NULL, -- Futures price at entry | S05 entry_px |
    FutExitPx        decimal(10, 1) NOT NULL, -- Futures price at exit | S05 exit_px |
    PtsNetSlip       float        NOT NULL, -- Futures points net of slippage | S05 pts_net_slip |
    FutNetRs         decimal(12, 2) NOT NULL, -- Same signal traded in futures, net Rs | S05 fut_net_rs | comparison only
    IsWin            AS CAST(CASE WHEN OptNetRs > 0 THEN 1 ELSE 0 END AS bit) PERSISTED, -- 1 when OptNetRs > 0 | computed | K20
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactTrade_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    SourceFile       nvarchar(260) NOT NULL, -- Source trades csv | ETL | lineage
    SourceRowNo      int          NOT NULL, -- 1-based data row number in SourceFile | ETL | lineage
    CONSTRAINT UQ_FactTrade_Natural UNIQUE (ModelRunKey, InstrumentKey, DecisionTsUtc),
    CONSTRAINT CK_FactTrade_Times CHECK (EntryTsUtc <= ExitTsUtc)
);

/* One row per weekly drift report x section x listed feature (only the published top lists). */
CREATE TABLE dw.FactFeatureDrift (
    DriftId          int IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactFeatureDrift PRIMARY KEY, -- Surrogate key | ETL |
    ReportDateKey    int          NOT NULL CONSTRAINT FK_FactFeatureDrift_Date REFERENCES dw.DimDate (DateKey), -- Date of the weekly report | S06 file name |
    ModelRunKey      smallint     NOT NULL CONSTRAINT FK_FactFeatureDrift_Run REFERENCES dw.DimModelRun (ModelRunKey), -- Model whose training data is the reference | S06 title (h10m) | production h10m run
    FeatureKey       smallint     NOT NULL CONSTRAINT FK_FactFeatureDrift_Feature REFERENCES dw.DimFeature (FeatureKey), -- Feature | S06 table |
    Section          varchar(16)  NOT NULL CONSTRAINT CK_FactFeatureDrift_Section CHECK (Section IN ('range-skew', 'psi-reliable', 'low-n')), -- Which table of the report | S06 '## ' heading |
    Psi              float        NOT NULL, -- Population stability index, live vs train | S06 PSI | > 0.25 drifted, 0.10-0.25 watch (K24); not counted for low-n
    NEff             int          NULL, -- Effective sample size | S06 n_eff | < 100 = PSI unreliable
    InBandPct        decimal(5, 1) NULL, -- Share of live values inside the train p0.5-p99.5 band, % | S06 in-band |
    NullPctTrain     decimal(5, 1) NULL, -- Null share in training data, % | S06 'null% train -> live' |
    NullPctLive      decimal(5, 1) NULL, -- Null share in live data, % | S06 'null% train -> live' |
    DriftStatus      varchar(16)  NULL, -- Status written by the report | S06 status | drifted, watch, low-N, RANGE-SKEW (the reports list no ok rows)
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactFeatureDrift_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    SourceFile       nvarchar(260) NOT NULL, -- Source report | ETL | lineage
    CONSTRAINT UQ_FactFeatureDrift UNIQUE (ReportDateKey, Section, FeatureKey)
);

/* One row per minute x instrument x feature with a non-null value (production panel, unpivoted).
   8.7 million rows: clustered columnstore (compressed column by column and fast for aggregates; the size saving is measured after the Phase 3 load).
   No primary key on purpose: a b-tree over 8.7 million rows would cost more than the table; uniqueness is proved by
   the reconciliation (COUNT(*) = COUNT(DISTINCT key) = 8,749,765). */
CREATE TABLE dw.FactFeatureValue (
    DateKey          int          NOT NULL CONSTRAINT FK_FactFeatureValue_Date REFERENCES dw.DimDate (DateKey), -- IST date | S07 date |
    TimeKey          smallint     NOT NULL CONSTRAINT FK_FactFeatureValue_Time REFERENCES dw.DimTime (TimeKey), -- IST minute of day | S07 timestamp |
    InstrumentKey    smallint     NOT NULL CONSTRAINT FK_FactFeatureValue_Instr REFERENCES dw.DimInstrument (InstrumentKey), -- Underlying | S07 instrument |
    FeatureKey       smallint     NOT NULL CONSTRAINT FK_FactFeatureValue_Feature REFERENCES dw.DimFeature (FeatureKey), -- Feature | S07 column name |
    FeatureValue     float        NOT NULL, -- Feature value at that minute | S07 cell | nulls are not stored (8,749,765 of 8,846,532 cells)
    EtlRunId         int          NOT NULL CONSTRAINT FK_FactFeatureValue_Etl REFERENCES etl.RunLog (EtlRunId), -- ETL run that loaded the row | etl.RunLog |
    INDEX CCI_FactFeatureValue CLUSTERED COLUMNSTORE
);
GO
