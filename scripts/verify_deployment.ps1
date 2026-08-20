param(
  [Parameter(Mandatory=$true)][string]$Url
)
$health = Invoke-WebRequest -UseBasicParsing -Uri "$Url/health" -TimeoutSec 15
if ($health.StatusCode -ne 200) { throw "health failed" }
$body = $health.Content | ConvertFrom-Json
if ($body.status -ne "ok") { throw "health status not ok" }
Write-Output (ConvertTo-Json @{ url=$Url; status=$body.status; database=$body.database } -Compress)

