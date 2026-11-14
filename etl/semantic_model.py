"""Phase 6: the Power BI semantic model, specified once.

Everything about the model that can be written as data lives here, and every Phase 6 artifact is generated from it:
  bi/powerbi/add_measures.csx      Tabular Editor 2 script: adds the measures you did not type by hand
  bi/powerbi/tidy_model.csx        Tabular Editor 2 script: hides keys, sets sort-by columns and summarization
  bi/docs/Phase6_Measure_Catalog.xlsx   measures (DAX, folder, format, KPI) + the SQL each is validated against
  bi/docs/Phase6_Build_Guide.docx  your step-by-step build guide
  bi/etl/validate_measures.py      runs every measure in YOUR open Power BI model and compares it with SQL
Table names are the rpt views without the schema. Each measure has an SQL twin computed from the same rpt views, and is
validated for 3 filter combinations of its family.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ------------------------------------------------------------------------------------------------ tables
# model table name -> source view. Load all of these (Power Query Navigator, schema rpt).
TABLES = {
    "Date": "rpt.Date", "Time": "rpt.Time", "Instrument": "rpt.Instrument", "Dataset": "rpt.Dataset", "Check": "rpt.Check",
    "Severity": "rpt.Severity", "GateClass": "rpt.GateClass", "ModelRun": "rpt.ModelRun", "ActualClass": "rpt.ActualClass",
    "PredictedClass": "rpt.PredictedClass", "Feature": "rpt.Feature",
    "DQFinding": "rpt.DQFinding", "DQDailyStatus": "rpt.DQDailyStatus", "DQDayVerdict": "rpt.DQDayVerdict",
    "ModelPrediction": "rpt.ModelPrediction", "ModelMetric": "rpt.ModelMetric", "FeatureImportance": "rpt.FeatureImportance",
    "Trade": "rpt.Trade", "FeatureDrift": "rpt.FeatureDrift", "FeatureValueDaily": "rpt.FeatureValueDaily",
    "EtlRun": "rpt.EtlRun", "EtlStep": "rpt.EtlStep", "Reconciliation": "rpt.Reconciliation", "UserDesk": "rpt.UserDesk",
}

# (from table, from column, to table, to column, active). All many-to-one, single direction (dimension filters fact).
RELATIONSHIPS = [
    ("DQFinding", "DateKey", "Date", "DateKey", True), ("DQFinding", "DatasetKey", "Dataset", "DatasetKey", True),
    ("DQFinding", "CheckKey", "Check", "CheckKey", True), ("DQFinding", "SeverityKey", "Severity", "SeverityKey", True),
    ("DQFinding", "GateClassKey", "GateClass", "GateClassKey", True),
    ("DQDailyStatus", "DateKey", "Date", "DateKey", True), ("DQDailyStatus", "DatasetKey", "Dataset", "DatasetKey", True),
    ("DQDayVerdict", "DateKey", "Date", "DateKey", True),
    ("ModelPrediction", "ModelRunKey", "ModelRun", "ModelRunKey", True), ("ModelPrediction", "InstrumentKey", "Instrument", "InstrumentKey", True),
    ("ModelPrediction", "DateKey", "Date", "DateKey", True), ("ModelPrediction", "TimeKey", "Time", "TimeKey", True),
    ("ModelPrediction", "ActualClassKey", "ActualClass", "ActualClassKey", True),
    ("ModelPrediction", "PredictedClassKey", "PredictedClass", "PredictedClassKey", True),
    ("ModelMetric", "ModelRunKey", "ModelRun", "ModelRunKey", True), ("ModelMetric", "ActualClassKey", "ActualClass", "ActualClassKey", True),
    ("ModelMetric", "PredictedClassKey", "PredictedClass", "PredictedClassKey", True),
    ("FeatureImportance", "ModelRunKey", "ModelRun", "ModelRunKey", True), ("FeatureImportance", "FeatureKey", "Feature", "FeatureKey", True),
    ("Trade", "ModelRunKey", "ModelRun", "ModelRunKey", True), ("Trade", "InstrumentKey", "Instrument", "InstrumentKey", True),
    ("Trade", "ExitDateKey", "Date", "DateKey", True), ("Trade", "EntryDateKey", "Date", "DateKey", False),
    ("Trade", "ExitTimeKey", "Time", "TimeKey", True), ("Trade", "EntryTimeKey", "Time", "TimeKey", False),
    ("FeatureDrift", "ReportDateKey", "Date", "DateKey", True), ("FeatureDrift", "ModelRunKey", "ModelRun", "ModelRunKey", True),
    ("FeatureDrift", "FeatureKey", "Feature", "FeatureKey", True),
    ("FeatureValueDaily", "DateKey", "Date", "DateKey", True), ("FeatureValueDaily", "InstrumentKey", "Instrument", "InstrumentKey", True),
    ("FeatureValueDaily", "FeatureKey", "Feature", "FeatureKey", True),
    ("EtlRun", "DateKey", "Date", "DateKey", True), ("EtlStep", "EtlRunId", "EtlRun", "EtlRunId", True),
    ("Reconciliation", "EtlRunId", "EtlRun", "EtlRunId", True),
]
# UserDesk has NO relationship: the dynamic RLS rule reads it with DAX.

SORT_BY = [("Date", "Month", "Month No"), ("Date", "Weekday", "Weekday No"), ("Time", "Session Bucket", "Session Bucket Order"),
           ("Severity", "Severity", "Severity Order"), ("GateClass", "Gate Class", "Gate Class Order"),
           ("ActualClass", "Actual Class", "Actual Class Order"), ("PredictedClass", "Predicted Class", "Predicted Class Order")]

RLS_ROLES = [
    ("NIFTY desk", "Instrument", '[Instrument] = "NIFTY"'),
    ("BANKNIFTY desk", "Instrument", '[Instrument] = "BANKNIFTY"'),
    ("Desk (dynamic)", "Instrument",
     "[InstrumentKey] IN CALCULATETABLE ( VALUES ( UserDesk[InstrumentKey] ), UserDesk[User] = USERPRINCIPALNAME () )"),
]

# ------------------------------------------------------------------------------------------------ validation filters
ALIAS = {"Date": "d", "Dataset": "ds", "Check": "c", "Severity": "sv", "ModelRun": "mr", "Instrument": "i",
         "ActualClass": "ac", "PredictedClass": "pc", "Feature": "f", "EtlRun": "er"}
HEADLINE = "v0_2026-10:vol:h10m"
CASES = {   # family -> 3 filter combinations, each a list of (table, column, value)
    "dq":     [[], [("Date", "Date", date(2026, 7, 14))], [("Dataset", "Dataset", "collector_a_minute_core"), ("Date", "Year-Month", "2026-08")]],
    "dqday":  [[("Date", "Date", date(2026, 6, 2))], [("Date", "Date", date(2026, 7, 14))],
               [("Date", "Date", date(2026, 11, 9)), ("Dataset", "Dataset", "collector_a_minute_core")]],
    "etl":    [[], [("Date", "Year-Month", "2026-11")], [("EtlRun", "Run Type", "incremental")]],
    "model":  [[("ModelRun", "Model Run", HEADLINE)], [("ModelRun", "Model Run", HEADLINE), ("Instrument", "Instrument", "BANKNIFTY")],
               [("ModelRun", "Model Run", "v0_2026-10:vol:h30m"), ("Date", "Year-Month", "2026-07")]],
    "trade":  [[], [("ModelRun", "Model Run", HEADLINE)], [("ModelRun", "Model Run", "r2026-09-17:opt_pnl:h30m"), ("Instrument", "Instrument", "NIFTY")]],
    "tradeday": [[("Date", "Date", date(2026, 6, 30))], [("ModelRun", "Model Run", HEADLINE), ("Date", "Date", date(2026, 7, 31))],
                 [("ModelRun", "Model Run", "r2026-09-17:opt_pnl:h30m"), ("Instrument", "Instrument", "NIFTY"), ("Date", "Date", date(2026, 9, 17))]],
    "drift":  [[], [("Date", "Date", date(2026, 9, 12))], [("Feature", "Feature", "iv_skew")]],
    "feature": [[("Feature", "Feature", "atm_iv")], [("Feature", "Feature", "atm_iv"), ("Instrument", "Instrument", "NIFTY")],
                [("Feature", "Feature", "book_press"), ("Date", "Year-Month", "2026-07")]],
}


def sql_lit(v) -> str:
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "N'" + str(v).replace("'", "''") + "'"


def where(filters, allowed: set[str], extra: list[str] = ()) -> str:
    conds = [f"{ALIAS[t]}.[{c}] = {sql_lit(v)}" for t, c, v in filters if t in allowed] + list(extra)
    return ("WHERE " + " AND ".join(conds)) if conds else ""


def dax_filters(filters) -> list[str]:
    out = []
    for t, c, v in filters:
        val = f"DATE ( {v.year}, {v.month}, {v.day} )" if isinstance(v, date) else '"' + str(v).replace('"', '""') + '"'
        out.append(f"'{t}'[{c}] = {val}")
    return out


def the_date(filters) -> date:
    return next(v for t, c, v in filters if t == "Date" and c == "Date")


# ------------------------------------------------------------------------------------------------ SQL twins
DQS = "FROM rpt.DQDailyStatus s JOIN rpt.[Date] d ON d.DateKey = s.DateKey JOIN rpt.Dataset ds ON ds.DatasetKey = s.DatasetKey"
DQF = ("FROM rpt.DQFinding x JOIN rpt.[Date] d ON d.DateKey = x.DateKey JOIN rpt.Dataset ds ON ds.DatasetKey = x.DatasetKey "
       "JOIN rpt.Severity sv ON sv.SeverityKey = x.SeverityKey JOIN rpt.[Check] c ON c.CheckKey = x.CheckKey")
MP = ("FROM rpt.ModelPrediction p JOIN rpt.ModelRun mr ON mr.ModelRunKey = p.ModelRunKey JOIN rpt.Instrument i ON i.InstrumentKey = p.InstrumentKey "
      "JOIN rpt.[Date] d ON d.DateKey = p.DateKey JOIN rpt.PredictedClass pc ON pc.PredictedClassKey = p.PredictedClassKey")
TR = ("FROM rpt.Trade t JOIN rpt.ModelRun mr ON mr.ModelRunKey = t.ModelRunKey JOIN rpt.Instrument i ON i.InstrumentKey = t.InstrumentKey "
      "JOIN rpt.[Date] d ON d.DateKey = t.ExitDateKey")
DQ_T, MP_T, TR_T = {"Date", "Dataset"}, {"ModelRun", "Instrument", "Date", "PredictedClass"}, {"ModelRun", "Instrument", "Date"}
DQF_T = {"Date", "Dataset", "Severity", "Check"}


def _not_date(filters):
    return [f for f in filters if f[0] != "Date"]


def sql_rolling(filters):
    d0 = the_date(filters)
    w = where(_not_date(filters), {"Dataset"})
    return f"""WITH daily AS (SELECT d.[Trading Day No] AS tdn, SUM(s.[Unresolved Day FAILs] + s.[Unresolved Minute FAILs]) AS u, SUM(s.[Checks Evaluated]) AS ch
                {DQS} {w} GROUP BY d.[Trading Day No]),
               cur AS (SELECT [Trading Day No] AS t FROM rpt.[Date] WHERE [Date] = {sql_lit(d0)})
               SELECT 1.0 * SUM(u) / NULLIF(SUM(ch), 0) FROM daily, cur WHERE tdn > cur.t - 20 AND tdn <= cur.t"""


def sql_days_since(filters):
    d0 = the_date(filters)
    w = where(_not_date(filters), {"Dataset"})
    return f"""WITH daily AS (SELECT d.[Trading Day No] AS tdn, SUM(s.[Unresolved Day FAILs] + s.[Unresolved Minute FAILs]) AS u
                {DQS} {w} GROUP BY d.[Trading Day No]),
               cur AS (SELECT [Trading Day No] AS t FROM rpt.[Date] WHERE [Date] = {sql_lit(d0)})
               SELECT cur.t - MAX(tdn) FROM daily, cur WHERE tdn <= cur.t AND u > 0 GROUP BY cur.t"""


def sql_daily_equity(filters):
    """Daily equity curve by exit date up to the case's date, over ALL calendar dates (as the DAX iterates ALL('Date'))."""
    d0 = the_date(filters)
    w = where(_not_date(filters), {"ModelRun", "Instrument"})
    return f"""WITH net AS (SELECT t.ExitDateKey AS dk, SUM(t.[Net P&L (Rs)]) AS n FROM rpt.Trade t
                  JOIN rpt.ModelRun mr ON mr.ModelRunKey = t.ModelRunKey JOIN rpt.Instrument i ON i.InstrumentKey = t.InstrumentKey {w} GROUP BY t.ExitDateKey),
               cal AS (SELECT d.DateKey, d.[Date] FROM rpt.[Date] d WHERE d.[Date] <= {sql_lit(d0)}),
               curve AS (SELECT cal.[Date], SUM(ISNULL(net.n, 0)) OVER (ORDER BY cal.[Date] ROWS UNBOUNDED PRECEDING) AS cum
                         FROM cal LEFT JOIN net ON net.dk = cal.DateKey),
               dd AS (SELECT [Date], cum, cum - GREATEST(0, MAX(cum) OVER (ORDER BY [Date] ROWS UNBOUNDED PRECEDING)) AS drawdown FROM curve)"""


@dataclass
class Measure:
    name: str
    folder: str
    fmt: str
    kpi: str
    desc: str
    dax: str
    family: str
    sql: object                      # str template with {where} / callable(filters) -> SQL
    hand: bool = False               # you type this one yourself (Part A of the guide)
    extra: list = field(default_factory=list)

    def sql_for(self, filters) -> str:
        return self.sql(filters) if callable(self.sql) else self.sql.format(**self._w(filters))

    def _w(self, filters):
        fam_tables = {"dq": DQ_T, "dqday": DQ_T, "model": MP_T, "trade": TR_T, "tradeday": TR_T,
                      "etl": {"Date", "EtlRun"}, "drift": {"Date", "Feature"}, "feature": {"Date", "Instrument", "Feature"}}[self.family]
        return {"w": where(filters, fam_tables), "wf": where(filters, DQF_T), "wm": where(filters, {"ModelRun"}),
                "wv": where(filters, {"Date"}),
                "and_w": (" AND " + where(filters, fam_tables)[6:]) if where(filters, fam_tables) else ""}


F_DQ, F_PIPE, F_MODEL, F_TRADE, F_FEAT = "1 Data quality", "2 Pipeline", "3 Model", "4 Trades", "5 Features"
P, PCT, N0, N2, N4 = "#,0", "0.0%", "#,0", "#,0.00", "0.0000"

MEASURES: list[Measure] = [
    # ---------------------------------------------------------------- data quality
    Measure("Checks Evaluated", F_DQ, P, "K02", "DQ findings in the resolved (latest) runs.",
            "SUM ( DQDailyStatus[Checks Evaluated] )", "dq", "SELECT SUM(s.[Checks Evaluated]) " + DQS + " {w}", hand=True),
    Measure("Pass + Note", F_DQ, P, "K03", "Findings with severity pass or note in the resolved runs.",
            "SUM ( DQDailyStatus[Pass] ) + SUM ( DQDailyStatus[Note] )", "dq", "SELECT SUM(s.[Pass] + s.[Note]) " + DQS + " {w}", hand=True),
    Measure("DQ Pass Rate", F_DQ, PCT, "K03", "(Pass + note) / checks evaluated. Green >= 95 %, red < 94 % (set from the real distribution).",
            "DIVIDE ( [Pass + Note], [Checks Evaluated] )", "dq", "SELECT 1.0 * SUM(s.[Pass] + s.[Note]) / NULLIF(SUM(s.[Checks Evaluated]), 0) " + DQS + " {w}", hand=True),
    Measure("FAILs", F_DQ, P, "", "Findings with severity fail in the resolved runs.",
            "SUM ( DQDailyStatus[Fail] )", "dq", "SELECT SUM(s.[Fail]) " + DQS + " {w}"),
    Measure("Unresolved FAILs", F_DQ, P, "K04", "FAILs in consumed datasets with no by-design rule and no written decision (day-level + minute-level).",
            "SUM ( DQDailyStatus[Unresolved Day FAILs] ) + SUM ( DQDailyStatus[Unresolved Minute FAILs] )", "dq",
            "SELECT SUM(s.[Unresolved Day FAILs] + s.[Unresolved Minute FAILs]) " + DQS + " {w}"),
    Measure("Warn + Fail Findings", F_DQ, P, "", "Findings with severity warn or fail in the resolved runs.",
            "CALCULATE ( COUNTROWS ( DQFinding ), DQFinding[Is Resolved Run] = TRUE (), Severity[Is Problem] = TRUE () )", "dq",
            "SELECT COUNT(*) " + DQF + " {wf} " ),
    Measure("Known-Cause FAILs and WARNs", F_DQ, P, "K05", "Warn/fail findings that carry a known cause (context only: a known cause does not resolve a FAIL).",
            "CALCULATE ( COUNTROWS ( DQFinding ), DQFinding[Is Resolved Run] = TRUE (), DQFinding[Has Known Cause] = TRUE (), Severity[Is Problem] = TRUE () )",
            "dq", "SELECT COUNT(*) " + DQF + " {wf}"),
    Measure("Cross-Source FAILs", F_DQ, P, "K08", "FAILs of the cross-source reconciliation checks X1-X8 in the resolved runs.",
            "CALCULATE ( COUNTROWS ( DQFinding ), DQFinding[Is Resolved Run] = TRUE (), 'Check'[Cross-Dataset] = TRUE (), Severity[Severity] = \"Fail\" )",
            "dq", "SELECT COUNT(*) " + DQF + " {wf}"),
    Measure("Worst Dataset", F_DQ, "", "", "Dataset(s) with the most warn + fail findings in the current filter (ties listed).",
            "-- 'Dataset' must be quoted: DATASET is a reserved word in current DAX\n"
            "VAR t = TOPN ( 1, VALUES ( 'Dataset'[Dataset] ), [Warn + Fail Findings], DESC )\nRETURN CONCATENATEX ( t, 'Dataset'[Dataset], \", \", 'Dataset'[Dataset], ASC )",
            "dq", None),
    Measure("Excluded Days", F_DQ, P, "K01", "Dates the production gate excluded.",
            "CALCULATE ( COUNTROWS ( DQDayVerdict ), DQDayVerdict[Verdict] = \"exclude\" )", "dq",
            "SELECT COUNT(*) FROM rpt.DQDayVerdict v JOIN rpt.[Date] d ON d.DateKey = v.DateKey {wv} {and_v}"),
    Measure("Day Verdict", F_DQ, "", "K01", "The gate's verdict for the selected date (ok / exclude / unchecked).",
            "SELECTEDVALUE ( DQDayVerdict[Verdict], \"select one date\" )", "dqday",
            "SELECT v.Verdict FROM rpt.DQDayVerdict v JOIN rpt.[Date] d ON d.DateKey = v.DateKey {wv}"),
    Measure("Rolling 20-Day Fail Rate", F_DQ, "0.000%", "K06", "Unresolved FAILs / checks over the 20 trading days up to the selected date.",
            "VAR cur = MAX ( 'Date'[Trading Day No] )\nVAR win = CALCULATETABLE ( VALUES ( 'Date'[DateKey] ), REMOVEFILTERS ( 'Date' ),\n"
            "    'Date'[Trading Day No] > cur - 20, 'Date'[Trading Day No] <= cur )\n"
            "-- REMOVEFILTERS again: otherwise the outer filter on the selected date would intersect with the window\n"
            "RETURN DIVIDE ( CALCULATE ( [Unresolved FAILs], REMOVEFILTERS ( 'Date' ), win ),\n"
            "    CALCULATE ( [Checks Evaluated], REMOVEFILTERS ( 'Date' ), win ) )",
            "dqday", sql_rolling),
    Measure("Days Since Unresolved FAIL", F_DQ, P, "K07", "Trading days since the latest date (up to the selected one) with an unresolved FAIL.",
            "VAR cur = MAX ( 'Date'[Trading Day No] )\n"
            "-- the per-day count is computed INSIDE CALCULATETABLE, where the date filters are removed\n"
            "VAR days = CALCULATETABLE ( ADDCOLUMNS ( VALUES ( 'Date'[Trading Day No] ), \"@u\", [Unresolved FAILs] ),\n"
            "    REMOVEFILTERS ( 'Date' ), 'Date'[Trading Day No] <= cur )\n"
            "VAR lastDay = MAXX ( FILTER ( days, [@u] > 0 ), 'Date'[Trading Day No] )\n"
            "RETURN IF ( NOT ISBLANK ( lastDay ), cur - lastDay )",
            "dqday", sql_days_since),
    # ---------------------------------------------------------------- pipeline
    Measure("ETL Runs", F_PIPE, P, "", "ETL runs in the selection.", "COUNTROWS ( EtlRun )", "etl",
            "SELECT COUNT(*) FROM rpt.EtlRun er JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    Measure("Failed ETL Runs", F_PIPE, P, "", "ETL runs that ended Failed.", "CALCULATE ( COUNTROWS ( EtlRun ), EtlRun[Status] = \"Failed\" )", "etl",
            "SELECT COUNT(*) FROM rpt.EtlRun er JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    Measure("Last Successful Load", F_PIPE, "yyyy-mm-dd hh:nn", "K10", "End time of the newest successful ETL run.",
            "CALCULATE ( MAX ( EtlRun[Ended] ), EtlRun[Status] = \"Succeeded\" )", "etl",
            "SELECT MAX(er.[Ended]) FROM rpt.EtlRun er JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    Measure("Avg Run Duration (s)", F_PIPE, "#,0.0", "K11", "Average ETL run duration in seconds.", "AVERAGE ( EtlRun[Duration (s)] )", "etl",
            "SELECT AVG(1.0 * er.[Duration (s)]) FROM rpt.EtlRun er JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    Measure("ETL Reconciliation Breaks", F_PIPE, P, "K09", "Reconciliation checks with status BREAK.",
            "CALCULATE ( COUNTROWS ( Reconciliation ), Reconciliation[Status] = \"BREAK\" )", "etl",
            "SELECT COUNT(*) FROM rpt.Reconciliation r JOIN rpt.EtlRun er ON er.EtlRunId = r.EtlRunId JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    Measure("Rows Rejected", F_PIPE, P, "", "Source rows the ETL rejected (etl.RejectRow).", "SUM ( EtlStep[Rows Rejected] )", "etl",
            "SELECT SUM(st.[Rows Rejected]) FROM rpt.EtlStep st JOIN rpt.EtlRun er ON er.EtlRunId = st.EtlRunId JOIN rpt.[Date] d ON d.DateKey = er.DateKey {w}"),
    # ---------------------------------------------------------------- model
    Measure("Predictions", F_MODEL, P, "K14", "Out-of-fold predictions.", "COUNTROWS ( ModelPrediction )", "model", "SELECT COUNT(*) " + MP + " {w}", hand=True),
    Measure("Correct Predictions", F_MODEL, P, "", "Predictions whose class equals the actual class.",
            "CALCULATE ( [Predictions], ModelPrediction[Is Correct] = TRUE () )", "model", "SELECT SUM(CAST(p.[Is Correct] AS int)) " + MP + " {w}", hand=True),
    Measure("Accuracy", F_MODEL, N4, "K12", "Correct / all. On a row of Actual Class it is that class's recall; on Predicted Class, its precision.",
            "DIVIDE ( [Correct Predictions], [Predictions] )", "model", "SELECT 1.0 * SUM(CAST(p.[Is Correct] AS int)) / NULLIF(COUNT(*), 0) " + MP + " {w}", hand=True),
    Measure("Balanced Accuracy", F_MODEL, N4, "K13", "Average recall over the three classes.",
            "AVERAGEX ( FILTER ( VALUES ( ActualClass[ActualClassKey] ), ActualClass[ActualClassKey] >= 0 && [Predictions] > 0 ), [Accuracy] )", "model",
            "SELECT AVG(r) FROM (SELECT 1.0 * SUM(CAST(p.[Is Correct] AS int)) / COUNT(*) AS r " + MP + " {w} GROUP BY p.ActualClassKey) x"),
    Measure("Macro F1", F_MODEL, N4, "K13", "Average F1 over the three classes (a class never predicted counts as 0).",
            "AVERAGEX (\n    FILTER ( VALUES ( ActualClass[ActualClassKey] ), ActualClass[ActualClassKey] >= 0 ),\n    VAR k = ActualClass[ActualClassKey]\n"
            "    VAR tp = CALCULATE ( [Predictions], REMOVEFILTERS ( ActualClass ), REMOVEFILTERS ( PredictedClass ), ModelPrediction[ActualClassKey] = k, ModelPrediction[PredictedClassKey] = k )\n"
            "    VAR act = CALCULATE ( [Predictions], REMOVEFILTERS ( ActualClass ), REMOVEFILTERS ( PredictedClass ), ModelPrediction[ActualClassKey] = k )\n"
            "    VAR prd = CALCULATE ( [Predictions], REMOVEFILTERS ( ActualClass ), REMOVEFILTERS ( PredictedClass ), ModelPrediction[PredictedClassKey] = k )\n"
            "    VAR pr = DIVIDE ( tp, prd, 0 )\n    VAR rc = DIVIDE ( tp, act, 0 )\n    RETURN DIVIDE ( 2 * pr * rc, pr + rc, 0 )\n)", "model",
            "SELECT AVG(CASE WHEN pr + rc = 0 THEN 0 ELSE 2 * pr * rc / (pr + rc) END) FROM (SELECT k.k, "
            "ISNULL(1.0 * SUM(IIF(p.ActualClassKey = k.k AND p.PredictedClassKey = k.k, 1, 0)) / NULLIF(SUM(IIF(p.PredictedClassKey = k.k, 1, 0)), 0), 0) AS pr, "
            "ISNULL(1.0 * SUM(IIF(p.ActualClassKey = k.k AND p.PredictedClassKey = k.k, 1, 0)) / NULLIF(SUM(IIF(p.ActualClassKey = k.k, 1, 0)), 0), 0) AS rc "
            + MP + " CROSS JOIN (VALUES (0), (1), (2)) k(k) {w} GROUP BY k.k) x"),
    Measure("Directional Precision", F_MODEL, N4, "K15", "Among predictions of Up or Down: share correct.",
            "DIVIDE ( CALCULATE ( [Correct Predictions], PredictedClass[Predicted Is Directional] = TRUE () ),\n        CALCULATE ( [Predictions], PredictedClass[Predicted Is Directional] = TRUE () ) )",
            "model", "SELECT 1.0 * SUM(CAST(p.[Is Correct] AS int)) / NULLIF(COUNT(*), 0) " + MP + " WHERE pc.[Predicted Is Directional] = 1 {and_w}"),
    Measure("Committed %", F_MODEL, PCT, "K15", "Share of predictions that are Up or Down (a trade would be placed).",
            "DIVIDE ( CALCULATE ( [Predictions], PredictedClass[Predicted Is Directional] = TRUE () ), [Predictions] )", "model",
            "SELECT 1.0 * SUM(CAST(pc.[Predicted Is Directional] AS int)) / NULLIF(COUNT(*), 0) " + MP + " {w}"),
    Measure("Precision at Threshold", F_MODEL, N4, "K15", "Directional precision among predictions with max probability >= the Threshold slicer.",
            "VAR t = [Threshold Value]\nRETURN DIVIDE (\n    CALCULATE ( [Correct Predictions], PredictedClass[Predicted Is Directional] = TRUE (), ModelPrediction[Max Prob] >= t ),\n"
            "    CALCULATE ( [Predictions], PredictedClass[Predicted Is Directional] = TRUE (), ModelPrediction[Max Prob] >= t ) )", "model",
            "SELECT 1.0 * SUM(CAST(p.[Is Correct] AS int)) / NULLIF(COUNT(*), 0) " + MP + " WHERE pc.[Predicted Is Directional] = 1 AND p.[Max Prob] >= 0.5 {and_w}"),
    Measure("Coverage at Threshold", F_MODEL, PCT, "K15", "Share of all predictions that are Up/Down with max probability >= the Threshold slicer.",
            "VAR t = [Threshold Value]\nRETURN DIVIDE ( CALCULATE ( [Predictions], PredictedClass[Predicted Is Directional] = TRUE (), ModelPrediction[Max Prob] >= t ), [Predictions] )",
            "model", "SELECT 1.0 * SUM(IIF(pc.[Predicted Is Directional] = 1 AND p.[Max Prob] >= 0.5, 1, 0)) / NULLIF(COUNT(*), 0) " + MP + " {w}"),
    Measure("Frozen Report Accuracy", F_MODEL, N4, "K12", "Accuracy as published in the frozen report (reconciliation target for [Accuracy]).",
            "CALCULATE ( SUM ( ModelMetric[Value] ), ModelMetric[Metric] = \"accuracy\" )", "model",
            "SELECT SUM(m.[Value]) FROM rpt.ModelMetric m JOIN rpt.ModelRun mr ON mr.ModelRunKey = m.ModelRunKey WHERE m.Metric = 'accuracy' {and_m}"),
    Measure("Total Importance", F_MODEL, N4, "K16", "Sum of permutation importance (use per feature, top 15).", "SUM ( FeatureImportance[Importance] )", "model",
            "SELECT SUM(fi.Importance) FROM rpt.FeatureImportance fi JOIN rpt.ModelRun mr ON mr.ModelRunKey = fi.ModelRunKey {wm}"),
    Measure("Drifted Features", F_MODEL, P, "K24", "Features with PSI > 0.25 in a PSI-reliable or range-skew section of a weekly drift report.",
            "CALCULATE ( DISTINCTCOUNT ( FeatureDrift[FeatureKey] ), FeatureDrift[PSI] > 0.25, FeatureDrift[Section] IN { \"psi-reliable\", \"range-skew\" } )",
            "drift", "SELECT COUNT(DISTINCT fd.FeatureKey) FROM rpt.FeatureDrift fd JOIN rpt.[Date] d ON d.DateKey = fd.ReportDateKey "
                     "JOIN rpt.Feature f ON f.FeatureKey = fd.FeatureKey WHERE fd.PSI > 0.25 AND fd.Section IN ('psi-reliable', 'range-skew') {and_w}"),
    # ---------------------------------------------------------------- trades
    Measure("Trades", F_TRADE, P, "K17", "Simulated trades (by exit date).", "COUNTROWS ( Trade )", "trade", "SELECT COUNT(*) " + TR + " {w}"),
    Measure("Net P&L", F_TRADE, N2, "K18", "Option P&L after costs, Rs.", "SUM ( Trade[Net P&L (Rs)] )", "trade", "SELECT SUM(t.[Net P&L (Rs)]) " + TR + " {w}", hand=True),
    Measure("Gross P&L", F_TRADE, N2, "K19", "Option P&L before costs, Rs.", "SUM ( Trade[Gross P&L (Rs)] )", "trade", "SELECT SUM(t.[Gross P&L (Rs)]) " + TR + " {w}"),
    Measure("Costs", F_TRADE, N2, "K19", "Entry + exit costs, Rs.", "SUM ( Trade[Total Cost (Rs)] )", "trade", "SELECT SUM(t.[Total Cost (Rs)]) " + TR + " {w}"),
    Measure("Cost per Trade", F_TRADE, N2, "K19", "Costs / trades, Rs.", "DIVIDE ( [Costs], [Trades] )", "trade",
            "SELECT SUM(t.[Total Cost (Rs)]) / NULLIF(COUNT(*), 0) " + TR + " {w}"),
    Measure("Cost % of Gross", F_TRADE, PCT, "K19", "Costs / |gross P&L|: how much of the edge the costs eat.", "DIVIDE ( [Costs], ABS ( [Gross P&L] ) )", "trade",
            "SELECT SUM(t.[Total Cost (Rs)]) / NULLIF(ABS(SUM(t.[Gross P&L (Rs)])), 0) " + TR + " {w}"),
    Measure("Win Rate", F_TRADE, PCT, "K20", "Trades with net P&L > 0 / trades.", "DIVIDE ( CALCULATE ( [Trades], Trade[Is Win] = TRUE () ), [Trades] )", "trade",
            "SELECT 1.0 * SUM(CAST(t.[Is Win] AS int)) / NULLIF(COUNT(*), 0) " + TR + " {w}"),
    Measure("Avg Net per Trade", F_TRADE, N2, "K20", "Net P&L / trades, Rs.", "DIVIDE ( [Net P&L], [Trades] )", "trade",
            "SELECT SUM(t.[Net P&L (Rs)]) / NULLIF(COUNT(*), 0) " + TR + " {w}"),
    Measure("Trades by Entry Date", F_TRADE, P, "", "Trades counted on their ENTRY date: the inactive relationship, switched on with USERELATIONSHIP.",
            "CALCULATE ( [Trades], USERELATIONSHIP ( Trade[EntryDateKey], 'Date'[DateKey] ) )", "trade",
            "SELECT COUNT(*) FROM rpt.Trade t JOIN rpt.ModelRun mr ON mr.ModelRunKey = t.ModelRunKey JOIN rpt.Instrument i ON i.InstrumentKey = t.InstrumentKey "
            "JOIN rpt.[Date] d ON d.DateKey = t.EntryDateKey {w}"),
    Measure("Cumulative Net P&L", F_TRADE, N2, "K21", "Net P&L from the first trade up to the selected (or last) date.",
            "VAR asOfDate = MAX ( 'Date'[Date] )\nRETURN CALCULATE ( [Net P&L], REMOVEFILTERS ( 'Date' ), 'Date'[Date] <= asOfDate )", "tradeday",
            lambda fl: sql_daily_equity(fl) + " SELECT cum FROM dd WHERE [Date] = " + sql_lit(the_date(fl)), hand=True),
    Measure("Net P&L MTD", F_TRADE, N2, "K23", "Month-to-date net P&L.", "TOTALMTD ( [Net P&L], 'Date'[Date] )", "tradeday",
            lambda fl: "SELECT SUM(t.[Net P&L (Rs)]) " + TR + " " + where(_not_date(fl), TR_T, [
                f"d.[Date] BETWEEN DATEFROMPARTS(YEAR({sql_lit(the_date(fl))}), MONTH({sql_lit(the_date(fl))}), 1) AND {sql_lit(the_date(fl))}"])),
    Measure("Net P&L FYTD", F_TRADE, N2, "K23", "Indian fiscal-year-to-date net P&L (year ends 31 March).", "TOTALYTD ( [Net P&L], 'Date'[Date], \"3/31\" )", "tradeday",
            lambda fl: "SELECT SUM(t.[Net P&L (Rs)]) " + TR + " " + where(_not_date(fl), TR_T, [
                f"d.[Fiscal Year] = (SELECT [Fiscal Year] FROM rpt.[Date] WHERE [Date] = {sql_lit(the_date(fl))}) AND d.[Date] <= {sql_lit(the_date(fl))}"]),
            hand=True),
    Measure("Drawdown", F_TRADE, N2, "K22", "Cumulative net P&L minus its running peak (equity starts at 0), on the selected date.",
            "VAR cur = MAX ( 'Date'[Date] )\nVAR peak = MAXX ( FILTER ( ALL ( 'Date'[Date] ), 'Date'[Date] <= cur ), [Cumulative Net P&L] )\n"
            "RETURN [Cumulative Net P&L] - MAX ( peak, 0 )", "tradeday",
            lambda fl: sql_daily_equity(fl) + " SELECT drawdown FROM dd WHERE [Date] = " + sql_lit(the_date(fl))),
    Measure("Max Drawdown", F_TRADE, N2, "K22", "Worst drawdown of the daily equity curve up to the selected date.",
            "VAR asOfDate = MAX ( 'Date'[Date] )\nVAR days = ADDCOLUMNS ( FILTER ( ALL ( 'Date'[Date] ), 'Date'[Date] <= asOfDate ), \"@net\", [Net P&L] )\n"
            "VAR curve = ADDCOLUMNS ( days, \"@cum\", VAR d = 'Date'[Date] RETURN SUMX ( FILTER ( days, 'Date'[Date] <= d ), [@net] ) )\n"
            "VAR peaks = ADDCOLUMNS ( curve, \"@peak\", VAR d = 'Date'[Date] RETURN MAX ( 0, MAXX ( FILTER ( curve, 'Date'[Date] <= d ), [@cum] ) ) )\n"
            "RETURN MINX ( peaks, [@cum] - [@peak] )", "tradeday",
            lambda fl: sql_daily_equity(fl) + " SELECT MIN(drawdown) FROM dd"),
    # ---------------------------------------------------------------- features
    Measure("Feature Daily Average", F_FEAT, "0.000000", "", "Minute-weighted average of the feature value (from the daily summary view).",
            "DIVIDE ( SUMX ( FeatureValueDaily, FeatureValueDaily[Average] * FeatureValueDaily[Minutes] ), SUM ( FeatureValueDaily[Minutes] ) )", "feature",
            "SELECT SUM(v.[Average] * v.Minutes) / NULLIF(SUM(v.Minutes), 0) FROM rpt.FeatureValueDaily v JOIN rpt.[Date] d ON d.DateKey = v.DateKey "
            "JOIN rpt.Instrument i ON i.InstrumentKey = v.InstrumentKey JOIN rpt.Feature f ON f.FeatureKey = v.FeatureKey {w}"),
]


def _worst_sql(filters):
    w = where(filters, DQF_T, ["x.[Is Resolved Run] = 1", "sv.[Is Problem] = 1"])
    return (f"WITH c AS (SELECT ds.Dataset, COUNT(*) AS n {DQF} {w} GROUP BY ds.Dataset) "
            "SELECT STRING_AGG(Dataset, ', ') WITHIN GROUP (ORDER BY Dataset) FROM c WHERE n = (SELECT MAX(n) FROM c)")


# measure-specific extra conditions that the generic templates do not carry
_EXTRA_DQF = {"Warn + Fail Findings": ["x.[Is Resolved Run] = 1", "sv.[Is Problem] = 1"],
              "Known-Cause FAILs and WARNs": ["x.[Is Resolved Run] = 1", "x.[Has Known Cause] = 1", "sv.[Is Problem] = 1"],
              "Cross-Source FAILs": ["x.[Is Resolved Run] = 1", "c.[Cross-Dataset] = 1", "sv.Severity = 'Fail'"]}
_EXTRA_ETL = {"Failed ETL Runs": ["er.Status = 'Failed'"], "Last Successful Load": ["er.Status = 'Succeeded'"],
              "ETL Reconciliation Breaks": ["r.Status = 'BREAK'"]}


# Exception counters return 0, not blank, when nothing matches: on a KPI card '0 breaks' is the good news and a blank card
# would look like missing data. Only these: forcing 0 everywhere would make tables list every row, even empty ones.
ZERO_WHEN_EMPTY = {"FAILs", "Unresolved FAILs", "Known-Cause FAILs and WARNs", "Cross-Source FAILs", "Excluded Days",
                   "Failed ETL Runs", "ETL Reconciliation Breaks", "Rows Rejected"}
for _m in MEASURES:
    if _m.name in ZERO_WHEN_EMPTY and not _m.dax.startswith("COALESCE"):
        _m.dax = f"COALESCE ( {_m.dax}, 0 )"


def measure_sql(m: Measure, filters) -> str:
    if m.name == "Worst Dataset":
        return _worst_sql(filters)
    if m.name in _EXTRA_DQF:
        return "SELECT COUNT(*) " + DQF + " " + where(filters, DQF_T, _EXTRA_DQF[m.name])
    if m.name in _EXTRA_ETL:
        base = m.sql.split("{w}")[0]
        return base + where(filters, {"Date", "EtlRun"}, _EXTRA_ETL[m.name])
    if m.name == "Excluded Days":
        return "SELECT COUNT(*) FROM rpt.DQDayVerdict v JOIN rpt.[Date] d ON d.DateKey = v.DateKey " + where(filters, {"Date"}, ["v.Verdict = 'exclude'"])
    if m.name == "Frozen Report Accuracy":
        return ("SELECT SUM(m.[Value]) FROM rpt.ModelMetric m JOIN rpt.ModelRun mr ON mr.ModelRunKey = m.ModelRunKey "
                + where(filters, {"ModelRun"}, ["m.Metric = 'accuracy'", "m.ActualClassKey = -2"]))
    if callable(m.sql):
        return m.sql(filters)
    return m.sql.format(**m._w(filters))


def dax_query(m: Measure, filters) -> str:
    args = ", ".join([f"[{m.name}]"] + dax_filters(filters))
    return f'EVALUATE ROW ( "v", CALCULATE ( {args} ) )'


def cases_for(m: Measure) -> list:
    return CASES[m.family]
