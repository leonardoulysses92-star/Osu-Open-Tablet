<#
Publishes a new version and keeps the previous one apart.

  powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Version 0.9.0 -Notes "What changes, in one sentence"

What it does, in order (nothing is pushed):
  1. checks the working tree is clean, the version is new and the tests pass;
  2. raises the version in pyproject.toml, osu_tablet_assistant/__init__.py and both READMEs;
  3. adds the entry to CHANGELOG.md, commits and creates the tag vX.Y.Z (the old versions stay as tags);
  4. builds the .exe with build_exe.ps1 and starts it once to make sure it opens;
  5. moves the previous "Osu Open Tablet vX.exe" of the Desktop folder into "Versiones anteriores" and
     copies the new one next to LICENSE.
At the end it prints the git push command. -DryRun only shows what would be done.
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Notes,
    [string]$Dest = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Osu Open Tablet'),
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
Set-Location $root

$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) { $git = 'C:\Program Files\Git\cmd\git.exe' }
function Run-Git { & $git @args; if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed" } }
function Edit-File([string]$Path, [scriptblock]$Change) {
    $text = [IO.File]::ReadAllText($Path)
    [IO.File]::WriteAllText($Path, (& $Change $text), (New-Object Text.UTF8Encoding $false))
}

if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)$') { throw "Version must look like 0.9.0, not '$Version'." }
$phase = "$($Matches[1]).$($Matches[2]).x"
$tag = "v$Version"
if (& $git tag --list $tag) { throw "The tag $tag already exists." }
if (& $git status --porcelain --untracked-files=no) { throw 'The working tree has uncommitted changes: commit or stash them first.' }
if (-not (& $git config user.name) -or -not (& $git config user.email)) {
    throw 'Set your git identity first: git config user.name "..." and git config user.email "..."'
}
$archive = Join-Path $Dest 'Versiones anteriores'
$old = @(Get-ChildItem $Dest -Filter 'Osu Open Tablet v*.exe' -ErrorAction SilentlyContinue)

if ($DryRun) {
    "DRY RUN for $tag"
    "  tests, then version -> $Version (pyproject.toml, __init__.py, README.md, README.en.md as $phase)"
    "  CHANGELOG.md entry: $Notes"
    "  commit + tag $tag, build_exe.ps1, open the exe once"
    "  move to '$archive': " + $(if ($old) { ($old.Name -join ', ') } else { '(nothing there yet)' })
    "  copy the new exe to '$Dest\Osu Open Tablet v$Version.exe'"
    return
}

$env:OSU_TABLET_GUI_TESTS = '1'
& py -3 -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'The tests fail: nothing was changed.' }

Edit-File 'pyproject.toml' { param($t) $t -replace '(?m)^version = "[^"]*"', "version = `"$Version`"" }
Edit-File 'osu_tablet_assistant/__init__.py' { param($t) $t -replace '(?m)^__version__ = "[^"]*"', "__version__ = `"$Version`"" }
Edit-File 'README.md' { param($t) $t -replace 'fase temprana \(\d+\.\d+\.x\)', "fase temprana ($phase)" }
Edit-File 'README.en.md' { param($t) $t -replace 'Early stage \(\d+\.\d+\.x\)', "Early stage ($phase)" }
$marker = '<!-- versiones-nuevas-aqui -->'
$entry = "$marker`n`n## $Version ($(Get-Date -Format 'yyyy-MM-dd'))`n`n- $Notes"
Edit-File 'CHANGELOG.md' { param($t)
    if (-not $t.Contains($marker)) { throw "CHANGELOG.md has no '$marker' line." }
    $t.Replace($marker, $entry)
}
Run-Git add -A
Run-Git commit -m "Version $Version" -m $Notes
Run-Git tag -a $tag -m "Version $Version"

& powershell -ExecutionPolicy Bypass -File (Join-Path $root 'build_exe.ps1')
$built = Join-Path $root 'dist\Osu Open Tablet.exe'
if (-not (Test-Path $built)) { throw 'The exe was not built.' }
$probe = Start-Process $built -ArgumentList '--dry-run' -PassThru
Start-Sleep 8
$alive = -not $probe.HasExited
& taskkill /PID $probe.Id /T /F | Out-Null     # the one-file exe starts a child: close the whole tree
if (-not $alive) { throw 'The new exe closed by itself in the first 8 seconds.' }

New-Item -ItemType Directory -Force $Dest | Out-Null
if ($old) {
    New-Item -ItemType Directory -Force $archive | Out-Null
    $old | ForEach-Object { Move-Item $_.FullName $archive -Force }
}
Copy-Item $built (Join-Path $Dest "Osu Open Tablet v$Version.exe") -Force

$asset = Join-Path $root "dist\Osu-Open-Tablet-v$Version.exe"      # no spaces: this is the file to upload to GitHub
Copy-Item $built $asset -Force
"Done: $tag built and copied; the previous version is in '$archive'."
"To publish the code:  git push origin main $tag"
"To let anyone download it: create a GitHub release for $tag and upload '$asset'."
"SHA-256 for the release notes: $((Get-FileHash $asset).Hash)"
