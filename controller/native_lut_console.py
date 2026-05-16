import ctypes
import math
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from array import array
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = Path.home() / "Downloads" / "PN921798.MOV"
VIDEO = DEFAULT_VIDEO
LUT = (
    Path.home()
    / "Documents"
    / "xwechat_files"
    / "wxid_l0x6o1pixx6c12_bff3"
    / "msg"
    / "file"
    / "2025-11"
    / "VLog_to_V709_forV35_ver100.cube"
)
RUNTIME_SHADER = ROOT / "shaders" / "vlog-exposure-runtime.glsl"
LIVE_SHADER = ROOT / "shaders" / "vlog-exposure-live.glsl"
MPV_EXPOSURE_SCRIPT = ROOT / "scripts" / "exposure_curve.lua"
MPV_RUNTIME_DIR = Path("C:/Temp/JiangtherapeeVideoEditor")
MPV_CURVE_STATE = MPV_RUNTIME_DIR / "exposure-curve.tsv"
MPV_LOG_PATH = MPV_RUNTIME_DIR / "mpv-current.log"
IPC_NAME = fr"\\.\pipe\JiangtherapeeVideoEditor-{os.getpid()}"
LOGS_DIR = ROOT / "logs"
EXPOSURE_REFRESH_MS = 20
CURVE_PATH = ROOT / "exposure-curve.json"
AUDIO_CURVE_PATH = ROOT / "audio-loudness-curve.json"
EXPORTS_DIR = ROOT / "exports"
EXPORT_PRESETS = [
    "HEVC NVENC Fast",
    "HEVC NVENC Balanced",
    "HEVC NVENC HQ",
    "HEVC x265 Compact",
    "HEVC x265 Master",
    "HEVC x265 Lossless",
    "AV1 NVENC Small",
    "AV1 NVENC HQ",
]
EXPORT_BYTES_PER_SECOND = {
    "HEVC NVENC Fast": 22 * 1024 * 1024,
    "HEVC NVENC Balanced": 28 * 1024 * 1024,
    "HEVC NVENC HQ": 34 * 1024 * 1024,
    "HEVC x265 Compact": 18 * 1024 * 1024,
    "HEVC x265 Master": 40 * 1024 * 1024,
    "HEVC x265 Lossless": 225 * 1024 * 1024,
    "AV1 NVENC Small": 14 * 1024 * 1024,
    "AV1 NVENC HQ": 22 * 1024 * 1024,
}
EXPORT_PRESET_INFO = {
    "HEVC NVENC Fast": ("HEVC / MOV", "GPU fast, smallest HEVC preview-grade master."),
    "HEVC NVENC Balanced": ("HEVC / MOV", "GPU balanced, good editing handoff."),
    "HEVC NVENC HQ": ("HEVC / MOV", "GPU high quality, default DaVinci-friendly choice."),
    "HEVC x265 Compact": ("HEVC / MOV", "CPU slow, best HEVC compression at compact size."),
    "HEVC x265 Master": ("HEVC / MOV", "CPU very slow, high quality archival-looking master."),
    "HEVC x265 Lossless": ("HEVC / MOV", "CPU extremely slow, huge near-intermediate file."),
    "AV1 NVENC Small": ("AV1 / MP4", "GPU small share copy; less NLE-friendly than HEVC."),
    "AV1 NVENC HQ": ("AV1 / MP4", "GPU high quality AV1, compact but compatibility varies."),
}
VIDEO_FPS = 30.0
FRAME_STEP = 1.0 / VIDEO_FPS
WAVEFORM_RATE = 2000
AUDIO_TRACK_COUNT = 4
AUDIO_REFRESH_MS = 25
PLAYHEAD_REFRESH_MS = 33


def resolve_mpv() -> str:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "mpv.exe"
        if candidate.exists():
            return str(candidate)

    packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    for candidate in packages.rglob("mpv.exe"):
        return str(candidate)

    raise FileNotFoundError("mpv.exe not found. Install mpv-player.mpv-CI.MSVC first.")


def resolve_ffmpeg() -> str:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("ffmpeg.exe not found on PATH.")


def set_high_performance_gpu(exe: str) -> None:
    try:
        import winreg

        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\DirectX\UserGpuPreferences",
        )
        winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
        winreg.CloseKey(key)
    except OSError:
        pass


def mpv_path(path: Path) -> str:
    return str(path).replace("\\", "/")


class RunLogger:
    def __init__(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = LOGS_DIR / f"run-{stamp}-{os.getpid()}.log"
        self._lock = threading.Lock()
        self._last_drag_at = 0.0
        self._file = self.path.open("a", encoding="utf-8", buffering=1)
        self.write("logger.start", root=str(ROOT), pid=os.getpid(), exe=sys.executable)

    def write(self, event: str, **fields: object) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._file.write(line + "\n")
                self._file.flush()
                os.fsync(self._file.fileno())
            except Exception:
                pass

    def write_drag(self, event: str, **fields: object) -> None:
        now = time.monotonic()
        if now - self._last_drag_at < 0.12:
            return
        self._last_drag_at = now
        self.write(event, **fields)

    def close(self) -> None:
        self.write("logger.close")
        with self._lock:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            except Exception:
                pass


def build_exposure_shader(ev: float) -> str:
    gain = 2.0 ** ev
    return f"""//!HOOK MAIN
//!BIND HOOKED
//!DESC Panasonic V-Log scene-linear exposure before display LUT ({ev:+.2f} EV)

float vlog_to_linear_channel(float v)
{{
    const float cut = 0.181;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return v < cut ? (v - 0.125) / 5.6 : pow(10.0, (v - d) / c) - b;
}}

float linear_to_vlog_channel(float x)
{{
    const float cut = 0.01;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return x < cut ? 5.6 * x + 0.125 : c * (log(x + b) / log(10.0)) + d;
}}

vec3 vlog_to_linear(vec3 v)
{{
    return vec3(
        vlog_to_linear_channel(v.r),
        vlog_to_linear_channel(v.g),
        vlog_to_linear_channel(v.b)
    );
}}

vec3 linear_to_vlog(vec3 x)
{{
    return vec3(
        linear_to_vlog_channel(x.r),
        linear_to_vlog_channel(x.g),
        linear_to_vlog_channel(x.b)
    );
}}

vec4 hook()
{{
    vec4 src = HOOKED_texOff(0);
    vec3 linear = vlog_to_linear(clamp(src.rgb, 0.0, 1.0));
    linear *= {gain:.9f};
    vec3 encoded = linear_to_vlog(max(linear, vec3(-0.0223214286)));
    return vec4(clamp(encoded, 0.0, 1.0), src.a);
}}
"""


def write_exposure_shader(ev: float, slot: int = 0) -> Path:
    path = ROOT / "shaders" / f"vlog-exposure-runtime-{slot}.glsl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_exposure_shader(ev), encoding="utf-8")
    RUNTIME_SHADER.write_text(build_exposure_shader(ev), encoding="utf-8")
    return path


def ensure_live_shader() -> None:
    if not LIVE_SHADER.exists():
        LIVE_SHADER.write_text(
            """//!PARAM exposure
//!DESC Scene-linear exposure in stops
//!TYPE DYNAMIC float
//!MINIMUM -4.0
//!MAXIMUM 4.0
0.0

//!HOOK MAIN
//!BIND HOOKED
//!DESC Panasonic V-Log scene-linear exposure before display LUT

float vlog_to_linear_channel(float v)
{
    const float cut = 0.181;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return v < cut ? (v - 0.125) / 5.6 : pow(10.0, (v - d) / c) - b;
}

float linear_to_vlog_channel(float x)
{
    const float cut = 0.01;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return x < cut ? 5.6 * x + 0.125 : c * (log(x + b) / log(10.0)) + d;
}

vec3 vlog_to_linear(vec3 v)
{
    return vec3(
        vlog_to_linear_channel(v.r),
        vlog_to_linear_channel(v.g),
        vlog_to_linear_channel(v.b)
    );
}

vec3 linear_to_vlog(vec3 x)
{
    return vec3(
        linear_to_vlog_channel(x.r),
        linear_to_vlog_channel(x.g),
        linear_to_vlog_channel(x.b)
    );
}

vec4 hook()
{
    vec4 src = HOOKED_texOff(0);
    vec3 linear = vlog_to_linear(clamp(src.rgb, 0.0, 1.0));
    linear *= exp2(exposure);
    vec3 encoded = linear_to_vlog(max(linear, vec3(-0.0223214286)));
    return vec4(clamp(encoded, 0.0, 1.0), src.a);
}
""",
            encoding="utf-8",
        )


def ensure_mpv_exposure_script() -> None:
    MPV_EXPOSURE_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    MPV_EXPOSURE_SCRIPT.write_text(
        r'''local mp = require "mp"
local utils = require "mp.utils"

local state_path = mp.get_opt("exposure_curve_file")
local fps = tonumber(mp.get_opt("exposure_curve_fps") or "60") or 60
local interval = 1.0 / math.max(1, fps)
local points = {}
local enabled = true
local last_ev = nil
local duration = nil

local function split_line(line)
    local cols = {}
    for part in string.gmatch(line, "[^\t]+") do
        cols[#cols + 1] = part
    end
    return cols
end

local function load_curve()
    if not state_path then
        return
    end
    local file = io.open(state_path, "r")
    if not file then
        return
    end
    local loaded = {}
    local header = file:read("*l")
    if header then
        if string.sub(header, 1, 8) == "enabled=" then
            enabled = string.sub(header, 9) ~= "0"
        end
    end
    for line in file:lines() do
        local cols = split_line(line)
        local t = tonumber(cols[1])
        local ev = tonumber(cols[2])
        if t and ev then
            loaded[#loaded + 1] = { t = t, ev = ev }
        end
    end
    file:close()
    table.sort(loaded, function(a, b) return a.t < b.t end)
    points = loaded
    last_ev = nil
end

local function value_at(t)
    if #points == 0 then
        return 0
    end
    if t <= points[1].t then
        return points[1].ev
    end
    for i = 1, #points - 1 do
        local a = points[i]
        local b = points[i + 1]
        if t <= b.t then
            local span = math.max(0.001, b.t - a.t)
            local u = math.max(0, math.min(1, (t - a.t) / span))
            local s = u * u * (3 - 2 * u)
            return a.ev + (b.ev - a.ev) * s
        end
    end
    return points[#points].ev
end

local function set_ev(ev)
    ev = math.max(-4, math.min(4, ev))
    if last_ev and math.abs(ev - last_ev) < 0.0005 then
        return
    end
    mp.set_property("glsl-shader-opts", string.format("exposure=%.4f", ev))
    last_ev = ev
end

local timer = mp.add_periodic_timer(interval, function()
    if not enabled then
        return
    end
    local t = mp.get_property_number("time-pos")
    duration = duration or mp.get_property_number("duration")
    if duration and t and t >= duration - 0.25 then
        return
    end
    if t then
        set_ev(value_at(t))
    end
end)
timer:resume()

mp.register_script_message("reload-exposure-curve", function()
    load_curve()
    local t = mp.get_property_number("time-pos") or 0
    duration = mp.get_property_number("duration") or duration
    if enabled then
        set_ev(value_at(t))
    end
end)

load_curve()
duration = mp.get_property_number("duration")
''',
        encoding="utf-8",
    )


def win32_pipe_command(payload: dict) -> dict:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p

    generic_read = 0x80000000
    generic_write = 0x40000000
    open_existing = 3
    handle = create_file(
        IPC_NAME,
        generic_read | generic_write,
        0,
        None,
        open_existing,
        0,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Could not open mpv IPC pipe")

    try:
        message = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(handle, message, len(message), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "Could not write mpv IPC command")

        chunks = []
        while True:
            buf = ctypes.create_string_buffer(4096)
            read = ctypes.c_uint32()
            ok = kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
            if not ok:
                raise OSError(ctypes.get_last_error(), "Could not read mpv IPC reply")
            chunks.append(buf.raw[: read.value])
            if b"\n" in chunks[-1]:
                break
        return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    finally:
        kernel32.CloseHandle(handle)


class JiangtherapeeVideoEditor:
    def __init__(self) -> None:
        self.mpv = resolve_mpv()
        set_high_performance_gpu(self.mpv)
        try:
            self.ffmpeg = resolve_ffmpeg()
        except FileNotFoundError:
            self.ffmpeg = ""
        self.process: subprocess.Popen | None = None
        self.video_path = DEFAULT_VIDEO
        self.exposure = 0.0
        self.audio_db_by_track: list[float] = [0.0 for _ in range(AUDIO_TRACK_COUNT)]
        self.audio_db = 0.0
        self.exposure_shader_slot = 0
        self.last_sent_exposure: float | None = None
        self.last_sent_audio_db_by_track: list[float | None] = [None for _ in range(AUDIO_TRACK_COUNT)]
        self.last_sent_audio_db: float | None = None
        self.exposure_refresh_pending = False
        self.exposure_worker_busy = False
        self.audio_refresh_pending = False
        self.audio_worker_busy = False
        self.applying_curve = False
        self.dragging_curve_point = False
        self.drag_point_index: int | None = None
        self.timeline_drag_mode: str | None = None
        self.drag_redraw_pending = False
        self.seeking_timeline = False
        self.seek_worker_busy = False
        self.pending_seek_time: float | None = None
        self.last_seek_sent_at = 0.0
        self.duration = 174.72
        self.current_time = 0.0
        self.timeline_window = 20.0
        self.timeline_center = 0.0
        self.detected_audio_tracks = AUDIO_TRACK_COUNT
        self.syncing_range_scale = False
        self.curve_enabled: tk.BooleanVar | None = None
        self.active_axis: tk.StringVar | None = None
        self.active_audio_track: tk.IntVar | None = None
        self.audio_track_vars: list[tk.BooleanVar] = []
        self.curve_points: list[dict[str, float]] = [{"time": 0.0, "ev": 0.0}, {"time": self.duration, "ev": 0.0}]
        self.audio_points_by_track: list[list[dict[str, float]]] = [self.default_audio_points() for _ in range(AUDIO_TRACK_COUNT)]
        self.audio_points: list[dict[str, float]] = self.audio_points_by_track[0]
        self.waveform_peaks_by_track: list[list[float]] = [[] for _ in range(AUDIO_TRACK_COUNT)]
        self.waveform_loading_by_track: list[bool] = [False for _ in range(AUDIO_TRACK_COUNT)]
        self.waveform_peaks: list[float] = self.waveform_peaks_by_track[0]
        self.waveform_loading = False
        self.export_process: subprocess.Popen | None = None
        self.export_log_path: Path | None = None
        self.export_output_path: Path | None = None
        self.export_quality: tk.StringVar | None = None
        self.export_estimate_text: tk.StringVar | None = None
        self.export_dialog: tk.Toplevel | None = None
        self.export_progress_text: tk.StringVar | None = None
        self.export_cancelled = False
        self.suppress_point_select = False
        self.logger = RunLogger()
        sys.excepthook = self.handle_unhandled_exception
        ensure_live_shader()
        ensure_mpv_exposure_script()
        self.write_mpv_curve_state()

        self.root = tk.Tk()
        self.root.title("JiangtherapeeVideoEditor")
        self.root.geometry("1280x820")
        self.root.minsize(960, 640)
        self.root.configure(bg="#eef3f8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_class("Button", "<space>", self.on_space)
        self.root.bind_class("TButton", "<space>", self.on_space)
        self.root.bind_class("Checkbutton", "<space>", self.on_space)
        self.root.bind_class("TCheckbutton", "<space>", self.on_space)
        self.root.bind_class("Radiobutton", "<space>", self.on_space)
        self.root.bind_class("TRadiobutton", "<space>", self.on_space)
        self.root.bind_class("Listbox", "<space>", self.on_space)
        self.root.bind_class("Canvas", "<space>", self.on_space)
        self.root.bind_all("<KeyPress-space>", self.on_space, add="+")
        self.status_text = tk.StringVar(master=self.root, value="Ready")
        self.metrics_text = tk.StringVar(master=self.root, value="mpv not running")
        self.timeline_text = tk.StringVar(master=self.root, value=f"{self.format_time(0.0)}   Frame 0   {self.exposure:+.2f} EV")
        self.curve_enabled = tk.BooleanVar(master=self.root, value=True)
        self.active_axis = tk.StringVar(master=self.root, value="video")
        self.active_audio_track = tk.IntVar(master=self.root, value=0)
        self.audio_track_vars = [
            tk.BooleanVar(master=self.root, value=(track == 0))
            for track in range(AUDIO_TRACK_COUNT)
        ]
        self.export_quality = tk.StringVar(master=self.root, value="HEVC NVENC HQ")
        self.export_estimate_text = tk.StringVar(master=self.root, value="")
        self.load_curves()
        self.enable_aero()
        self.build_style()
        self.build_ui()
        self.root.after(500, self.launch)
        self.root.after(250, self.refresh_playhead)
        self.root.after(900, self.refresh_metrics)
        self.log("app.ready", log=str(self.logger.path), video=str(self.video_path), lut=str(LUT))

    def log(self, event: str, **fields: object) -> None:
        if hasattr(self, "logger"):
            self.logger.write(event, **fields)

    def log_drag(self, event: str, **fields: object) -> None:
        if hasattr(self, "logger"):
            self.logger.write_drag(event, **fields)

    def handle_unhandled_exception(self, exc_type, exc, tb) -> None:
        self.log("exception.unhandled", exc_type=getattr(exc_type, "__name__", str(exc_type)), error=str(exc))
        sys.__excepthook__(exc_type, exc, tb)

    def default_audio_points(self) -> list[dict[str, float]]:
        return [{"time": 0.0, "db": 0.0}, {"time": self.duration, "db": 0.0}]

    def selected_audio_track(self) -> int:
        if not self.active_audio_track:
            return 0
        return max(0, min(AUDIO_TRACK_COUNT - 1, int(self.active_audio_track.get())))

    def checked_audio_tracks(self) -> list[int]:
        if not self.audio_track_vars:
            return [self.selected_audio_track()]
        return [
            idx
            for idx, variable in enumerate(self.audio_track_vars[:AUDIO_TRACK_COUNT])
            if bool(variable.get())
        ]

    def selected_audio_tracks(self) -> list[int]:
        tracks = self.checked_audio_tracks()
        if tracks:
            return tracks
        track = self.selected_audio_track()
        self.audio_track_vars[track].set(True)
        return [track]

    def audio_track_label(self, tracks: list[int] | None = None) -> str:
        tracks = self.selected_audio_tracks() if tracks is None else tracks
        return "+".join(f"A{track + 1}" for track in tracks)

    def set_audio_track_group(self, tracks: list[int], primary: int | None = None) -> None:
        normalized = sorted({max(0, min(AUDIO_TRACK_COUNT - 1, int(track))) for track in tracks})
        if not normalized:
            normalized = [self.selected_audio_track()]
        primary = normalized[0] if primary is None or primary not in normalized else primary
        if self.active_axis:
            self.active_axis.set("audio")
        if self.active_audio_track:
            self.active_audio_track.set(primary)
        for idx, variable in enumerate(self.audio_track_vars[:AUDIO_TRACK_COUNT]):
            variable.set(idx in normalized)
        self.audio_points = self.audio_points_by_track[primary]
        self.audio_db = self.audio_db_by_track[primary]
        self.waveform_peaks = self.waveform_peaks_by_track[primary]
        self.waveform_loading = self.waveform_loading_by_track[primary]
        self.log("ui.audio_track_group_change", tracks=[track + 1 for track in normalized], primary=primary + 1)
        self.ensure_waveform_async()
        self.select_mpv_audio_track(primary)
        self.apply_curve_now()
        self.redraw_curve()

    def set_audio_track(self, track: int) -> None:
        track = max(0, min(AUDIO_TRACK_COUNT - 1, int(track)))
        tracks = self.checked_audio_tracks()
        if not tracks:
            tracks = [track]
        primary = track if track in tracks else (self.selected_audio_track() if self.selected_audio_track() in tracks else tracks[0])
        self.set_audio_track_group(tracks, primary=primary)

    def set_audio_track_preset(self, tracks: list[int]) -> None:
        self.set_audio_track_group(tracks, primary=tracks[0] if tracks else None)

    def select_mpv_audio_track(self, track: int) -> None:
        self.log("audio.track.preview_select", track=track + 1)
        if self.process and self.process.poll() is None:
            track_reply = self.command(["get_property", "track-list"])
            aid = track + 1
            tracks = track_reply.get("data") if track_reply and track_reply.get("error") == "success" else []
            if isinstance(tracks, list):
                audio_tracks = [item for item in tracks if isinstance(item, dict) and item.get("type") == "audio"]
                if track < len(audio_tracks):
                    aid = int(audio_tracks[track].get("id", aid))
            reply = self.command(["set_property", "aid", aid])
            if reply and reply.get("error") not in (None, "success"):
                self.log("audio.track.preview_select_failed", track=track + 1, aid=aid, reply=reply)
            else:
                self.log("audio.track.preview_select_ok", track=track + 1, aid=aid)

    def reset_edit_state_for_video(self, duration: float | None = None) -> None:
        if duration is not None and duration > 0:
            self.duration = float(duration)
        self.current_time = 0.0
        self.timeline_window = min(max(FRAME_STEP, 20.0), max(FRAME_STEP, self.duration))
        self.timeline_center = 0.0
        self.exposure = 0.0
        self.audio_db_by_track = [0.0 for _ in range(AUDIO_TRACK_COUNT)]
        self.audio_db = 0.0
        self.last_sent_exposure = None
        self.last_sent_audio_db_by_track = [None for _ in range(AUDIO_TRACK_COUNT)]
        self.last_sent_audio_db = None
        self.curve_points = [{"time": 0.0, "ev": 0.0}, {"time": self.duration, "ev": 0.0}]
        self.audio_points_by_track = [self.default_audio_points() for _ in range(AUDIO_TRACK_COUNT)]
        primary = self.selected_audio_track()
        self.audio_points = self.audio_points_by_track[primary]
        self.waveform_peaks_by_track = [[] for _ in range(AUDIO_TRACK_COUNT)]
        self.waveform_loading_by_track = [False for _ in range(AUDIO_TRACK_COUNT)]
        self.waveform_peaks = self.waveform_peaks_by_track[primary]
        self.waveform_loading = False
        self.detected_audio_tracks = AUDIO_TRACK_COUNT
        self.pending_seek_time = None
        self.seeking_timeline = False
        self.dragging_curve_point = False
        self.drag_point_index = None
        self.timeline_drag_mode = None
        self.display_time(0.0)
        self.update_range_scale()
        self.write_mpv_curve_state()

    def open_video_dialog(self) -> None:
        initial_dir = str(self.video_path.parent if self.video_path and self.video_path.exists() else Path.home() / "Downloads")
        self.log("video.open.dialog", initial_dir=initial_dir)
        path = filedialog.askopenfilename(
            title="Open target video",
            initialdir=initial_dir,
            filetypes=[
                ("Video files", "*.mov *.mp4 *.m4v *.mkv *.avi *.mts *.m2ts"),
                ("MOV files", "*.mov"),
                ("MP4 files", "*.mp4"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            self.log("video.open.cancel")
            return
        self.change_video(Path(path))

    def change_video(self, path: Path) -> None:
        path = path.expanduser().resolve()
        if not path.exists():
            self.status_text.set(f"Video not found: {path}")
            self.log("video.change.failed", path=str(path), reason="not_found")
            return
        if self.export_process and self.export_process.poll() is None:
            self.cancel_export()
        self.log("video.change.start", old=str(self.video_path), new=str(path))
        was_running = bool(self.process and self.process.poll() is None)
        self.stop()
        self.video_path = path
        self.reset_edit_state_for_video()
        self.save_curve()
        self.status_text.set(f"Video loaded: {path.name}")
        self.redraw_curve()
        if was_running:
            self.root.after(650, self.launch)

    def enable_aero(self) -> None:
        try:
            hwnd = self.root.winfo_id()
            accent = AccentPolicy(3, 0xCCF8FBFF, 0, 0)
            data = WindowCompositionAttributeData(
                19,
                ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent),
            )
            ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        except Exception:
            self.root.attributes("-alpha", 0.96)

    def build_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure(
            "Title.TLabel",
            font=("Segoe UI Variable Display", 18, "bold"),
            foreground="#102033",
            background="#eef3f8",
        )
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#52677d", background="#eef3f8")
        style.configure("Body.TLabel", font=("Segoe UI", 10), foreground="#1c2d3f", background="#eef3f8")
        style.configure("Value.TLabel", font=("Cascadia Mono", 11, "bold"), foreground="#0f5ba7", background="#eef3f8")
        style.configure("Glass.TFrame", background="#eef3f8")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14, style="Glass.TFrame")
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="Glass.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="JiangtherapeeVideoEditor", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status_text, style="Sub.TLabel").pack(side="right")

        self.video_shell = tk.Frame(
            outer,
            bg="#9eb6cc",
            highlightthickness=1,
            highlightbackground="#c7d7e6",
            highlightcolor="#8aaed0",
        )
        self.video_shell.pack(fill="both", expand=True)
        self.video_frame = tk.Frame(self.video_shell, bg="#020407")
        self.video_frame.pack(fill="both", expand=True, padx=1, pady=1)
        self.video_frame.bind("<Configure>", lambda _event: self.resize_embedded_video())

        console = ttk.Frame(outer, padding=(0, 12, 0, 0), style="Glass.TFrame")
        console.pack(fill="x")

        toggles = ttk.Frame(console, style="Glass.TFrame")
        toggles.pack(fill="x", pady=(0, 10))
        ttk.Label(toggles, textvariable=self.timeline_text, style="Value.TLabel").pack(side="left")
        ttk.Button(toggles, text="LUT On/Off", command=lambda: self.command(["cycle", "lut"]), takefocus=False).pack(side="left", fill="x", expand=True)
        ttk.Button(toggles, text="Mute", command=lambda: self.command(["cycle", "mute"]), takefocus=False).pack(side="left", fill="x", expand=True)

        curve_panel = ttk.Frame(console, style="Glass.TFrame")
        curve_panel.pack(fill="x", pady=(10, 0))
        curve_header = ttk.Frame(curve_panel, style="Glass.TFrame")
        curve_header.pack(fill="x")
        ttk.Checkbutton(curve_header, text="Curve", variable=self.curve_enabled, command=self.apply_curve_now, takefocus=False).pack(side="left")
        ttk.Radiobutton(curve_header, text="Video EV", variable=self.active_axis, value="video", command=self.on_axis_change, takefocus=False).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(curve_header, text="Audio dB", variable=self.active_axis, value="audio", command=self.on_axis_change, takefocus=False).pack(side="left", padx=(8, 0))
        for track in range(AUDIO_TRACK_COUNT):
            ttk.Checkbutton(
                curve_header,
                text=f"A{track + 1}",
                variable=self.audio_track_vars[track],
                command=lambda value=track: self.set_audio_track(value),
                takefocus=False,
            ).pack(side="left", padx=(6 if track == 0 else 2, 0))
        ttk.Button(curve_header, text="A1+A2", command=lambda: self.set_audio_track_preset([0, 1]), takefocus=False).pack(side="left", padx=(8, 0))
        ttk.Button(curve_header, text="A3+A4", command=lambda: self.set_audio_track_preset([2, 3]), takefocus=False).pack(side="left", padx=(4, 0))
        ttk.Button(curve_header, text="All", command=lambda: self.set_audio_track_preset(list(range(AUDIO_TRACK_COUNT))), takefocus=False).pack(side="left", padx=(4, 0))
        ttk.Button(curve_header, text="-1f", command=lambda: self.nudge_time(-1), takefocus=False).pack(side="left", padx=(10, 0))
        ttk.Button(curve_header, text="+1f", command=lambda: self.nudge_time(1), takefocus=False).pack(side="left", padx=(8, 0))
        ttk.Button(curve_header, text="Launch", style="Accent.TButton", command=self.launch, takefocus=False).pack(side="right", padx=(8, 0))
        ttk.Button(curve_header, text="Stop", command=self.stop, takefocus=False).pack(side="right")
        ttk.Button(curve_header, text="Open Video", command=self.open_video_dialog, takefocus=False).pack(side="right", padx=(8, 0))
        ttk.Button(curve_header, text="Export", style="Accent.TButton", command=self.open_export_dialog, takefocus=False).pack(side="right")
        ttk.Button(curve_header, text="Cancel Export", command=self.cancel_export, takefocus=False).pack(side="right", padx=(8, 0))
        ttk.Button(curve_header, text="Restore", command=self.restore_workspace, takefocus=False).pack(side="right", padx=(8, 0))
        ttk.Button(curve_header, text="Save State", command=self.export_workspace, takefocus=False).pack(side="right")

        zoom_row = ttk.Frame(curve_panel, style="Glass.TFrame")
        zoom_row.pack(fill="x", pady=(8, 0))
        for label, span in [("5s", 5.0), ("10s", 10.0), ("30s", 30.0), ("All", self.duration)]:
            ttk.Button(zoom_row, text=label, command=lambda value=span: self.set_timeline_window(value), takefocus=False).pack(side="left", padx=(0, 6))
        ttk.Button(zoom_row, text="Center", command=self.center_timeline, takefocus=False).pack(side="right", padx=(6, 0))

        self.range_scale = ttk.Scale(
            curve_panel,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            command=self.on_range_scale,
            takefocus=False,
        )
        self.range_scale.pack(fill="x", pady=(8, 0))

        self.timeline = tk.Canvas(curve_panel, height=152, bg="#dfeaf4", highlightthickness=1, highlightbackground="#b8ccdd")
        self.timeline.pack(fill="x", pady=(8, 6))
        self.timeline.bind("<Button-1>", self.on_timeline_click)
        self.timeline.bind("<Control-Button-1>", self.on_timeline_ctrl_click)
        self.timeline.bind("<B1-Motion>", self.on_timeline_drag)
        self.timeline.bind("<ButtonRelease-1>", self.on_timeline_release)
        self.timeline.bind("<Button-3>", self.on_timeline_right_click)

        export_row = ttk.Frame(curve_panel, style="Glass.TFrame")
        # Kept for state binding, but hidden; Export now lives in the chooser dialog.
        ttk.Label(export_row, text="Export", style="Sub.TLabel").pack(side="left")
        self.export_combo = ttk.Combobox(
            export_row,
            textvariable=self.export_quality,
            values=EXPORT_PRESETS,
            width=24,
            state="readonly",
            takefocus=False,
        )
        self.export_combo.pack(side="left", padx=(8, 8))
        self.export_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_export_estimate())
        ttk.Label(export_row, textvariable=self.export_estimate_text, style="Sub.TLabel").pack(side="left")

        self.point_list = tk.Listbox(curve_panel, height=3, exportselection=False, takefocus=False)
        self.point_list.pack(fill="x")
        self.point_list.bind("<<ListboxSelect>>", self.on_point_select)
        self.redraw_curve()
        self.update_export_estimate()

        self.metrics_label = ttk.Label(console, textvariable=self.metrics_text, style="Sub.TLabel", justify="left")

    def launch(self) -> None:
        if self.process and self.process.poll() is not None:
            self.log("mpv.previous_exited", code=self.process.poll())
            self.process = None
        if self.process and self.process.poll() is None:
            self.status_text.set("mpv is already running")
            self.log("mpv.launch.skipped", reason="already_running", pid=self.process.pid)
            return

        ensure_live_shader()
        ensure_mpv_exposure_script()
        self.write_mpv_curve_state()
        self.root.update_idletasks()
        self.video_frame.update_idletasks()
        video_wid = self.video_frame.winfo_id()
        args = [
            self.mpv,
            "--no-config",
            f"--log-file={mpv_path(MPV_LOG_PATH)}",
            "--msg-level=all=v",
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
            "--keep-open=yes",
            "--keep-open-pause=yes",
            "--idle=yes",
            "--no-border",
            "--no-osc",
            f"--wid={video_wid}",
            f"--input-ipc-server={IPC_NAME}",
            f"--script={mpv_path(MPV_EXPOSURE_SCRIPT)}",
            f"--script-opts=exposure_curve_file={mpv_path(MPV_CURVE_STATE)},exposure_curve_fps=50",
            f"--glsl-shader={mpv_path(LIVE_SHADER)}",
            f"--glsl-shader-opts=exposure={self.exposure:.3f}",
            f"--lut={mpv_path(LUT)}",
            "--lut-type=auto",
            str(self.video_path),
        ]
        self.log("mpv.launch.start", args=args, curve_state=str(MPV_CURVE_STATE))
        try:
            self.process = subprocess.Popen(args)
        except Exception as exc:
            self.log("mpv.launch.failed", error=str(exc))
            raise
        self.last_sent_exposure = self.exposure
        self.last_sent_audio_db_by_track = [None for _ in range(AUDIO_TRACK_COUNT)]
        self.last_sent_audio_db = None
        self.status_text.set("Embedded native player launched")
        self.log("mpv.launch.ok", pid=self.process.pid, hwnd=video_wid)
        self.root.after(600, self.resize_embedded_video)
        self.root.after(900, self.refresh_duration)
        self.root.after(1200, self.sync_mpv_curve_state)
        self.root.after(1250, lambda: self.set_audio_db(self.audio_db_by_track[self.selected_audio_track()]))

    def resize_embedded_video(self) -> None:
        if not hasattr(self, "video_frame"):
            return
        parent = self.video_frame.winfo_id()
        width = max(1, self.video_frame.winfo_width())
        height = max(1, self.video_frame.winfo_height())
        children: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd: int, _lparam: int) -> int:
            children.append(hwnd)
            return 1

        try:
            ctypes.windll.user32.EnumChildWindows(parent, enum_proc(collect), 0)
            for child in children:
                ctypes.windll.user32.MoveWindow(child, 0, 0, width, height, True)
            self.log_drag("mpv.resize", children=len(children), width=width, height=height)
        except Exception:
            self.log("mpv.resize.exception", width=width, height=height)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.log("mpv.stop", pid=self.process.pid)
            self.process.terminate()
            self.status_text.set("Stopping mpv")
        else:
            self.log("mpv.stop.skipped", reason="not_running")
            self.status_text.set("mpv is not running")

    def toggle_pause(self) -> None:
        self.log("input.pause_toggle", time=self.current_time)
        self.command(["cycle", "pause"])

    def on_space(self, event: tk.Event | None = None) -> str:
        if event is not None and event.type != tk.EventType.KeyPress:
            return "break"
        widget = str(event.widget) if event is not None and getattr(event, "widget", None) is not None else ""
        self.log("input.space", widget=widget, time=self.current_time)
        self.root.after_idle(self.toggle_pause)
        return "break"

    def command(self, command: list) -> dict | None:
        if not self.process or self.process.poll() is not None:
            self.status_text.set("Launch mpv first")
            self.log("ipc.command.skipped", command=command, reason="mpv_not_running")
            return None
        try:
            reply = win32_pipe_command({"command": command})
            error = reply.get("error") if isinstance(reply, dict) else None
            if error and error != "success":
                self.log("ipc.command.error", command=command, reply=reply)
            return reply
        except OSError as exc:
            self.status_text.set(f"IPC not ready: {exc}")
            self.log("ipc.command.exception", command=command, error=str(exc))
            return None

    def refresh_duration(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        reply = self.command(["get_property", "duration"])
        value = reply.get("data") if reply and reply.get("error") == "success" else None
        if isinstance(value, (int, float)) and value > 0:
            self.duration = float(value)
            self.log("media.duration", duration=self.duration)
            self.display_time(self.current_time)
            self.update_range_scale()
            self.redraw_curve()
        track_reply = self.command(["get_property", "track-list"])
        tracks = track_reply.get("data") if track_reply and track_reply.get("error") == "success" else []
        if isinstance(tracks, list):
            audio_count = sum(1 for item in tracks if isinstance(item, dict) and item.get("type") == "audio")
            if audio_count:
                self.detected_audio_tracks = audio_count
                self.log("media.audio_tracks", count=audio_count)

    def on_axis_change(self) -> None:
        self.log("ui.axis_change", axis=self.active_axis.get())
        if self.active_axis.get() == "audio":
            track = self.selected_audio_track()
            self.audio_points = self.audio_points_by_track[track]
            self.audio_db = self.audio_db_by_track[track]
            self.waveform_peaks = self.waveform_peaks_by_track[track]
            self.waveform_loading = self.waveform_loading_by_track[track]
            self.ensure_waveform_async()
            self.select_mpv_audio_track(track)
        self.apply_curve_now()
        self.redraw_curve()

    def waveform_cache_path(self, track: int | None = None) -> Path:
        if track is None:
            track = self.selected_audio_track()
        stamp = int(self.video_path.stat().st_mtime) if self.video_path.exists() else 0
        return ROOT / f"waveform-{self.video_path.stem}-a{track + 1}-{stamp}-{WAVEFORM_RATE}.json"

    def ensure_waveform_async(self) -> None:
        track = self.selected_audio_track()
        self.waveform_peaks = self.waveform_peaks_by_track[track]
        self.waveform_loading = self.waveform_loading_by_track[track]
        if self.waveform_peaks or self.waveform_loading:
            return
        cache = self.waveform_cache_path(track)
        if cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                peaks = payload.get("peaks", [])
                if isinstance(peaks, list):
                    self.waveform_peaks_by_track[track] = [float(value) for value in peaks]
                    self.waveform_peaks = self.waveform_peaks_by_track[track]
                    self.status_text.set(f"A{track + 1} waveform cache loaded")
                    self.log("waveform.cache.loaded", path=str(cache), track=track + 1, samples=len(self.waveform_peaks))
                    self.redraw_curve()
                    return
            except Exception:
                self.log("waveform.cache.failed", path=str(cache), track=track + 1)
        if not self.ffmpeg:
            self.status_text.set("ffmpeg not found; waveform unavailable")
            self.log("waveform.unavailable", reason="ffmpeg_not_found")
            return
        self.waveform_loading_by_track[track] = True
        self.waveform_loading = True
        self.status_text.set(f"Preparing A{track + 1} waveform")
        self.log("waveform.build.start", track=track + 1)
        threading.Thread(target=self.build_waveform_cache, args=(track,), daemon=True).start()

    def build_waveform_cache(self, track: int) -> None:
        peaks: list[float] = []
        try:
            args = [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-i",
                str(self.video_path),
                "-map",
                f"0:a:{track}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(WAVEFORM_RATE),
                "-f",
                "f32le",
                "-",
            ]
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if not proc.stdout:
                raise RuntimeError("ffmpeg produced no audio stream")
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                floats = array("f")
                floats.frombytes(chunk[: len(chunk) - (len(chunk) % 4)])
                peaks.extend(min(1.0, abs(sample)) for sample in floats)
            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"ffmpeg failed with exit code {code}")
            cache = self.waveform_cache_path(track)
            cache.write_text(json.dumps({"rate": WAVEFORM_RATE, "peaks": peaks}, separators=(",", ":")), encoding="utf-8")
            self.log("waveform.build.ok", path=str(cache), track=track + 1, samples=len(peaks))
            self.root.after(0, lambda: self.finish_waveform(track, peaks, f"A{track + 1} waveform ready"))
        except Exception as exc:
            self.log("waveform.build.failed", track=track + 1, error=str(exc))
            self.root.after(0, lambda exc=exc: self.finish_waveform(track, [], f"A{track + 1} waveform failed: {exc}"))

    def finish_waveform(self, track: int, peaks: list[float], message: str) -> None:
        if not 0 <= track < AUDIO_TRACK_COUNT:
            return
        self.waveform_loading_by_track[track] = False
        self.waveform_peaks_by_track[track] = peaks
        if track == self.selected_audio_track():
            self.waveform_loading = False
            self.waveform_peaks = peaks
        self.status_text.set(message)
        self.redraw_curve()

    def is_audio_axis(self) -> bool:
        return bool(self.active_axis and self.active_axis.get() == "audio")

    def current_points(self) -> list[dict[str, float]]:
        if self.is_audio_axis():
            return self.audio_points_by_track[self.selected_audio_track()]
        return self.curve_points

    def clone_points(self, points: list[dict[str, float]]) -> list[dict[str, float]]:
        key = self.current_value_key()
        return [
            {"time": float(point["time"]), key: float(point.get(key, 0.0))}
            for point in sorted(points, key=lambda item: item["time"])
        ]

    def set_current_points(self, points: list[dict[str, float]]) -> None:
        if self.is_audio_axis():
            primary = self.selected_audio_track()
            for track in self.selected_audio_tracks():
                self.audio_points_by_track[track] = self.clone_points(points)
            self.audio_points = self.audio_points_by_track[primary]
        else:
            self.curve_points = points

    def current_value_key(self) -> str:
        return "db" if self.is_audio_axis() else "ev"

    def current_value(self) -> float:
        return self.audio_db_by_track[self.selected_audio_track()] if self.is_audio_axis() else self.exposure

    def set_selected_audio_values(self, value: float) -> None:
        db = round(max(-24.0, min(12.0, float(value))) * 10.0) / 10.0
        for track in self.selected_audio_tracks():
            self.audio_db_by_track[track] = db
        self.audio_db = self.audio_db_by_track[self.selected_audio_track()]

    def current_unit(self) -> str:
        return "dB" if self.is_audio_axis() else "EV"

    def current_value_range(self) -> tuple[float, float]:
        return (-24.0, 12.0) if self.is_audio_axis() else (-4.0, 4.0)

    def clamp_current_value(self, value: float) -> float:
        lo, hi = self.current_value_range()
        clamped = max(lo, min(hi, float(value)))
        return round(clamped, 1 if self.is_audio_axis() else 3)

    def sorted_curve_points(self) -> list[dict[str, float]]:
        return sorted(self.current_points(), key=lambda item: item["time"])

    def curve_ev_at(self, seconds: float) -> float:
        return self.curve_value_at(seconds, self.curve_points, "ev", self.exposure)

    def audio_db_at(self, seconds: float, track: int | None = None) -> float:
        if track is None:
            track = self.selected_audio_track()
        track = max(0, min(AUDIO_TRACK_COUNT - 1, int(track)))
        return self.curve_value_at(seconds, self.audio_points_by_track[track], "db", self.audio_db_by_track[track])

    def current_curve_value_at(self, seconds: float) -> float:
        return self.audio_db_at(seconds) if self.is_audio_axis() else self.curve_ev_at(seconds)

    def normalize_audio_point(self, point: dict, fallback: float = 0.0) -> dict[str, float] | None:
        if "time" not in point:
            return None
        return {
            "time": max(0.0, min(self.duration, float(point["time"]))),
            "db": round(max(-24.0, min(12.0, float(point.get("db", fallback)))) * 10.0) / 10.0,
        }

    def normalize_audio_tracks_payload(self, payload: dict) -> list[list[dict[str, float]]]:
        tracks = [self.default_audio_points() for _ in range(AUDIO_TRACK_COUNT)]
        raw_tracks = payload.get("tracks")
        if isinstance(raw_tracks, list):
            for idx, track_payload in enumerate(raw_tracks[:AUDIO_TRACK_COUNT]):
                if isinstance(track_payload, dict):
                    raw_points = track_payload.get("points", [])
                else:
                    raw_points = track_payload
                loaded = [
                    point
                    for point in (
                        self.normalize_audio_point(raw_point)
                        for raw_point in raw_points
                        if isinstance(raw_point, dict)
                    )
                    if point is not None
                ]
                if loaded:
                    tracks[idx] = sorted(loaded, key=lambda item: item["time"])
            return tracks

        raw_points = payload.get("points", [])
        loaded = [
            point
            for point in (
                self.normalize_audio_point(raw_point)
                for raw_point in raw_points
                if isinstance(raw_point, dict)
            )
            if point is not None
        ]
        if loaded:
            tracks[0] = sorted(loaded, key=lambda item: item["time"])
        return tracks

    def audio_tracks_payload(self) -> list[dict[str, object]]:
        return [
            {
                "index": idx,
                "name": f"A{idx + 1}",
                "points": sorted(points, key=lambda item: item["time"]),
            }
            for idx, points in enumerate(self.audio_points_by_track)
        ]

    def write_mpv_curve_state(self) -> None:
        MPV_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        enabled = "1" if self.curve_enabled and self.curve_enabled.get() else "0"
        lines = [f"enabled={enabled}"]
        points = sorted(self.curve_points, key=lambda item: item["time"])
        if not points:
            points = [{"time": 0.0, "ev": self.exposure}]
        for point in points:
            t = max(0.0, min(self.duration, float(point["time"])))
            ev = max(-4.0, min(4.0, float(point.get("ev", 0.0))))
            lines.append(f"{t:.6f}\t{ev:.6f}")
        tmp_path = MPV_CURVE_STATE.with_suffix(".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="ascii")
        os.replace(tmp_path, MPV_CURVE_STATE)
        self.log_drag("curve.runtime.write", enabled=enabled, points=len(points), path=str(MPV_CURVE_STATE))

    def sync_mpv_curve_state(self) -> None:
        self.write_mpv_curve_state()
        if self.process and self.process.poll() is None:
            current_exposure = self.exposure
            reply = self.command(["script-message", "reload-exposure-curve"])
            if reply and reply.get("error") != "success":
                self.exposure = current_exposure
                self.flush_exposure()
            else:
                self.log_drag("curve.runtime.reload", reply=reply)

    def curve_value_at(self, seconds: float, points: list[dict[str, float]], key: str, fallback: float) -> float:
        seconds = round(max(0.0, min(self.duration, seconds)) * VIDEO_FPS) / VIDEO_FPS
        points = sorted(points, key=lambda item: item["time"])
        if not points:
            return fallback
        if seconds <= points[0]["time"]:
            return points[0][key]
        for left, right in zip(points, points[1:]):
            if seconds <= right["time"]:
                span = max(0.001, right["time"] - left["time"])
                u = max(0.0, min(1.0, (seconds - left["time"]) / span))
                smooth = u * u * (3.0 - 2.0 * u)
                return left[key] + (right[key] - left[key]) * smooth
        return points[-1][key]

    def format_time(self, seconds: float) -> str:
        seconds = max(0.0, seconds)
        minute = int(seconds // 60)
        sec = seconds - minute * 60
        return f"{minute:02d}:{sec:05.2f}"

    def display_time(self, seconds: float, editing: bool = False) -> None:
        seconds = max(0.0, min(self.duration, seconds))
        frame = round(seconds * VIDEO_FPS)
        axis = f"Audio {self.audio_track_label()}" if self.is_audio_axis() else "Video"
        value = self.current_value()
        precision = 1 if self.is_audio_axis() else 2
        self.timeline_text.set(
            f"{axis}   {self.format_time(seconds)} / {self.format_time(self.duration)}   "
            f"Frame {frame:d}   {value:+.{precision}f} {self.current_unit()}"
        )

    def seek_to(self, seconds: float) -> None:
        target = max(0.0, min(self.duration, seconds))
        self.log("seek.direct", from_time=self.current_time, to_time=target)
        self.current_time = target
        self.display_time(target)
        self.queue_seek(target, immediate=True)
        if self.curve_enabled.get():
            self.apply_curve_now()
        self.redraw_curve()

    def preview_time_at(self, seconds: float, send_seek: bool = True) -> None:
        target = max(0.0, min(self.duration, seconds))
        self.log_drag("seek.preview", to_time=target, send_seek=send_seek)
        self.current_time = target
        self.display_time(target)
        if send_seek:
            self.queue_seek(target)

    def queue_seek(self, seconds: float, immediate: bool = False) -> None:
        target = max(0.0, min(self.duration, seconds))
        self.log_drag("seek.queue", to_time=target, immediate=immediate)
        self.pending_seek_time = target
        if immediate:
            self.flush_seek()
            return
        if self.seek_worker_busy:
            return
        elapsed_ms = (time.monotonic() - self.last_seek_sent_at) * 1000.0
        delay = 0 if elapsed_ms >= 45 else int(45 - elapsed_ms)
        self.seek_worker_busy = True
        self.root.after(delay, self.flush_seek)

    def flush_seek(self) -> None:
        target = self.pending_seek_time
        self.pending_seek_time = None
        self.seek_worker_busy = False
        if target is None:
            return
        self.last_seek_sent_at = time.monotonic()
        self.log_drag("seek.flush", to_time=target)
        self.command(["set_property", "time-pos", target])
        if self.pending_seek_time is not None:
            self.queue_seek(self.pending_seek_time)

    def refresh_point_list(self) -> None:
        if not hasattr(self, "point_list"):
            return
        if self.dragging_curve_point:
            return
        selection = self.point_list.curselection()
        selected_text = self.point_list.get(selection[0]) if selection else None
        self.suppress_point_select = True
        try:
            self.point_list.delete(0, tk.END)
            key = self.current_value_key()
            unit = self.current_unit()
            precision = 1 if self.is_audio_axis() else 2
            for point in self.sorted_curve_points():
                self.point_list.insert(tk.END, f"{self.format_time(point['time'])}   {point[key]:+.{precision}f} {unit}")
            if selected_text:
                for idx in range(self.point_list.size()):
                    if self.point_list.get(idx) == selected_text:
                        self.point_list.selection_set(idx)
                        break
        finally:
            self.root.after_idle(lambda: setattr(self, "suppress_point_select", False))

    def redraw_curve(self) -> None:
        if not hasattr(self, "timeline"):
            return
        self.update_range_scale()
        canvas = self.timeline
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        pad_x = 12
        ruler_top = height - 22
        ruler_y = height - 13
        start, end = self.timeline_bounds()
        mid_y = (ruler_top + 6) / 2
        canvas.create_rectangle(pad_x, ruler_top, width - pad_x, height - 1, fill="#d3e0ec", outline="")
        canvas.create_line(pad_x, ruler_y, width - pad_x, ruler_y, fill="#a8bdd0", width=3, capstyle=tk.ROUND)
        if self.is_audio_axis():
            self.draw_waveform(canvas, width, height, start, end, pad_x, ruler_top)
        canvas.create_line(pad_x, mid_y, width - pad_x, mid_y, fill="#9fb4c8")
        lo, hi = self.current_value_range()
        grid_values = [lo, (lo + hi) / 2, hi] if self.is_audio_axis() else [-4, -2, 0, 2, 4]
        for value in grid_values:
            y = self.value_to_y(value, height)
            canvas.create_line(pad_x, y, width - pad_x, y, fill="#cfdae5")
        for tick in self.timeline_ticks(start, end):
            x = self.time_to_x(tick, width)
            canvas.create_line(x, ruler_y - 6, x, ruler_y + 5, fill="#8ea7bc")
            canvas.create_text(x, height - 2, text=self.format_time(tick), fill="#52677d", font=("Segoe UI", 8), anchor="s")
        samples = []
        for i in range(width - 2 * pad_x):
            x = pad_x + i
            t = start + (i / max(1, width - 2 * pad_x - 1)) * (end - start)
            samples.append((x, self.value_to_y(self.current_curve_value_at(t), height)))
        if len(samples) > 1:
            line_color = "#0f5ba7" if not self.is_audio_axis() else "#147a55"
            canvas.create_line(*[coord for point in samples for coord in point], fill=line_color, width=2, smooth=True)
        for point in self.sorted_curve_points():
            if point["time"] < start or point["time"] > end:
                continue
            x = self.time_to_x(point["time"], width)
            y = self.value_to_y(point[self.current_value_key()], height)
            outline = "#0f5ba7" if not self.is_audio_axis() else "#147a55"
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ffffff", outline=outline, width=2)
        if start <= self.current_time <= end:
            play_x = self.time_to_x(self.current_time, width)
            canvas.create_line(play_x, 6, play_x, ruler_y, fill="#d35b2a", width=2)
            canvas.create_oval(play_x - 7, ruler_y - 15, play_x + 7, ruler_y - 1, fill="#d35b2a", outline="#ffffff", width=2)
            canvas.create_line(play_x, ruler_y - 1, play_x, ruler_y + 7, fill="#d35b2a", width=2)
        canvas.create_text(12, 12, text=f"{self.format_time(start)} - {self.format_time(end)}", fill="#52677d", font=("Segoe UI", 9), anchor="nw")
        self.refresh_point_list()

    def draw_waveform(self, canvas: tk.Canvas, width: int, height: int, start: float, end: float, pad_x: int, ruler_top: int) -> None:
        if not self.waveform_peaks:
            text = "Preparing waveform" if self.waveform_loading else "Waveform unavailable"
            canvas.create_text(width / 2, (ruler_top + 8) / 2, text=text, fill="#52677d", font=("Segoe UI", 9))
            return
        center_y = (ruler_top + 8) / 2
        amplitude_height = max(8, (ruler_top - 12) / 2)
        samples = self.waveform_peaks
        for x in range(pad_x, width - pad_x):
            t0 = start + ((x - pad_x) / max(1, width - 2 * pad_x)) * (end - start)
            t1 = start + ((x + 1 - pad_x) / max(1, width - 2 * pad_x)) * (end - start)
            i0 = max(0, min(len(samples) - 1, int(t0 * WAVEFORM_RATE)))
            i1 = max(i0 + 1, min(len(samples), int(math.ceil(t1 * WAVEFORM_RATE))))
            peak = max(samples[i0:i1]) if i1 > i0 else samples[i0]
            y0 = center_y - peak * amplitude_height
            y1 = center_y + peak * amplitude_height
            canvas.create_line(x, y0, x, y1, fill="#7fae9d")

    def time_to_x(self, seconds: float, width: int) -> float:
        start, end = self.timeline_bounds()
        return 12 + ((max(start, min(end, seconds)) - start) / max(0.001, end - start)) * max(1, width - 24)

    def x_to_time(self, x: float) -> float:
        width = max(1, self.timeline.winfo_width())
        start, end = self.timeline_bounds()
        return max(start, min(end, start + ((x - 12) / max(1, width - 24)) * (end - start)))

    def y_to_ev(self, y: float) -> float:
        return self.y_to_value(y)

    def y_to_value(self, y: float) -> float:
        height = max(1, self.timeline.winfo_height())
        curve_bottom = max(20, height - 28)
        lo, hi = self.current_value_range()
        value = ((curve_bottom - y) / max(1, curve_bottom - 8)) * (hi - lo) + lo
        return self.clamp_current_value(value)

    def timeline_bounds(self) -> tuple[float, float]:
        span = max(FRAME_STEP, min(self.duration, self.timeline_window))
        center = max(0.0, min(self.duration, self.timeline_center))
        start = center - span / 2
        end = center + span / 2
        if start < 0:
            end -= start
            start = 0.0
        if end > self.duration:
            start -= end - self.duration
            end = self.duration
        return max(0.0, start), min(self.duration, end)

    def timeline_start_max(self) -> float:
        span = max(FRAME_STEP, min(self.duration, self.timeline_window))
        return max(0.0, self.duration - span)

    def timeline_start(self) -> float:
        start, _end = self.timeline_bounds()
        return start

    def set_timeline_start(self, start: float) -> None:
        span = max(FRAME_STEP, min(self.duration, self.timeline_window))
        max_start = self.timeline_start_max()
        clamped = max(0.0, min(max_start, start))
        self.timeline_center = clamped + span / 2
        self.update_range_scale()
        self.redraw_curve()

    def update_range_scale(self) -> None:
        if not hasattr(self, "range_scale"):
            return
        max_start = self.timeline_start_max()
        self.syncing_range_scale = True
        try:
            self.range_scale.configure(to=max_start if max_start > 0 else 1.0)
            self.range_scale.set(self.timeline_start() if max_start > 0 else 0.0)
        finally:
            self.root.after_idle(lambda: setattr(self, "syncing_range_scale", False))

    def on_range_scale(self, value: str) -> None:
        if self.syncing_range_scale:
            return
        try:
            start = float(value)
        except ValueError:
            return
        span = max(FRAME_STEP, min(self.duration, self.timeline_window))
        self.timeline_center = max(0.0, min(self.timeline_start_max(), start)) + span / 2
        self.redraw_curve()

    def timeline_ticks(self, start: float, end: float) -> list[float]:
        span = max(0.001, end - start)
        if span <= 5:
            step = 0.5
        elif span <= 10:
            step = 1.0
        elif span <= 30:
            step = 5.0
        else:
            step = 30.0
        first = (int(start / step) + 1) * step
        ticks = []
        value = first
        while value < end:
            ticks.append(value)
            value += step
        return ticks

    def set_timeline_window(self, seconds: float) -> None:
        start = self.timeline_start()
        self.timeline_window = max(FRAME_STEP, min(self.duration, seconds))
        self.set_timeline_start(start)
        self.redraw_curve()

    def center_timeline(self) -> None:
        self.timeline_center = self.current_time
        self.update_range_scale()
        self.redraw_curve()

    def nudge_time(self, frames: int) -> None:
        self.seek_to(self.current_time + frames / VIDEO_FPS)

    def nudge_time_seconds(self, seconds: float) -> None:
        self.seek_to(self.current_time + seconds)

    def ev_to_y(self, ev: float, height: int) -> float:
        return self.value_to_y(ev, height)

    def value_to_y(self, value: float, height: int) -> float:
        curve_bottom = max(20, height - 28)
        lo, hi = self.current_value_range()
        clamped = max(lo, min(hi, value))
        return curve_bottom - ((clamped - lo) / max(0.001, hi - lo)) * max(1, curve_bottom - 8)

    def timeline_ruler_top(self) -> int:
        return max(0, self.timeline.winfo_height() - 22)

    def timeline_ruler_y(self) -> int:
        return max(0, self.timeline.winfo_height() - 13)

    def is_playhead_handle_hit(self, event: tk.Event) -> bool:
        start, end = self.timeline_bounds()
        if not start <= self.current_time <= end:
            return False
        play_x = self.time_to_x(self.current_time, max(1, self.timeline.winfo_width()))
        ruler_y = self.timeline_ruler_y()
        return abs(event.x - play_x) <= 12 and ruler_y - 20 <= event.y <= ruler_y + 9

    def is_timeline_ruler_hit(self, event: tk.Event) -> bool:
        return event.y >= self.timeline_ruler_top()

    def nearest_point_index(self, seconds: float, max_distance: float | None = None) -> int | None:
        if max_distance is None:
            max_distance = max(0.08, (self.timeline_bounds()[1] - self.timeline_bounds()[0]) * 0.02)
        points = self.sorted_curve_points()
        best = None
        best_distance = max_distance
        for idx, point in enumerate(points):
            distance = abs(point["time"] - seconds)
            if distance <= best_distance:
                best = idx
                best_distance = distance
        return best

    def hit_point_index(self, event: tk.Event, radius: float = 9.0) -> int | None:
        width = max(1, self.timeline.winfo_width())
        height = max(1, self.timeline.winfo_height())
        start, end = self.timeline_bounds()
        best: int | None = None
        best_distance = radius
        for idx, point in enumerate(self.sorted_curve_points()):
            if point["time"] < start or point["time"] > end:
                continue
            x = self.time_to_x(point["time"], width)
            y = self.value_to_y(point[self.current_value_key()], height)
            distance = math.hypot(event.x - x, event.y - y)
            if distance <= best_distance:
                best = idx
                best_distance = distance
        return best

    def on_timeline_click(self, event: tk.Event) -> None:
        seconds = self.x_to_time(event.x)
        if self.is_playhead_handle_hit(event) or self.is_timeline_ruler_hit(event):
            self.log("timeline.click.playhead", x=event.x, y=event.y, time=seconds)
            self.timeline_drag_mode = "playhead"
            self.dragging_curve_point = False
            self.drag_point_index = None
            self.seeking_timeline = True
            self.preview_time_at(seconds, send_seek=True)
            self.redraw_curve()
            return

        idx = self.hit_point_index(event)
        if idx is not None:
            self.log("timeline.click.point", index=idx, x=event.x, y=event.y, time=seconds)
            self.timeline_drag_mode = "point"
            self.dragging_curve_point = True
            self.drag_point_index = idx
            self.select_point_index(idx)
            point = self.sorted_curve_points()[idx]
            self.set_axis_value(point[self.current_value_key()])
            self.preview_time_at(point["time"], send_seek=True)
        else:
            self.log("timeline.click.empty", x=event.x, y=event.y, time=seconds)
            self.timeline_drag_mode = None
            self.dragging_curve_point = False
            self.drag_point_index = None
            self.seek_to(seconds)
        self.redraw_curve()

    def on_timeline_ctrl_click(self, event: tk.Event) -> str:
        seconds = self.x_to_time(event.x)
        value = self.y_to_value(event.y)
        if self.is_timeline_ruler_hit(event):
            value = self.current_curve_value_at(seconds)
        points = self.sorted_curve_points()
        idx = self.nearest_point_index(seconds, max_distance=FRAME_STEP / 2)
        key = self.current_value_key()
        if idx is not None:
            self.log(
                "curve.point.update",
                axis=self.active_axis.get(),
                tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
                index=idx,
                time=seconds,
                value=value,
            )
            points[idx]["time"] = seconds
            points[idx][key] = value
        else:
            self.log(
                "curve.point.add",
                axis=self.active_axis.get(),
                tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
                time=seconds,
                value=value,
            )
            points.append({"time": seconds, key: value})
        self.set_current_points(sorted(points, key=lambda item: item["time"]))
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        selected_idx = min(
            range(len(self.current_points())),
            key=lambda point_idx: abs(self.current_points()[point_idx]["time"] - seconds),
        )
        self.save_curve()
        self.select_point_index(selected_idx)
        self.seek_to(seconds)
        self.set_axis_value(value)
        self.redraw_curve()
        return "break"

    def on_timeline_right_click(self, event: tk.Event) -> str:
        idx = self.hit_point_index(event, radius=10.0)
        if idx is None:
            self.log("timeline.right_click.empty", x=event.x, y=event.y)
            return "break"
        points = self.sorted_curve_points()
        if len(points) <= 1:
            return "break"
        del points[idx]
        self.log(
            "curve.point.delete",
            axis=self.active_axis.get(),
            tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
            index=idx,
        )
        self.set_current_points(points)
        self.dragging_curve_point = False
        self.drag_point_index = None
        self.timeline_drag_mode = None
        self.save_curve()
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        self.redraw_curve()
        return "break"

    def on_timeline_drag(self, event: tk.Event) -> None:
        if self.timeline_drag_mode == "playhead":
            seconds = self.x_to_time(event.x)
            self.log_drag("timeline.drag.playhead", x=event.x, y=event.y, time=seconds)
            self.seeking_timeline = True
            self.preview_time_at(seconds, send_seek=True)
            if not self.drag_redraw_pending:
                self.drag_redraw_pending = True
                self.root.after(16, self.flush_drag_redraw)
            return

        idx = self.drag_point_index
        if idx is None:
            selection = self.point_list.curselection()
            if not selection:
                return
            idx = selection[0]
            self.drag_point_index = idx
        self.dragging_curve_point = True
        points = self.sorted_curve_points()
        if idx >= len(points):
            return
        point = points[idx]
        point["time"] = self.x_to_time(event.x)
        point[self.current_value_key()] = self.y_to_value(event.y)
        self.log_drag(
            "timeline.drag.point",
            axis=self.active_axis.get(),
            tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
            index=idx,
            time=point["time"],
            value=point[self.current_value_key()],
        )
        self.set_current_points(points)
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        self.preview_time_at(point["time"], send_seek=True)
        self.set_axis_value(point[self.current_value_key()], send=True)
        if not self.drag_redraw_pending:
            self.drag_redraw_pending = True
            self.root.after(16, self.flush_drag_redraw)

    def flush_drag_redraw(self) -> None:
        self.drag_redraw_pending = False
        if self.dragging_curve_point or self.timeline_drag_mode == "playhead":
            self.redraw_curve()

    def on_timeline_release(self, _event: tk.Event) -> None:
        if self.timeline_drag_mode == "playhead":
            self.log("timeline.release.playhead", time=self.current_time)
            self.timeline_drag_mode = None
            self.seeking_timeline = False
            self.seek_to(self.current_time)
            return
        if not self.dragging_curve_point:
            self.timeline_drag_mode = None
            return
        self.log("timeline.release.point", index=self.drag_point_index, time=self.current_time)
        self.dragging_curve_point = False
        self.timeline_drag_mode = None
        points = self.sorted_curve_points()
        idx = self.drag_point_index
        self.drag_point_index = None
        self.set_current_points(points)
        self.save_curve()
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        if idx is not None and idx < len(points):
            point = points[idx]
            self.select_point_index(idx)
            self.seek_to(point["time"])
            self.applying_curve = True
            try:
                self.set_axis_value(point[self.current_value_key()])
            finally:
                self.applying_curve = False
        self.redraw_curve()

    def on_point_select(self, _event: tk.Event) -> None:
        if self.suppress_point_select:
            self.log_drag("point_list.select.suppressed")
            return
        selection = self.point_list.curselection()
        if not selection:
            return
        point = self.sorted_curve_points()[selection[0]]
        self.log("point_list.select", axis=self.active_axis.get(), index=selection[0], time=point["time"])
        self.set_axis_value(point[self.current_value_key()])
        self.seek_to(point["time"])
        self.redraw_curve()

    def select_point_index(self, idx: int) -> None:
        if not hasattr(self, "point_list"):
            return
        self.suppress_point_select = True
        try:
            self.point_list.selection_clear(0, tk.END)
            if 0 <= idx < self.point_list.size():
                self.point_list.selection_set(idx)
        finally:
            self.root.after_idle(lambda: setattr(self, "suppress_point_select", False))

    def add_or_update_point(self) -> None:
        seconds = self.current_time
        points = self.sorted_curve_points()
        idx = self.nearest_point_index(seconds, max_distance=0.5)
        key = self.current_value_key()
        if idx is not None:
            self.log(
                "curve.point.update_current",
                axis=self.active_axis.get(),
                tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
                index=idx,
                time=seconds,
                value=self.current_value(),
            )
            points[idx]["time"] = seconds
            points[idx][key] = self.current_value()
        else:
            self.log(
                "curve.point.add_current",
                axis=self.active_axis.get(),
                tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
                time=seconds,
                value=self.current_value(),
            )
            points.append({"time": seconds, key: self.current_value()})
        self.set_current_points(sorted(points, key=lambda item: item["time"]))
        self.save_curve()
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        self.redraw_curve()

    def delete_selected_point(self) -> None:
        selection = self.point_list.curselection()
        if not selection:
            return
        points = self.sorted_curve_points()
        if len(points) <= 1:
            return
        self.log(
            "curve.point.delete_selected",
            axis=self.active_axis.get(),
            tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
            index=selection[0],
        )
        del points[selection[0]]
        self.set_current_points(points)
        self.save_curve()
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        self.redraw_curve()

    def update_selected_point_ev(self) -> None:
        selection = self.point_list.curselection()
        if not selection:
            return
        points = self.sorted_curve_points()
        idx = selection[0]
        if idx >= len(points):
            return
        self.log(
            "curve.point.value_update_selected",
            axis=self.active_axis.get(),
            tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
            index=idx,
            value=self.current_value(),
        )
        points[idx][self.current_value_key()] = self.current_value()
        self.set_current_points(points)
        self.save_curve()
        if not self.is_audio_axis():
            self.sync_mpv_curve_state()
        self.redraw_curve()

    def apply_curve_now(self) -> None:
        self.log(
            "curve.apply_now",
            enabled=self.curve_enabled.get(),
            axis=self.active_axis.get(),
            audio_track=self.selected_audio_track() + 1,
            audio_tracks=[track + 1 for track in self.selected_audio_tracks()] if self.is_audio_axis() else [],
            time=self.current_time,
        )
        if self.curve_enabled.get():
            self.applying_curve = True
            try:
                if self.is_audio_axis():
                    self.set_audio_db(self.audio_db_at(self.current_time, self.selected_audio_track()))
                else:
                    self.set_exposure(self.curve_ev_at(self.current_time), send=False)
            finally:
                self.applying_curve = False
        self.sync_mpv_curve_state()

    def save_curve(self) -> None:
        payload = {"version": 1, "duration": self.duration, "points": sorted(self.curve_points, key=lambda item: item["time"])}
        CURVE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        audio_payload = {
            "version": 2,
            "duration": self.duration,
            "active_track": self.selected_audio_track(),
            "selected_tracks": self.selected_audio_tracks(),
            "tracks": self.audio_tracks_payload(),
        }
        AUDIO_CURVE_PATH.write_text(json.dumps(audio_payload, indent=2), encoding="utf-8")
        self.log(
            "curve.save",
            video_points=len(self.curve_points),
            audio_points_by_track=[len(points) for points in self.audio_points_by_track],
            curve_path=str(CURVE_PATH),
            audio_curve_path=str(AUDIO_CURVE_PATH),
        )
        self.write_mpv_curve_state()

    def load_curves(self) -> None:
        try:
            if CURVE_PATH.exists():
                payload = json.loads(CURVE_PATH.read_text(encoding="utf-8"))
                points = payload.get("points", [])
                loaded = [
                    {"time": float(point["time"]), "ev": float(point.get("ev", 0.0))}
                    for point in points
                    if "time" in point
                ]
                if loaded:
                    self.curve_points = sorted(loaded, key=lambda item: item["time"])
        except Exception:
            pass
        try:
            if AUDIO_CURVE_PATH.exists():
                payload = json.loads(AUDIO_CURVE_PATH.read_text(encoding="utf-8"))
                if self.active_audio_track and "active_track" in payload:
                    self.active_audio_track.set(max(0, min(AUDIO_TRACK_COUNT - 1, int(payload.get("active_track", 0)))))
                selected_tracks = payload.get("selected_tracks")
                if isinstance(selected_tracks, list) and self.audio_track_vars:
                    normalized = {
                        max(0, min(AUDIO_TRACK_COUNT - 1, int(track)))
                        for track in selected_tracks
                    }
                    if normalized:
                        for idx, variable in enumerate(self.audio_track_vars[:AUDIO_TRACK_COUNT]):
                            variable.set(idx in normalized)
                self.audio_points_by_track = self.normalize_audio_tracks_payload(payload)
                track = self.selected_audio_track()
                self.audio_points = self.audio_points_by_track[track]
                self.audio_db_by_track = [self.audio_db_at(0.0, idx) for idx in range(AUDIO_TRACK_COUNT)]
                self.audio_db = self.audio_db_by_track[track]
                self.log("curve.audio.load", audio_points_by_track=[len(points) for points in self.audio_points_by_track])
        except Exception as exc:
            self.log("curve.audio.load_failed", error=str(exc))

    def workspace_payload(self) -> dict:
        return {
            "version": 2,
            "video": str(self.video_path),
            "lut": str(LUT),
            "duration": self.duration,
            "current_time": self.current_time,
            "timeline_window": self.timeline_window,
            "timeline_start": self.timeline_start(),
            "active_axis": self.active_axis.get() if self.active_axis else "video",
            "active_audio_track": self.selected_audio_track(),
            "selected_audio_tracks": self.selected_audio_tracks(),
            "curve_enabled": bool(self.curve_enabled.get()) if self.curve_enabled else True,
            "export_quality": self.export_quality.get() if self.export_quality else "HEVC NVENC HQ",
            "exposure": self.exposure,
            "audio_db": self.audio_db_by_track[self.selected_audio_track()],
            "audio_db_by_track": self.audio_db_by_track,
            "video_points": sorted(self.curve_points, key=lambda item: item["time"]),
            "audio_tracks": self.audio_tracks_payload(),
        }

    def export_workspace(self) -> None:
        default_name = f"{self.video_path.stem}-workspace.json"
        self.log("workspace.save.dialog")
        path = filedialog.asksaveasfilename(
            title="Save workspace state",
            initialdir=str(ROOT / "exports"),
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("Workspace JSON", "*.json"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            self.log("workspace.save.cancel")
            return
        payload = self.workspace_payload()
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.status_text.set(f"Workspace saved: {Path(path).name}")
        self.log("workspace.save.ok", path=path)

    def restore_workspace(self) -> None:
        self.log("workspace.restore.dialog")
        path = filedialog.askopenfilename(
            title="Restore workspace state",
            initialdir=str(ROOT / "exports"),
            filetypes=[("Workspace JSON", "*.json"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            self.log("workspace.restore.cancel")
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            workspace_video = payload.get("video")
            if workspace_video:
                workspace_video_path = Path(str(workspace_video)).expanduser()
                if workspace_video_path.exists() and workspace_video_path.resolve() != self.video_path.resolve():
                    self.log("workspace.restore.video_switch", path=str(workspace_video_path))
                    self.stop()
                    self.video_path = workspace_video_path.resolve()
                    self.current_time = 0.0
                    self.waveform_peaks_by_track = [[] for _ in range(AUDIO_TRACK_COUNT)]
                    self.waveform_loading_by_track = [False for _ in range(AUDIO_TRACK_COUNT)]
            video_points = payload.get("video_points", payload.get("points", []))
            loaded_video = [
                {"time": float(point["time"]), "ev": float(point.get("ev", 0.0))}
                for point in video_points
                if "time" in point
            ]
            if loaded_video:
                self.curve_points = sorted(loaded_video, key=lambda item: item["time"])
            if "audio_tracks" in payload:
                self.audio_points_by_track = self.normalize_audio_tracks_payload({"tracks": payload.get("audio_tracks", [])})
            elif "audio_points" in payload:
                self.audio_points_by_track = self.normalize_audio_tracks_payload({"points": payload.get("audio_points", [])})
            self.duration = float(payload.get("duration", self.duration))
            self.timeline_window = max(FRAME_STEP, min(self.duration, float(payload.get("timeline_window", self.timeline_window))))
            self.set_timeline_start(float(payload.get("timeline_start", self.timeline_start())))
            if self.active_axis:
                self.active_axis.set(str(payload.get("active_axis", "video")))
            if self.active_audio_track:
                self.active_audio_track.set(max(0, min(AUDIO_TRACK_COUNT - 1, int(payload.get("active_audio_track", 0)))))
            selected_tracks = payload.get("selected_audio_tracks")
            if isinstance(selected_tracks, list) and self.audio_track_vars:
                normalized = {
                    max(0, min(AUDIO_TRACK_COUNT - 1, int(track)))
                    for track in selected_tracks
                }
                if normalized:
                    for idx, variable in enumerate(self.audio_track_vars[:AUDIO_TRACK_COUNT]):
                        variable.set(idx in normalized)
                else:
                    self.audio_track_vars[self.selected_audio_track()].set(True)
            if self.curve_enabled:
                self.curve_enabled.set(bool(payload.get("curve_enabled", True)))
            if self.export_quality:
                quality = str(payload.get("export_quality", self.export_quality.get()))
                if quality in EXPORT_PRESETS:
                    self.export_quality.set(quality)
            self.update_export_estimate()
            self.exposure = round(float(payload.get("exposure", self.exposure)), 3)
            raw_db = payload.get("audio_db_by_track")
            if isinstance(raw_db, list):
                for idx, value in enumerate(raw_db[:AUDIO_TRACK_COUNT]):
                    self.audio_db_by_track[idx] = round(max(-24.0, min(12.0, float(value))) * 10.0) / 10.0
            elif "audio_db" in payload:
                self.audio_db_by_track[self.selected_audio_track()] = round(max(-24.0, min(12.0, float(payload.get("audio_db", 0.0)))) * 10.0) / 10.0
            track = self.selected_audio_track()
            self.audio_points = self.audio_points_by_track[track]
            self.audio_db = self.audio_db_by_track[track]
            self.save_curve()
            self.seek_to(float(payload.get("current_time", self.current_time)))
            self.set_exposure(self.curve_ev_at(self.current_time))
            self.set_audio_db(self.audio_db_at(self.current_time, track))
            if self.is_audio_axis():
                self.ensure_waveform_async()
            self.update_export_estimate()
            self.redraw_curve()
            self.status_text.set(f"Workspace restored: {Path(path).name}")
            self.log(
                "workspace.restore.ok",
                path=path,
                video=str(self.video_path),
                video_points=len(self.curve_points),
                audio_points_by_track=[len(points) for points in self.audio_points_by_track],
                active_audio_track=track + 1,
            )
            if not self.process or self.process.poll() is not None:
                self.root.after(650, self.launch)
        except Exception as exc:
            self.status_text.set(f"Restore failed: {exc}")
            self.log("workspace.restore.failed", path=path, error=str(exc))

    def format_size(self, bytes_value: float) -> str:
        if bytes_value >= 1024 ** 3:
            return f"{bytes_value / (1024 ** 3):.1f} GB"
        return f"{bytes_value / (1024 ** 2):.0f} MB"

    def estimated_export_size(self, quality: str | None = None) -> float:
        selected = quality or (self.export_quality.get() if self.export_quality else "HEVC NVENC HQ")
        return max(0.0, self.duration) * EXPORT_BYTES_PER_SECOND.get(selected, EXPORT_BYTES_PER_SECOND["HEVC NVENC HQ"])

    def export_extension(self, quality: str) -> str:
        return ".mp4" if quality.startswith("AV1") else ".MOV"

    def export_group(self, quality: str) -> str:
        return EXPORT_PRESET_INFO.get(quality, ("HEVC / MOV", ""))[0]

    def export_note(self, quality: str) -> str:
        return EXPORT_PRESET_INFO.get(quality, ("HEVC / MOV", ""))[1]

    def update_export_estimate(self) -> None:
        if not self.export_estimate_text:
            return
        quality = self.export_quality.get() if self.export_quality else "HEVC NVENC HQ"
        size = self.format_size(self.estimated_export_size(quality))
        if quality.startswith("AV1"):
            note = "est. " + size + " | MP4, no tmcd"
        elif quality.endswith("Lossless"):
            note = "est. " + size + " | very slow"
        elif "x265" in quality:
            note = "est. " + size + " | CPU slow"
        else:
            note = "est. " + size + " | GPU"
        self.export_estimate_text.set(note)

    def open_export_dialog(self) -> None:
        if self.export_dialog and self.export_dialog.winfo_exists():
            self.export_dialog.lift()
            self.log("export.dialog.lift")
            return
        self.log("export.dialog.open")
        dialog = tk.Toplevel(self.root)
        self.export_dialog = dialog
        dialog.title("Export")
        dialog.geometry("900x430")
        dialog.minsize(760, 360)
        dialog.configure(bg="#eef3f8")
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=14, style="Glass.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Choose Export Format", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"Duration {self.format_time(self.duration)}   Video 3840x2160 50p 10-bit   Audio {AUDIO_TRACK_COUNT}x PCM 24-bit",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        columns = ("format", "preset", "estimate", "note")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=9, selectmode="browse")
        tree.heading("format", text="Format")
        tree.heading("preset", text="Quality")
        tree.heading("estimate", text="Estimated Size")
        tree.heading("note", text="Notes")
        tree.column("format", width=105, stretch=False)
        tree.column("preset", width=180, stretch=False)
        tree.column("estimate", width=130, stretch=False, anchor="e")
        tree.column("note", width=430, stretch=True)
        tree.pack(fill="both", expand=True)
        for quality in EXPORT_PRESETS:
            tree.insert(
                "",
                "end",
                iid=quality,
                values=(
                    self.export_group(quality),
                    quality,
                    self.format_size(self.estimated_export_size(quality)),
                    self.export_note(quality),
                ),
            )
        selected = self.export_quality.get() if self.export_quality else "HEVC NVENC HQ"
        if selected in EXPORT_PRESETS:
            tree.selection_set(selected)
            tree.focus(selected)

        self.export_progress_text = tk.StringVar(master=dialog, value=self.export_status_text())
        ttk.Label(frame, textvariable=self.export_progress_text, style="Sub.TLabel").pack(anchor="w", pady=(10, 0))

        buttons = ttk.Frame(frame, style="Glass.TFrame")
        buttons.pack(fill="x", pady=(10, 0))

        def choose_and_export() -> None:
            selection = tree.selection()
            if not selection:
                self.log("export.dialog.no_selection")
                return
            quality = selection[0]
            if self.export_quality:
                self.export_quality.set(quality)
            self.update_export_estimate()
            self.log("export.dialog.select", quality=quality, estimate=self.format_size(self.estimated_export_size(quality)))
            self.start_export(quality)

        ttk.Button(buttons, text="Export Selected", style="Accent.TButton", command=choose_and_export, takefocus=False).pack(side="right")
        ttk.Button(buttons, text="Cancel Export", command=self.cancel_export, takefocus=False).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Close", command=dialog.destroy, takefocus=False).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def export_status_text(self) -> str:
        if self.export_cancelled:
            return "Export cancelled"
        if self.export_process and self.export_process.poll() is None:
            name = self.export_output_path.name if self.export_output_path else "export"
            return f"Running: {name}"
        if self.export_output_path:
            return f"Last output: {self.export_output_path.name}"
        return "No export running"

    def set_axis_value(self, value: float, send: bool = True) -> None:
        if self.is_audio_axis():
            self.set_audio_db(value, send=send)
        else:
            if self.curve_enabled and self.curve_enabled.get() and self.timeline_drag_mode == "point":
                self.set_exposure(value, send=False)
                return
            self.set_exposure(value, send=send)

    def set_exposure(self, value: float, send: bool = True, update_scale: bool = True) -> None:
        self.exposure = round(max(-4.0, min(4.0, float(value))), 3)
        self.display_time(self.current_time)
        self.log_drag("exposure.set", value=self.exposure, send=send, time=self.current_time)
        if not send:
            return
        if self.last_sent_exposure == self.exposure:
            return

        self.status_text.set(f"Exposure {self.exposure:+.2f} EV")
        if not self.process or self.process.poll() is not None:
            self.status_text.set(f"Exposure staged at {self.exposure:+.2f} EV")
            return

        if not self.exposure_refresh_pending:
            self.exposure_refresh_pending = True
            self.root.after(EXPOSURE_REFRESH_MS, self.flush_exposure)

    def flush_exposure(self) -> None:
        self.exposure_refresh_pending = False
        if self.exposure_worker_busy:
            self.exposure_refresh_pending = True
            self.root.after(EXPOSURE_REFRESH_MS, self.flush_exposure)
            return
        if self.last_sent_exposure == self.exposure:
            return
        if not self.process or self.process.poll() is not None:
            self.status_text.set(f"Exposure staged at {self.exposure:+.2f} EV")
            return

        ev = self.exposure
        self.exposure_worker_busy = True
        self.log_drag("exposure.flush", value=ev)
        try:
            reply = win32_pipe_command({"command": ["set", "glsl-shader-opts", f"exposure={ev:.3f}"]})
            if reply and reply.get("error") == "success":
                self.last_sent_exposure = ev
                self.status_text.set(f"Exposure refreshed {ev:+.2f} EV")
                self.log_drag("exposure.flush.ok", value=ev)
            else:
                self.status_text.set(f"Exposure refresh failed: {reply}")
                self.log("exposure.flush.failed", value=ev, reply=reply)
        except Exception as exc:
            self.status_text.set(f"Exposure refresh failed: {exc}")
            self.log("exposure.flush.exception", value=ev, error=str(exc))
        finally:
            self.exposure_worker_busy = False
            if self.exposure != ev:
                self.exposure_refresh_pending = True
                self.root.after(EXPOSURE_REFRESH_MS, self.flush_exposure)

    def set_audio_db(self, value: float, send: bool = True) -> None:
        track = self.selected_audio_track()
        self.set_selected_audio_values(value)
        self.display_time(self.current_time)
        self.log_drag(
            "audio.set",
            tracks=[audio_track + 1 for audio_track in self.selected_audio_tracks()],
            primary=track + 1,
            value=self.audio_db,
            send=send,
            time=self.current_time,
        )
        if not send:
            return
        if self.last_sent_audio_db_by_track[track] == self.audio_db:
            return
        if not self.process or self.process.poll() is not None:
            self.status_text.set(f"A{track + 1} staged at {self.audio_db:+.1f} dB")
            return
        if not self.audio_refresh_pending:
            self.audio_refresh_pending = True
            self.root.after(AUDIO_REFRESH_MS, self.flush_audio_gain)

    def flush_audio_gain(self) -> None:
        self.audio_refresh_pending = False
        if self.audio_worker_busy:
            self.audio_refresh_pending = True
            self.root.after(AUDIO_REFRESH_MS, self.flush_audio_gain)
            return
        track = self.selected_audio_track()
        current_db = self.audio_db_by_track[track]
        self.audio_db = current_db
        if self.last_sent_audio_db_by_track[track] == current_db:
            return
        if not self.process or self.process.poll() is not None:
            return

        db = current_db
        volume = max(0.0, min(400.0, 100.0 * (10.0 ** (db / 20.0))))
        self.audio_worker_busy = True
        self.log_drag("audio.flush", track=track + 1, db=db, volume=volume)
        try:
            self.select_mpv_audio_track(track)
            reply = win32_pipe_command({"command": ["set_property", "volume", volume]})
            if reply and reply.get("error") == "success":
                self.last_sent_audio_db_by_track[track] = db
                self.last_sent_audio_db = db
                self.status_text.set(f"A{track + 1} {db:+.1f} dB")
                self.log_drag("audio.flush.ok", track=track + 1, db=db, volume=volume)
            else:
                self.status_text.set(f"Audio refresh failed: {reply}")
                self.log("audio.flush.failed", track=track + 1, db=db, reply=reply)
        except Exception as exc:
            self.status_text.set(f"Audio refresh failed: {exc}")
            self.log("audio.flush.exception", track=track + 1, db=db, error=str(exc))
        finally:
            self.audio_worker_busy = False
            if self.audio_db != db:
                self.audio_refresh_pending = True
                self.root.after(AUDIO_REFRESH_MS, self.flush_audio_gain)

    def refresh_metrics(self) -> None:
        if self.exposure_worker_busy or self.audio_worker_busy:
            self.root.after(1200, self.refresh_metrics)
            return
        if self.process and self.process.poll() is None:
            wanted = ["time-pos", "estimated-vf-fps", "hwdec-current", "frame-drop-count", "decoder-frame-drop-count"]
            values = {}
            for name in wanted:
                reply = self.command(["get_property", name])
                values[name] = reply.get("data") if reply and reply.get("error") == "success" else "?"
            self.metrics_text.set(
                f"Time {values['time-pos']} s   FPS {values['estimated-vf-fps']}   HW {values['hwdec-current']}   "
                f"Drop {values['frame-drop-count']}   Decoder {values['decoder-frame-drop-count']}"
            )
        else:
            self.metrics_text.set("mpv not running")
        self.root.after(2200, self.refresh_metrics)

    def refresh_playhead(self) -> None:
        if self.process and self.process.poll() is not None:
            code = self.process.poll()
            self.log("mpv.exited.detected", code=code, time=self.current_time)
            self.process = None
            self.status_text.set(f"mpv exited: {code}; press Launch")
        if self.dragging_curve_point:
            self.root.after(PLAYHEAD_REFRESH_MS, self.refresh_playhead)
            return
        if self.seeking_timeline:
            self.root.after(PLAYHEAD_REFRESH_MS, self.refresh_playhead)
            return
        if self.exposure_worker_busy or self.audio_worker_busy:
            self.root.after(PLAYHEAD_REFRESH_MS, self.refresh_playhead)
            return
        if self.process and self.process.poll() is None:
            reply = self.command(["get_property", "time-pos"])
            value = reply.get("data") if reply and reply.get("error") == "success" else None
            if isinstance(value, (int, float)):
                self.current_time = float(value)
                self.display_time(self.current_time)
                if self.curve_enabled.get():
                    self.applying_curve = True
                    try:
                        target = self.curve_ev_at(self.current_time)
                        if abs(target - self.exposure) >= 0.002:
                            self.set_exposure(target, send=False, update_scale=True)
                        track = self.selected_audio_track()
                        audio_target = self.audio_db_at(self.current_time, track)
                        if abs(audio_target - self.audio_db_by_track[track]) >= 0.05:
                            self.set_audio_db(audio_target)
                    finally:
                        self.applying_curve = False
                self.redraw_curve()
        self.root.after(PLAYHEAD_REFRESH_MS, self.refresh_playhead)

    def export_curve(self) -> None:
        self.open_export_dialog()

    def start_export(self, quality: str | None = None) -> None:
        if self.export_process and self.export_process.poll() is None:
            self.status_text.set("Export already running")
            self.log("export.start.skipped", reason="already_running", pid=self.export_process.pid)
            return
        self.save_curve()
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.export_cancelled = False
        quality = quality or (self.export_quality.get() if self.export_quality else "HEVC NVENC HQ")
        ext = self.export_extension(quality)
        quality_slug = quality.replace(" ", "-")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.export_output_path = EXPORTS_DIR / f"{self.video_path.stem}_{quality_slug}_{stamp}{ext}"
        self.export_log_path = EXPORTS_DIR / f"{self.video_path.stem}_{quality_slug}_{stamp}.log"
        script = ROOT / "export-vlog-exposure.ps1"
        args = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InputPath",
            str(self.video_path),
            "-CurvePath",
            str(CURVE_PATH),
            "-AudioCurvePath",
            str(AUDIO_CURVE_PATH),
            "-CurveStep",
            f"{FRAME_STEP:.12f}",
            "-Quality",
            quality,
            "-OutputPath",
            str(self.export_output_path),
        ]
        log = self.export_log_path.open("w", encoding="utf-8", errors="replace")
        self.log("export.start", quality=quality, output=str(self.export_output_path), log=str(self.export_log_path), args=args)
        self.export_process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        self.status_text.set(f"{quality} export started: {self.export_output_path.name}")
        if self.export_progress_text:
            self.export_progress_text.set(f"Running: {self.export_output_path.name}")
        self.root.after(1500, self.check_export)

    def check_export(self) -> None:
        if not self.export_process:
            return
        code = self.export_process.poll()
        if self.export_cancelled:
            if code is None:
                self.status_text.set("Cancelling export...")
                if self.export_progress_text:
                    self.export_progress_text.set("Cancelling export...")
                self.root.after(500, self.check_export)
            else:
                self.cleanup_cancelled_export()
                self.status_text.set("Export cancelled")
                if self.export_progress_text:
                    self.export_progress_text.set("Export cancelled")
            return
        if code is None:
            quality = self.export_quality.get() if self.export_quality else "Export"
            self.status_text.set(f"{quality} export running")
            self.log_drag("export.running", quality=quality, output=str(self.export_output_path) if self.export_output_path else "")
            if self.export_progress_text:
                self.export_progress_text.set(self.export_status_text())
            self.root.after(2500, self.check_export)
        elif code == 0:
            name = self.export_output_path.name if self.export_output_path else "output"
            self.status_text.set(f"Export finished: {name}")
            self.log("export.finished", code=code, output=str(self.export_output_path) if self.export_output_path else "")
            if self.export_progress_text:
                self.export_progress_text.set(f"Finished: {name}")
        else:
            log = self.export_log_path.name if self.export_log_path else "export log"
            self.status_text.set(f"Export failed: {code}; see {log}")
            self.log("export.failed", code=code, log=str(self.export_log_path) if self.export_log_path else "")
            if self.export_progress_text:
                self.export_progress_text.set(f"Failed: {code}; see {log}")

    def cleanup_cancelled_export(self) -> None:
        for path in [
            self.export_output_path,
            self.export_output_path.with_suffix(".ffprobe.json") if self.export_output_path else None,
            self.export_output_path.with_suffix(".sendcmd.txt") if self.export_output_path else None,
            self.export_output_path.with_suffix(".curve.json") if self.export_output_path else None,
            self.export_output_path.with_suffix(".audio-curve.json") if self.export_output_path else None,
        ]:
            if not path:
                continue
            try:
                if path.exists():
                    path.unlink()
                    self.log("export.cancel.cleanup", path=str(path))
            except OSError:
                self.log("export.cancel.cleanup_failed", path=str(path))

    def cancel_export(self) -> None:
        if not self.export_process or self.export_process.poll() is not None:
            self.export_cancelled = False
            self.status_text.set("No export running")
            self.log("export.cancel.skipped", reason="not_running")
            if self.export_progress_text:
                self.export_progress_text.set("No export running")
            return
        self.export_cancelled = True
        pid = self.export_process.pid
        self.log("export.cancel.request", pid=pid, output=str(self.export_output_path) if self.export_output_path else "")
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self.export_process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.status_text.set("Cancelling export...")
            self.log("export.cancel.wait_timeout", pid=pid)
            if self.export_progress_text:
                self.export_progress_text.set("Cancelling export...")
            return
        self.cleanup_cancelled_export()
        self.status_text.set("Export cancelled")
        self.log("export.cancelled", pid=pid)
        if self.export_progress_text:
            self.export_progress_text.set("Export cancelled")

    def on_close(self) -> None:
        self.log("app.close.request")
        self.cancel_export()
        self.stop()
        self.log("app.close.destroy")
        self.logger.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


class AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint32),
        ("AnimationId", ctypes.c_int),
    ]


class WindowCompositionAttributeData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


if __name__ == "__main__":
    JiangtherapeeVideoEditor().run()
