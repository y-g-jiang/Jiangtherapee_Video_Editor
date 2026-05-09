param(
  [int]$Seconds = 20,
  [string]$Video = "$env:USERPROFILE\Downloads\PN921798.MOV",
  [string]$Lut = "$env:USERPROFILE\Documents\xwechat_files\wxid_l0x6o1pixx6c12_bff3\msg\file\2025-11\VLog_to_V709_forV35_ver100.cube",
  [switch]$NoLut
)

$ErrorActionPreference = "Stop"

function Resolve-Mpv {
  $cmd = Get-Command mpv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }

  $wingetMpv = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter mpv.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  if ($wingetMpv) { return $wingetMpv }

  throw "mpv.exe was not found."
}

$mpv = Resolve-Mpv
$videoPath = (Resolve-Path -LiteralPath $Video).Path
$lutPath = if ($NoLut) { $null } else { (Resolve-Path -LiteralPath $Lut).Path }
$ipc = "\\.\pipe\native-lut-player-$PID"
$log = Join-Path $PSScriptRoot "probe-native-lut.log"
Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue

$args = @(
  "--no-config",
  "--idle=no",
  "--vo=gpu-next",
  "--gpu-api=d3d11",
  "--gpu-context=d3d11",
  "--gpu-hwdec-interop=d3d11va",
  "--hwdec=d3d11va",
  "--hwdec-codecs=hevc,h264",
  "--d3d11va-zero-copy=yes",
  "--d3d11-flip=yes",
  "--d3d11-output-format=rgb10_a2",
  "--framedrop=vo",
  "--vd-lavc-framedrop=nonref",
  "--video-sync=audio",
  "--demuxer-thread=yes",
  "--cache=yes",
  "--demuxer-max-bytes=1024MiB",
  "--demuxer-max-back-bytes=256MiB",
  "--priority=high",
  "--input-ipc-server=$ipc",
  "--log-file=$log",
  "--msg-level=all=warn,vo=info,vd=info",
  $videoPath
)

if ($lutPath) {
  $args = $args[0..($args.Count - 2)] + @("--lut=$lutPath", "--lut-type=auto", $videoPath)
}

$process = Start-Process -FilePath $mpv -ArgumentList $args -PassThru
Start-Sleep -Seconds ([Math]::Max(4, $Seconds))

try {
  $properties = @(
    "time-pos",
    "container-fps",
    "estimated-vf-fps",
    "display-fps",
    "hwdec-current",
    "hwdec-interop",
    "current-vo",
    "video-format",
    "video-frame-info",
    "frame-drop-count",
    "decoder-frame-drop-count",
    "mistimed-frame-count",
    "vo-delayed-frame-count",
    "avsync"
  )

  foreach ($property in $properties) {
    $payload = @{ command = @("get_property", $property) } | ConvertTo-Json -Compress
    $client = New-Object System.IO.Pipes.NamedPipeClientStream(".", "native-lut-player-$PID", [System.IO.Pipes.PipeDirection]::InOut)
    $client.Connect(1000)
    $writer = New-Object System.IO.StreamWriter($client)
    $writer.AutoFlush = $true
    $reader = New-Object System.IO.StreamReader($client)
    $writer.WriteLine($payload)
    $line = $reader.ReadLine()
    try { $reader.Dispose() } catch {}
    try { $writer.Dispose() } catch {}
    try { $client.Dispose() } catch {}
    [pscustomobject]@{ Property = $property; Result = $line }
  }
} finally {
  if (!$process.HasExited) {
    Stop-Process -Id $process.Id -Force
  }
}

"Log: $log"
