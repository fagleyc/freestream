<#
Remove the Microsoft "Windows Driver Policy" so legacy cross-signed kernel
drivers (IOtech DaqX600e for the DaqBook/StrainBook) can load again.

Per Microsoft: https://support.microsoft.com/en-us/windows/the-windows-driver-policy-ecd2a78c-750c-415d-93f2-e37302ce0443
Security note: this lowers driver-loading protection on this PC. Windows
updates may silently re-deploy the policy - re-run check_daqx_driver.ps1
after major updates, and this script again if needed.

USAGE (both from an elevated PowerShell - right-click, Run as Administrator):

  1) .\remove_driver_policy.ps1 -Preflight
       Suspends BitLocker for the next 2 restarts (avoids a recovery-key
       prompt) and reboots into the UEFI firmware, where YOU disable
       Secure Boot (usually under Security or Boot tab).

  2) .\remove_driver_policy.ps1
       (after booting back into Windows with Secure Boot OFF)
       Deletes the two policy files from the EFI partition and from
       C:\Windows\System32\CodeIntegrity, then prompts you to restart.
       After the restart, go back into UEFI and RE-ENABLE Secure Boot.

  3) .\check_daqx_driver.ps1   - verify the driver now loads.
#>

param([switch]$Preflight)

$ErrorActionPreference = "Stop"

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: run this from an elevated (Administrator) PowerShell." -ForegroundColor Red
    exit 1
}

$GUIDS = @(
    "{784C4414-79F4-4C32-A6A5-F0FB42A51D0D}",   # Windows Driver Policy (audit)
    "{8F9CB695-5D48-48D6-A329-7202B44607E3}"    # Windows Driver Policy (enforce)
)

if ($Preflight) {
    Write-Host "== Preflight: BitLocker + reboot to UEFI ==" -ForegroundColor Cyan
    $blv = Get-BitLockerVolume -MountPoint C: -ErrorAction SilentlyContinue
    if ($blv -and $blv.ProtectionStatus -eq "On") {
        Write-Host "BitLocker is ON for C: - suspending for the next 2 restarts..."
        Suspend-BitLocker -MountPoint C: -RebootCount 2 | Out-Null
        Write-Host "BitLocker suspended (auto-resumes after 2 restarts)." -ForegroundColor Green
    } else {
        Write-Host "BitLocker not active on C: - nothing to suspend." -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "Rebooting into UEFI firmware setup in 15 s ..." -ForegroundColor Yellow
    Write-Host "IN THE FIRMWARE: find 'Secure Boot' (Security or Boot tab) and set it to DISABLED, then save & exit."
    Write-Host "Back in Windows, run this script again WITHOUT -Preflight."
    shutdown /r /fw /t 15
    exit 0
}

Write-Host "== Windows Driver Policy removal ==" -ForegroundColor Cyan

# refuse to proceed if Secure Boot is still on
try {
    if (Confirm-SecureBootUEFI) {
        Write-Host "ERROR: Secure Boot is still ENABLED. Run '.\remove_driver_policy.ps1 -Preflight'" -ForegroundColor Red
        Write-Host "first, disable Secure Boot in the firmware, then run this again."
        exit 1
    }
    Write-Host "Secure Boot: disabled - OK to proceed."
} catch {
    Write-Host "NOTE: could not query Secure Boot state ($_). Proceeding anyway." -ForegroundColor Yellow
}

# mount the EFI system partition on a free drive letter
$efi = $null
foreach ($letter in "S","T","U") {
    if (-not (Test-Path "${letter}:\")) {
        mountvol "${letter}:" /S
        if ($LASTEXITCODE -eq 0) { $efi = "${letter}:"; break }
    }
}
if (-not $efi) {
    Write-Host "ERROR: could not mount the EFI system partition." -ForegroundColor Red
    exit 1
}
Write-Host "EFI partition mounted at $efi"

$deleted = 0
try {
    foreach ($dir in "$efi\EFI\Microsoft\Boot\CiPolicies\Active",
                     "$env:windir\System32\CodeIntegrity\CiPolicies\Active") {
        foreach ($g in $GUIDS) {
            $f = Join-Path $dir "$g.cip"
            if (Test-Path $f) {
                Remove-Item $f -Force -Confirm:$false
                Write-Host "  deleted $f" -ForegroundColor Green
                $deleted++
            } else {
                Write-Host "  not present: $f" -ForegroundColor DarkGray
            }
        }
    }
} finally {
    mountvol $efi /D
    Write-Host "EFI partition unmounted."
}

Write-Host ""
if ($deleted -gt 0) {
    Write-Host "$deleted policy file(s) removed." -ForegroundColor Green
} else {
    Write-Host "No policy files found - policy may already be removed." -ForegroundColor Yellow
}
Write-Host "NEXT: 1) restart  2) re-enable Secure Boot in UEFI  3) run .\check_daqx_driver.ps1" -ForegroundColor Cyan
$ans = Read-Host "Restart into UEFI now? (y/n)"
if ($ans -eq "y") { shutdown /r /fw /t 5 }
