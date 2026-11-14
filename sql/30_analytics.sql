/* Phase 5 step 5.2: six analytic queries (CTEs + window functions) over the warehouse.
   Each block starts with '-- name: <id>'. bi/etl/analytics_check.py runs every block and compares the result with the
   same calculation done independently in pandas from the SOURCE files (not from the warehouse).
   Window frames are written out (ROWS ...) on purpose: the default frame with ORDER BY is RANGE UNBOUNDED PRECEDING,
   which treats ties as one row and spills to disk (a common silent bug and performance trap).
*/

-- name: q1_rolling_fail_rate
-- K06: unresolved FAILs / checks evaluated over the last 20 trading days, for every DQ date.
WITH daily AS (
    SELECT s.DateKey, d.TradingDayIndex,
           SUM(s.UnresolvedDayFails + s.UnresolvedMinuteFails) AS unresolved,
           SUM(s.ChecksEvaluated)                               AS checks
    FROM dw.FactDQDailyStatus s
    JOIN dw.DimDate d ON d.DateKey = s.DateKey
    GROUP BY s.DateKey, d.TradingDayIndex
)
SELECT DateKey,
       unresolved,
       checks,
       SUM(unresolved) OVER (ORDER BY TradingDayIndex ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS unresolved_20d,
       SUM(checks)     OVER (ORDER BY TradingDayIndex ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS checks_20d,
       CAST(1.0 * SUM(unresolved) OVER (ORDER BY TradingDayIndex ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
              / SUM(checks)   OVER (ORDER BY TradingDayIndex ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS decimal(12, 8)) AS fail_rate_20d
FROM daily
ORDER BY DateKey;

-- name: q2_pass_rate_day_over_day
-- K03 with LAG: today's pass rate ((pass + note) / checks) and the change from the previous DQ date.
WITH daily AS (
    SELECT DateKey, SUM(PassCount + NoteCount) AS good, SUM(ChecksEvaluated) AS checks
    FROM dw.FactDQDailyStatus
    GROUP BY DateKey
)
SELECT DateKey,
       CAST(1.0 * good / checks AS decimal(12, 8)) AS pass_rate,
       CAST(1.0 * good / checks - LAG(1.0 * good / checks) OVER (ORDER BY DateKey) AS decimal(12, 8)) AS change_vs_previous
FROM daily
ORDER BY DateKey;

-- name: q3_worst_datasets
-- RANK: datasets by number of warn + fail findings in the resolved runs (ties share a rank); top 10.
WITH problems AS (
    SELECT ds.DatasetCode, COUNT(*) AS warn_fail
    FROM dw.FactDQFinding f
    JOIN dw.DimDataset ds ON ds.DatasetKey = f.DatasetKey
    JOIN dw.DimSeverity sv ON sv.SeverityKey = f.SeverityKey
    WHERE f.IsResolvedRun = 1 AND sv.IsProblem = 1
    GROUP BY ds.DatasetCode
), ranked AS (
    SELECT DatasetCode, warn_fail, RANK() OVER (ORDER BY warn_fail DESC) AS rnk FROM problems
)
SELECT DatasetCode, warn_fail, rnk
FROM ranked
WHERE rnk <= 10
ORDER BY rnk, DatasetCode;

-- name: q4_drawdown
-- K21 / K22: running net P&L per scenario in exit order, its running peak (equity starts at 0), the drawdown from that
-- peak, and the worst drawdown. Ties on exit time are broken by the row order in the source file.
WITH equity AS (
    SELECT t.ModelRunKey, t.ExitTsUtc, t.SourceRowNo,
           SUM(t.OptNetRs) OVER (PARTITION BY t.ModelRunKey ORDER BY t.ExitTsUtc, t.SourceRowNo ROWS UNBOUNDED PRECEDING) AS cum,
           SUM(t.OptNetRs) OVER (PARTITION BY t.ModelRunKey) AS total
    FROM dw.FactTrade t
), peaks AS (
    SELECT *, GREATEST(0, MAX(cum) OVER (PARTITION BY ModelRunKey ORDER BY ExitTsUtc, SourceRowNo ROWS UNBOUNDED PRECEDING)) AS peak
    FROM equity
)
SELECT r.RunCode, COUNT(*) AS trades, MAX(p.cum) AS best_cum,
       MIN(p.cum - p.peak) AS max_drawdown, MAX(p.total) AS final_net
FROM peaks p
JOIN dw.DimModelRun r ON r.ModelRunKey = p.ModelRunKey
GROUP BY r.RunCode
ORDER BY r.RunCode;

-- name: q5_fail_islands
-- Gaps and islands: runs of consecutive trading days on which a dataset had at least one FAIL (resolved runs).
-- Consecutive days have consecutive TradingDayIndex values, so TradingDayIndex - ROW_NUMBER() is constant per island.
WITH fail_days AS (
    SELECT DISTINCT ds.DatasetCode, d.TradingDayIndex, d.DateKey
    FROM dw.FactDQDailyStatus s
    JOIN dw.DimDataset ds ON ds.DatasetKey = s.DatasetKey
    JOIN dw.DimDate d ON d.DateKey = s.DateKey
    WHERE s.FailCount > 0
), grouped AS (
    SELECT *, TradingDayIndex - ROW_NUMBER() OVER (PARTITION BY DatasetCode ORDER BY TradingDayIndex) AS island
    FROM fail_days
)
SELECT DatasetCode, MIN(DateKey) AS first_day, MAX(DateKey) AS last_day, COUNT(*) AS trading_days
FROM grouped
GROUP BY DatasetCode, island
ORDER BY DatasetCode, first_day;

-- name: q6_days_since_unresolved
-- K07: for every DQ date, trading days since the latest date (up to and including it) that had an unresolved FAIL.
-- NULL until the first such date.
WITH daily AS (
    SELECT s.DateKey, d.TradingDayIndex, SUM(s.UnresolvedDayFails + s.UnresolvedMinuteFails) AS unresolved
    FROM dw.FactDQDailyStatus s
    JOIN dw.DimDate d ON d.DateKey = s.DateKey
    GROUP BY s.DateKey, d.TradingDayIndex
)
SELECT DateKey, unresolved,
       TradingDayIndex - MAX(IIF(unresolved > 0, TradingDayIndex, NULL))
                         OVER (ORDER BY TradingDayIndex ROWS UNBOUNDED PRECEDING) AS days_since_unresolved
FROM daily
ORDER BY DateKey;
