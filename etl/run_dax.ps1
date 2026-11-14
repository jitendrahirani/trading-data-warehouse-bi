<#  Runs DAX queries against the model of an OPEN Power BI Desktop file (read-only) and writes the first value of each result.
    Power BI Desktop hosts the model in a local Analysis Services process (msmdsrv.exe) on a random port; this finds the
    process that actually holds a model (more than one msmdsrv can be running), connects with the ADOMD.NET client that
    DAX Studio installs, and runs each query.
    Used by bi/etl/validate_measures.py:  run_dax.ps1 -QueryFile q.json -OutFile r.json [-Port 12345]
#>
param([Parameter(Mandatory)][string]$QueryFile, [Parameter(Mandatory)][string]$OutFile, [int]$Port = 0)
# Add-Type reports a harmless ReflectionTypeLoadException for optional types in this DLL; the client classes still load.
try { Add-Type -Path 'C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.AdomdClient.dll' -ErrorAction Stop } catch { }
$conn = $null
if ($Port -ne 0) {
    $conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$Port"); $conn.Open()
} else {
    foreach ($p in (Get-Process msmdsrv -ErrorAction SilentlyContinue)) {
        $pt = (Get-NetTCPConnection -OwningProcess $p.Id -State Listen | Select-Object -First 1).LocalPort
        $c = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$pt"); $c.Open()
        $cmd = $c.CreateCommand(); $cmd.CommandText = 'SELECT [ID] FROM $SYSTEM.TMSCHEMA_TABLES'
        $r = $cmd.ExecuteReader(); $n = 0; while ($r.Read()) { $n++ }; $r.Close()
        if ($n -gt 0) { $conn = $c; $Port = $pt; break } else { $c.Close() }
    }
}
if (-not $conn) { Write-Output 'NO MODEL: open TradingBI.pbip in Power BI Desktop first.'; exit 2 }
$queries = Get-Content $QueryFile -Raw -Encoding UTF8 | ConvertFrom-Json
$out = foreach ($q in $queries) {
    try {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $q.dax
        $r = $cmd.ExecuteReader()
        $v = $null
        if ($r.Read()) { $v = $r.GetValue(0) }
        $r.Close()
        if ($v -is [datetime]) { $v = $v.ToString('yyyy-MM-ddTHH:mm:ss') }
        elseif ($v -is [DBNull]) { $v = $null }
        [pscustomobject]@{ id = $q.id; value = $v; error = $null }
    } catch {
        [pscustomobject]@{ id = $q.id; value = $null; error = $_.Exception.Message }
    }
}
$conn.Close()
@($out) | ConvertTo-Json -Depth 3 | Set-Content -Path $OutFile -Encoding UTF8
Write-Output "port $Port, $(@($out).Count) queries"
