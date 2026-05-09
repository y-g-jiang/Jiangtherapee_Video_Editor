$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (!$python) {
  $python = Get-Command py -ErrorAction SilentlyContinue
}
if (!$python) {
  throw "Python was not found."
}

$script = Join-Path $PSScriptRoot "controller\native_lut_console.py"
& $python.Source $script
