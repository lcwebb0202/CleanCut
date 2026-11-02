# Gets the newest version of CleanCut from GitHub and puts it in this folder. Your settings (.env),
# speech models and any review in progress are kept.
#
#   "Update CleanCut" runs this with no options: update now if there's anything new.
#   "Start CleanCut" runs it with -Check: if there's a newer version, say so and ask.
#   -Record (first-time setup): note that this folder holds the newest version.
#   -Info (the CleanCut helper, for the page's "Update available" button): print what's installed,
#    what's newest and what's new, as JSON.
#
# Exit codes: 0 = nothing changed, 1 = something went wrong, 2 = updated (the caller restarts
# CleanCut, which rebuilds it). Only files that came from GitHub are ever replaced or removed.
param([switch]$Check, [switch]$Record, [switch]$Info, [switch]$NoPause)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # the progress bar makes downloads very slow in 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = "lcwebb0202/CleanCut"
$api = "https://api.github.com/repos/$repo"
$here = $PSScriptRoot
$versionFile = Join-Path $here ".cleancut-version"   # the GitHub commit this folder holds
$filesList = Join-Path $here ".cleancut-files.txt"   # the files that came from GitHub
$keep = @(".env", ".cleancut-version", ".cleancut-files.txt")

function Get-GitHub($url) {
    Invoke-RestMethod $url -TimeoutSec 15 -Headers @{ "User-Agent" = "CleanCut-updater" }
}

function Get-Installed {
    if (Test-Path $versionFile) { (Get-Content $versionFile -TotalCount 1).Trim() } else { "" }
}

function Get-Busy {
    try {
        $state = (Invoke-RestMethod "http://127.0.0.1:8800/api/state" -TimeoutSec 10).job.state
        if ($state -in @("waiting", "analyzing", "rendering")) { return $state }
    } catch { }  # CleanCut isn't running, so it isn't busy
    return $null
}

function Get-RepoFiles($sha) {
    $tree = Get-GitHub "$api/git/trees/${sha}?recursive=1"
    @($tree.tree | Where-Object { $_.type -eq "blob" } | ForEach-Object { $_.path })
}

# The changes between two versions, newest first: each commit's first line as the title and the
# rest (without the co-author line) as the description.
function Get-News($old, $new) {
    $commits = @()
    if ($old) {
        try { $commits = @((Get-GitHub "$api/compare/$old...$new").commits) } catch { }
        [array]::Reverse($commits)
    }
    if (-not $commits) { $commits = @(Get-GitHub "$api/commits?sha=$new&per_page=5") }
    @($commits | ForEach-Object {
        $lines = @(($_.commit.message -replace "`r", "") -split "`n")
        $body = ($lines[1..($lines.Count)] | Where-Object { $_ -notmatch "^Co-Authored-By:" }) -join "`n"
        $body = $body.Trim() -replace "(?<!
)
(?![
-])", " "  # re-join wrapped lines, keep paragraphs and lists
        [pscustomobject]@{ title = $lines[0].Trim(); text = $body }
    })
}

function Show-News($old, $new) {
    $news = @(Get-News $old $new)
    Write-Host ""
    Write-Host "What's new:"
    $news | Select-Object -First 15 | ForEach-Object { Write-Host ("  - " + $_.title) }
    if ($news.Count -gt 15) { Write-Host "  ...and $($news.Count - 15) more" }
    Write-Host ""
}

# Luke's own copy is kept up to date with Git, not with this.
if (Test-Path (Join-Path $here ".git")) {
    if ($Info) { Write-Output (@{ git = $true } | ConvertTo-Json -Compress); exit 0 }
    if (-not $Check -and -not $Record) {
        Write-Host "This copy of CleanCut is managed with Git. Update it with: git pull"
    }
    exit 0
}

try {
    $latest = (Get-GitHub "$api/commits/main").sha
} catch {
    if ($Check -or $Info) { exit 0 }  # no internet: just start as usual / try again later
    Write-Host "Couldn't reach GitHub to check for a new version. Check the internet connection and try again."
    exit 1
}
$installed = Get-Installed

if ($Record) {
    # A fresh download from GitHub is the newest version.
    Set-Content $versionFile $latest -Encoding ASCII
    try {
        $files = Get-RepoFiles $latest | Where-Object { Test-Path -LiteralPath (Join-Path $here $_) }
        Set-Content $filesList $files -Encoding UTF8
    } catch { }
    exit 0
}

if ($Info) {
    $news = @()
    if ($installed -and $installed -ne $latest) { $news = @(Get-News $installed $latest | Select-Object -First 30) }
    Write-Output (@{ git = $false; current = $installed; latest = $latest; news = $news } | ConvertTo-Json -Compress -Depth 4)
    exit 0
}

if ($installed -eq $latest) {
    if (-not $Check) { Write-Host "CleanCut is already up to date." }
    exit 0
}

if ($Check) {
    Write-Host ""
    Write-Host "A new version of CleanCut is available."
    if ($installed) { try { Show-News $installed $latest } catch { } }
    $busy = Get-Busy
    if ($busy) {
        Write-Host "CleanCut is busy right now ($busy), so it won't update yet."
        Write-Host "When it's done, double-click ""Update CleanCut""."
        Write-Host ""
        exit 0
    }
    $answer = Read-Host "Update now? (Y/N)"
    if ($answer -notmatch "^\s*y") { exit 0 }
} else {
    $busy = Get-Busy
    if ($busy) {
        Write-Host "CleanCut is busy right now ($busy). Try again once it has finished,"
        Write-Host "or cancel what it's doing first."
        exit 1
    }
}

Write-Host "Downloading the newest version..."
$temp = Join-Path ([IO.Path]::GetTempPath()) ("cleancut-update-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory $temp | Out-Null
    $zip = Join-Path $temp "cleancut.zip"
    Invoke-WebRequest "https://codeload.github.com/$repo/zip/$latest" -OutFile $zip -UseBasicParsing -TimeoutSec 300
    Expand-Archive $zip (Join-Path $temp "files")
    $root = (Get-ChildItem (Join-Path $temp "files") -Directory | Select-Object -First 1).FullName

    $new = @(Get-ChildItem $root -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($root.Length + 1).Replace("\", "/")
    } | Where-Object { $_ -notin $keep })
    if ($new.Count -lt 5 -or "update.ps1" -notin $new) { throw "the download looks incomplete" }

    foreach ($f in $new) {
        $dest = Join-Path $here $f
        New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
        Copy-Item -LiteralPath (Join-Path $root $f) $dest -Force
    }
    # Files that came from GitHub last time but are gone now.
    if (Test-Path $filesList) {
        foreach ($f in Get-Content $filesList) {
            if ($f -and $f -notin $new -and $f -notin $keep) {
                Remove-Item -LiteralPath (Join-Path $here $f) -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Set-Content $filesList $new -Encoding UTF8
    Set-Content $versionFile $latest -Encoding ASCII
} catch {
    Write-Host ""
    Write-Host "The update didn't work: $($_.Exception.Message)"
    Write-Host "Take a screenshot of this window and send it to Luke."
    exit 1
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Updated."
if (-not $Check) { try { Show-News $installed $latest } catch { } }
if (-not $NoPause) { [void](Read-Host "Press Enter to restart CleanCut with the new version") }
exit 2
