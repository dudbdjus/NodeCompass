param(
    [Parameter(Mandatory)] [string] $Compiler,
    [string] $PayloadDir,
    [string] $OutputDir
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (!$PayloadDir) { $PayloadDir = Join-Path $root 'build\packaging-preflight\dist' }
if (!$OutputDir) { $OutputDir = Join-Path $root 'dist\0.3.2-nodecompass-installer' }
$compilerPath = (Resolve-Path -LiteralPath $Compiler).ProviderPath
$payload = (Resolve-Path -LiteralPath $PayloadDir).ProviderPath
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$output = (Resolve-Path -LiteralPath $OutputDir).ProviderPath
$libraryNotice = Get-ChildItem -Path (Join-Path $root '.venv\Lib\site-packages\ip2location-*.dist-info\licenses\LICENSE.TXT') -File
if (@($libraryNotice).Count -ne 1) { throw 'Install the project dependencies into .venv before building the installer.' }
Copy-Item -LiteralPath $libraryNotice.FullName -Destination (Join-Path $payload 'IP2Location-Python-LICENSE.txt')


& $compilerPath "/DPayloadDir=$payload" "/O$output" (Join-Path $PSScriptRoot 'NodeCompass.iss')
if ($LASTEXITCODE -ne 0) { throw "Installer build failed (Inno Setup returned $LASTEXITCODE)." }
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $output 'NodeCompass-0.3.2-Setup-win-x64.exe')
