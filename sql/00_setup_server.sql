/* ============================================================================================
   Phase 0.2  One-time server setup for the BI showcase.
   Run in SSMS, connected to localhost with Windows authentication. Safe to run again.
   ============================================================================================ */
USE master;
GO

/* 1. Which SQL Server is this? Expect an edition containing "Developer". */
SELECT  @@SERVERNAME                      AS server_name,
        SERVERPROPERTY('Edition')         AS edition,
        SERVERPROPERTY('ProductVersion')  AS product_version,
        SERVERPROPERTY('ProductLevel')    AS product_level;
GO

/* 2. Cap SQL Server's memory at 1.5 GB. The laptop has 7.7 GB, and by default SQL Server takes
      as much as it can and keeps it. 'max server memory' is an "advanced" option, so advanced
      options must be shown first. */
EXEC sys.sp_configure N'show advanced options', 1;
RECONFIGURE;
EXEC sys.sp_configure N'max server memory (MB)', 1536;
RECONFIGURE;
GO

SELECT name, value_in_use
FROM   sys.configurations
WHERE  name IN (N'max server memory (MB)', N'show advanced options');
GO

/* 3. Two databases:
        TradingBI_Dev   where we build and test
        TradingBI_Prod  the "released" copy (Dev -> Prod promotion is shown in Phase 9)
      SIMPLE recovery: a laptop demo needs no point-in-time restore. A production credit-risk mart
      would normally use FULL recovery with log backups. */
IF DB_ID(N'TradingBI_Dev') IS NULL
    CREATE DATABASE TradingBI_Dev;
GO
IF DB_ID(N'TradingBI_Prod') IS NULL
    CREATE DATABASE TradingBI_Prod;
GO
ALTER DATABASE TradingBI_Dev  SET RECOVERY SIMPLE;
ALTER DATABASE TradingBI_Prod SET RECOVERY SIMPLE;
GO

SELECT name, recovery_model_desc, compatibility_level, create_date
FROM   sys.databases
WHERE  name LIKE N'TradingBI%';
GO

/* 4. SQL Server services: startup type should be Manual for both (set in the installer or in
      SQL Server Configuration Manager). SQL Server Agent is needed from Phase 4.5. */
SELECT servicename, startup_type_desc, status_desc
FROM   sys.dm_server_services;
GO
