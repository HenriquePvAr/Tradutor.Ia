param([switch]$Apply)
if ($Apply) { throw 'Remote deployment disabled in this workspace.' }
Write-Output 'DRY RUN: supabase db push; supabase functions deploy; supabase secrets set (not executed).'
