/* Phase 2: row-level-security mapping (requirement N4, persona P2 'Desk Head').
   Power BI's dynamic RLS role filters DimInstrument to the rows mapped to USERPRINCIPALNAME(); the same table could
   drive a SQL Server security policy. Users here are role-played test identities, not real people.
*/
SET NOCOUNT ON;

/* One row per user x desk the user may see (row-level security). */
CREATE TABLE sec.UserInstrument (
    UserPrincipal    nvarchar(256) NOT NULL, -- Login as Power BI reports it (USERPRINCIPALNAME) | seeded (role-played users) | lower case
    InstrumentKey    smallint     NOT NULL CONSTRAINT FK_UserInstrument_Instr REFERENCES dw.DimInstrument (InstrumentKey), -- Desk the user may see | seeded |
    GrantedAt        datetime2(0) NOT NULL CONSTRAINT DF_UserInstrument_GrantedAt DEFAULT SYSDATETIME(), -- When the access was granted | seeded | audit
    CONSTRAINT PK_UserInstrument PRIMARY KEY (UserPrincipal, InstrumentKey)
);
GO
