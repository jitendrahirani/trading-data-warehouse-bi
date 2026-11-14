/* Phase 2 step 2.2: schemas.
   Run against TradingBI_Dev (bi/etl/apply_ddl.py runs every script in order). Re-runnable.

   stg  raw copies of the source files, all text, plus lineage columns (Phase 3 loads them)
   dw   the star schema: Dim* and Fact* tables
   etl  ETL control tables (run log, step log, rejects, reconciliation, watermarks) and the load procedures
   rpt  views that Power BI and the paginated report read (Phase 4/6); reports never read dw directly
   sec  row-level-security mapping (which Windows user sees which desk)
*/
SET NOCOUNT ON;
IF SCHEMA_ID(N'stg') IS NULL EXEC (N'CREATE SCHEMA stg AUTHORIZATION dbo');
IF SCHEMA_ID(N'dw')  IS NULL EXEC (N'CREATE SCHEMA dw  AUTHORIZATION dbo');
IF SCHEMA_ID(N'etl') IS NULL EXEC (N'CREATE SCHEMA etl AUTHORIZATION dbo');
IF SCHEMA_ID(N'rpt') IS NULL EXEC (N'CREATE SCHEMA rpt AUTHORIZATION dbo');
IF SCHEMA_ID(N'sec') IS NULL EXEC (N'CREATE SCHEMA sec AUTHORIZATION dbo');
GO
