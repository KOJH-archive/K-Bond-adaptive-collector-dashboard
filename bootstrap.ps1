param([switch]$CheckOnly, [switch]$NoBrowser, [switch]$NoCollect, [int]$Seconds = 0)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

function Test-Python([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        & $Path -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

try {
    $configPath = Join-Path $PSScriptRoot '.venv\pyvenv.cfg'
    $completeEnvironment = (Test-Path -LiteralPath $configPath) -and ((Get-Content -LiteralPath $configPath -Raw) -match 'include-system-site-packages = true')
    if (-not (Test-Python $projectPython) -or -not $completeEnvironment) {
        $candidates = @(
            (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
        )
        foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
            $command = Get-Command $name -ErrorAction SilentlyContinue
            if ($command) { $candidates += $command.Source }
        }
        $installed = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $installed) {
            $candidates += Get-ChildItem -LiteralPath $installed -Filter python.exe -Recurse | Select-Object -ExpandProperty FullName
        }
        $basePython = $null
        foreach ($candidate in $candidates | Select-Object -Unique) {
            if (Test-Python $candidate) { $basePython = $candidate; break }
        }
        if (-not $basePython) { throw 'Python 3.10+ was not found. Install Python and run this file again.' }
        Write-Host 'Preparing the private V3 Python environment (first run only)...'
        & $basePython -m venv --system-site-packages (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare the V3 environment.' }
    }
    & $projectPython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('flask') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing the V3 dependency (internet needed on first run)...'
        & $projectPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the internet connection and retry.' }
    }
    if ($CheckOnly) {
        & $projectPython -c 'import flask,sqlite3,sys; print(sys.version)'
        exit $LASTEXITCODE
    }
    $appArgs = @((Join-Path $PSScriptRoot 'app.py'))
    if ($NoBrowser) { $appArgs += '--no-browser' }
    if ($NoCollect) { $appArgs += '--no-collect' }
    if ($Seconds -gt 0) { $appArgs += @('--seconds', $Seconds.ToString()) }
    & $projectPython @appArgs
    exit $LASTEXITCODE
} catch {
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'The previous K-Bond version has not been modified.'
    exit 1
}
