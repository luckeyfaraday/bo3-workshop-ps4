# Builds the patched converter (ffport.exe) from ItsJokerZz/PS4-BO3-Customs v1.50 + this project's fixes.
# Needs git, Python 3.10+ and the .NET 10 SDK (https://dotnet.microsoft.com/download/dotnet/10.0).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-ffport.ps1
#
# Result: ffport\ffport.exe (the path config.json uses by default).
param(
    [string]$Source = "$PSScriptRoot\..\upstream\src",
    [string]$Out = "$PSScriptRoot\..\ffport",
    [string]$Tag = "v1.50"
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

if (-not (Test-Path "$Source\.git")) {
    git clone --quiet https://github.com/ItsJokerZz/PS4-BO3-Customs $Source
}
git -C $Source fetch --quiet --tags
git -C $Source checkout --quiet --force $Tag
git -C $Source clean -fdq

git -C $Source apply --whitespace=nowarn "$root\patches\ffporter-fixes.diff"
python "$root\patches\apply-data-fixes.py" "$Source\Tool\src\FFPorter.Core.T7\Data\Shipped\t7_gsc"

dotnet publish "$Source\Tool\src\FFPorter.Cli\FFPorter.Cli.csproj" -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:NuGetAudit=false `
    -p:DebugType=none -p:DebugSymbols=false -o $Out
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }
Write-Host "built $Out\ffport.exe"
