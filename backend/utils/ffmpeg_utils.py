"""
FFmpeg 可执行路径解析工具

优先顺序：
1) 环境变量 TIKCLIP_FFMPEG_PATH / TIKCLIP_FFPROBE_PATH / FFMPEG_PATH / FFPROBE_PATH
2) 系统 PATH 中的 ffmpeg/ffprobe

用途：统一为后端所有调用点提供 ffmpeg/ffprobe 路径，便于在桌面安装包内置二进制并实现零依赖。
"""

import os
import shutil
from pathlib import Path
from typing import Optional


def _resolve_from_env(var_names: list[str]) -> Optional[str]:
    for var in var_names:
        value = os.getenv(var)
        if value and os.path.exists(value):
            return value
    return None


def get_ffmpeg_path() -> str:
    """返回 ffmpeg 可执行文件路径（或命令名）。"""
    # 1) 环境变量优先
    env_path = _resolve_from_env([
        "TIKCLIP_FFMPEG_PATH",
        "FFMPEG_PATH",
    ])
    if env_path:
        return env_path

    # 2) 系统 PATH
    which = shutil.which("ffmpeg")
    if which:
        return which

    # 3) Executável embutido em resources/ffmpeg
    bundled = Path(__file__).resolve().parents[2] / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if bundled.is_file():
        return str(bundled)

    # 4) 兜底返回命令名（可能仍会失败，但保留兼容性）
    return "ffmpeg"


def get_ffprobe_path() -> str:
    """返回 ffprobe 可执行文件路径（或命令名）。"""
    # 1) 环境变量优先
    env_path = _resolve_from_env([
        "TIKCLIP_FFPROBE_PATH",
        "FFPROBE_PATH",
    ])
    if env_path:
        return env_path

    # 2) 系统 PATH
    which = shutil.which("ffprobe")
    if which:
        return which

    # 3) Executável embutido em resources/ffmpeg
    bundled = Path(__file__).resolve().parents[2] / "ffmpeg" / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if bundled.is_file():
        return str(bundled)

    # 4) 兜底返回命令名
    return "ffprobe"




JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def ytdlp_js_runtimes() -> dict:
    """YouTube 现在要先解 JS 校验（yt-dlp-ejs）才能拿到视频流。

    yt-dlp 默认只启用 deno，用户机器上多半没有；装了 node / bun / quickjs 也会被当成不可用，
    报 "The page needs to be reloaded"。把本机能找到的运行时都交给它，按顺序择优使用。
    """
    found = {name: {"path": path} for name in JS_RUNTIMES if (path := shutil.which(name))}
    return {"js_runtimes": found} if found else {}


def ytdlp_ffmpeg_options() -> dict:
    """给 yt-dlp 的外部工具选项：ffmpeg 位置，以及可用的 JS 运行时。

    yt-dlp 只在 PATH 里找 ffmpeg，不读 TIKCLIP_FFMPEG_PATH。桌面安装包的 ffmpeg 不在 PATH 上，
    Windows 上合并 bestvideo+bestaudio、转字幕格式都会失败，导入就卡住或找不到 mp4。
    只有解析到真实文件时才传，否则交给 yt-dlp 自己找。
    """
    options = ytdlp_js_runtimes()
    path = get_ffmpeg_path()
    if os.path.isabs(path) and os.path.exists(path):
        options["ffmpeg_location"] = path
    return options


def is_nvenc_available() -> bool:
    """Verifica se o codificador NVIDIA NVENC está disponível via hardware."""
    try:
        from backend.services.video_encoder import encoder
        return encoder() == 'h264_nvenc'
    except Exception:
        return False


def get_h264_encoder_args(preset: str = "veryfast", crf: str = "23") -> list[str]:
    """Retorna os argumentos FFmpeg para codificação H.264 usando GPU NVENC se disponível."""
    if is_nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(crf), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
