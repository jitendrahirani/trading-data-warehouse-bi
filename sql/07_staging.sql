/* Phase 3 step 3.2: staging tables (schema stg).
   Rules
   - A staging table mirrors ONE source as it is: text sources (CSV, Markdown, Excel) stay text, exactly as written in the
     file; binary sources (npz, Parquet) keep their native types. Typing, cleansing and key lookups happen in Phase 4,
     where a bad value goes to etl.RejectRow instead of failing the load.
   - Text columns are generously sized so staging never truncates (a truncation would silently change the data).
   - Every row carries lineage: EtlRunId, SourceFile, SourceRowNo, LoadedAt (requirement N6).
   - Truncate-and-load per batch: a stg table holds only the current batch (full load = everything; incremental = only new
     or changed files). Heaps with no indexes: the fastest shape for a bulk load that is read once.
   - Three tables are DERIVED by Python from the production code (marked 'derived'): gate classes and day verdicts from
     ml_pipeline/dq/gate.py, dataset contracts from ml_pipeline/dq/contracts.py. They are recomputed in full on every run,
     because a contract change can re-classify old findings without any new file arriving.
   The production panel's 124 feature columns are generated into 08_stage_panel.sql by bi/etl/extract.py.
*/
SET NOCOUNT ON;

/* S01: one row per DQ run folder (manifest.json), for the run list and its own row-count control. */
CREATE TABLE stg.DQRun (
    run_id           nvarchar(60)  NOT NULL, -- Run folder name | S01 folder name |
    created_utc      nvarchar(30)  NULL, -- Run creation time as written in the manifest | S01 manifest.json created_utc | yyyymmddThhmmssZ
    manifest_rows    int           NULL, -- Rows the run says it wrote | S01 manifest.json rows | control total
    manifest_dates   int           NULL, -- Dates the run says it evaluated | S01 manifest.json dates |
    date_files       int           NOT NULL, -- date=*/findings.csv files found in the folder | extract.py |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- manifest.json path relative to the repo | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- Always 1 (one row per manifest) | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDQRun_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S01: one row per line of a findings.csv (all 12 columns as text), plus the run it belongs to. */
CREATE TABLE stg.DQFinding (
    [date]           nvarchar(20)  NULL, -- Trading date | S01 date | yyyy-mm-dd
    dataset          nvarchar(100) NULL, -- Dataset name | S01 dataset |
    [check]          nvarchar(100) NULL, -- Check name | S01 check |
    [column]         nvarchar(100) NULL, -- Column the finding is about | S01 column | '' when none
    symbol           nvarchar(100) NULL, -- Symbol or entity | S01 symbol | '' when none
    minute_from      nvarchar(20)  NULL, -- Start of the affected range | S01 minute_from | hh:mm or hh:mm:ss
    minute_to        nvarchar(20)  NULL, -- End of the affected range | S01 minute_to |
    severity         nvarchar(20)  NULL, -- pass, note, warn, fail | S01 severity |
    [value]          nvarchar(200) NULL, -- Measured value | S01 value | text, sometimes words
    threshold        nvarchar(100) NULL, -- Threshold | S01 threshold |
    known_cause      nvarchar(1000) NULL, -- Recorded explanation | S01 known_cause |
    detail           nvarchar(1000) NULL, -- Free-text detail | S01 detail |
    run_id           nvarchar(60)  NOT NULL, -- Run folder | S01 folder name |
    run_created_utc  nvarchar(30)  NOT NULL, -- Run creation time | S01 manifest.json created_utc | orders runs for 'latest run wins'
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- findings.csv path relative to the repo | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based data row in the file (header not counted) | extract.py | lineage; with SourceFile = the finding's identity
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDQFinding_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S01 derived: the production gate's branch for every FAIL in a resolved run (all runs, recomputed every load). */
CREATE TABLE stg.DQFailClass (
    finding_file     nvarchar(260) NOT NULL, -- SourceFile of the FAIL finding | extract.py | joins to stg.DQFinding / dw.FactDQFinding
    finding_row_no   int           NOT NULL, -- SourceRowNo of the FAIL finding | extract.py |
    gate_class       nvarchar(60)  NOT NULL, -- Gate branch | bi/etl/dq_rules.py classify_fail | one of dq_rules.FAIL_CLASSES
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Code that derived it | extract.py | 'ml_pipeline/dq/gate.py'
    SourceRowNo      int           NOT NULL, -- Sequence number | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDQFailClass_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S01 derived: the production gate's verdict for every date (recomputed every load). */
CREATE TABLE stg.DQDayVerdict (
    [date]           nvarchar(20)  NOT NULL, -- Trading date | gate.day_verdict |
    status           nvarchar(20)  NOT NULL, -- ok, exclude, unchecked | gate.day_verdict status |
    reasons_n        int           NOT NULL, -- Number of exclusion reasons | gate.day_verdict reasons |
    masks_n          int           NOT NULL, -- Number of masked minute ranges | gate.day_verdict masks |
    accepted_n       int           NOT NULL, -- FAILs accepted by a decision | gate.day_verdict accepted |
    reason_text      nvarchar(2000) NULL, -- Reasons joined with ' ; ' | gate.day_verdict reasons |
    runs             nvarchar(1000) NULL, -- Runs whose findings were used, joined | gate.day_verdict runs |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Code that derived it | extract.py | 'ml_pipeline/dq/gate.py'
    SourceRowNo      int           NOT NULL, -- Sequence number | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDQDayVerdict_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S02: written DQ decisions (ml_pipeline/dq/decisions.csv; '#' comment lines skipped). */
CREATE TABLE stg.DQDecision (
    [date]           nvarchar(20)  NULL, -- Date the decision applies to | S02 date |
    dataset          nvarchar(100) NULL, -- Dataset, or * for all | S02 dataset |
    [check]          nvarchar(100) NULL, -- Check, blank = all | S02 check |
    symbol           nvarchar(100) NULL, -- Symbol, blank = all | S02 symbol |
    action           nvarchar(20)  NULL, -- accept or exclude_day | S02 action |
    reason           nvarchar(1000) NULL, -- Why | S02 reason |
    decided_by       nvarchar(100) NULL, -- Who decided | S02 decided_by |
    decided_on       nvarchar(20)  NULL, -- When | S02 decided_on |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- File path relative to the repo | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based data row (comments not counted) | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDQDecision_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S10 derived: dataset contracts as the production DQ code defines them today (feeds the SCD2 DimDataset). */
CREATE TABLE stg.DatasetContract (
    dataset          nvarchar(100) NOT NULL, -- Dataset name | contracts.py CONTRACTS key | 'cross_dataset' added (the X1-X8 checks)
    dataset_group    nvarchar(100) NOT NULL, -- Business group | contracts.py Contract.group |
    publication      nvarchar(20)  NOT NULL, -- same_day, t_plus_1, overnight, reference | contracts.py Contract.publication |
    cadence          nvarchar(20)  NOT NULL, -- minute, 5s, tick, 5min, 15min, daily, events | contracts.py Contract.cadence |
    is_consumed      bit           NOT NULL, -- In the gate's CONSUMED list | gate.py CONSUMED |
    session_end_before nvarchar(5) NOT NULL, -- Session end before the change date | contracts.py CLOSE_BEFORE | 15:30
    session_end_from nvarchar(5)   NOT NULL, -- Session end from the change date | contracts.py CLOSE_FROM | 15:40
    session_end_change_date nvarchar(10) NOT NULL, -- Date the session end changed | contracts.py CLOSE_EFFECTIVE_DATE | 2026-08-03
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Code it came from | extract.py | 'ml_pipeline/dq/contracts.py'
    SourceRowNo      int           NOT NULL, -- Sequence number | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgDatasetContract_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S03: one row per out-of-fold prediction (5 npz files, native types). */
CREATE TABLE stg.ModelPrediction (
    horizon_min      tinyint       NOT NULL, -- Horizon from the file name gbt_h{N}m.npz | extract.py |
    ts_ns            bigint        NOT NULL, -- Decision minute, nanoseconds since 1970-01-01 UTC | S03 ts |
    inst             tinyint       NOT NULL, -- Instrument code | S03 inst | 0 = NIFTY, 1 = BANKNIFTY (train_baseline.py; verified: 100% label agreement vs 61-70% if swapped)
    y                tinyint       NOT NULL, -- Actual class 0 Down, 1 Flat, 2 Up | S03 y | label + 1
    proba_down       float         NOT NULL, -- Probability of Down | S03 proba[:,0] |
    proba_flat       float         NOT NULL, -- Probability of Flat | S03 proba[:,1] |
    proba_up         float         NOT NULL, -- Probability of Up | S03 proba[:,2] |
    pos              int           NOT NULL, -- Row position in the label-filtered, time-sorted training frame | S03 pos | not a position in the panel file
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- npz path relative to the repo | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based array index | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgModelPrediction_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S04: headline metrics and confusion-matrix cells parsed from the frozen reports baseline_h*.md. */
CREATE TABLE stg.ModelMetric (
    horizon_min      tinyint       NOT NULL, -- Horizon | S04 file name |
    metric           nvarchar(40)  NOT NULL, -- Metric name | S04 report | accuracy, balanced_accuracy, macro_f1, directional_precision, committed_pct, rows, features, confusion
    actual_class     nvarchar(10)  NULL, -- Row of a confusion cell | S04 confusion block | Down, Flat, Up; NULL for other metrics
    predicted_class  nvarchar(10)  NULL, -- Column of a confusion cell | S04 confusion block |
    metric_value     float         NOT NULL, -- Value as published | S04 report |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Report path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- Sequence within the report | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgModelMetric_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S04: the top-15 permutation importances listed in each report. */
CREATE TABLE stg.FeatureImportance (
    horizon_min      tinyint       NOT NULL, -- Horizon | S04 file name |
    importance_rank  tinyint       NOT NULL, -- Position in the list, 1 = first | S04 list order |
    feature          nvarchar(100) NOT NULL, -- Feature name | S04 list |
    importance       float         NOT NULL, -- Importance as published | S04 list |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Report path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- Same as importance_rank | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgFeatureImportance_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S04: model metadata models/baseline_h*_meta.json (production runs). */
CREATE TABLE stg.ModelMeta (
    horizon_min      tinyint       NOT NULL, -- Horizon | S04 meta horizon |
    trained_at       nvarchar(30)  NULL, -- Training time | S04 meta trained_at | yyyymmdd_hhmmss
    n_rows           int           NULL, -- Rows trained and evaluated | S04 meta n_rows |
    label_mode       nvarchar(20)  NULL, -- Label definition | S04 meta label_mode |
    n_features       int           NULL, -- Features | S04 meta n_features |
    model_file       nvarchar(200) NULL, -- Model bundle | S04 meta model_file |
    balanced_accuracy_cv float     NULL, -- CV balanced accuracy | S04 meta |
    directional_precision_cv float NULL, -- CV directional precision | S04 meta |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- meta.json path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- Always 1 | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgModelMeta_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S05: one row per simulated trade (20 CSV files, all columns as text) plus the scenario taken from the path. */
CREATE TABLE stg.Trade (
    instrument       nvarchar(20)  NULL, -- Source column instrument, exactly as written in the file | S05 instrument |
    [date]           nvarchar(20)  NULL, -- Source column date, exactly as written in the file | S05 date |
    dir              nvarchar(10)  NULL, -- long or short | S05 dir |
    direction        nvarchar(10)  NULL, -- Up or Down | S05 direction |
    decision_ts      nvarchar(40)  NULL, -- Signal time, ISO with offset | S05 decision_ts |
    entry_ts         nvarchar(40)  NULL, -- Entry time | S05 entry_ts |
    exit_ts          nvarchar(40)  NULL, -- Exit time | S05 exit_ts |
    hold_min         nvarchar(20)  NULL, -- Minutes held | S05 hold_min |
    exit_reason      nvarchar(20)  NULL, -- Source column exit_reason, exactly as written in the file | S05 exit_reason |
    tradingsymbol    nvarchar(60)  NULL, -- Option contract | S05 tradingsymbol |
    strike           nvarchar(30)  NULL, -- Source column strike, exactly as written in the file | S05 strike |
    expiry           nvarchar(20)  NULL, -- Source column expiry, exactly as written in the file | S05 expiry |
    lot_size         nvarchar(20)  NULL, -- Source column lot_size, exactly as written in the file | S05 lot_size |
    qty              nvarchar(20)  NULL, -- Source column qty, exactly as written in the file | S05 qty |
    signal_prob      nvarchar(40)  NULL, -- Source column signal_prob, exactly as written in the file | S05 signal_prob |
    entry_price      nvarchar(30)  NULL, -- Source column entry_price, exactly as written in the file | S05 entry_price |
    exit_price       nvarchar(30)  NULL, -- Source column exit_price, exactly as written in the file | S05 exit_price |
    entry_cost       nvarchar(30)  NULL, -- Source column entry_cost, exactly as written in the file | S05 entry_cost |
    exit_cost        nvarchar(30)  NULL, -- Source column exit_cost, exactly as written in the file | S05 exit_cost |
    target_price     nvarchar(30)  NULL, -- Source column target_price, exactly as written in the file | S05 target_price |
    stop_price       nvarchar(30)  NULL, -- Source column stop_price, exactly as written in the file | S05 stop_price |
    exit_quote_age_min nvarchar(30) NULL, -- Source column exit_quote_age_min, exactly as written in the file | S05 exit_quote_age_min |
    opt_net_rs       nvarchar(30)  NULL, -- Option net P&L, Rs | S05 opt_net_rs | control total per file
    prem0            nvarchar(30)  NULL, -- Source column prem0, exactly as written in the file | S05 prem0 |
    entry_px         nvarchar(30)  NULL, -- Source column entry_px, exactly as written in the file | S05 entry_px |
    exit_px          nvarchar(30)  NULL, -- Source column exit_px, exactly as written in the file | S05 exit_px |
    pts_net_slip     nvarchar(40)  NULL, -- Source column pts_net_slip, exactly as written in the file | S05 pts_net_slip |
    fut_net_rs       nvarchar(30)  NULL, -- Source column fut_net_rs, exactly as written in the file | S05 fut_net_rs |
    run_set          nvarchar(40)  NOT NULL, -- 'production v0_2026-10' or 'research 2026-09-17' | file path |
    label_mode       nvarchar(20)  NOT NULL, -- vol, fut_cost, opt_pnl | file path |
    horizon_min      tinyint       NOT NULL, -- Horizon | file name trades_h{N}m.csv |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- CSV path relative to the repo | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based data row | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgTrade_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S06: one row per feature line of a weekly drift report table (text as written). */
CREATE TABLE stg.FeatureDrift (
    report_date      nvarchar(20)  NOT NULL, -- Report date | S06 file name drift_report_yyyy-mm-dd.md |
    section_title    nvarchar(200) NOT NULL, -- The '## ' heading above the table | S06 |
    feature          nvarchar(100) NOT NULL, -- Source column feature, exactly as written in the file | S06 feature |
    psi              nvarchar(30)  NULL, -- Source column psi, exactly as written in the file | S06 PSI |
    n_eff            nvarchar(30)  NULL, -- Source column n_eff, exactly as written in the file | S06 n_eff |
    in_band          nvarchar(30)  NULL, -- e.g. 46% | S06 in-band |
    null_train_live  nvarchar(40)  NULL, -- e.g. 0% -> 0% | S06 'null% train -> live' |
    status           nvarchar(30)  NULL, -- Source column status, exactly as written in the file | S06 status |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Report path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- Line number in the file | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgFeatureDrift_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S08: the feature workbook model_feature_list.xlsx -> Features (all rows, text). */
CREATE TABLE stg.Feature (
    sr_no            nvarchar(20)  NULL, -- Source column sr_no, exactly as written in the file | S08 Sr No |
    feature_name     nvarchar(100) NULL, -- Source column feature_name, exactly as written in the file | S08 Feature exact name |
    feature_group    nvarchar(100) NULL, -- Source column feature_group, exactly as written in the file | S08 Group |
    short_description nvarchar(4000) NULL, -- Source column short_description, exactly as written in the file | S08 Short description |
    websocket_used   nvarchar(2000) NULL, -- Source column websocket_used, exactly as written in the file | S08 Websocket data used or not |
    websocket_should nvarchar(2000) NULL, -- Source column websocket_should, exactly as written in the file | S08 Websocket data should be used or not |
    status           nvarchar(1000) NULL, -- Source column status, exactly as written in the file | S08 Status |
    research_basis   nvarchar(4000) NULL, -- Source column research_basis, exactly as written in the file | S08 Research basis and usability |
    comment          nvarchar(1000) NULL, -- Source column comment, exactly as written in the file | S08 Comment |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- Workbook path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based data row on the sheet | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgFeature_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);

/* S09: NSE holiday calendar exchange_calendar_seed.csv (text). */
CREATE TABLE stg.Holiday (
    holiday_date     nvarchar(20)  NULL, -- Source column holiday_date, exactly as written in the file | S09 holiday_date |
    holiday_name     nvarchar(100) NULL, -- Source column holiday_name, exactly as written in the file | S09 holiday_name |
    segment          nvarchar(100) NULL, -- Source column segment, exactly as written in the file | S09 segment |
    market_status    nvarchar(40)  NULL, -- Source column market_status, exactly as written in the file | S09 market_status |
    source_label     nvarchar(200) NULL, -- Source column source_label, exactly as written in the file | S09 source_label |
    source_url       nvarchar(400) NULL, -- Source column source_url, exactly as written in the file | S09 source_url |
    EtlRunId         int           NOT NULL, -- ETL run that staged the row | etl.RunLog | lineage
    SourceFile       nvarchar(260) NOT NULL, -- File path | extract.py | lineage
    SourceRowNo      int           NOT NULL, -- 1-based data row | extract.py | lineage
    LoadedAt         datetime2(3)  NOT NULL CONSTRAINT DF_stgHoliday_LoadedAt DEFAULT SYSDATETIME() -- When the row was staged | SQL Server | lineage
);
GO
