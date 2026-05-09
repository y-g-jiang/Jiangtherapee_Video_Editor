param(
    [double]$Exposure = 0.0,
    [string]$InputPath = "$HOME\Downloads\PN921798.MOV",
    [string]$OutputPath = "",
    [string]$CurvePath = "",
    [string]$AudioCurvePath = "",
    [double]$CurveStep = 0.25,
    [ValidateSet(
        "NVENC",
        "Archival",
        "HEVC NVENC Fast",
        "HEVC NVENC Balanced",
        "HEVC NVENC HQ",
        "HEVC x265 Compact",
        "HEVC x265 Master",
        "HEVC x265 Lossless",
        "AV1 NVENC Small",
        "AV1 NVENC HQ"
    )]
    [string]$Quality = "HEVC NVENC HQ",
    [switch]$Preview2s
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exports = Join-Path $root "exports"
New-Item -ItemType Directory -Path $exports -Force | Out-Null

function Resolve-Quality([string]$Value) {
    switch ($Value) {
        "NVENC" { return "HEVC NVENC HQ" }
        "Archival" { return "HEVC x265 Lossless" }
        default { return $Value }
    }
}

$Quality = Resolve-Quality $Quality

if (-not $OutputPath) {
    if ($CurvePath) {
        $suffix = "EVcurve_$($Quality.Replace(' ', '-'))"
    } else {
        $sign = if ($Exposure -ge 0) { "+" } else { "" }
        $suffix = "EV$sign$($Exposure.ToString('0.###').Replace('.', 'p'))_$($Quality.Replace(' ', '-'))"
    }
    if ($Preview2s) { $suffix += "_2s" }
    $stem = [IO.Path]::GetFileNameWithoutExtension($InputPath)
    $extension = if ($Quality.StartsWith("AV1")) { ".mp4" } else { ".MOV" }
    $OutputPath = Join-Path $exports "$stem`_$suffix$extension"
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$outputDir = Split-Path -Parent $OutputPath
if ($outputDir) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source

function Format-Number([double]$Value) {
    return $Value.ToString("0.############", [Globalization.CultureInfo]::InvariantCulture)
}

function Format-Bytes([double]$Bytes) {
    if ($Bytes -ge 1GB) {
        return "$(([math]::Round($Bytes / 1GB, 1)).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)) GB"
    }
    return "$(([math]::Round($Bytes / 1MB, 0)).ToString('0', [Globalization.CultureInfo]::InvariantCulture)) MB"
}

function Get-EstimatedBytesPerSecond([string]$Quality) {
    switch ($Quality) {
        "HEVC x265 Lossless" { return 225MB }
        "HEVC x265 Master" { return 40MB }
        "HEVC x265 Compact" { return 18MB }
        "AV1 NVENC HQ" { return 22MB }
        "AV1 NVENC Small" { return 14MB }
        "HEVC NVENC Fast" { return 22MB }
        "HEVC NVENC Balanced" { return 28MB }
        default { return 34MB }
    }
}

function Get-EstimatedOutputBytes([double]$Duration, [string]$Quality, [bool]$Preview) {
    $seconds = if ($Preview) { [math]::Min(2.0, $Duration) } else { $Duration }
    return $seconds * (Get-EstimatedBytesPerSecond $Quality)
}

function Get-MediaDuration([string]$Path) {
    try {
        $text = (& $ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $Path | Select-Object -First 1).Trim()
        if ($text) {
            return [double]::Parse($text, [Globalization.CultureInfo]::InvariantCulture)
        }
    } catch {
    }
    return 174.72
}

function Assert-FreeSpace([string]$Path, [double]$Duration, [string]$Quality, [bool]$Preview) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $driveRoot = [IO.Path]::GetPathRoot($fullPath)
    if (-not $driveRoot) { return }

    $drive = [System.IO.DriveInfo]::new($driveRoot)
    $estimate = Get-EstimatedOutputBytes $Duration $Quality $Preview
    switch ($Quality) {
        "HEVC x265 Lossless" { $floor = 8GB }
        "HEVC x265 Master" { $floor = 4GB }
        "HEVC x265 Compact" { $floor = 3GB }
        "AV1 NVENC HQ" { $floor = 3GB }
        "AV1 NVENC Small" { $floor = 2GB }
        "HEVC NVENC Fast" { $floor = 3GB }
        default { $floor = 4GB }
    }
    $minimum = if ($Preview) { [math]::Max(512MB, $estimate * 1.5) } else { [math]::Max($floor, $estimate * 1.25) }

    if ($drive.AvailableFreeSpace -lt $minimum) {
        throw "Not enough free space on $driveRoot. Need about $(Format-Bytes $minimum) for this $Quality export; available $(Format-Bytes $drive.AvailableFreeSpace)."
    }
    Write-Host "Estimated output: $(Format-Bytes $estimate)"
    Write-Host "Free space: $(Format-Bytes $drive.AvailableFreeSpace) on $driveRoot; estimated need $(Format-Bytes $minimum)"
}

function Invoke-NativeChecked([string]$Exe, [object[]]$NativeArgs) {
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @NativeArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                [Console]::Out.WriteLine($_.Exception.Message)
            } else {
                [Console]::Out.WriteLine($_.ToString())
            }
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
}

function Get-AudioStreamCount([string]$Path) {
    try {
        $json = & $ffprobe -v error -select_streams a -show_entries stream=index -of json $Path | ConvertFrom-Json
        return @($json.streams).Count
    } catch {
        return 0
    }
}

function Build-EvExpression([object[]]$Points, [double]$FallbackExposure) {
    if (-not $Points -or $Points.Count -eq 0) {
        return Format-Number $FallbackExposure
    }

    $sorted = @($Points | Sort-Object -Property time)
    $expr = Format-Number ([double]$sorted[$sorted.Count - 1].ev)

    for ($i = $sorted.Count - 2; $i -ge 0; $i--) {
        $a = $sorted[$i]
        $b = $sorted[$i + 1]
        $t0 = [double]$a.time
        $t1 = [double]$b.time
        $e0 = [double]$a.ev
        $e1 = [double]$b.ev
        if ($t1 -le $t0) { continue }

        $u = "clip((T-$(Format-Number $t0))/$(Format-Number ($t1 - $t0)),0,1)"
        $s = "($u*$u*(3-2*$u))"
        $segment = "($(Format-Number $e0)+(($(Format-Number $e1)-$(Format-Number $e0))*$s))"
        $expr = "if(lt(T,$(Format-Number $t1)),$segment,$expr)"
    }

    $first = $sorted[0]
    return "if(lt(T,$(Format-Number ([double]$first.time))),$(Format-Number ([double]$first.ev)),$expr)"
}

function Build-DbExpression([object[]]$Points, [double]$FallbackDb) {
    if (-not $Points -or $Points.Count -eq 0) {
        return Format-Number $FallbackDb
    }

    $sorted = @($Points | Sort-Object -Property time)
    $expr = Format-Number ([double]$sorted[$sorted.Count - 1].db)

    for ($i = $sorted.Count - 2; $i -ge 0; $i--) {
        $a = $sorted[$i]
        $b = $sorted[$i + 1]
        $t0 = [double]$a.time
        $t1 = [double]$b.time
        $d0 = [double]$a.db
        $d1 = [double]$b.db
        if ($t1 -le $t0) { continue }

        $u = "clip((t-$(Format-Number $t0))/$(Format-Number ($t1 - $t0)),0,1)"
        $s = "($u*$u*(3-2*$u))"
        $segment = "($(Format-Number $d0)+(($(Format-Number $d1)-$(Format-Number $d0))*$s))"
        $expr = "if(lt(t,$(Format-Number $t1)),$segment,$expr)"
    }

    $first = $sorted[0]
    return "if(lt(t,$(Format-Number ([double]$first.time))),$(Format-Number ([double]$first.db)),$expr)"
}

function Test-DbCurveTrivial([object[]]$Points) {
    if (-not $Points -or $Points.Count -eq 0) { return $true }
    foreach ($point in $Points) {
        if ([math]::Abs([double]$point.db) -gt 0.0005) { return $false }
    }
    return $true
}

function Get-AudioTrackPoints([object]$AudioCurve, [int]$TrackIndex) {
    if (-not $AudioCurve) {
        return @()
    }

    if ($AudioCurve.PSObject.Properties.Name -contains "tracks") {
        $tracks = @($AudioCurve.tracks)
        if ($TrackIndex -lt $tracks.Count) {
            $track = $tracks[$TrackIndex]
            if ($track.PSObject.Properties.Name -contains "points") {
                return @($track.points)
            }
            return @($track)
        }
        return @()
    }

    if ($TrackIndex -eq 0 -and ($AudioCurve.PSObject.Properties.Name -contains "points")) {
        return @($AudioCurve.points)
    }

    return @()
}

function Get-CurveEvAt([object[]]$Points, [double]$Seconds, [double]$FallbackExposure) {
    if (-not $Points -or $Points.Count -eq 0) { return $FallbackExposure }
    $sorted = @($Points | Sort-Object -Property time)
    if ($Seconds -le [double]$sorted[0].time) { return [double]$sorted[0].ev }
    for ($i = 0; $i -lt $sorted.Count - 1; $i++) {
        $a = $sorted[$i]
        $b = $sorted[$i + 1]
        $t0 = [double]$a.time
        $t1 = [double]$b.time
        if ($Seconds -le $t1) {
            $span = [math]::Max(0.001, $t1 - $t0)
            $u = [math]::Max(0.0, [math]::Min(1.0, ($Seconds - $t0) / $span))
            $s = $u * $u * (3.0 - 2.0 * $u)
            return [double]$a.ev + ([double]$b.ev - [double]$a.ev) * $s
        }
    }
    return [double]$sorted[$sorted.Count - 1].ev
}

function Build-LutExpression([double]$Ev) {
    $gain = [math]::Pow(2.0, $Ev).ToString("0.############", [Globalization.CultureInfo]::InvariantCulture)
    $v = "(val/maxval)"
    $lin = "if(lt($v,0.181),(($v-0.125)/5.6),(pow(10,(($v-0.598206)/0.241514))-0.00873))"
    $lin2 = "($lin*$gain)"
    $encoded = "if(lt($lin2,0.01),(5.6*$lin2+0.125),(0.241514*(log($lin2+0.00873)/log(10))+0.598206))"
    return "clip(($encoded)*maxval,0,maxval)"
}

function Build-SendcmdFile([object[]]$Points, [double]$FallbackExposure, [double]$Duration, [double]$Step, [string]$OutputPath) {
    $path = [IO.Path]::ChangeExtension($OutputPath, ".sendcmd.txt")
    $lines = New-Object System.Collections.Generic.List[string]
    $count = [math]::Ceiling($Duration / [math]::Max(0.02, $Step))
    $lastExpr = ""
    for ($i = 0; $i -le $count; $i++) {
        $t = $i * $Step
        $ev = Get-CurveEvAt $Points $t $FallbackExposure
        $expr = Build-LutExpression $ev
        if ($expr -eq $lastExpr) { continue }
        $timeText = Format-Number $t
        $lines.Add("$timeText lut r '$expr';")
        $lines.Add("$timeText lut g '$expr';")
        $lines.Add("$timeText lut b '$expr';")
        $lastExpr = $expr
    }
    Set-Content -LiteralPath $path -Value $lines -Encoding ASCII
    return $path
}

function Get-FilterPath([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $cwd = [IO.Path]::GetFullPath((Get-Location).Path)
    if (-not $cwd.EndsWith([IO.Path]::DirectorySeparatorChar)) {
        $cwd += [IO.Path]::DirectorySeparatorChar
    }
    if ($fullPath.StartsWith($cwd, [StringComparison]::OrdinalIgnoreCase)) {
        return $fullPath.Substring($cwd.Length).Replace("\", "/")
    }
    return $fullPath.Replace("\", "/").Replace(":", "\:")
}

if ($CurvePath) {
    $curve = Get-Content -LiteralPath $CurvePath -Raw | ConvertFrom-Json
    $points = @($curve.points)
    $evExpr = Build-EvExpression $points $Exposure
    $durationForCurve = if ($curve.duration) { [double]$curve.duration } else { 174.72 }
    $curveCopy = [IO.Path]::ChangeExtension($OutputPath, ".curve.json")
    Copy-Item -LiteralPath $CurvePath -Destination $curveCopy -Force
} else {
    $evExpr = Format-Number $Exposure
}

$durationForSpace = if ($CurvePath) { $durationForCurve } else { Get-MediaDuration $InputPath }
Assert-FreeSpace $OutputPath $durationForSpace $Quality ([bool]$Preview2s)
$av1Export = $Quality.StartsWith("AV1")

$audioFilters = @{}
$audioCurveActive = $false
if ($AudioCurvePath -and (Test-Path -LiteralPath $AudioCurvePath)) {
    $audioCurve = Get-Content -LiteralPath $AudioCurvePath -Raw | ConvertFrom-Json
    $audioStreamCountForCurves = Get-AudioStreamCount $InputPath
    for ($audioIndex = 0; $audioIndex -lt $audioStreamCountForCurves; $audioIndex++) {
        $audioPoints = @(Get-AudioTrackPoints $audioCurve $audioIndex)
        if (-not (Test-DbCurveTrivial $audioPoints)) {
            $dbExpr = Build-DbExpression $audioPoints 0.0
            $audioFilters[$audioIndex] = "volume='pow(10,($dbExpr)/20)':eval=frame"
            $audioCurveActive = $true
        }
    }
    if ($audioCurveActive) {
        $audioCurveCopy = [IO.Path]::ChangeExtension($OutputPath, ".audio-curve.json")
        Copy-Item -LiteralPath $AudioCurvePath -Destination $audioCurveCopy -Force
    }
}

if ($CurvePath) {
    $initialEv = Get-CurveEvAt $points 0.0 $Exposure
    $expr = Build-LutExpression $initialEv
    $sendcmdPath = Build-SendcmdFile $points $Exposure $durationForCurve $CurveStep $OutputPath
    $sendcmdEscaped = Get-FilterPath $sendcmdPath
    $curveFilter = "sendcmd=f='$sendcmdEscaped',lutrgb@lut=r='$expr':g='$expr':b='$expr'"
} else {
    $expr = Build-LutExpression $Exposure
    $curveFilter = "lutrgb=r='$expr':g='$expr':b='$expr'"
}

$filter = @(
    "zscale=rin=full:r=full:min=bt709:m=gbr:tin=bt709:t=bt709:pin=bt709:p=bt709",
    "format=gbrp16le",
    $curveFilter,
    "zscale=rin=full:r=full:min=gbr:m=bt709:tin=bt709:t=bt709:pin=bt709:p=bt709",
    "format=p010le"
) -join ","

$common = @(
    "-y",
    "-hide_banner",
    "-nostdin"
)
if ($Preview2s) {
    $common += @("-t", "2")
}
$common += @(
    "-i", $InputPath,
    "-map_metadata", "0",
    "-filter:v:0", $filter,
    "-color_range:v:0", "pc",
    "-colorspace:v:0", "bt709",
    "-color_trc:v:0", "bt709",
    "-color_primaries:v:0", "bt709"
)

if ($av1Export) {
    $common += @(
        "-map", "0:v:0",
        "-map", "0:a?",
        "-movflags", "use_metadata_tags"
    )
} else {
    $common += @(
        "-map", "0",
        "-map_chapters", "0",
        "-copy_unknown",
        "-c:d", "copy",
        "-c:s", "copy",
        "-tag:v:0", "hvc1",
        "-movflags", "use_metadata_tags",
        "-brand", "qt  "
    )
}

if ($audioCurveActive) {
    $audioStreamCount = Get-AudioStreamCount $InputPath
    for ($audioIndex = 0; $audioIndex -lt $audioStreamCount; $audioIndex++) {
        if ($audioFilters.ContainsKey($audioIndex)) {
            $common += @(
                "-filter:a:$audioIndex", $audioFilters[$audioIndex],
                "-c:a:$audioIndex", "pcm_s24le"
            )
        } elseif ($av1Export) {
            $common += @("-c:a:$audioIndex", "pcm_s24be")
        } else {
            $common += @("-c:a:$audioIndex", "copy")
        }
    }
}

switch ($Quality) {
    "HEVC x265 Lossless" {
        $common += @(
            "-c:v:0", "libx265",
            "-preset:v:0", "slow",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "yuv420p10le",
            "-x265-params", "lossless=1:repeat-headers=1:aud=1"
        )
    }
    "HEVC x265 Master" {
        $common += @(
            "-c:v:0", "libx265",
            "-preset:v:0", "slow",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "yuv420p10le",
            "-crf:v:0", "14",
            "-x265-params", "repeat-headers=1:aq-mode=3:aq-strength=0.9:deblock=-1,-1:psy-rd=1.4:psy-rdoq=2.0"
        )
    }
    "HEVC x265 Compact" {
        $common += @(
            "-c:v:0", "libx265",
            "-preset:v:0", "slow",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "yuv420p10le",
            "-crf:v:0", "20",
            "-x265-params", "repeat-headers=1:aq-mode=3:aq-strength=0.9:deblock=-1,-1"
        )
    }
    "AV1 NVENC Small" {
        $common += @(
            "-c:v:0", "av1_nvenc",
            "-tag:v:0", "av01",
            "-pix_fmt:v:0", "p010le",
            "-preset:v:0", "p7",
            "-tune:v:0", "uhq",
            "-rc:v:0", "vbr",
            "-cq:v:0", "24",
            "-b:v:0", "0",
            "-multipass:v:0", "fullres",
            "-spatial-aq:v:0", "1",
            "-temporal-aq:v:0", "1"
        )
    }
    "AV1 NVENC HQ" {
        $common += @(
            "-c:v:0", "av1_nvenc",
            "-tag:v:0", "av01",
            "-pix_fmt:v:0", "p010le",
            "-preset:v:0", "p7",
            "-tune:v:0", "uhq",
            "-rc:v:0", "vbr",
            "-cq:v:0", "16",
            "-b:v:0", "0",
            "-multipass:v:0", "fullres",
            "-spatial-aq:v:0", "1",
            "-temporal-aq:v:0", "1"
        )
    }
    "HEVC NVENC Fast" {
        $common += @(
            "-c:v:0", "hevc_nvenc",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "p010le",
            "-preset:v:0", "p4",
            "-tune:v:0", "hq",
            "-rc:v:0", "vbr",
            "-cq:v:0", "18",
            "-b:v:0", "120M",
            "-maxrate:v:0", "180M",
            "-bufsize:v:0", "360M",
            "-spatial_aq:v:0", "1",
            "-temporal_aq:v:0", "1",
            "-b_ref_mode:v:0", "middle"
        )
    }
    "HEVC NVENC Balanced" {
        $common += @(
            "-c:v:0", "hevc_nvenc",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "p010le",
            "-preset:v:0", "p6",
            "-tune:v:0", "uhq",
            "-rc:v:0", "vbr",
            "-cq:v:0", "12",
            "-b:v:0", "170M",
            "-maxrate:v:0", "220M",
            "-bufsize:v:0", "440M",
            "-multipass:v:0", "fullres",
            "-spatial_aq:v:0", "1",
            "-temporal_aq:v:0", "1",
            "-b_ref_mode:v:0", "middle"
        )
    }
    default {
        $common += @(
            "-c:v:0", "hevc_nvenc",
            "-profile:v:0", "main10",
            "-pix_fmt:v:0", "p010le",
            "-preset:v:0", "p7",
            "-tune:v:0", "uhq",
            "-rc:v:0", "vbr",
            "-cq:v:0", "8",
            "-b:v:0", "220M",
            "-maxrate:v:0", "260M",
            "-bufsize:v:0", "520M",
            "-multipass:v:0", "fullres",
            "-spatial_aq:v:0", "1",
            "-temporal_aq:v:0", "1",
            "-b_ref_mode:v:0", "disabled",
            "-forced-idr:v:0", "1"
        )
    }
}

if (-not $audioCurveActive) {
    if ($av1Export) {
        $common += @("-c:a", "pcm_s24be")
    } else {
        $common += @("-c:a", "copy")
    }
}

$common += @($OutputPath)

Write-Host "Input:  $InputPath"
Write-Host "Output: $OutputPath"
if ($CurvePath) {
    Write-Host "Curve: $CurvePath"
} else {
    Write-Host "Exposure: $Exposure EV"
}
if ($audioCurveActive) {
    Write-Host "Audio curve: $AudioCurvePath"
    Write-Host "Audio tracks with gain curves: $((@($audioFilters.Keys) | Sort-Object | ForEach-Object { 'A' + ([int]$_ + 1) }) -join ', ')"
}
Write-Host "Quality: $Quality"
if ($av1Export) {
    Write-Host "Container note: AV1 MP4 keeps video/audio and metadata, but omits the original camera timecode data stream."
}
Write-Host ""
$ffmpegExitCode = Invoke-NativeChecked -Exe $ffmpeg -NativeArgs $common
if ($ffmpegExitCode -ne 0) {
    throw "ffmpeg failed with exit code $ffmpegExitCode"
}

$reportPath = [IO.Path]::ChangeExtension($OutputPath, ".ffprobe.json")
& $ffprobe -v error -show_format -show_streams -show_chapters -print_format json $OutputPath | Out-File -LiteralPath $reportPath -Encoding utf8

Write-Host ""
Write-Host "Done."
Write-Host "Report: $reportPath"
