"""ffprobe / ffmpeg helpers."""
import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass
class MediaInfo:
    path: str
    width: int
    height: int
    fps: Fraction
    duration: float
    pix_fmt: str
    audio_index: int | None  # index among audio streams (for -map 0:a:N)
    audio_channels: int = 2
    audio_layout: str = "stereo"
    audio_rate: int = 48000
    audio_language: str = ""  # its language tag ("eng"), if it has one
    color: dict = field(default_factory=dict)
    # Where each stream starts, relative to the file's start. ffmpeg filters see this timeline,
    # so all plan times use it too.
    video_offset: float = 0.0
    audio_offset: float = 0.0

    @property
    def ten_bit(self) -> bool:
        return "10" in self.pix_fmt or "12" in self.pix_fmt

    @property
    def raw_pix_fmt(self) -> str:
        return "yuv420p10le" if self.ten_bit else "yuv420p"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def probe(path: str) -> MediaInfo:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path]).stdout
    data = json.loads(out)
    streams = data["streams"]
    video = next(s for s in streams if s["codec_type"] == "video"
                 and not s.get("disposition", {}).get("attached_pic"))
    audios = [s for s in streams if s["codec_type"] == "audio"]

    # Prefer the default audio track, else the first one.
    audio_index, audio = None, None
    for i, s in enumerate(audios):
        if s.get("disposition", {}).get("default"):
            audio_index, audio = i, s
            break
    if audio is None and audios:
        audio_index, audio = 0, audios[0]

    duration = float(data["format"].get("duration") or video.get("duration") or 0)
    info = MediaInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=Fraction(video.get("r_frame_rate") or video["avg_frame_rate"]),
        duration=duration,
        pix_fmt=video.get("pix_fmt", "yuv420p"),
        audio_index=audio_index,
    )
    file_start = float(data["format"].get("start_time") or 0)
    info.video_offset = max(0.0, float(video.get("start_time") or file_start) - file_start)
    if audio:
        info.audio_offset = max(0.0, float(audio.get("start_time") or file_start) - file_start)
        info.audio_channels = int(audio.get("channels", 2))
        info.audio_layout = audio.get("channel_layout") or ("stereo" if info.audio_channels == 2 else "mono")
        info.audio_rate = int(audio.get("sample_rate", 48000))
        info.audio_language = (audio.get("tags") or {}).get("language", "")
    for key in ("color_primaries", "color_transfer", "color_space", "color_range"):
        if video.get(key) and video[key] != "unknown":
            info.color[key] = video[key]
    return info


# Layouts with a center speaker (ffmpeg names). In films that's where the dialogue is.
CENTER_LAYOUTS = {"3.0", "3.1", "4.0", "4.1", "5.0", "5.0(side)", "5.1", "5.1(side)", "6.0", "6.1", "6.1(back)",
                  "7.0", "7.0(front)", "7.1", "7.1(wide)", "7.1(wide-side)", "3.1.2", "5.1.2", "5.1.4", "7.1.2",
                  "7.1.4", "7.2.3", "9.1.4", "hexagonal", "octagonal", "hexadecagonal", "22.2"}


def has_center(info: MediaInfo) -> bool:
    return info.audio_channels >= 3 and info.audio_layout in CENTER_LAYOUTS


def extract_speech_audio(info: MediaInfo, wav_path: str) -> None:
    """16 kHz mono WAV of the main audio track, for speech recognition. Surround tracks keep
    only the center speaker, where the dialogue is: with the music and explosions left out,
    Whisper caught 90 instead of 80 of Deadpool's 138 swears."""
    af = ["-af", "pan=mono|c0=FC"] if has_center(info) else ["-ac", "1"]
    run(["ffmpeg", "-y", "-v", "error", "-i", info.path, "-map", f"0:a:{info.audio_index}",
         *af, "-ar", "16000", "-c:a", "pcm_s16le", wav_path])


_encoder_cache: dict[str, bool] = {}


def encoder_works(name: str) -> bool:
    """True if ffmpeg can actually open this encoder (e.g. NVENC needs a GPU)."""
    if name not in _encoder_cache:
        try:
            run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
                 "-frames:v", "1", "-c:v", name, "-f", "null", "-"])
            _encoder_cache[name] = True
        except subprocess.CalledProcessError:
            _encoder_cache[name] = False
    return _encoder_cache[name]


def onnx_providers() -> list[str]:
    """ONNX Runtime providers to use: the NVIDIA card if there really is one, else the processor.
    (The GPU build lists CUDA even on PCs without an NVIDIA card, and then crashes.)"""
    import ctranslate2
    import onnxruntime as ort

    gpu = ctranslate2.get_cuda_device_count() > 0 and "CUDAExecutionProvider" in ort.get_available_providers()
    return ["CUDAExecutionProvider", "CPUExecutionProvider"] if gpu else ["CPUExecutionProvider"]
