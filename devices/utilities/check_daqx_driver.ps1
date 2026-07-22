<#
Quick check: is the DaqX600e kernel driver loading, or being flagged by an
Application Control / Code Integrity policy?

  .\check_daqx_driver.ps1            # status + recent flag events
  (elevated adds: a live start attempt + EFI policy-file check)

Verdicts:
  RUNNING   -> driver loaded, DaqX fully usable (run probe_daqbook.py next)
  BLOCKED   -> the Windows Driver Policy flagged it (see remove_driver_policy.ps1)
  STOPPED   -> installed but not started (start it elevated: sc start DaqX600e)
#>

$ErrorActionPreference = "SilentlyContinue"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "== DaqX600e driver status ==" -ForegroundColor Cyan

$svc = Get-CimInstance Win32_SystemDriver -Filter "Name='DaqX600e'"
if (-not $svc) {
    Write-Host "Service DaqX600e: NOT INSTALLED" -ForegroundColor Red
    Write-Host "(install it once via the DaqX applet or ask Claude to re-create it)"
    exit 2
}
Write-Host ("Service: {0}  State: {1}  StartMode: {2}" -f $svc.Name, $svc.State, $svc.StartMode)
Write-Host ("Binary : {0}  (exists: {1})" -f $svc.PathName, (Test-Path "$env:windir\System32\drivers\daqx600e.sys"))

# try to start it when elevated and not running - THE definitive flag test
if ($isAdmin -and $svc.State -ne "Running") {
    Write-Host "`nAttempting to start the driver (elevated)..." -ForegroundColor Cyan
    sc.exe start DaqX600e | Out-Null
    Start-Sleep -Seconds 1
    $svc = Get-CimInstance Win32_SystemDriver -Filter "Name='DaqX600e'"
    Write-Host "Post-start state: $($svc.State)"
}

# recent flag events (System 7000 = failed to start; CI 3077 = policy block)
Write-Host "`n== Recent flag events ==" -ForegroundColor Cyan
$sys = Get-WinEvent -FilterHashtable @{LogName="System"; Id=7000} -MaxEvents 50 |
       Where-Object { $_.Message -match "DaqX600e" } | Select-Object -First 3
$ci  = Get-WinEvent -LogName "Microsoft-Windows-CodeIntegrity/Operational" -MaxEvents 200 |
       Where-Object { $_.Id -in 3077,3076 -and $_.Message -match "daqx600e" } | Select-Object -First 3
if (-not $sys -and -not $ci) { Write-Host "(none found)" -ForegroundColor Green }
foreach ($e in @($sys) + @($ci)) {
    if ($e) { Write-Host ("[{0}] id {1}: {2}" -f $e.TimeCreated, $e.Id,
              ($e.Message -split "`n")[0]) -ForegroundColor Yellow }
}

# policy files still present?
Write-Host "`n== Windows Driver Policy files ==" -ForegroundColor Cyan
$guids = "{784C4414-79F4-4C32-A6A5-F0FB42A51D0D}", "{8F9CB695-5D48-48D6-A329-7202B44607E3}"
foreach ($g in $guids) {
    $local = "$env:windir\System32\CodeIntegrity\CiPolicies\Active\$g.cip"
    Write-Host ("local {0}: {1}" -f $g, $(if (Test-Path $local) {"PRESENT"} else {"absent"}))
}
if ($isAdmin) {
    $efi = $null
    foreach ($letter in "S","T","U") {
        if (-not (Test-Path "${letter}:\")) { mountvol "${letter}:" /S; if ($LASTEXITCODE -eq 0) { $efi = "${letter}:"; break } }
    }
    if ($efi) {
        foreach ($g in $guids) {
            $f = "$efi\EFI\Microsoft\Boot\CiPolicies\Active\$g.cip"
            Write-Host ("EFI   {0}: {1}" -f $g, $(if (Test-Path $f) {"PRESENT"} else {"absent"}))
        }
        mountvol $efi /D
    }
} else {
    Write-Host "(run elevated to also check the EFI partition copies)"
}

# verdict
Write-Host "`n== Verdict ==" -ForegroundColor Cyan
$svc = Get-CimInstance Win32_SystemDriver -Filter "Name='DaqX600e'"
$blockedRecently = ($sys -or $ci) -and $svc.State -ne "Running"
if ($svc.State -eq "Running") {
    Write-Host "RUNNING - driver loaded. Next: python probe_daqbook.py" -ForegroundColor Green
} elseif ($blockedRecently) {
    Write-Host "BLOCKED - the Windows Driver Policy is flagging the driver." -ForegroundColor Red
    Write-Host "See remove_driver_policy.ps1 (and re-run this check after)."
} else {
    Write-Host "STOPPED - no recent block events; try elevated: sc start DaqX600e" -ForegroundColor Yellow
}
