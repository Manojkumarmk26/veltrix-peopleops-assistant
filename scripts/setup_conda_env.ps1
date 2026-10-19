<#
.SYNOPSIS
  Create (or update) the project Conda environment inside the project folder.

.DESCRIPTION
  Everything stays inside the project directory (e.g. on D:):
    .\conda_env     the environment itself (conda create --prefix)
    .\.conda_pkgs   conda package cache   (CONDA_PKGS_DIRS)
    .\.pip_cache    pip download cache     (PIP_CACHE_DIR)
    .\.hf_cache     Hugging Face models    (HF_HOME)
  The variables are also stored in the environment, so `conda activate .\conda_env`
  sets them automatically.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup_conda_env.ps1
#>
param(
    [string]$Conda = "conda"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvDir = Join-Path $Root "conda_env"

if (-not (Get-Command $Conda -ErrorAction SilentlyContinue)) {
    $candidate = "C:\ProgramData\anaconda3\Scripts\conda.exe"
    if (Test-Path $candidate) { $Conda = $candidate } else { throw "conda not found; pass -Conda <path to conda.exe>" }
}

$env:CONDA_PKGS_DIRS = Join-Path $Root ".conda_pkgs"
$env:PIP_CACHE_DIR = Join-Path $Root ".pip_cache"
$env:HF_HOME = Join-Path $Root ".hf_cache"

if (-not (Test-Path (Join-Path $EnvDir "python.exe"))) {
    & $Conda create --yes --prefix $EnvDir python=3.11 pip
}
& (Join-Path $EnvDir "python.exe") -m pip install -r (Join-Path $Root "requirements.txt")

& $Conda env config vars set --prefix $EnvDir `
    "CONDA_PKGS_DIRS=$env:CONDA_PKGS_DIRS" "PIP_CACHE_DIR=$env:PIP_CACHE_DIR" "HF_HOME=$env:HF_HOME"

# Pre-download the embedding model into .hf_cache
& (Join-Path $EnvDir "python.exe") -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2', device='cpu')"

Write-Host "Environment ready: $EnvDir"
Write-Host "Run tests:  $EnvDir\python.exe -m pytest"
Write-Host "Run API:    $EnvDir\python.exe -m uvicorn app.main:app --port 8000"
