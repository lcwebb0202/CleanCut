# Keeps Windows from going to sleep while CleanCut is waiting for its start time or working on a
# movie, and lets it sleep normally the rest of the time. The screen can still turn off.
#
# "Start CleanCut" runs this with -Install, which adds it to the Windows Startup folder (so it runs
# hidden after every sign-in) and starts it now. Closing a laptop's lid still puts it to sleep.
param([switch]$Install)

$name = "CleanCut keep-awake"

if ($Install) {
    $startup = [Environment]::GetFolderPath("Startup")
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $startup "$name.lnk"))
    $link.TargetPath = "powershell.exe"
    $link.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $link.WindowStyle = 7  # minimized, in case the hidden style is ignored
    $link.Description = "Keeps the PC awake while CleanCut is busy"
    $link.Save()  # re-written every time, so it follows the folder if it was moved
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList $link.Arguments
    exit 0
}

# Only one copy at a time.
$mutex = New-Object System.Threading.Mutex($false, "Local\CleanCutKeepAwake")
if (-not $mutex.WaitOne(0)) { exit 0 }

Add-Type -Namespace CleanCut -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
'@
$CONTINUOUS = [uint32]"0x80000000"
$SYSTEM_REQUIRED = [uint32]"0x00000001"

while ($true) {
    $busy = $false
    try {
        $state = (Invoke-RestMethod "http://127.0.0.1:8800/api/state" -TimeoutSec 10).job.state
        $busy = $state -in @("waiting", "analyzing", "rendering")
    } catch { }  # CleanCut isn't running: nothing to keep awake for
    if ($busy) {
        [void][CleanCut.Power]::SetThreadExecutionState($CONTINUOUS -bor $SYSTEM_REQUIRED)
    } else {
        [void][CleanCut.Power]::SetThreadExecutionState($CONTINUOUS)
    }
    Start-Sleep -Seconds 30
}
