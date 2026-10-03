"""
用 FFmpeg 实读媒体文件的音视频流信息。

命名规则的变量表里只有**档位名**（视频 `1080P`、音频 `192K` 这类），拿不到真实的
编码、分辨率与动态范围；模板本身也没有执行外部程序的能力。所以这几项只能在文件
落盘之后读 —— 合并完成、改名交付之前对成品跑一次 `ffmpeg -i`，从它打印的流信息里取。

不引入 ffprobe：发行包只附带 ffmpeg.exe（见本目录的 __init__.py），而 `ffmpeg -i`
打印的流信息与 ffprobe 同源，够用且不增加分发体积。代价是读不到 side data，
因此 HDR10 与 HDR10+ 无法区分，一律报 HDR10。
"""

from ..common.runtime import runtime

from . import get_bundle_ffmpeg_path

from dataclasses import dataclass
from pathlib import Path
import subprocess
import shutil
import logging
import os
import re

logger = logging.getLogger(__name__)

# 读取流信息只是扫一遍文件头，给 20 秒已经非常宽裕；真卡住也不该把合并流程挂死
TIMEOUT = 20

# 一条流的信息行。`  Stream #0:1(und)[0x2]: Audio: ...` —— 行首有两个空格缩进，
# 流序号与冒号之间是语言（und）与轨道 id（[0x2]）这类修饰，有无、先后都不固定，
# 一概用 `[^:]*` 吞掉
_STREAM_LINE_RE = re.compile(r"^\s*Stream #\d+:\d+[^:]*: (Video|Audio|Subtitle|Data): (.+)$", re.MULTILINE)

# 码流里报的是 FFmpeg 的内部名（小写、无连字符），这里换成与发布组、
# 以及 tinyMediaManager 的 ${audioCodec} 一致的写法
_CODEC_NAMES = {
    "aac": "AAC",
    "eac3": "EAC3",
    "ac3": "AC3",
    "flac": "FLAC",
    "alac": "ALAC",
    "mp3": "MP3",
    "opus": "OPUS",
    "vorbis": "VORBIS",
    "dts": "DTS",
    "truehd": "TRUEHD",
    "mlp": "MLP",
    "pcm_s16le": "PCM",
    "pcm_s24le": "PCM",
    "pcm_s32le": "PCM",
    "pcm_f32le": "PCM",
    "pcm_bluray": "PCM",
}

# 视频编码同样要归一到 B 站解析侧的写法（util/parse 里给 Episode.video_codec
# 填的就是这几个值），这样实读值与档位值语义一致，混用也不会出现两种拼法
_VIDEO_CODEC_NAMES = {
    "h264": "AVC",
    "avc1": "AVC",
    "hevc": "HEVC",
    "h265": "HEVC",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg4": "MPEG4",
    "mpeg2video": "MPEG2",
    "vc1": "VC1",
    "theora": "THEORA",
}

# FFmpeg 报的是声道布局名而不是数字，这里换成 TMM 的 ${audioChannelsDot} 那种写法
_CHANNEL_LAYOUTS = {
    "mono": "1.0",
    "stereo": "2.0",
    "2.1": "2.1",
    "3.0": "3.0",
    "quad": "4.0",
    "4.0": "4.0",
    "5.0": "5.0",
    "5.1": "5.1",
    "6.1": "6.1",
    "7.1": "7.1",
}

_CHANNELS_COUNT_RE = re.compile(r"\b(\d+) channels\b")

# 分辨率那一小段：`1920x1080 [SAR 1:1 DAR 16:9]`，取开头那个 WxH。
# 锚在段首是为了避开编码标签里的 `0x31637661` 这类十六进制字面量
_RESOLUTION_RE = re.compile(r"^(\d{2,5})x(\d{2,5})\b")

# 色彩信息写在像素格式的括号里：`yuv420p(tv, bt709)`、
# `yuv420p10le(tv, bt2020nc/bt2020/smpte2084)`。以 tv/pc 开头才认，
# 免得把 `h264 (High)` 这种 profile 括号吃掉
_COLOR_INFO_RE = re.compile(r"\((?:tv|pc|limited|full)\s*(?:,\s*([^)]*))?\)")

# 传输特性 → 动态范围。ffmpeg 不带 ffprobe 时读不到 side data，
# 因此 HDR10 与 HDR10+ 一律归到 HDR10
_TRANSFER_CURVES = {
    "smpte2084": "HDR10",
    "arib-std-b67": "HLG",
    "smpte428": "HDR",
    "bt2020-10": "HDR",
    "bt2020-12": "HDR",
}

# 杜比视界的编码标签。它同样走 smpte2084 传输特性，只能靠标签区分，
# 所以要先于传输特性判定
_DOLBY_VISION_TAGS = ("dvhe", "dvh1", "dav1")

# 内嵌封面在容器里同样是一条视频流。挑主视频轨时要跳过它们，
# 否则分辨率与动态范围会被读成封面的尺寸
_IMAGE_CODECS = ("mjpeg", "png", "bmp", "gif", "webp")

def _creation_flags() -> int:
    # 本进程没有控制台，子进程弹黑框会闪在界面上
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    return 0

def _resolve_ffmpeg() -> str | None:
    """
    找可用的 FFmpeg，找不到返回 None

    init_ffmpeg() 会把 FFmpeg 所在目录插进 PATH 最前面，所以 which 通常能命中；
    再兜一次随包附带的那个，免得 PATH 被别的进程改乱
    """
    executable = runtime.ffmpeg.executable or "ffmpeg"

    if path := shutil.which(executable):
        return path

    bundled = get_bundle_ffmpeg_path()

    if bundled.exists():
        return str(bundled)

    return None

def _normalize_codec(raw: str) -> str:
    key = raw.strip().lower()

    return _CODEC_NAMES.get(key, key.upper())

def _normalize_video_codec(raw: str) -> str:
    key = raw.strip().lower()

    return _VIDEO_CODEC_NAMES.get(key, key.upper())

def _normalize_channels(segment: str) -> str:
    """
    从音频流那一行的剩余部分里取声道

    布局出现在采样率与采样格式之间：`44100 Hz, stereo, fltp`。也可能是带后缀的
    `5.1(side)`，或实在认不出布局时的 `2 channels`
    """
    for token in (item.strip() for item in segment.split(",")):
        layout = token.split("(")[0].strip()

        if layout in _CHANNEL_LAYOUTS:
            return _CHANNEL_LAYOUTS[layout]

    if match := _CHANNELS_COUNT_RE.search(segment):
        count = int(match.group(1))

        return "1.0" if count <= 1 else f"{count}.0"

    # 认不出就留空：宁可让规则里的这一段空着，也不要编一个声道数出来
    return ""

def _parse_resolution(detail: str) -> str:
    for token in (item.strip() for item in detail.split(",")):
        if match := _RESOLUTION_RE.match(token):
            return f"{match.group(1)}x{match.group(2)}"

    return ""

def _parse_dynamic_range(detail: str) -> str:
    """
    判定动态范围：SDR / HDR10 / HLG / DV

    判据是像素格式括号里的传输特性；杜比视界另看编码标签。两者都没有时按 SDR ——
    这也是绝大多数片源的情况，不要因为读不到就当 HDR
    """
    lowered = detail.lower()

    if any(tag in lowered for tag in _DOLBY_VISION_TAGS):
        return "DV"

    if not (match := _COLOR_INFO_RE.search(detail)):
        return "SDR"

    color = (match.group(1) or "").strip()

    if not color:
        return "SDR"

    # 括号里不止色彩信息，后面还会跟扫描方式这类字段：
    # `yuv444p(tv, bt2020nc/bt2020/smpte2084, progressive)`。色彩信息只是第一段，
    # 不先截断就会把 `progressive` 一起带进传输特性里（曾经如此，HDR 全被判成 SDR）
    color = color.split(",")[0].strip()

    # 完整写法是 `色彩空间/原色/传输特性`，简写（三者相同时）只写一个值
    transfer = color.split("/")[-1].strip().lower()

    return _TRANSFER_CURVES.get(transfer, "SDR")

@dataclass(frozen = True)
class Streams:
    """一次探测读到的全部流信息，读不到的项一律留空"""

    audio_codec: str = ""
    audio_channels: str = ""
    video_codec: str = ""
    video_resolution: str = ""
    video_dynamic_range: str = ""

    def __bool__(self):
        # 五项全空说明这条命令行没有提供任何可用信息
        return any((self.audio_codec, self.video_codec, self.video_resolution))

def parse_streams(output: str) -> Streams:
    """
    从 `ffmpeg -i` 的输出里取主视频流与第一条音频流的信息

    纯函数，便于测试：只看文本，不碰文件系统
    """
    first_audio: str | None = None
    videos: list[str] = []

    for kind, detail in _STREAM_LINE_RE.findall(output):
        if kind == "Video":
            videos.append(detail)

        elif kind == "Audio" and first_audio is None:
            # B 站的产物只有一条音轨，第一条就是它
            first_audio = detail

    # 封面图优先排除；全是图片时（纯图集）退而取第一条，至少还有个尺寸
    detail_video = next(
        (
            item for item in videos
            if item.split(",")[0].split("(")[0].strip().lower() not in _IMAGE_CODECS
        ),
        videos[0] if videos else None
    )

    audio_codec = audio_channels = ""
    video_codec = video_resolution = video_dynamic_range = ""

    if first_audio is not None:
        # 编码名后面跟的是 profile，如 `aac (LC)`、`eac3 (Dolby Digital Plus)`
        audio_codec = _normalize_codec(first_audio.split(",")[0].split("(")[0])

        if audio_codec:
            audio_channels = _normalize_channels(first_audio)

    if detail_video is not None:
        video_codec = _normalize_video_codec(detail_video.split(",")[0].split("(")[0])
        video_resolution = _parse_resolution(detail_video)
        video_dynamic_range = _parse_dynamic_range(detail_video)

    return Streams(
        audio_codec = audio_codec,
        audio_channels = audio_channels,
        video_codec = video_codec,
        video_resolution = video_resolution,
        video_dynamic_range = video_dynamic_range,
    )

def parse_audio_stream(output: str) -> tuple[str, str] | None:
    """
    从 `ffmpeg -i` 的输出里取第一条音频流的 (编码, 声道)

    保留这个窄接口：调用方只关心音轨时不必去拆 Streams
    """
    streams = parse_streams(output)

    if not streams.audio_codec:
        return None

    return streams.audio_codec, streams.audio_channels

def parse_video_stream(output: str) -> tuple[str, str, str] | None:
    """从 `ffmpeg -i` 的输出里取第一条视频流的 (编码, 分辨率, 动态范围)"""
    streams = parse_streams(output)

    if not streams.video_codec:
        return None

    return streams.video_codec, streams.video_resolution, streams.video_dynamic_range

def probe_streams(path: Path | str) -> Streams:
    """
    实读一个媒体文件的音视频流，返回 Streams（读不到时各项留空）

    只跑一次 FFmpeg：音轨与视频轨在同一条命令的输出里，分两次读纯属浪费
    """
    executable = _resolve_ffmpeg()

    if not executable:
        logger.warning("没有可用的 FFmpeg，无法读取流信息：%s", path)

        return Streams()

    # 不指定输出文件：FFmpeg 打印完流信息就会以「没有输出文件」的错误退出，
    # 但那正是我们要的那段文本。-hide_banner 只是去掉与流无关的版本横幅
    try:
        completed = subprocess.run(
            [executable, "-hide_banner", "-i", str(path)],
            stdout = subprocess.DEVNULL,
            stderr = subprocess.PIPE,
            stdin = subprocess.DEVNULL,
            text = True,
            encoding = "utf-8",
            errors = "replace",
            timeout = TIMEOUT,
            creationflags = _creation_flags()
        )

    except (OSError, subprocess.SubprocessError):
        logger.exception("调用 FFmpeg 读取流信息失败：%s", path)

        return Streams()

    return parse_streams(completed.stderr)

def probe_audio_stream(path: Path | str) -> tuple[str, str] | None:
    """
    实读一个媒体文件的音轨，返回 (编码, 声道)；没有音轨或读不到时返回 None

    取第一条音频流：B 站的产物只有一条音轨
    """
    streams = probe_streams(path)

    if not streams.audio_codec:
        return None

    return streams.audio_codec, streams.audio_channels
