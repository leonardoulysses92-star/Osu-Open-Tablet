<#
Publishes a new version and keeps the previous one apart.

  powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Change small|medium|large -Notes "What changes, in one sentence"

The version number follows the size of the change (written 0.G.M.S, shown without the leading 0, e.g. 0.8.0.1 is "8.0.1"):
  small  -> the last number goes up      0.8.0   -> 0.8.0.1   ("8.0.1")  fixes and tiny tweaks
  medium -> the third number goes up     0.8.0.1 -> 0.8.1     ("8.1")    a feature or a noticeable improvement
  large  -> the second number goes up    0.8.1   -> 0.9.0     ("9")      a big change
-Version X.Y.Z[.W] sets the number by hand instead of -Change.

What it does, in order (nothing is pushed):
  1. checks the working tree is clean, the version is new and the tests pass;
  2. raises the version in pyproject.toml, osu_tablet_assistant/__init__.py and both READMEs;
  3. adds the entry to CHANGELOG.md, commits and creates the tag vX.Y.Z (the old versions stay as tags);
  4. builds the .exe with tools\build_exe.ps1 and starts it once to make sure it opens;
  5. moves the previous "Osu-Open-Tablet-vX.exe" of the destination folder (-Dest, default "Osu Open Tablet"
     on the Desktop) into "Versiones anteriores" and copies the new one there.
At the end it prints the git push command. -DryRun only shows what would be done.
Versions 1.0 and above are refused: the project stays below 1.0 until the owner decides otherwise.
#>
param(
    [string]$Version,
    [ValidateSet('small', 'medium', 'large')][string]$Change,
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

$versionPattern = '^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$'
if ($Version -and $Change) { throw 'Use -Version or -Change, not both.' }
if (-not $Version) {
    if (-not $Change) { throw 'Say how big the change is (-Change small|medium|large) or give -Version.' }
    $current = ([IO.File]::ReadAllText((Join-Path $root 'osu_tablet_assistant/__init__.py')) -replace '(?s).*__version__ = "([^"]+)".*', '$1')
    if ($current -notmatch $versionPattern) { throw "Cannot read the current version ('$current')." }
    $g = [int]$Matches[2]; $m = [int]$Matches[3]; $s = if ($Matches[4]) { [int]$Matches[4] } else { 0 }
    $Version = switch ($Change) {
        'large'  { "0.$($g + 1).0" }
        'medium' { "0.$g.$($m + 1)" }
        'small'  { "0.$g.$m.$($s + 1)" }
    }
}
if ($Version -notmatch $versionPattern) { throw "Version must look like 0.9.0 or 0.8.0.1, not '$Version'." }
if ([int]$Matches[1] -ge 1) { throw 'Version 1.0 and above is not allowed yet: the project stays below 1.0 until the owner decides otherwise.' }
$phase = "$($Matches[1]).$($Matches[2]).x"
$tag = "v$Version"
if (& $git tag --list $tag) { throw "The tag $tag already exists." }
if (& $git status --porcelain --untracked-files=no) { throw 'The working tree has uncommitted changes: commit or stash them first.' }
if (-not (& $git config user.name) -or -not (& $git config user.email)) {
    throw 'Set your git identity first: git config user.name "..." and git config user.email "..."'
}
$archive = Join-Path $Dest 'Versiones anteriores'
$old = @(Get-ChildItem $Dest -Filter '*.exe' -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^Osu[- ]Open[- ]Tablet[- ]v\d' })

if ($DryRun) {
    "DRY RUN for $tag"
    "  tests, then version -> $Version (pyproject.toml, __init__.py, README.md, README.en.md as $phase)"
    "  CHANGELOG.md entry: $Notes"
    "  commit + tag $tag, tools\build_exe.ps1, open the exe once"
    "  move to '$archive': " + $(if ($old) { ($old.Name -join ', ') } else { '(nothing there yet)' })
    "  copy the new exe to '$Dest\Osu-Open-Tablet-v$Version.exe' (no spaces: this is also the file to upload to GitHub)"
    return
}

$env:OSU_TABLET_GUI_TESTS = '1'
& py -3 -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'The tests fail: nothing was changed.' }

Edit-File 'pyproject.toml' { param($t) $t -replace '(?m)^version = "[^"]*"', "version = `"$Version`"" }
Edit-File 'osu_tablet_assistant/__init__.py' { param($t) $t -replace '(?m)^__version__ = "[^"]*"', "__version__ = `"$Version`"" }
Edit-File 'README.md' { param($t) $t -replace 'de pruebas \(\d+\.\d+\.x\)', "de pruebas ($phase)" }
Edit-File 'README.en.md' { param($t) $t -replace 'test version \(\d+\.\d+\.x\)', "test version ($phase)" }
$marker = '<!-- versiones-nuevas-aqui -->'
$entry = "$marker`n`n## $Version ($(Get-Date -Format 'yyyy-MM-dd'))`n`n- $Notes"
Edit-File 'CHANGELOG.md' { param($t)
    if (-not $t.Contains($marker)) { throw "CHANGELOG.md has no '$marker' line." }
    $t.Replace($marker, $entry)
}
Run-Git add -A
Run-Git commit -m "Version $Version" -m $Notes
Run-Git tag -a $tag -m "Version $Version"

& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build_exe.ps1')
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
$asset = Join-Path $Dest "Osu-Open-Tablet-v$Version.exe"      # no spaces: also the file to upload to GitHub
Copy-Item $built $asset -Force

"Done: $tag built and copied; the previous version is in '$archive'."
"To publish the code:  git push origin main $tag"
"To let anyone download it: create a GitHub release for $tag and upload '$asset'."
"SHA-256 for the release notes: $((Get-FileHash $asset).Hash)"
