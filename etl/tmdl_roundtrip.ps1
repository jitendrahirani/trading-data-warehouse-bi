<#  Parse a TMDL folder with Microsoft's TmdlSerializer (Microsoft.AnalysisServices.Tabular 19.x from DAX Studio), print
    what it contains, and write it back to -Out in canonical TMDL. Any structural error throws, and the script exits 1.
    Used by bi/etl/build_pbip.py.
#>
param([Parameter(Mandatory)][string]$In, [Parameter(Mandatory)][string]$Out)
$ErrorActionPreference = 'Stop'
try {
    $null = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Core.dll')
    $null = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Tabular.Json.dll')
    $asm = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Tabular.dll')
    $ser = $asm.GetType('Microsoft.AnalysisServices.Tabular.TmdlSerializer')
    $db = $ser.GetMethod('DeserializeDatabaseFromFolder', [type[]]@([string])).Invoke($null, @($In))
    $m = $db.Model
    $nm = 0; foreach ($t in $m.Tables) { $nm += $t.Measures.Count }
    $inactive = @($m.Relationships | Where-Object { -not $_.IsActive }).Count
    $dateKey = @($m.Tables['Date'].Columns | Where-Object { $_.IsKey }).Count
    "parsed: compatibility $($db.CompatibilityLevel), tables $($m.Tables.Count), relationships $($m.Relationships.Count) ($inactive inactive), measures $nm, roles $($m.Roles.Count), expressions $($m.Expressions.Count), Date dataCategory '$($m.Tables['Date'].DataCategory)' key columns $dateKey"
    if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
    $ser.GetMethod('SerializeDatabaseToFolder', [type[]]@($db.GetType(), [string])).Invoke($null, @($db, $Out))
    'OK'
} catch {
    $e = $_.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    "ERROR: $($e.Message)"
    exit 1
}
