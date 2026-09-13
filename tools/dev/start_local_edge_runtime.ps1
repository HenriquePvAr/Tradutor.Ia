param([switch]$Background)
$ErrorActionPreference = 'Stop'
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$status = @(& npx.cmd --prefix apps/auth-service supabase status -o env 2>$null)
$ErrorActionPreference = $previousErrorAction
$api = ($status | Select-String '^API_URL=').ToString().Split('=',2)[1].Trim('"')
$anon = ($status | Select-String '^ANON_KEY=').ToString().Split('=',2)[1].Trim('"')
if (-not $api -or -not $anon) { throw 'local_supabase_config_missing' }
$uri = [Uri]$api
if ($uri.Host -notin @('127.0.0.1','localhost')) { throw 'remote_target_rejected' }
$envPath = Join-Path $env:TEMP 'yomu-sekai-local-edge.env'
Set-Content -LiteralPath $envPath -Value "SUPABASE_URL=$api`nSUPABASE_ANON_KEY=$anon`nMOCK_TRANSLATION_PROVIDER=true" -Encoding ascii
$args = "--prefix apps/auth-service supabase functions serve --env-file `"$envPath`""
if ($Background) { Start-Process -FilePath 'npx.cmd' -ArgumentList $args -WorkingDirectory (Get-Location) -WindowStyle Hidden | Out-Null; Write-Output 'EDGE_RUNTIME_STARTED=BACKGROUND' }
else { & npx.cmd --prefix apps/auth-service supabase functions serve --env-file $envPath }
