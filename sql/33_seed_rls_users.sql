/* Phase 6 step 6.5: role-played users for dynamic row-level security (persona P2 'Desk Head', P3 'Risk reviewer').
   example.com is a reserved domain: these are test identities, not real people. In Power BI Desktop use
   Modeling > View as > Other user = one of these names, plus the role 'Desk (dynamic)'. Re-runnable (MERGE).
*/
SET NOCOUNT ON;
MERGE sec.UserInstrument AS t
USING (VALUES (N'nifty.head@example.com', CAST(1 AS smallint)),          -- NIFTY desk head: NIFTY only
              (N'banknifty.head@example.com', CAST(2 AS smallint)),      -- BANKNIFTY desk head: BANKNIFTY only
              (N'risk.reviewer@example.com', CAST(1 AS smallint)),       -- risk reviewer: both desks
              (N'risk.reviewer@example.com', CAST(2 AS smallint))) AS s (UserPrincipal, InstrumentKey)
ON t.UserPrincipal = s.UserPrincipal AND t.InstrumentKey = s.InstrumentKey
WHEN NOT MATCHED BY TARGET THEN INSERT (UserPrincipal, InstrumentKey) VALUES (s.UserPrincipal, s.InstrumentKey)
WHEN NOT MATCHED BY SOURCE AND t.UserPrincipal LIKE N'%@example.com' THEN DELETE;
GO
