param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
if ($Clean) {
    Remove-Item -LiteralPath ".\build-onefile", ".\dist-onefile" -Recurse -Force -ErrorAction SilentlyContinue
}
python -m PyInstaller --noconfirm --clean --windowed --onefile --name JiangtherapeeVideoEditor --distpath .\dist-onefile --workpath .\build-onefile .\controller\native_lut_console.py
Copy-Item -LiteralPath .\dist-onefile\JiangtherapeeVideoEditor.exe -Destination .\JiangtherapeeVideoEditor.exe -Force
