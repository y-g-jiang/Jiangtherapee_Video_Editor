param(
  [string]$Video = "$env:USERPROFILE\Downloads\PN921798.MOV",
  [string]$Lut = "$env:USERPROFILE\Documents\xwechat_files\wxid_l0x6o1pixx6c12_bff3\msg\file\2025-11\VLog_to_V709_forV35_ver100.cube",
  [switch]$NoLut,
  [switch]$Safe,
  [switch]$Stats
)

$ErrorActionPreference = "Stop"

function Resolve-Mpv {
  $cmd = Get-Command mpv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }

  $wingetMpv = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter mpv.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  if ($wingetMpv) { return $wingetMpv }

  throw "mpv.exe was not found. Install mpv-player.mpv-CI.MSVC with winget first."
}

$mpv = Resolve-Mpv
$videoPath = (Resolve-Path -LiteralPath $Video).Path
$lutPath = if ($NoLut) { $null } else { (Resolve-Path -LiteralPath $Lut).Path }

New-Item -Path "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences" -Force | Out-Null
New-ItemProperty -Path "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences" -Name $mpv -Value "GpuPreference=2;" -PropertyType String -Force | Out-Null

$common = @(
  "--no-config",
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
  "--force-window=yes",
  "--geometry=90%x90%",
  "--osd-level=1",
  "--osd-playing-msg=Native GPU LUT: D3D11 + HEVC hwdec"
)

if ($Safe) {
  $common = $common | Where-Object { $_ -ne "--d3d11va-zero-copy=yes" }
  $common += "--hwdec=d3d11va-copy"
  $common += "--gpu-hwdec-interop=auto"
}

if ($Stats) {
  $common += '--term-status-msg=FPS:${estimated-vf-fps} Drop:${frame-drop-count} DecDrop:${decoder-frame-drop-count} HW:${hwdec-current} VO:${current-vo} Pos:${time-pos}'
  $common += "--msg-level=all=warn,vo=info,vd=info"
}

if ($lutPath) {
  $common += "--lut=$lutPath"
  $common += "--lut-type=auto"
}

& $mpv @common $videoPath
