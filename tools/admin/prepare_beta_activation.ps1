param([switch]$Apply)
if ($Apply) { throw 'Remote mutation disabled in this workspace. Review and deploy manually.' }
Write-Output 'DRY RUN: migrations, Edge Functions, required secrets, providers and missing configuration.'
& "$PSScriptRoot\..\.venv-beta\Scripts\python.exe" "$PSScriptRoot\release\check_beta_configuration.py"
