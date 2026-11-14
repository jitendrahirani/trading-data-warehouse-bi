param([string]$OutFile)
try { Add-Type -Path 'C:\Program Files\DAX Studio\bin\Microsoft.AnalysisServices.AdomdClient.dll' -ErrorAction Stop } catch { }
$best = $null
foreach ($p in (Get-Process msmdsrv -ErrorAction SilentlyContinue)) {
  $port = (Get-NetTCPConnection -OwningProcess $p.Id -State Listen | Select-Object -First 1).LocalPort
  $c = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$port"); $c.Open()
  $cmd = $c.CreateCommand(); $cmd.CommandText = 'SELECT [ID] FROM $SYSTEM.TMSCHEMA_TABLES'
  $r = $cmd.ExecuteReader(); $n = 0; while ($r.Read()) { $n++ }; $r.Close()
  if ($n -gt 0) { $best = $c; break } else { $c.Close() }
}
if (-not $best) { 'NO MODEL'; exit }
function Q($sql) { $cmd = $best.CreateCommand(); $cmd.CommandText = $sql; $r = $cmd.ExecuteReader(); $rows = @()
  while ($r.Read()) { $o = @{}; for ($i = 0; $i -lt $r.FieldCount; $i++) { $o[$r.GetName($i)] = $r.GetValue($i) }; $rows += [pscustomobject]$o }; $r.Close(); $rows }
$out = [pscustomobject]@{
  tables = Q 'SELECT [ID], [Name], [IsHidden], [DataCategory] FROM $SYSTEM.TMSCHEMA_TABLES'
  columns = Q 'SELECT [ID], [TableID], [ExplicitName], [InferredName], [IsHidden], [ExplicitDataType], [SortByColumnID], [IsKey] FROM $SYSTEM.TMSCHEMA_COLUMNS'
  rels = Q 'SELECT [ID], [FromTableID], [FromColumnID], [ToTableID], [ToColumnID], [IsActive], [CrossFilteringBehavior], [FromCardinality], [ToCardinality] FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS'
  measures = Q 'SELECT [Name], [TableID], [Expression], [DisplayFolder], [FormatString], [State], [ErrorMessage] FROM $SYSTEM.TMSCHEMA_MEASURES'
}
$out | ConvertTo-Json -Depth 4 | Set-Content -Path $OutFile -Encoding UTF8
$best.Close()
'ok'
