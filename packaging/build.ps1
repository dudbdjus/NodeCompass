param([string]$Python, [switch]$Release)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $root '.venv\Scripts\python.exe'
}
$spec = Join-Path $PSScriptRoot 'ProxyManager.spec'
$stage = Join-Path $root $(if ($Release) { 'build\packaging-release-0.3.2' } else { 'build\packaging-preflight' })
$dist = Join-Path $stage 'dist'
$work = Join-Path $stage 'work'

& $Python -c "import struct, sys; sys.exit(not (sys.platform == 'win32' and sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8))"
if ($LASTEXITCODE -ne 0) {
    throw 'Use Windows x64 CPython 3.12 to build NodeCompass.'
}
if ($Release) {
    $lock = Join-Path $root 'requirements-release.lock'
    & $Python -c "import sys; from pathlib import Path; from importlib.metadata import version; pins = [line.split('==',1) for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines() if line and not line.startswith('#')]; sys.exit(sys.version_info[:3] != (3,12,16) or any(version(name) != value for name,value in pins))" $lock
    if ($LASTEXITCODE -ne 0) {
        throw 'Release interpreter or dependencies differ from the reviewed lock.'
    }
}
$originalPath = $env:PATH
$originalPythonPath = $env:PYTHONPATH
try {
    # Native DLL discovery must not use runtimes from unrelated desktop apps.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYTHONPATH = $null
    & $Python -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    $env:PATH = $originalPath
    $env:PYTHONPATH = $originalPythonPath
}
Get-Item -LiteralPath (Join-Path $dist 'NodeCompass.exe')
