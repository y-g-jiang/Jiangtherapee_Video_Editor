# JiangtherapeeVideoEditor

Performance-first Windows V-Log video editor for Panasonic G9M2 style MOV files.

The current build is tuned for `PN921798.MOV`: 4K 50p 10-bit HEVC, four mono 24-bit PCM audio tracks, Panasonic V-Log/V-Gamut metadata, and Panasonic `VLog_to_V709_forV35_ver100.cube`.

## What It Does

- Plays the original 4K 50p HEVC file through mpv/libplacebo.
- Uses D3D11VA hardware decoding and D3D11 GPU rendering.
- Applies the Panasonic V-Log to V-709 `.cube` LUT in the preview path.
- Lets you keyframe V-Log scene-linear exposure over time.
- Lets you keyframe audio loudness over time for A1, A2, A3, and A4 independently or as groups.
- Exports a new high-quality MOV/MP4 with the video exposure curve and audio dB curves baked in.
- Keeps Panasonic XML metadata and the source stream layout as far as FFmpeg reasonably allows.

## Requirements

- Windows 11.
- FFmpeg and FFprobe on `PATH`.
- mpv on `PATH`, or installed through WinGet as `mpv-player.mpv-CI.MSVC`.
- NVIDIA GPU recommended for fast HEVC/AV1 NVENC export.
- Python 3.14 only if running from source or rebuilding the exe.

## Quick Start

Double-click:

```text
JiangtherapeeVideoEditor.exe
```

Or run the PowerShell launcher:

```powershell
.\JiangtherapeeVideoEditor.ps1
```

The app launches mpv inside the main window automatically. If the player is closed, press `Launch`.

## Playback Controls

- `Space`: pause or continue playback.
- `LUT On/Off`: preview original V-Log vs Panasonic V-709 LUT.
- `Mute`: mute preview audio.
- `-1f` / `+1f`: move one 30 fps timeline step.
- `5s` / `10s` / `30s` / `All`: change the visible timeline span.
- `Center`: center the visible timeline range on the current playhead.
- Bottom range slider: manually choose which part of the video the curve canvas shows.

## Video Exposure Curve

Select `Video EV` to edit exposure.

- `Ctrl + left click` on the curve: add or update a keyframe.
- Drag a keyframe: change its time and exposure.
- Right-click a keyframe: delete it.
- Drag the red playhead handle in the lower ruler: scrub video time.

Exposure is applied before the Panasonic V-709 LUT:

```text
V-Log code -> Panasonic V-Log inverse OETF -> scene-linear * 2^EV -> Panasonic V-Log OETF -> V-709 LUT
```

Panasonic V-Log constants used by the shader and export filter:

```text
linear section: V = 5.6 * L + 0.125
log section:    V = 0.241514 * log10(L + 0.00873) + 0.598206
inverse cut:    V = 0.181
```

Reference: Panasonic V-Log/V-Gamut Reference Manual.

## Audio Loudness Curves

Select `Audio dB` to edit audio gain. The app detects the four mono audio tracks and exposes them as `A1`, `A2`, `A3`, and `A4`.

Track selection:

- Click `A1`, `A2`, `A3`, or `A4` to include or exclude a track from editing.
- `A1+A2`: edit tracks 1 and 2 together.
- `A3+A4`: edit tracks 3 and 4 together.
- `All`: edit all four tracks together.

When more than one track is selected, curve edits are written to every selected track. Preview audio listens to the current primary track, which is the last track button or group you selected.

Loudness editing:

- Keyframes use the same curve canvas controls as video exposure.
- Dragging vertically snaps to `0.1 dB`.
- The top readout shows the current selected track group and value, for example:

```text
Audio A1+A2   00:12.30 / 02:54.72   Frame 369   -1.2 dB
```

Each audio track has its own waveform cache and its own saved loudness curve.

## Workspace Save And Restore

- `Save State`: writes a workspace JSON with current time, visible timeline range, selected axis, selected audio tracks, video exposure curve, audio curves, and export quality.
- `Restore`: loads a workspace JSON back into the editor.

Curve files in the app folder:

- `exposure-curve.json`: video EV curve.
- `audio-loudness-curve.json`: four-track audio dB curves.

## Export

Press `Export` to open the export chooser. It lists format, quality preset, estimated size, and notes.

Available presets:

- `HEVC NVENC Fast`
- `HEVC NVENC Balanced`
- `HEVC NVENC HQ`
- `HEVC x265 Compact`
- `HEVC x265 Master`
- `HEVC x265 Lossless`
- `AV1 NVENC Small`
- `AV1 NVENC HQ`

`Cancel Export` terminates the running PowerShell/FFmpeg export process and removes incomplete output files.

The export script can also be run directly:

```powershell
.\export-vlog-exposure.ps1 -CurvePath .\exposure-curve.json -AudioCurvePath .\audio-loudness-curve.json -Quality "HEVC NVENC HQ"
```

The export operation is:

```text
video: V-Log code -> inverse OETF -> scene-linear exposure curve -> OETF -> 10-bit HEVC/AV1
audio: per-track PCM -> per-track dB envelope -> PCM
```

Important limitation: because pixels change, the video stream must be re-encoded. FFmpeg may rewrite some MOV atom-level details such as muxer encoder, HEVC extradata, timebase, vendor id, and PCM sample entry. Panasonic XML metadata, timecode data, audio streams, chapters, and color tags are preserved as far as practical.

## Logs

Every run writes JSONL event logs:

```text
logs/run-YYYYMMDD-HHMMSS-pid.log
```

The current mpv log is written to:

```text
C:\Temp\JiangtherapeeVideoEditor\mpv-current.log
```

These logs record launch, IPC, seek, curve editing, audio track selection, export start/finish/failure, and shutdown events.

## Source Layout

```text
controller/native_lut_console.py   Main Tk/Aero UI, mpv IPC, curves, workspace, export launcher
export-vlog-exposure.ps1           FFmpeg export pipeline
shaders/vlog-exposure-live.glsl    GPU V-Log exposure shader
scripts/exposure_curve.lua         Generated mpv-side 50 Hz exposure curve updater
JiangtherapeeVideoEditor.ps1       PowerShell launcher
play-native-lut.ps1                Simple mpv playback launcher
probe-native-lut.ps1               mpv playback probe helper
```

Generated folders such as `build-onefile`, `dist-onefile`, `exports`, `logs`, and waveform caches are not required for source use.

## Build

From this directory:

```powershell
python -m PyInstaller --noconfirm --clean --windowed --onefile --name JiangtherapeeVideoEditor --distpath .\dist-onefile --workpath .\build-onefile .\controller\native_lut_console.py
Copy-Item .\dist-onefile\JiangtherapeeVideoEditor.exe .\JiangtherapeeVideoEditor.exe -Force
```

## License

Private working release by y-g-jiang. Add an explicit license before public redistribution.
