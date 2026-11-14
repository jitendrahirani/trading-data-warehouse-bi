/* DEV ONLY: drops every table in the stg, dw, etl and sec schemas so the DDL can be rebuilt from scratch.
   bi/etl/apply_ddl.py runs it only with --rebuild and refuses to run it unless the database name ends in _Dev.
   Order: foreign keys first (so tables can go in any order), then the tables.
*/
SET NOCOUNT ON;
IF DB_NAME() NOT LIKE N'%[_]Dev'
BEGIN
    RAISERROR (N'90_drop_all.sql refuses to run outside a database whose name ends in _Dev.', 16, 1);
    RETURN;
END;

DECLARE @sql nvarchar(max) = N'';
SELECT @sql += N'ALTER TABLE ' + QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name)
             + N' DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';' + CHAR(10)
FROM sys.foreign_keys AS fk
JOIN sys.tables AS t ON t.object_id = fk.parent_object_id
WHERE SCHEMA_NAME(t.schema_id) IN (N'stg', N'dw', N'etl', N'sec');
EXEC sys.sp_executesql @sql;

SET @sql = N'';
SELECT @sql += N'DROP TABLE ' + QUOTENAME(SCHEMA_NAME(schema_id)) + N'.' + QUOTENAME(name) + N';' + CHAR(10)
FROM sys.tables
WHERE SCHEMA_NAME(schema_id) IN (N'stg', N'dw', N'etl', N'sec');
EXEC sys.sp_executesql @sql;
GO
