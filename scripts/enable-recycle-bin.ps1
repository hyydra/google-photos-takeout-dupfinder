<#
.SYNOPSIS
  Report (default) or enable the Recycle Bin on every fixed local drive of the current user.

.DESCRIPTION
  Windows deletes files PERMANENTLY, with no warning, on a drive whose Recycle Bin is disabled
  ("Don't move files to the Recycle Bin"), and a full bin purges its oldest items. The dupfinder's local
  clean-up sends duplicates to the Recycle Bin, so this script shows (and optionally fixes) each drive:

    * Enabled?         NukeOnDelete = 0
    * Capacity (MB)    MaxCapacity; default Windows size is a percentage of the drive
    * Used / free      what is already in the bin, so you can see if it is nearly full

  It changes only the current user's per-drive Recycle Bin settings (HKCU), never deletes anything and never
  empties a bin. Removable drives (USB sticks, SD cards) are skipped: they have no Recycle Bin.

.PARAMETER Apply
  Actually write the settings. Without it the script only prints what it would do.

.PARAMETER MaxCapacityGB
  With -Apply: set each drive's bin capacity to this many GB (never shrinks an existing larger capacity).
  Omit to leave capacities alone and only make sure the bin is enabled.

.PARAMETER Drive
  Limit to these drive letters, e.g. -Drive K  or  -Drive K,L. Default: every fixed drive.

.EXAMPLE
  .\enable-recycle-bin.ps1 -Apply -Drive K -MaxCapacityGB 450   # only K:
  .\enable-recycle-bin.ps1                       # report only
  .\enable-recycle-bin.ps1 -Apply                # enable the bin on every fixed drive
  .\enable-recycle-bin.ps1 -Apply -MaxCapacityGB 400
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [int]$MaxCapacityGB = 0,
    [string[]]$Drive = @()      # e.g. -Drive K  (default: every fixed drive)
)

$ErrorActionPreference = 'Stop'
$base = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume'

function Get-BinUsedBytes([string]$root) {
    $bin = Join-Path $root '$Recycle.Bin'
    if (-not (Test-Path -LiteralPath $bin -ErrorAction SilentlyContinue)) { return 0 }
    $sum = 0
    Get-ChildItem -LiteralPath $bin -Force -ErrorAction SilentlyContinue | ForEach-Object {
        Get-ChildItem -LiteralPath $_.FullName -Force -Filter '$R*' -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.PSIsContainer) {
                $sum += (Get-ChildItem -LiteralPath $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
            } else { $sum += $_.Length }
        }
    }
    return [int64]$sum
}

$changed = 0
$letters = @($Drive | ForEach-Object { ($_ -replace '[:\\]', '').ToUpper() })
$volumes = Get-CimInstance Win32_Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 3 } |
    Where-Object { $letters.Count -eq 0 -or $letters -contains $_.DriveLetter.TrimEnd(':').ToUpper() } | Sort-Object DriveLetter
foreach ($v in $volumes) {
    $guid = ($v.DeviceID -replace '^\\\\\?\\Volume', '' -replace '\\$', '')      # {xxxxxxxx-...}
    $key = Join-Path $base $guid
    $sizeGB = [math]::Round($v.Capacity / 1GB)
    $exists = Test-Path -LiteralPath $key
    $cur = if ($exists) { Get-ItemProperty -LiteralPath $key } else { $null }
    $enabled = $exists -and ($cur.NukeOnDelete -eq 0)
    $capMB = if ($exists) { [int64]$cur.MaxCapacity } else { 0 }
    $usedGB = [math]::Round((Get-BinUsedBytes $v.DriveLetter) / 1GB, 1)

    $wantCapMB = $capMB
    if ($MaxCapacityGB -gt 0) { $wantCapMB = [math]::Max($capMB, [int64]$MaxCapacityGB * 1024) }
    if ($wantCapMB -le 0) { $wantCapMB = [int64]([math]::Round($v.Capacity / 1MB * 0.10)) }   # Windows default: 10%

    $state = if ($enabled) { 'ENABLED ' } else { 'DISABLED' }
    $full = if ($capMB -gt 0 -and ($usedGB * 1024) -gt ($capMB * 0.9)) { '  <-- NEARLY FULL: new items will purge the oldest' } else { '' }
    Write-Host ("{0}  {1}  drive {2} GB  bin cap {3} GB  used {4} GB{5}" -f $v.DriveLetter, $state, $sizeGB, [math]::Round($capMB / 1024, 1), $usedGB, $full)

    $needsChange = (-not $enabled) -or ($wantCapMB -gt $capMB)
    if ($needsChange) {
        if ($Apply) {
            if (-not $exists) { New-Item -Path $key -Force | Out-Null }
            Set-ItemProperty -LiteralPath $key -Name NukeOnDelete -Value 0 -Type DWord
            Set-ItemProperty -LiteralPath $key -Name MaxCapacity -Value ([int]$wantCapMB) -Type DWord
            Write-Host ("        applied: bin enabled, capacity {0} GB" -f [math]::Round($wantCapMB / 1024, 1)) -ForegroundColor Green
            $changed++
        } else {
            Write-Host ("        would set: bin enabled, capacity {0} GB   (re-run with -Apply)" -f [math]::Round($wantCapMB / 1024, 1)) -ForegroundColor Yellow
        }
    }
}

if ($Apply -and $changed -gt 0) {
    Write-Host "`nSettings written for $changed drive(s). Windows reads them when Explorer restarts: sign out and in," -ForegroundColor Cyan
    Write-Host "or run:  Stop-Process -Name explorer -Force   (Explorer restarts by itself)." -ForegroundColor Cyan
} elseif (-not $Apply) {
    Write-Host "`nReport only. Nothing was changed." -ForegroundColor Cyan
}
Write-Host "This script never deletes files and never empties a Recycle Bin. To make room on a full bin, empty it yourself from Explorer."
