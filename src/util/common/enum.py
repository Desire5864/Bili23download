from PySide6.QtCore import QLocale

from enum import Enum, IntEnum, IntFlag

class ToastNotificationCategory(Enum):
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

class QRCodeScanStatus(IntEnum):
    WAITING_FOR_SCAN = 86101             # 等待扫码
    WAITING_FOR_CONFIRMATION = 86090     # 等待确认
    SUCCESS = 0                          # 登录成功
    EXPIRED = 86038                      # 二维码过期

class Scaling(Enum):
    SCALE_100 = "1"
    SCALE_125 = "1.25"
    SCALE_150 = "1.5"
    SCALE_175 = "1.75"
    SCALE_200 = "2"
    AUTO = "Auto"

class Language(Enum):
    CHINESE_SIMPLIFIED = QLocale("zh_CN")
    CHINESE_TRADITIONAL = QLocale("zh_TW")
    ENGLISH = QLocale("en_US")

    AUTO = QLocale()

class WhenClose(Enum):
    EXIT = 1
    MINIMIZE = 2
    ALWAYS_ASK = 3

class FileConflictResolution(Enum):
    AUTO_RENAME = 1
    OVERWRITE = 2

class DanmakuType(Enum):
    XML = "xml"
    ASS = "ass"
    JSON = "json"

class SubtitleType(Enum):
    SRT = "srt"
    LRC = "lrc"
    TXT = "txt"
    ASS = "ass"
    JSON = "json"

class CoverType(Enum):
    JPG = "jpg"
    PNG = "png"
    AVIF = "avif"
    WEBP = "webp"

class MetadataType(Enum):
    NFO = "nfo"
    JSON = "json"

class ProxyMode(Enum):
    DISABLED = "disabled"       # 不启用代理，始终直连
    SYSTEM = "system"           # 跟随系统代理（环境变量，Windows / macOS 上还包括系统代理设置）
    MANUAL = "manual"           # 使用程序内手动配置的代理服务器

class ProxyType(Enum):
    HTTP = "http"
    # SOCKS4 = "socks4"
    # SOCKS5 = "socks5"

class FFmpegSource(Enum):
    BUNDLED = "bundled"
    SYSTEM = "system"
    CUSTOM = "custom"

class NumberingType(Enum):
    FROM_SPECIFIED = 0
    USE_PARSE_LIST = 1
    CONTINUOUS = 2

class ParseAutoCheckMode(Enum):
    CURRENT = "current"
    ALL = "all"
    MAIN = "main"

class Channel(IntEnum):
    UNKNOWN = 0
    PORTABLE = 1
    INSTALLER = 2
    PACKAGE = 3

class ConventionType(IntEnum):
    NORMAL = 11
    PART = 12
    COLLECTION = 13
    INTERACTIVE_VIDEO = 14
    BANGUMI = 20
    CHEESE = 30
    FAVORITE = 40
    SPACE = 50
    HISTORY = 60
    WATCH_LATER = 70
    WEEKLY = 80
    AUDIO = 90

class MediaType(IntEnum):
    UNKNOWN = 0
    DASH = 1
    MP4 = 2
    FLV = 3
    M4A = 4

class DownloadStatus(IntEnum):
    QUEUED = 0                      # 排队中
    PARSING = 1                     # 解析中
    DOWNLOADING = 2                 # 下载中
    PAUSED = 3                      # 已暂停
    COMPLETED = 4                   # 已完成

    FFMPEG_QUEUED = 5               # 等待 FFmpeg 处理中
    MERGING = 6                     # 合并中

    CONVERTING = 7                  # 转换中

    ADDITIONAL_PROCESSING = 8       # 额外处理（如提取封面、生成字幕等）

    FAILED = 100                    # 下载失败
    FFMPEG_FAILED = 101             # FFmpeg 处理失败

    INVALID = 1000                  # 无效状态，在下载未完成时，移动文件导致找不到临时文件

# 这些状态成立的前提是"内存里有东西在推进它"。Downloader 与 Merger 都只活在内存里，
# 进程一退（容器重建、程序重启）就一个都不剩，而任务库里留下的仍是这些值 —— 于是
# 界面上会出现一个进度永远不动、既暂停不了也继续不了的「下载中」。
#
# 它们全部映射到 PAUSED：语义准确（停着、可继续），而且续传是安全的 ——
# Downloader.start() 会按 Download.files 里的分片断点接着下，队列空了就直接进合并。
#
# 桌面端由 QueryWorker.get_task_list 做同一件事，这里供面板与 MCP 工具复用。
ACTIVE_STATUSES = frozenset({
    DownloadStatus.QUEUED,
    DownloadStatus.PARSING,
    DownloadStatus.DOWNLOADING,
    DownloadStatus.FFMPEG_QUEUED,
    DownloadStatus.MERGING,
    DownloadStatus.CONVERTING,
    DownloadStatus.ADDITIONAL_PROCESSING,
})

# 控制层做残留态归一化时用的集合：ACTIVE_STATUSES 去掉 QUEUED。
#
# 差别就在 QUEUED 这一项。"排队等槽位"**本来就不需要活的驱动对象** —— 它只是还没轮到，
# 内存里没有下载器是正常的。而"内存里没有下载器 + 状态在 ACTIVE_STATUSES 里"正是
# 判残留态的条件，于是排队中的任务会被当成重启残留：
#   · 显示上被归一化成"已暂停"（面板给 queued 行画的是「暂停」按钮，看着像能点）；
#   · 用户点「暂停」时先被偷偷改成 PAUSED，再收到"它没在下载，没什么可暂停的"。
#
# 残留的 QUEUED 由 QueryWorker 在加载时改写成 PAUSED（界面列表才是未完成任务的权威
# 副本），所以模型里出现 QUEUED 就一定意味着"真的在排队"，不该再归一化。
#
# 只读展示那条兜底路径（拿不到界面列表、只能看数据库）仍用 ACTIVE_STATUSES：
# 那种时候 QUEUED 确实多半是残留，报"已暂停"比报"排队中"诚实。
RUNNING_STATUSES = frozenset(ACTIVE_STATUSES - {DownloadStatus.QUEUED})

class DownloadType(IntFlag):
    VIDEO            = 1 << 0       # 下载独立视频流
    AUDIO            = 1 << 1       # 下载独立音频流
    DANMAKU          = 1 << 2       # 下载弹幕
    SUBTITLE         = 1 << 3       # 下载字幕
    COVER            = 1 << 4       # 下载封面
    METADATA         = 1 << 5       # 下载元数据
    CHAPTER          = 1 << 6       # 嵌入章节信息

class VideoContainer(Enum):
    MP4 = "mp4"
    MKV = "mkv"

class ParserType(Enum):
    VIDEO = "USER_UPLOADS"
    INTERACTIVE_VIDEO = "INTERACTIVE_VIDEO"
    BANGUMI = "BANGUMI"
    CHEESE = "COURSE"
    LESSON = "MALL_COURSE"
    SPACE = "PROFILE"
    FAVLIST = "FAVORITES"
    POPULAR = "WEEKLY"
    COLLECTION_LIST = "COLLECTION_LIST"
    HISTORY = "HISTORY"
    WATCH_LATER = "WATCH_LATER"
    DYNAMIC = "DYNAMIC"
    AUDIO = "AUDIO"
    BATCH = "BATCH"
    UNKNOWN = "UNKNOWN"

class OriginalFileType(IntEnum):
    BOTH = 0
    VIDEO = 1
    AUDIO = 2

class AutoSelectMode(Enum):
    MANUAL = 0
    SELECT_ALL = 1
    CONDITIONAL = 2

class Area(Enum):
    CN = "cn"
    OV = "ov"

class DuplicateDownloadResolution(Enum):
    CONTINUE = 0
    SKIP = 1
    ALWAYS_ASK = 2

class VariableType(IntEnum):
    TEXT = 0                 # 文本类型
    DATETIME = 1             # 日期时间类型
    NUMBER = 2               # 数字类型
