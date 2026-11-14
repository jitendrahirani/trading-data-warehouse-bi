<#  Write measure expressions into the model of an OPEN Power BI Desktop file (the way Tabular Editor does, through TOM).
    -InFile: JSON array of {name, expression}. After it runs, press Ctrl+S in Power BI Desktop to save.
    Used by bi/etl/update_measures.py.
#>
param([Parameter(Mandatory)][string]$InFile)
$ErrorActionPreference = 'Stop'
$null = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Core.dll')
$null = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Tabular.Json.dll')
$null = [Reflection.Assembly]::LoadFrom('C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.Tabular.dll')
$target = $null
foreach ($p in (Get-Process msmdsrv -ErrorAction SilentlyContinue)) {
    $port = (Get-NetTCPConnection -OwningProcess $p.Id -State Listen | Select-Object -First 1).LocalPort
    $srv = New-Object Microsoft.AnalysisServices.Tabular.Server
    $srv.Connect("Data Source=localhost:$port")
    foreach ($d in $srv.Databases) { if ($d.Model.Tables.Count -gt 0) { $target = $d } }
    if ($target) { break } else { $srv.Disconnect() }
}
if (-not $target) { 'NO MODEL'; exit 2 }
$items = Get-Content $InFile -Raw -Encoding UTF8 | ConvertFrom-Json
$changed = 0
foreach ($it in $items) {
    $m = $null
    foreach ($t in $target.Model.Tables) { if ($t.Measures.Contains($it.name)) { $m = $t.Measures[$it.name] } }
    if (-not $m) { "missing: $($it.name)"; continue }
    if ($m.Expression -ne $it.expression) { $m.Expression = $it.expression; $changed++ }
}
$target.Model.SaveChanges() | Out-Null
"updated $changed measure(s) in the open model; press Ctrl+S in Power BI Desktop"
$srv.Disconnect()
