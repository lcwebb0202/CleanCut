# CleanCut's helper. It runs hidden on Windows and does what CleanCut can't do from inside Docker:
#  - keeps the PC from going to sleep while CleanCut is waiting for its start time or working on a
#    movie, and lets it sleep normally the rest of the time (the screen can still turn off);
#  - checks GitHub for a new version when it starts and every day at noon, and tells the CleanCut
#    page, which then shows "Update available" with what's new;
#  - runs the update when someone clicks "Update now" on the page: new files (update.ps1), then a
#    rebuild and restart of CleanCut, then it restarts itself with the new version of this file.
#
# "Start CleanCut" runs this with -Install, which adds it to the Windows Startup folder (so it runs
# hidden after every sign-in) and starts it now. Closing a laptop's lid still puts it to sleep.
param([switch]$Install, [switch]$Restarted)

$name = "CleanCut helper"
$here = $PSScriptRoot
$url = "http://127.0.0.1:8800/api/helper"
$psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""

if ($Install) {
    $startup = [Environment]::GetFolderPath("Startup")
    Remove-Item (Join-Path $startup "CleanCut keep-awake.lnk") -ErrorAction SilentlyContinue  # its old name
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $startup "$name.lnk"))
    $link.TargetPath = "powershell.exe"
    $link.Arguments = $psArgs
    $link.WindowStyle = 7  # minimized, in case the hidden style is ignored
    $link.Description = "Keeps the PC awake while CleanCut is busy and installs CleanCut updates"
    $link.Save()  # re-written every time, so it follows the folder if it was moved
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList $psArgs
    exit 0
}

# Only one copy at a time. After an update the old copy hands over to the new one.
$mutex = New-Object System.Threading.Mutex($false, "Local\CleanCutHelper")
try {
    if (-not $mutex.WaitOne($(if ($Restarted) { 60000 } else { 0 }))) { exit 0 }
} catch [System.Threading.AbandonedMutexException] { }  # the old copy ended without letting go: ours now

Add-Type -Namespace CleanCut -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
'@
$CONTINUOUS = [uint32]"0x80000000"
$SYSTEM_REQUIRED = [uint32]"0x00000001"

$log = Join-Path $here ".cleancut-update.log"
$info = $null          # what update.ps1 -Info said last
$checked = [datetime]::MinValue
$status = ""           # "" / "updating" / "error"
$problem = ""

function Get-Info {
    try {
        $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here "update.ps1") -Info
        if ($out) { return ($out | Out-String | ConvertFrom-Json) }
    } catch { }
    return $null
}

function Update-CleanCut {
    # New files first. update.ps1 refuses if CleanCut is busy, and keeps the settings.
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here "update.ps1") -NoPause 2>&1
    $code = $LASTEXITCODE
    $out | Out-File $log -Encoding utf8
    if ($code -eq 0) { return "" }  # nothing new after all
    if ($code -ne 2) { return (($out | Where-Object { "$_".Trim() }) | Select-Object -Last 2) -join " " }
    # Then rebuild and restart CleanCut with them (minutes; the page keeps hearing "updating").
    # --force-recreate: always restart, so the page knows the update finished even when only the
    # helper's own files changed.
    $build = Join-Path $here ".cleancut-build.log"
    $p = Start-Process cmd.exe -WorkingDirectory $here -WindowStyle Hidden -PassThru `
                       -ArgumentList "/c docker compose up -d --build --force-recreate > `"$build`" 2>&1"
    [void]$p.Handle  # keeps the exit code readable
    while (-not $p.HasExited) { [void](Send-Status); Start-Sleep -Seconds 10 }
    Get-Content $build -ErrorAction SilentlyContinue | Out-File $log -Append -Encoding utf8
    Remove-Item $build -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { return "Rebuilding CleanCut didn't work. The details are in .cleancut-update.log in the CleanCut folder." }
    return $null  # updated
}

# Tells the page what's installed, what's new and how an update is going. The reply says whether
# CleanCut is busy and whether someone clicked "Update now".
function Send-Status {
    $body = @{ git = [bool]$info.git; current = "$($info.current)"; latest = "$($info.latest)"; news = @($info.news)
               checked = $(if ($info) { ([DateTimeOffset]$checked).ToUnixTimeSeconds() } else { 0 })
               status = $status; error = $problem } | ConvertTo-Json -Compress -Depth 4
    try {
        Invoke-RestMethod $url -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                          -ContentType "application/json; charset=utf-8" -TimeoutSec 10
    } catch { $null }  # CleanCut isn't running
}

while ($true) {
    $noon = (Get-Date).Date.AddHours(12)
    if (-not $info -or ((Get-Date) -ge $noon -and $checked -lt $noon)) {
        $new = Get-Info
        if ($new) { $info = $new; $checked = Get-Date }
    }

    $reply = Send-Status
    if ($reply -and $reply.busy) {
        [void][CleanCut.Power]::SetThreadExecutionState($CONTINUOUS -bor $SYSTEM_REQUIRED)
    } else {
        [void][CleanCut.Power]::SetThreadExecutionState($CONTINUOUS)
    }

    if ($reply -and $reply.update) {
        $status = "updating"; $problem = ""
        [void](Send-Status)
        $result = Update-CleanCut
        if ($null -eq $result) {
            # Hand over to the new version of this file.
            $mutex.ReleaseMutex()
            Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "$psArgs -Restarted"
            exit 0
        }
        $status = $(if ($result) { "error" } else { "" }); $problem = $result
        $new = Get-Info
        if ($new) { $info = $new; $checked = Get-Date }
    }
    Start-Sleep -Seconds 10
}
