<#
Builds "Osu Open Tablet.exe" (one file, no console window) with PyInstaller.

  powershell -ExecutionPolicy Bypass -File build_exe.ps1

Needs Python 3.10+ with tkinter and PyInstaller (py -3 -m pip install pyinstaller).
The result is written to dist\Osu Open Tablet.exe.

Why every module and data file is listed by hand: the program loads some of its
modules on demand (the pen tests, the recommendation engine, the OTD controller),
and PyInstaller cannot see those imports. Without --hidden-import the .exe starts
but fails later with "No module named ...". The lists below are generated from the
package, so a new module or data file is picked up without editing this script.
#>
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$package = 'osu_tablet_assistant'
# A short path keeps PyInstaller clear of Windows' 260-character path limit.
$build = Join-Path $env:TEMP 'osu-open-tablet-build'

& py -3 -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is not installed. Run:  py -3 -m pip install pyinstaller'
}

if (Test-Path $build) { Remove-Item $build -Recurse -Force }
$src = Join-Path $build 'src'
New-Item -ItemType Directory $src | Out-Null
Copy-Item (Join-Path $root $package) (Join-Path $src $package) -Recurse
Get-ChildItem $src -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

Set-Content (Join-Path $build 'launcher.py') -Encoding utf8 -Value @"
import sys
from $package.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
"@

$modules = & py -3 -c "import pkgutil, sys; sys.path.insert(0, sys.argv[1]); import $package as p; print(' '.join(m.name for m in pkgutil.walk_packages(p.__path__, '$package.')))" $src
$options = @('--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'Osu Open Tablet', '--paths', 'src',
             '--distpath', 'dist', '--workpath', 'work', '--specpath', 'spec')
foreach ($module in ($modules -split ' ' | Where-Object { $_ })) { $options += @('--hidden-import', $module) }

$packageDir = Join-Path $src $package
foreach ($file in (Get-ChildItem $packageDir -Recurse -File | Where-Object { $_.Extension -ne '.py' })) {
    $target = $file.DirectoryName.Substring($src.Length + 1)
    $options += @('--add-data', "$($file.FullName);$target")
}

Push-Location $build
try {
    & py -3 -m PyInstaller @options launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
} finally {
    Pop-Location
}

$out = Join-Path $root 'dist'
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $build 'dist\Osu Open Tablet.exe') $out -Force
Write-Host "Built: $(Join-Path $out 'Osu Open Tablet.exe')"
