"""
内置的 Web 面板服务器。

面向**人**：浏览器里登录面板、看下载队列与进度、解析链接、建下载任务、
扫码登录 B 站账号。它与 MCP 服务器（面向 AI 客户端，JSON-RPC）相互独立 ——
各有开关与端口，可以只开其中一个。

两道门分清楚：
  · 面板登录（本文件的 /api/panel/*）拦的是"谁能打开这个网页"；
  · B 站授权登录（util/web/bili_login.py）决定"用哪个 B 站账号下载"。
两者互不相干：没登录 B 站也能用面板，只是下不了会员内容与高画质。

工具调用直接复用 MCP 的工具注册表：解析、入队、查询在本项目里只有一份实现，
面板不重写任何业务逻辑，也就不会出现"网页里能下、客户端里不行"这类分叉。
工具内部已经通过 `call_in_main_thread()` 把访问 Qt 对象的动作切回主线程，
因此这里的 HTTP 线程是安全的。

默认只绑环回地址，与 MCP 保持同一套安全基线；容器部署时用环境变量
`BILI23_WEB_HOST` 放开，见 `common/net.py`。
"""

from ..common.config import config, appdata_dir
from ..common.enum import Area, OriginalFileType, ProxyMode, VideoContainer
from ..common.net import WEB_BIND_HOST_ENV, resolve_bind_host
from ..common._json import loads, dumps_bytes, JSONDecodeError

from .panel import PANEL_HTML
from .auth import (
    MIN_PASSWORD_LENGTH, LoginThrottle, PanelCredentials, SessionStore,
    ensure_credentials, hash_password
)
from . import bili_login
from . import naming
from . import identify
from . import favorites

from ..common import notify
from ..common import wecom_callback

from ..clouddrive import (
    CloudDriveAuthError, CloudDriveClient, CloudDriveError, status_label
)
from ..clouddrive.client import DEFAULT_PORT as DEFAULT_CD2_PORT

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs
import secrets
import logging
import os
import re
import shutil
import time

logger = logging.getLogger(__name__)

# 面板页面与数据接口的路径。页面上用相对路径取数据，所以访问 / 或 /index.html
# 都能命中同一个接口
INDEX_PATHS = ("/", "/index.html")
API_PATH = "/api/call"

# 健康检查端点。docker compose 的 healthcheck 用它判断进程与事件循环是否还活着
HEALTH_PATH = "/health"

# 面板自身的接口。刻意不放进 MCP 工具注册表：工具是给 AI 客户端用的稳定契约，
# 而登录、改密码、扫码这些是人在网页上做的会话操作，混进去只会让工具列表变脏
PANEL_LOGIN_PATH = "/api/panel/login"
PANEL_LOGOUT_PATH = "/api/panel/logout"
PANEL_PASSWORD_PATH = "/api/panel/password"
PANEL_SESSION_PATH = "/api/panel/session"
PANEL_QR_START_PATH = "/api/panel/qr/start"
PANEL_QR_POLL_PATH = "/api/panel/qr/poll"
PANEL_BILI_LOGOUT_PATH = "/api/panel/bili/logout"

# 设置页。读用 GET、写用 POST —— 分成两个路径而不是一个路径两种方法，
# 是为了让 do_GET / do_POST 两张路由表各自是平的，鉴权只有一处判定
PANEL_SETTINGS_PATH = "/api/panel/settings"
PANEL_SETTINGS_SAVE_PATH = "/api/panel/settings/save"

# 日志页。参数走查询串（GET 没有请求体），可读文件由服务端列出来 ——
# 面板不该拿着任意路径去读宿主机文件，范围严格圈在日志目录内
PANEL_LOGS_PATH = "/api/panel/logs"

# 云端备份：把 NAS 的下载目录同步到 115 网盘（CloudDrive2 挂载盘）
PANEL_SYNC_PATH = "/api/panel/sync"

# 云端同步的 CD2 配置（执行容器 / 源目录 / 目标目录）：面板「云端同步」页可改，
# 读 GET、写 POST，与设置页同一条约定
PANEL_SYNC_CONFIG_PATH = "/api/panel/sync_config"
PANEL_SYNC_CONFIG_SAVE_PATH = "/api/panel/sync_config/save"

# MCP 访问令牌的重新生成。旧令牌随即作废（MCP 每个请求都现查配置，不用重启）
PANEL_MCP_TOKEN_PATH = "/api/panel/mcp_token"

# 配置文件设置：导出（GET）/ 导入 / 恢复默认（POST），对应桌面版「配置文件设置」
PANEL_CONFIG_EXPORT_PATH = "/api/panel/config_export"
PANEL_CONFIG_IMPORT_PATH = "/api/panel/config_import"
PANEL_CONFIG_RESET_PATH = "/api/panel/config_reset"

# 命名规则页：读 / 预览 / 保存。预览单独一个接口是因为规则串要边打边看效果，
# 与"保存"是两种节奏；而规则表本身走整表提交（同类型下只能有一条默认规则，
# 逐条写会让配置停在自相矛盾的中间态）
PANEL_NAMING_PATH = "/api/panel/naming"
PANEL_NAMING_PREVIEW_PATH = "/api/panel/naming/preview"
PANEL_NAMING_SAVE_PATH = "/api/panel/naming/save"

# 名称识别页：读 / 预览 / 保存。与命名规则页同一套节奏 —— 预览要边打边看，
# 保存整表提交（一张按顺序匹配的表，半存进去会让优先级变得不可解释）
PANEL_IDENTIFY_PATH = "/api/panel/identify"
PANEL_IDENTIFY_PREVIEW_PATH = "/api/panel/identify/preview"
PANEL_IDENTIFY_SAVE_PATH = "/api/panel/identify/save"

# 粘一条 TMDB 链接换回剧名 / 年份 / 编号 / 季号。要访问外网，跑在 HTTP 线程上
# （服务器是 ThreadingHTTPServer，慢请求不会把整个面板顶住）
PANEL_IDENTIFY_TMDB_PATH = "/api/panel/identify/tmdb"

# 已完成页：清空历史记录。只删数据库里的记录行，**不动任何已下载文件** ——
# 见 api_done_clear 的注释
PANEL_DONE_CLEAR_PATH = "/api/panel/done/clear"

# 账号收藏：收藏夹 / 订阅合集 / 追番追剧 / 稍后再看 / 历史记录。GET 带 ?kind= 选分类。
#
# 前三个分类回的是"可点开的卡片"，后两个分类回的是"该去解析哪个地址"
# （见 util/web/favorites.py 的 direct_parse_payload）—— 同一扇门两种应答，
# 是因为桌面端就是这么分的：后两类压根没有列表接口
PANEL_FAVORITES_PATH = "/api/panel/favorites"

# 通知页：读 / 保存 / 测试发送。测试单独一个接口是因为它要真的发一条出去，
# 与"保存"是两回事 —— 用户往往想先验证连通性再决定要不要开
PANEL_NOTIFY_PATH = "/api/panel/notify"
PANEL_NOTIFY_SAVE_PATH = "/api/panel/notify/save"
PANEL_NOTIFY_TEST_PATH = "/api/panel/notify/test"

# 企业微信回调（接收消息）。🔴 这个路径**故意不要求登录**，也不在 /api/panel/
# 命名空间下：企微服务器来敲这扇门时手里没有我们的面板令牌，只有它自己算的
# msg_signature。拦人的活由签名本身干（见 util/common/wecom_callback.py）。
# 路径单独起一段，是为了让"哪些端点匿名"一眼看得出来 —— 混在 /api/panel/ 里
# 迟早会有人以为它也该有门禁
WECOM_CALLBACK_PATH = "/api/wecom/callback"

# 会话 Cookie 名。带前缀是为了在同一台机器上开多个面板时不互相顶掉
SESSION_COOKIE = "bili23_panel_session"

# 请求体上限。面板只提交链接、id 列表与几个短字段，超出这个量级的只可能是异常或恶意请求
MAX_BODY_SIZE = 256 * 1024

# 拒绝请求时最多丢弃多少请求体。见 WebPanelHandler._drain_body()
DRAIN_LIMIT = 1024 * 1024

# 请求行里的查询串。BaseHTTPRequestHandler 交给 log_message 的是整个
# `GET /api/panel/logs?file=app.log&token=xxx HTTP/1.1`，而面板允许用
# `?token=…` 传访问令牌 —— 令牌不该在日志里留副本，见 log_message
REQUEST_QUERY_RE = re.compile(r"\?[^ \"]*")

def redact_request_line(text: str) -> str:
    """把请求行里的查询串换成占位符。路径留着，排查够用"""
    return REQUEST_QUERY_RE.sub("?<redacted>", str(text))

def generate_token() -> str:
    return secrets.token_urlsafe(32)

class PanelError(Exception):
    """带 HTTP 状态码的接口错误。文案会原样交给页面显示"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)

        self.status = status

def api_login(handler, payload: dict) -> dict:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")

    # 在线爆破的第一道闸：连续失败的来源地址按指数退避锁住。
    # 判定放在校验之前，被锁住的请求连 PBKDF2 都不用算
    peer = handler.client_address[0] if handler.client_address else "?"

    throttle = handler.server.login_throttle

    wait = throttle.wait_seconds(peer)

    if wait > 0:
        raise PanelError(f"尝试过于频繁，请 {int(wait) + 1} 秒后再试", 429)

    if not handler.server.credentials.verify(username, password):
        throttle.record_failure(peer)

        # 只记用户名不记密码。失败日志是排查"为什么进不去"的唯一线索
        logger.warning("Web 面板登录失败，用户名：%s", username or "(空)")

        raise PanelError("用户名或密码不正确", 401)

    throttle.reset(peer)

    session = handler.server.sessions.issue(username)

    handler.set_cookie(SESSION_COOKIE, session, max_age = handler.server.sessions.ttl)

    logger.info("Web 面板登录成功：%s", username)

    return {"ok": True, "username": username}

def api_logout(handler, payload: dict) -> dict:
    # 只作废当前这一个会话，别的设备上开着的面板不该被一起踢掉
    token = handler.cookie(SESSION_COOKIE)

    if token:
        handler.server.sessions.revoke(token)

    handler.clear_cookie(SESSION_COOKIE)

    return {"ok": True}

def api_password(handler, payload: dict) -> dict:
    # 用令牌进来的请求没有会话，也就没有用户名 —— 退回配置里的那一个，
    # 免得脚本或 MCP 客户端明明握着正确密码却改不了
    username = handler.current_user() or handler.server.credentials.username

    current = str(payload.get("current") or "")
    password = str(payload.get("password") or "")

    if not handler.server.credentials.verify(username, current):
        raise PanelError("当前密码不正确", 401)

    if len(password) < MIN_PASSWORD_LENGTH:
        raise PanelError(f"新密码至少需要 {MIN_PASSWORD_LENGTH} 位", 400)

    if password == current:
        raise PanelError("新密码与当前密码相同", 400)

    stored = hash_password(password)

    # 写配置要回 GUI 线程：config.set 会落盘，与 MCP 工具同一条约束
    from ..mcp.invoke import call_in_main_thread

    def persist():
        config.set(config.web_panel_password, stored)

        return True

    call_in_main_thread(persist, timeout = 10.0)

    handler.server.credentials.update(username, stored)

    # 改密码后把所有会话作废，再给当前这个补发一枚：
    # 不作废的话，别人手里那枚旧会话在新密码生效后照样能用，改密码就白改了
    handler.server.sessions.revoke_all()

    session = handler.server.sessions.issue(username)

    handler.set_cookie(SESSION_COOKIE, session, max_age = handler.server.sessions.ttl)

    logger.info("Web 面板密码已修改：%s", username)

    return {"ok": True}

def api_session(handler, payload: dict) -> dict:
    """
    当前会话信息。能走到这里就说明已经通过了鉴权，页面据此判断要不要显示登录框
    """
    return {
        "ok": True,
        "username": handler.current_user(),
        "via_token": handler.token_authenticated(),
    }

def api_qr_start(handler, payload: dict) -> dict:
    try:
        return bili_login.start()

    except Exception as e:
        logger.exception("获取 B 站登录二维码失败")

        raise PanelError(f"获取二维码失败：{e}", 502)

def api_qr_poll(handler, payload: dict) -> dict:
    key = str(payload.get("qrcode_key") or "").strip()

    if not key:
        raise PanelError("缺少 qrcode_key", 400)

    try:
        return bili_login.poll(key)

    except Exception as e:
        logger.exception("查询扫码状态失败")

        raise PanelError(f"查询扫码状态失败：{e}", 502)

def api_bili_logout(handler, payload: dict) -> dict:
    try:
        bili_login.logout()

    except Exception as e:
        logger.exception("退出 B 站登录失败")

        raise PanelError(f"退出失败：{e}", 500)

    return {"ok": True}

# ---------------------------------------------------------------------------
# 设置页
#
# 只开放「下载运行参数」这一类，且是一张**白名单**：能任意写配置等于把整份
# config.json 交给浏览器 —— 里面有 MCP/面板令牌、B 站 Cookie 与绝对路径。
#
# 每项自带类型与范围，校验在服务端再走一遍：前端的 min/max 只是提示，
# 直接 curl 一样能提交任意值
# ---------------------------------------------------------------------------
# 三种新类型的额外维度：
#   enum    —— 下拉选择，options 是显示文案（顺序即枚举序），提交的是下标
#   priority—— 可上下移动的优先级，options 是 [档位 id, 显示文案] 列表，
#               提交的是按优先级排好序的 id 数组
#
# 「媒体选项」里的 keep_original_files_type 与 video_container 存的是枚举对象，
# 不能直接进 JSON，交给 ENUM_FIELDS 在取/存两侧做转换

def _priority_maps(key: str):
    """三个优先级各自对应的「档位反向表 + 译法」，请求期按 key 取"""
    from ..common import data as media_data
    from ..common.translator import Translator

    if key == "video_quality_priority":
        return media_data.reversed_video_quality_map, Translator.VIDEO_QUALITY

    if key == "audio_quality_priority":
        return media_data.reversed_audio_quality_map, Translator.AUDIO_QUALITY

    if key == "video_codec_priority":
        return media_data.reversed_video_codec_map, Translator.VIDEO_CODEC

    raise KeyError(key)

def _priority_options(key: str) -> list:
    """
    当前配置里的优先级列表 → 前端要的 [id, 显示文案] 列表

    两条纪律：
    · 候选项只取**配置里已有的档位**，与桌面版那个拖拽弹窗一致（它也是只列当前
      项、纯调序，不给加档）。另外不能反过来遍历 reversed_map：画质表里有
      auto/200、编码表里有 auto/20 这类从未进过优先级列表的档位，一旦列进候选，
      用户原样保存也会被"档位组合变了"拒绝
    · 必须在**请求期**对着当时的 config 算：测试会把 config 换成假对象，导入期
      算死的话假配置永远进不来；生产上配置在别处（GUI/MCP）被改过，这里也能跟上

    译名取不到不该让整个设置页打不开，退回到档位键
    """
    reversed_map, translate = _priority_maps(key)

    options = []

    for quality_id in config.get(getattr(config, key)) or []:
        quality_key = reversed_map.get(quality_id, str(quality_id))

        # translate 本身就是「key → 文案」的单层映射（查不到返回 None，不抛异常）。
        # 之前这里画蛇添足套了两层 translate(Translator.VIDEO_QUALITY(key))：
        # 画质把译文当 key 再查一次查不到 → None → 前端回退成裸 id（127/126…）；
        # 音质/编码内层用错了 VIDEO_QUALITY 查出 None，外层 translate(None)
        # 按约定返回整张映射表 → 前端 [object Object]
        try:
            name = translate(quality_key)

        except Exception:
            name = None

        options.append([quality_id, name if isinstance(name, str) and name else quality_key])

    return options

SETTINGS_FIELDS = (
    {
        "key": "download_parallel",
        "group": "下载运行参数",
        "label": "同时下载任务数",
        "type": "int",
        "min": 1,
        "max": 10,
        "hint": "同一时刻最多几个任务在下载，其余排队。改动对排队中的任务立即生效",
    },
    {
        "key": "merge_parallel",
        "group": "下载运行参数",
        "label": "同时合并任务数",
        "type": "int",
        "min": 1,
        "max": 4,
        "hint": "同一时刻最多几个任务在做 FFmpeg 合并/转换，其余排队。封装拷贝很轻，开到 2 能明显缩短整批收尾；选了大码率重编码时再往上加要留意 CPU",
    },
    {
        "key": "download_thread",
        "group": "下载运行参数",
        "label": "单任务线程数",
        "type": "int",
        "min": 1,
        "max": 10,
        "hint": "每个任务的分片并发数，同时用作连接池上限。只对之后新建的任务生效",
    },
    {
        "key": "speed_limit_enabled",
        "group": "下载运行参数",
        "label": "启用限速",
        "type": "bool",
        "hint": "限速按单个任务计算，多任务并行时总速率是它们的和",
    },
    {
        "key": "speed_limit_rate",
        "group": "下载运行参数",
        "label": "限速速率",
        "type": "float",
        "min": 0.1,
        "max": 1024.0,
        "unit": "MB/s",
        "hint": "仅在上面的开关打开时生效",
    },
    {
        "key": "auto_retry_enabled",
        "group": "下载运行参数",
        "label": "任务自动重试",
        "type": "bool",
        "hint": "只对网络类可重试错误生效，403/404 这类永久错误仍直接失败",
    },
    {
        "key": "auto_retry_max_count",
        "group": "下载运行参数",
        "label": "最大重试次数",
        "type": "int",
        "min": 1,
        "max": 20,
        "hint": "退避节奏固定在程序内部，这里只控制次数",
    },

    # 媒体选项：下哪几路流、下完之后怎么处理
    {
        "key": "download_video_stream",
        "group": "媒体选项",
        "label": "下载独立视频流",
        "type": "bool",
        "hint": "关掉则只保留音频",
    },
    {
        "key": "download_audio_stream",
        "group": "媒体选项",
        "label": "下载独立音频流",
        "type": "bool",
        "hint": "关掉则视频没有声音",
    },
    {
        "key": "merge_video_audio",
        "group": "媒体选项",
        "label": "合并视频与音频",
        "type": "bool",
        "hint": "关掉则视频与音频存成两个独立文件，不会得到单个完整视频",
    },
    {
        "key": "keep_original_files",
        "group": "媒体选项",
        "label": "保留原始分片文件",
        "type": "bool",
        "hint": "合并之外另存未合并的流文件",
    },
    {
        "key": "keep_original_files_type",
        "group": "媒体选项",
        "label": "保留的原始文件类型",
        "type": "enum",
        "options": ["两者都保留", "仅视频", "仅音频"],
        "hint": "上面的开关打开后才生效",
    },

    # 画质、音质和编码优先级：三项互相独立，各自排一次序。
    # options 不写死在这里 —— 候选随当前配置走，由 api_settings_get 在请求期算
    {
        "key": "video_quality_priority",
        "group": "画质、音质和编码优先级",
        "label": "画质优先级",
        "type": "priority",
        "hint": "点开拖动调整，越靠前越优先。B 站没给到某档位时，也按这份顺序往下挑",
    },
    {
        "key": "audio_quality_priority",
        "group": "画质、音质和编码优先级",
        "label": "音质优先级",
        "type": "priority",
        "hint": "点开拖动调整，越靠前越优先",
    },
    {
        "key": "video_codec_priority",
        "group": "画质、音质和编码优先级",
        "label": "视频编码优先级",
        "type": "priority",
        "hint": "点开拖动调整，越靠前越优先",
    },

    # 下载格式
    {
        "key": "video_container",
        "group": "下载格式",
        "label": "输出容器格式",
        "type": "enum",
        "options": ["mp4", "mkv"],
        "hint": "成品文件的封装格式",
    },
    {
        "key": "m4a_to_mp3",
        "group": "下载格式",
        "label": "M4A 转 MP3",
        "type": "bool",
        "hint": "只对纯音频流的任务生效，同时下载视频时不会转",
    },

    # CDN 设置。cdn_server_list 是虚拟键：真实落点是按地区区分的两份列表，
    # 读写都要先过一遍地区判定（见 api_settings_get / api_settings_save）
    {
        "key": "prefer_cdn_server_provider",
        "group": "CDN 设置",
        "label": "优先使用服务商 CDN",
        "type": "bool",
        "hint": "优先使用服务商提供的 CDN，提高下载稳定性",
    },
    {
        "key": "area",
        "group": "CDN 设置",
        "label": "地理位置",
        "type": "enum",
        "options": ["国内", "海外"],
        "hint": "选择实际位置，以自动匹配合适的 CDN 服务器并提升下载速度",
    },
    {
        "key": "cdn_server_list",
        "group": "CDN 设置",
        "label": "服务商 CDN 列表",
        "type": "cdnlist",
        "hint": "拖动或用箭头调整顺序，越靠前越优先；列表按上面的地理位置自动切换",
    },

    # 代理设置。桌面版标记 proxy_mode 重启生效（共享网络客户端启动时建好），
    # 面板版保存后会丢弃旧客户端，下一个请求按新配置重建，立即生效
    {
        "key": "proxy_mode",
        "group": "代理设置",
        "label": "代理模式",
        "type": "enum",
        "options": ["不启用代理", "使用系统代理", "手动设置代理"],
        "hint": "选择用于解析和下载的代理；保存后立即生效",
    },
    {
        "key": "proxy_server",
        "group": "代理设置",
        "label": "代理服务器",
        "type": "str",
        "hint": "手动设置代理时使用，填服务器地址",
    },
    {
        "key": "proxy_port",
        "group": "代理设置",
        "label": "代理端口",
        "type": "int",
        "min": 1,
        "max": 65535,
        "hint": "手动设置代理时使用",
    },
    {
        "key": "proxy_uname",
        "group": "代理设置",
        "label": "代理用户名",
        "type": "str",
        "hint": "代理需要认证时填写，留空表示匿名",
    },
    {
        "key": "proxy_password",
        "group": "代理设置",
        "label": "代理密码",
        "type": "str",
        "secret": True,
        "hint": "代理需要认证时填写",
    },

    # 其他高级设置。User-Agent 是逐请求实时读取的（get_headers 每次取配置），
    # 改完即生效，不像代理那样需要重建共享客户端
    {
        "key": "user_agent",
        "group": "其他高级设置",
        "label": "自定义 User-Agent",
        "type": "str",
        "min": 1,
        "hint": "为网络请求设置自定义 User-Agent 字符串，留空将无法通过 B 站接口校验，建议保留默认值",
    },

    # 批量解析 / 自动解析共用这一个开关（消费点在 DynamicEpisodeParser.update_page_node）。
    # 🔴 它**必须能读**：面板的批量解析弹窗要拿它给那个复选框做回填，否则复选框永远
    # 从默认值起步、提交时又把默认值写回去 —— 用户开着的开关会被一次解析悄悄关掉
    # （桌面端那个对话框是"读配置 → 回填 → 写回"，走的就是同一条路）
    {
        "key": "auto_add_to_download_list",
        "group": "其他高级设置",
        "label": "解析完自动加入下载列表",
        "type": "bool",
        "hint": "批量解析与自动解析里，每条链接解析完就立刻建下载任务，不等你勾选。关掉时解析结果只做预览，勾好再手动开始下载",
    },

    # MCP 服务器。令牌与运行状态不走这套字段：令牌有独立的重新生成接口，
    # 状态是运行期事实而不是配置项，两块都由 api_settings_get 单独下发
    {
        "key": "mcp_enabled",
        "group": "MCP 服务器",
        "label": "启用 MCP 服务器",
        "type": "bool",
        "hint": "让 AI 客户端通过 Model Context Protocol 解析链接并管理下载任务",
    },
    {
        "key": "mcp_port",
        "group": "MCP 服务器",
        "label": "端口",
        "type": "int",
        "min": 1024,
        "max": 65535,
        "unit": "",
        "hint": "保存后自动重启 MCP 服务器生效",
    },
)

# 存进配置的是枚举成员，读写两端各转一次
ENUM_FIELDS = {
    "keep_original_files_type": OriginalFileType,
    "video_container": VideoContainer,
    "area": Area,
    "proxy_mode": ProxyMode,
}

# 会下发给前端的可序列化字段。SETTINGS_FIELDS 里还可能挂着函数之类的
# 私有约定，不能整份丢出去
SETTING_OUT_KEYS = ("key", "label", "group", "type", "hint", "unit", "min", "max", "options", "secret")

def _field_item(key: str):
    """把设置键映射到 config 上的配置项对象"""
    return getattr(config, key)

def _coerce_setting(field: dict, raw):
    """按声明转换并校验网页提交的值，越界一律拒绝"""
    kind, label = field["type"], field["label"]

    if kind == "enum":
        # 下拉框提交的是下标；非整数与非数字一律说明请求构造错了
        if isinstance(raw, bool) or isinstance(raw, (dict, list)):
            raise PanelError(f"{label}需要一个数字", 400)

        try:
            index = int(raw)

        except (TypeError, ValueError):
            raise PanelError(f"{label}需要一个数字", 400)

        if index < 0 or index >= len(field.get("options") or []):
            raise PanelError(f"{label}的取值不在可选范围内", 400)

        return index

    if kind == "priority":
        # 前端把排序后的 id 数组塞进隐藏框，这里原样解析并校验
        if not isinstance(raw, (str, list, tuple)):
            raise PanelError(f"{label}需要一个列表", 400)

        if isinstance(raw, str):
            try:
                raw = loads(raw)

            except JSONDecodeError:
                raise PanelError(f"{label}的格式不正确", 400)

        if not isinstance(raw, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
            raise PanelError(f"{label}需要一个整数列表", 400)

        valid = {item[0] for item in _priority_options(field["key"])}

        if set(raw) != valid or not raw:
            # 丢项、重复、混入未知 id 都会让选择逻辑跑到不该去的地方，一律拒绝
            raise PanelError(f"{label}的档位组合不正确", 400)

        return list(raw)

    if kind == "str":
        # 地址、用户名、密码这类短文本。数字也收（浏览器表单可能交数字），
        # 但字典/列表说明请求构造错了
        if raw is None:
            return ""

        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            raw = str(raw)

        if not isinstance(raw, str):
            raise PanelError(f"{label}需要文本", 400)

        raw = raw.strip()

        if len(raw) > 255:
            raise PanelError(f"{label}过长", 400)

        min_len = field.get("min")

        if min_len and len(raw) < min_len:
            raise PanelError(f"{label}不能为空", 400)

        return raw

    if kind == "cdnlist":
        # 前端隐藏框交的是 JSON 字符串（与 priority 同一条链路），先解开再验形状；
        # 这里只做形状校验，host 集合是否与现有列表一致要到保存接口里验 ——
        # 得先知道同一份提交把地区改成了什么，才能确定比对哪份列表
        if isinstance(raw, str):
            try:
                raw = loads(raw)

            except JSONDecodeError:
                raise PanelError(f"{label}格式不正确", 400)

        if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
            raise PanelError(f"{label}格式不正确", 400)

        return raw

    if kind == "bool":
        if isinstance(raw, bool):
            return raw

        # 前端可能发 0/1；但字典或列表说明请求构造错了，不能默默当成真
        if raw in (0, 1):
            return bool(raw)

        raise PanelError(f"{label}只能是开关值", 400)

    # bool 是 int 的子类，这里显式挡掉，免得 True 被当成 1 悄悄写进去
    if isinstance(raw, bool) or isinstance(raw, (dict, list)):
        raise PanelError(f"{label}需要一个数字", 400)

    try:
        number = float(raw)

    except (TypeError, ValueError):
        raise PanelError(f"{label}需要一个数字", 400)

    if number != number or number in (float("inf"), float("-inf")):
        raise PanelError(f"{label}需要一个有效数字", 400)

    if kind == "int":
        if not number.is_integer():
            raise PanelError(f"{label}需要整数", 400)

        number = int(number)

    low, high = field.get("min"), field.get("max")

    if low is not None and number < low:
        raise PanelError(f"{label}不能小于 {low}", 400)

    if high is not None and number > high:
        raise PanelError(f"{label}不能大于 {high}", 400)

    return number

def _enum_to_ui(key: str, value):
    """
    取给用户看的值：枚举退回**在枚举类里的位置**

    用位置而不是 .value：这两个枚举一个是 IntEnum（BOTH=0/VIDEO=1/AUDIO=2，
    碰巧等于位置），另一个的值是字符串（MP4="mp4"），只有位置能同时对两者成立，
    也正好对上字段声明里 options 的下标
    """
    enum_cls = ENUM_FIELDS.get(key)

    if enum_cls is None:
        return value

    try:
        return list(enum_cls).index(value)

    except (ValueError, TypeError):
        # 配置里存的值对不上（手改过 config.json）时别让整个设置页挂掉
        return 0

def _enum_to_config(key: str, value):
    """存回配置的值：下标还原成枚举成员"""
    enum_cls = ENUM_FIELDS.get(key)

    if enum_cls is None:
        return value

    members = list(enum_cls)

    if isinstance(value, bool) or not isinstance(value, int):
        # 非数字只可能是枚举成员本身，直接放行
        return value

    if 0 <= value < len(members):
        return members[value]

    raise PanelError(f"{key}的取值超出范围", 400)

def _mcp_status() -> dict:
    """
    MCP 服务器的运行期状态，随设置一起下发给「MCP 服务器」组

    状态是运行期事实不是配置项，所以不进 SETTINGS_FIELDS；令牌也在这里带出 ——
    面板本身就在鉴权墙后面，而客户端配置那段 JSON 必须拿令牌拼出来才有用
    """
    from ..common.runtime import runtime
    from ..mcp.server import mcp_server_manager

    return {
        "running": bool(mcp_server_manager.running),
        "enabled": config.get(config.mcp_enabled),
        "port": config.get(config.mcp_port),
        "token": config.get(config.mcp_token),
        "error": getattr(runtime.mcp, "last_error", "") or "",
    }

def _current_area() -> Area:
    return config.get(config.area)

def _cdn_entries(area: Area = None) -> list:
    """当前地区（或指定地区）的服务商 CDN 列表，元素是 {host, provider}"""
    if area is None:
        area = _current_area()

    item = config.cn_cdn_server_list if area == Area.CN else config.ov_cdn_server_list

    return config.get(item)

def _disk_free_text(path: str) -> str:
    """
    下载目录所在分区的可用空间，给人看的写法。

    容器里 /downloads 就是宿主那个挂载点，所以这里算出来的就是"还能再存多少"。
    单位按量级切：够 1T 就用 T（一位小数），否则退到 G —— 盘快满的时候
    "287.4 GB" 比 "0.3 TB" 有用得多。目录本身可能还没建（首次部署就是），
    逐级上溯到存在的祖先，反正最后落盘的就是那个分区。
    """
    for candidate in (path, *[str(parent) for parent in Path(path).parents]):
        try:
            free = shutil.disk_usage(candidate).free

        except OSError:
            continue

        if free >= 1024 ** 4:
            return f"{free / 1024 ** 4:.1f} TB"

        return f"{free / 1024 ** 3:.1f} GB"

    return "—"

def api_settings_get(handler, payload: dict) -> dict:
    """当前设置。字段定义与值一起下发，前端据此渲染，不硬编码任何一项"""
    fields = []

    for field in SETTINGS_FIELDS:
        key = field["key"]

        entry = {name: field[name] for name in SETTING_OUT_KEYS if name in field}

        if field["type"] == "cdnlist":
            # 虚拟键：config 上没有同名属性，走不了通用读取路径
            entries = _cdn_entries()

            entry["value"] = [e["host"] for e in entries]
            entry["options"] = [[e["host"], e.get("provider") or ""] for e in entries]
            fields.append(entry)

            continue

        try:
            raw_value = _enum_to_ui(key, config.get(_field_item(key)))

        except Exception:
            # 单个项读不出来不该拖垮整页，先给空值让用户至少看得见其余项
            raw_value = None

        if field.get("secret"):
            # 🔴 密文字段不下发真值。代理密码与面板令牌同性质 —— 拿到就能借你的
            # 代理身份出去；而它一旦落在 DOM 的 value 里，整页截图、浏览器扩展、
            # 剪贴板历史都可能把它带走。这里只回一位"设了没"，前端把输入框留空、
            # placeholder 写成状态提示；留空提交表示不改（见 api_settings_save）
            entry["set"] = bool(raw_value)
            entry["value"] = ""

        else:
            entry["value"] = raw_value

        if field["type"] == "priority":
            entry["options"] = _priority_options(key)

        fields.append(entry)

    return {
        "ok": True,
        "fields": fields,
        # 画质优先级现在是可编辑字段，这里只留只读的下载目录与它的剩余空间
        "readonly": [
            {"label": "下载目录", "value": config.get(config.download_path)},
            {"label": "剩余空间", "value": _disk_free_text(str(config.get(config.download_path)))},
        ],
        "mcp": _mcp_status(),
    }

def api_settings_save(handler, payload: dict) -> dict:
    submitted = payload.get("settings")

    if not isinstance(submitted, dict) or not submitted:
        raise PanelError("没有要保存的设置项", 400)

    known = {field["key"]: field for field in SETTINGS_FIELDS}
    updates = []

    for key, raw in submitted.items():
        field = known.get(key)

        if field is None:
            raise PanelError(f"不认识的设置项：{key}", 400)

        # 🔴 密文字段留空 = 本次不改。前端手里根本没有真值（见 api_settings_get），
        # 不回填的话，用户只是改了个"并发上限"就会把代理密码一起抹掉 ——
        # 而且他完全看不出发生了什么
        if field.get("secret") and not str(raw or "").strip():
            continue

        updates.append((key, _coerce_setting(field, raw)))

    # cdn_server_list 是虚拟键：host 顺序要还原成 {host, provider} 字典列表，
    # 写到哪个键由「保存后的地区」决定（area 可能和列表在同一次提交里改）
    cdn_hosts = next((v for k, v in updates if k == "cdn_server_list"), None)
    cdn_item = None

    if cdn_hosts is not None:
        area_raw = next((v for k, v in updates if k == "area"), None)
        area = _enum_to_config("area", area_raw) if area_raw is not None else _current_area()

        by_host = {e["host"]: e for e in _cdn_entries(area)}

        if set(cdn_hosts) != set(by_host):
            raise PanelError("CDN 列表与当前配置不一致，请重新载入后再修改", 409)

        updates = [(k, [dict(by_host[h]) for h in v] if k == "cdn_server_list" else v)
                   for k, v in updates]

        cdn_item = config.cn_cdn_server_list if area == Area.CN else config.ov_cdn_server_list

    # 写配置要回 GUI 线程：config.set 会落盘并触发信号，与 MCP 工具同一条约束
    from ..mcp.invoke import call_in_main_thread

    def persist():
        for key, value in updates:
            if key == "cdn_server_list":
                config.set(cdn_item, value)

                continue

            config.set(_field_item(key), _enum_to_config(key, value))

        # 并发上限改了要让调度器立刻按新值重排：不 emit 的话，得等某个任务
        # 状态变化才会重新评估，用户会觉得"改了半天没反应"。
        # 合并并发同理 —— 调低之后，多出来的合并任务要退回排队态
        if any(key in ("download_parallel", "merge_parallel") for key, _ in updates):
            from ..common.signal_bus import signal_bus

            signal_bus.download.auto_manage_concurrent_downloads.emit()

        # MCP 的开关与端口要重启服务器线程才吃得上：桌面版由设置对话框负责重启，
        # 面板版就地重启，行为对齐。未启用时 restart() 内部自然短路，无副作用
        if any(key in ("mcp_enabled", "mcp_port") for key, _ in updates):
            from ..mcp.server import mcp_server_manager

            mcp_server_manager.restart()

        # 代理各项写进了共享网络客户端的挂载表：丢弃旧客户端，下一个请求
        # 按新配置重建。已持久化的 Cookie 会随重建自动回填，登录态不丢；
        # 在途请求持着旧客户端的引用跑完，互不影响
        if any(key in ("proxy_mode", "proxy_server", "proxy_port", "proxy_uname", "proxy_password")
               for key, _ in updates):
            from ..network.request import reset_client

            reset_client()

        return True

    call_in_main_thread(persist, timeout = 10.0)

    # 标了 secret 的字段（目前是代理密码）日志里只留星号：这行日志会进
    # app.log，而日志页原样回显给浏览器 —— 明文落盘等于把密码贴在墙上
    secret_keys = {field["key"] for field in SETTINGS_FIELDS if field.get("secret")}

    logger.info("Web 面板修改设置：%s", ", ".join(
        f"{key}={'***' if key in secret_keys else value!r}" for key, value in updates
    ))

    return api_settings_get(handler, {})

def api_mcp_token(handler, payload: dict) -> dict:
    """
    重新生成 MCP 访问令牌

    与桌面版「重新生成」按钮同语义：写完即生效。MCP 请求处理器每次都现查
    配置里的令牌，旧令牌下一个请求就被拒，不需要重启服务器线程
    """
    from ..mcp.invoke import call_in_main_thread

    def regen():
        token = generate_token()

        config.set(config.mcp_token, token)

        return token

    token = call_in_main_thread(regen, timeout = 10.0)

    logger.info("Web 面板重新生成了 MCP 访问令牌")

    return {"ok": True, "token": token, "mcp": _mcp_status()}

# ---------------------------------------------------------------------------
# 配置文件设置
#
# 对应桌面版「其他高级设置 → 配置文件设置」：导出 / 导入 / 恢复默认。
# 与桌面版整文件覆盖 + 重启不同，面板版逐键 config.set，内存与磁盘同步更新，
# 改完立即生效，无需重启；代价是非法值逐键拒绝而不是整份拒收

# 🔴 敏感键绝不进导出、也绝不接受导入（见 SENSITIVE_CONFIG_KEYS）：
# config.json 与桌面版同构，里面躺着 B 站会话 Cookie、面板与 MCP 的
# 访问令牌、代理密码。这些值一旦进了浏览器下载的 JSON —— 被分享、被
# 同步盘上传、被截图 —— 等于把全部凭据一次性交出去。导出脱敏、导入
# 忽略，两边都写死在服务端，前端改不动这条线

# (group, name) 二元组集合：与 util/common/config.py 里的声明一一对应。
# 用 group+name 而不是属性名，是防止将来有人在别的组里加一个同名的
# 非敏感项被误伤
SENSITIVE_CONFIG_KEYS = {
    ("Cookie", "SESSDATA"),
    ("Cookie", "bili_jct"),
    ("Cookie", "DedeUserID"),
    ("Cookie", "DedeUserID__ckMd5"),
    ("Web Panel", "web_panel_password"),
    ("Web Panel", "web_panel_token"),
    ("MCP", "mcp_token"),
    ("Advanced", "proxy_password"),
    # 通知凭据：企业微信应用 Secret 与 Telegram Bot token 都等于"别人以你的
    # 名义发消息"的权限。同样不能跟着导出的 JSON 走
    ("Notification", "notification_wecom_secret"),
    ("Notification", "notification_telegram_token"),
    # 回调凭据：拿这两项就能算出合法签名、伪造一条"来自企业微信"的推送。
    # 掩码之外还有个好处 —— 导出文件常常被截图或丢进同步盘，
    # 而这个 Token 一泄露，回调端点就等同于对攻击者敞开
    ("Notification", "notification_wecom_callback_token"),
    ("Notification", "notification_wecom_aes_key"),
}

# 脱敏后的占位值。挑一个不可能与真实值混淆的样子
SENSITIVE_MASK = "******"

def _iter_config_items():
    """枚举配置对象上的配置项

    duck typing 而非 isinstance(ConfigItem)：测试里的 FakeItem 只要长着
    name/group/defaultValue 三个属性就能被枚举，不必把 qfluentwidgets 拖进单测。
    注意 qfluentwidgets 的 ConfigItem.key 是 "group.name" 复合键，条目名要走 .name
    """
    for name in dir(config):
        if name.startswith("_"):
            continue

        try:
            item = getattr(config, name)

        except AttributeError:
            continue

        if hasattr(item, "name") and hasattr(item, "group") and hasattr(item, "defaultValue"):
            yield item

def _export_value(item):
    """导出单项的值

    语言这类配置的值是 QLocale/枚举，直接 JSON 序列化会炸；按 item 自带的
    serializer 转成落盘格式（与 config.json 里看到的一致），没有就原样给
    """
    value = config.get(item)
    serializer = getattr(item, "serializer", None)

    if serializer is None:
        return value

    try:
        return serializer.serialize(value)

    except Exception:
        return str(value)

def api_config_export(handler, payload: dict) -> dict:
    """
    导出全部配置项，按 {group: {key: value}} 组织，与 config.json 同构

    敏感键替换为掩码（值为空时保持空串，"没设置"不构成泄露）：
    导出的用途是分享与备份非敏感设置，令牌与 Cookie 留在它们本来
    的地方 —— 面板的令牌显示在 MCP 页，B 站凭据在扫码登录里
    """
    data = {}
    redacted = []

    for item in _iter_config_items():
        value = _export_value(item)

        if (item.group, item.name) in SENSITIVE_CONFIG_KEYS and value not in ("", None):
            value = SENSITIVE_MASK
            redacted.append(f"{item.group}.{item.name}")

        data.setdefault(item.group, {})[item.name] = value

    return {"ok": True, "data": data, "redacted": redacted}

def api_config_import(handler, payload: dict) -> dict:
    """从字典恢复配置：认识的键逐个写入，不认识的跳过并在结果里说明"""
    data = payload.get("data")

    if not isinstance(data, dict) or not data:
        raise PanelError("需要一个非空的配置字典", 400)

    index = {(item.group, item.name): item for item in _iter_config_items()}
    applied, ignored = 0, []

    from ..mcp.invoke import call_in_main_thread

    def apply():
        nonlocal applied

        for group, entries in data.items():
            if not isinstance(entries, dict):
                ignored.append(str(group))

                continue

            for key, value in entries.items():
                if (group, key) in SENSITIVE_CONFIG_KEYS:
                    # 双保险的另一边：既不让"导出→改→导回"把掩码写进真值，
                    # 也不给"构造一份带令牌的配置"留任何入口。敏感凭据的
                    # 变更只走各自的专用接口（改密码 / 重新生成令牌 / 扫码）
                    ignored.append(f"{group}.{key}（敏感项，不允许导入）")

                    continue

                item = index.get((group, key))

                if item is None:
                    ignored.append(f"{group}.{key}")

                    continue

                try:
                    config.set(item, value)

                    applied += 1

                except Exception as e:
                    # 单键失败不拖累整份导入：validator 拒收的值记下来让用户知道
                    ignored.append(f"{group}.{key}（{e}）")

    call_in_main_thread(apply, timeout = 30.0)

    logger.info("Web 面板导入配置：写入 %d 项，忽略 %d 项", applied, len(ignored))

    return {"ok": True, "applied": applied, "ignored": ignored[:30]}

def api_config_reset(handler, payload: dict) -> dict:
    """
    全部配置项恢复默认值（包括面板账号密码，提示语里必须说清楚）

    🔴 恢复默认后必须把三件运行期凭据就地补齐，否则留下一个
    "磁盘上是默认、内存里是旧值"的分裂状态：
      · 面板账号 —— reset 只是把哈希清空，要按 ensure_credentials 的语义
        重新落一份默认哈希，并同步给 HTTP 线程的凭据快照；
      · 面板令牌 —— 默认值是空串，直接沿用会让"空令牌拒绝一切"退化成
        "旧令牌永远有效"，等于改不了也撤不回，必须换一枚新的；
      · 会话 —— 账号密码都回默认了，所有已签发的会话必须全部作废，
        否则"恢复默认"成了把别人挤下线之外什么都不做的摆设

    返回值里带上新令牌，HTTP 侧据此更新 server.token —— 不更新的话
    旧的 ?token= 链接在"恢复默认"之后依然畅通
    """
    from ..mcp.invoke import call_in_main_thread

    def reset():
        for item in _iter_config_items():
            try:
                config.set(item, item.defaultValue)

            except Exception:
                logger.exception("恢复默认值失败：%s.%s", item.group, item.name)

        # 面板账号：reset 刚把哈希清空，这里按启动时的同一条路径补默认值。
        # ensure_credentials 只在主线程调用（它会写配置），此刻正好在
        username, password_hash = ensure_credentials()

        # 面板令牌换新。config 上没有这一项时（测试的假配置）跳过，
        # 不要让一个加固动作把测试环境打穿
        new_token = None

        if getattr(config, "web_panel_token", None) is not None:
            new_token = generate_token()

            config.set(config.web_panel_token, new_token)

        # MCP 令牌同理换新（MCP 每个请求都现查配置，落盘即生效）
        if getattr(config, "mcp_token", None) is not None:
            config.set(config.mcp_token, generate_token())

        # MCP 的开关与端口也被重置了，重启一次对齐运行状态。放在
        # try 里：桌面端没起 MCP 时 restart 内部自然短路，但测试
        # 环境的假 config 可能缺字段，别让它拖垮整个 reset
        try:
            from ..mcp.server import mcp_server_manager

            mcp_server_manager.restart()

        except Exception:
            logger.exception("恢复默认后重启 MCP 服务器失败")

        return username, password_hash, new_token

    username, password_hash, new_token = call_in_main_thread(reset, timeout = 30.0)

    handler.server.credentials.update(username, password_hash)

    if new_token:
        handler.server.token = new_token

    handler.server.sessions.revoke_all()

    logger.info("Web 面板已恢复默认配置；账号回到默认值，面板令牌已换新，全部会话作废")

    return {"ok": True}

# ---------------------------------------------------------------------------
# 命名规则页
#
# 三个接口：读整表、预览一条规则串、整表保存。逻辑全在 util/web/naming.py，
# 这里只做 HTTP 层的搬运与错误码映射。
#
# 保存是唯一会写配置的动作，必须回 GUI 线程（config.set 会发 Qt 信号），与 MCP
# 工具同一条约束。校验（会真渲染一次）留在 HTTP 线程上先做完 —— 畸形规则不该
# 被带进主线程，那里抛异常要等超时才有人知道
# ---------------------------------------------------------------------------

def api_naming_get(handler, payload: dict) -> dict:
    """规则表 + 类型目录（含各类型的参考变量与预览形态）+ 内置规则表"""
    return naming.build_payload()

def api_naming_preview(handler, payload: dict) -> dict:
    """
    渲染一条规则串

    校验不通过时回的是 200 + valid=false，不是 4xx：用户打字打到一半必然会
    经过一串非法中间态，那不是"请求错了"，页面也不该把它当失败处理
    """
    type_id = payload.get("type")

    if type_id is None or type_id == "":
        raise PanelError("缺少规则类型", 400)

    try:
        return naming.preview(str(payload.get("rule") or ""), type_id)

    except naming.NamingError as e:
        raise PanelError(str(e), 400)

def api_naming_save(handler, payload: dict) -> dict:
    """整表保存。任何一条不通过就整份拒收，见 naming._check_entries"""
    from ..mcp.invoke import call_in_main_thread

    try:
        rules = naming.normalized_rules(payload.get("rules"))

    except naming.NamingError as e:
        raise PanelError(str(e), 400)

    call_in_main_thread(lambda: naming.apply_rules(rules), timeout = 20.0)

    logger.info("Web 面板保存命名规则：共 %d 条", len(rules))

    # 回一份写盘后的真实结果：服务端补过 id、收敛过默认标记，页面据此刷新
    return {"ok": True, "rules": naming.list_rules()}

# ---------------------------------------------------------------------------
# 名称识别页
#
# 逻辑全在 util/web/identify.py（匹配与应用再往下落在 util/common/naming_alias.py，
# 那是运行期命名调的同一份实现）。这里只做 HTTP 层的搬运与错误码映射。
#
# 与命名规则页一样，保存回 GUI 线程、校验留在 HTTP 线程
# ---------------------------------------------------------------------------

def api_identify_get(handler, payload: dict) -> dict:
    """识别表 + 两个下拉的选项 + 影视当前的默认规则（预览要用它渲染）"""
    return identify.build_payload()

def api_identify_preview(handler, payload: dict) -> dict:
    """
    渲染一条识别规则的效果

    校验不通过时回 200 + valid=false，与命名规则页同理：用户打字打到一半必然
    经过一串非法中间态，那不是"请求错了"
    """
    entry = payload.get("alias")

    if not isinstance(entry, dict):
        raise PanelError("缺少要预览的识别规则", 400)

    return identify.preview(entry)

def api_identify_save(handler, payload: dict) -> dict:
    """整表保存。任何一条不通过就整份拒收，见 naming_alias.normalize_entries"""
    from ..mcp.invoke import call_in_main_thread

    try:
        entries = identify.normalized_aliases(payload.get("aliases"))

    except identify.AliasError as e:
        raise PanelError(str(e), 400)

    call_in_main_thread(lambda: identify.apply_aliases(entries), timeout = 20.0)

    logger.info("Web 面板保存名称识别规则：共 %d 条", len(entries))

    # 回一份写盘后的真实结果：服务端补过 id，页面据此刷新
    return {"ok": True, "aliases": identify.list_aliases()}

def api_identify_tmdb(handler, payload: dict) -> dict:
    """
    把一条 TMDB 链接拆成识别规则要的字段

    取不到时回 200 + ok=false（和预览接口同一套道理）：链接粘错、TMDB 打不开
    都不是"请求坏了"，页面就地提示即可，不必让前端走错误分支
    """
    link = payload.get("link")

    if not isinstance(link, str) or not link.strip():
        raise PanelError("缺少 TMDB 链接", 400)

    return identify.tmdb_lookup(link.strip())

# ---------------------------------------------------------------------------
# 已完成页：清空记录
#
# 走的是桌面端「取消已完成任务」同一条路（task_manager.delete_many(..., completed=True)），
# 语义也一致：**只删数据库里的记录行，不碰任何已下载文件**。
# 这一点必须在界面上说清楚 —— 用户按「清理记录」要的是把列表腾空，
# 要是顺手删掉 1.3 GB 的成品，那是不可逆的破坏。
# ---------------------------------------------------------------------------

def api_done_clear(handler, payload: dict) -> dict:
    """清空全部已完成记录（不动文件）"""
    from ..mcp.invoke import call_in_main_thread

    def clear() -> int:
        from ..download.task.manager import task_manager

        tasks = task_manager.query(True)

        if tasks:
            # 数据库删除交给写线程：结构性操作与进度写入共用同一个写线程，
            # 天然保证先后顺序（同桌面端 batch_cancel 的做法）。
            # 代价是它异步落地，接口返回时删表未必已经执行完 ——
            # 页面那边按"乐观清空 + 稍后回读"处理，不在这里空转等
            task_manager.delete_many(tasks, completed = True)

        return len(tasks)

    removed = call_in_main_thread(clear, timeout = 20.0)

    logger.info("Web 面板清理已完成记录：共 %d 条（未删除任何文件）", removed)

    return {"ok": True, "removed": removed}

# ---------------------------------------------------------------------------
# 收藏页
#
# 只读。五个分类里，收藏夹 / 订阅合集 / 追番追剧取的是 B站的列表接口；
# 稍后再看 / 历史记录没有列表接口，回的是"该去解析哪个地址"，由前端喂给
# parse_url。要下载哪个收藏夹，点开卡片后走的仍是面板那条已经验过的
# 解析 → 建任务链路，这里不重写。
#
# 取数逻辑在 util/web/favorites.py，跑在 HTTP 线程上（同步请求，服务器是
# ThreadingHTTPServer，慢请求不会顶住整个面板）。
# ---------------------------------------------------------------------------

def api_favorites_get(handler, payload: dict) -> dict:
    kind = str(payload.get("kind") or "favorite").strip().lower()

    if kind not in favorites.KINDS:
        raise PanelError("不认识的收藏分类：{0}".format(kind), 400)

    try:
        page = int(payload.get("page") or 1)

    except (TypeError, ValueError):
        raise PanelError("page 必须是整数", 400)

    if page < 1:
        raise PanelError("page 必须大于 0", 400)

    # 走解析的那两类会在这里就返回（page 对它们没有意义）
    return favorites.list_favorites(kind, page = page)

# ---------------------------------------------------------------------------
# 通知页
#
# 逻辑全在 util/common/notify.py —— 下载流程（任务完成/失败）调的是同一份实现，
# 这里只做 HTTP 层的搬运与错误码映射。
#
# 保存回 GUI 线程（config.set 发 Qt 信号），校验与测试发送留在 HTTP 线程：
# 测试发送是一次真实的网络请求，正该在请求线程里等它
# ---------------------------------------------------------------------------

def _notify_payload() -> dict:
    """通知页要的全部状态：配置 + 已就绪渠道 + 最近发送记录 + 回调状态"""
    config_values = notify.load_settings()

    # 🔴 凭据不回真值，只回"这一项设了没"。前端据此把输入框留空、把 placeholder
    # 写成「已设置 · 留空则不修改」，要不要换由用户重新输入决定。
    #
    # 下发真值的话它们会出现在 DOM 的 value 里 —— 页面源码、整页截图、浏览器扩展
    # 都读得到，而页面上还有个"复制"按钮随时能把整页内容送进剪贴板历史。这四项
    # 又恰好都是"拿到就能以你的名义发消息/伪造回调"的东西，没有理由让它离开服务端
    payload_config = {
        key: ("" if key in notify.SECRET_KEYS else value)
        for key, value in config_values.items()
    }

    return {
        "ok": True,
        "config": payload_config,
        # 凭据是否已有值。真值留在服务端，前端只需要这一位
        "secrets": {key: bool(config_values.get(key)) for key in notify.SECRET_KEYS},
        # 谁开着且凭据齐全。判据用真值算，只在下发这一层脱敏
        "channels": notify.enabled_channels(config_values),
        "history": notify.history(),
        # 回调侧：企微后台该填的那条完整 URL 由前端拼（它知道当前 origin），
        # 这里只把路径与"凭据齐没齐"交代清楚
        "callback_path": WECOM_CALLBACK_PATH,
        "callback_ready": notify.callback_ready(config_values),
        "callback_history": notify.callback_history(),
    }

def api_notify_get(handler, payload: dict) -> dict:
    """读配置。四个凭据按 SECRET_KEYS 脱敏，前端只拿到"设了没"这一位"""
    return _notify_payload()

def api_notify_save(handler, payload: dict) -> dict:
    from ..mcp.invoke import call_in_main_thread

    values = payload.get("config") or {}

    if not isinstance(values, dict):
        raise PanelError("配置必须是一个对象", 400)

    # 空提交必须在回填之前拒掉。少了这一步，一个 `{}` 会被"留空即不改"的回填
    # 补成一份凭据齐全的配置，然后以"全部开关都没给"为由把开关统统关掉 ——
    # 请求方以为自己什么都没改，实际把通知全停了
    if not values:
        raise PanelError("没有要保存的通知设置", 400)

    values = dict(values)
    current = notify.load_settings()

    # 🔴 凭据是"留空即不改"。前端手里根本没有真值（见 _notify_payload），输入框
    # 本来就是空的 —— 不在这儿把原值填回去的话，用户只改一个"指定接收人"就会顺手
    # 把 Secret 清空，而且他看不出发生了什么。想换凭据就直接输入新值覆盖
    for key in notify.SECRET_KEYS:
        if not str(values.get(key) or "").strip():
            values[key] = current.get(key, "")

    try:
        # 先在 HTTP 线程验一遍。除了把错误原样回给表单，它还保证进主线程的
        # 一定是干净数据 —— 校验里会出 NotifyError，那是个纯值错误，
        # 让它在主线程里抛出来只会变成一条没人接的异常
        clean = notify.normalize_settings(values)

    except notify.NotifyError as e:
        raise PanelError(str(e), 400)

    call_in_main_thread(lambda: notify.apply_settings(clean), timeout = 20.0)

    # 🔴 日志里只出现开关与渠道，绝不出现 webhook / token —— app.log 会原样
    # 回显到面板的日志页，等于把凭据贴在界面上（与 settings/save 同一条纪律）
    logger.info(
        "Web 面板保存通知设置：企业微信 %s，Telegram %s，完成通知 %s，失败通知 %s，企微回调 %s",
        "开" if clean["wecom_enabled"] else "关",
        "开" if clean["telegram_enabled"] else "关",
        "开" if clean["on_complete"] else "关",
        "开" if clean["on_fail"] else "关",
        "开" if clean["wecom_callback_enabled"] else "关",
    )

    return _notify_payload()

def api_notify_test(handler, payload: dict) -> dict:
    """
    发一条测试消息

    发送失败同样回 200 + ok=false：那是用户要看的**结果**（"代理没填对"），
    不是请求本身错了。给 4xx 会被前端的统一错误处理拿去当异常抛

    表单里的现值优先 —— 用户多半是"填完先测一下再保存"
    """
    channel = str(payload.get("channel") or "").strip()

    if channel not in notify.CHANNEL_LABELS:
        raise PanelError("没有指定要测试的通知渠道", 400)

    submitted = payload.get("config")

    try:
        if isinstance(submitted, dict) and submitted:
            ok, detail = notify.send_test(channel, submitted)

        else:
            ok, detail = notify.send_test(channel)

    except notify.NotifyError as e:
        # 表单还没填完就点了测试。把校验结论当结果回，不去打断用户
        ok, detail = False, str(e)

    logger.info("Web 面板测试通知（%s）：%s", notify.CHANNEL_LABELS[channel], detail if ok else f"失败 - {detail}")

    response = _notify_payload()
    response["message"] = detail

    return dict(response, ok = ok)

# ---------------------------------------------------------------------------
# 云端备份（CloudDrive2）
#
# 触发一次「下载目录 → 115 网盘」的备份。
#
# 早先这里是经 docker.sock 在 clouddrive2 容器里跑 `cp -a` —— 那是文件系统级
# 复制，CD2 只能把每个文件当成一条独立的传输任务排进队列：点一次，CD2 的任务
# 列表里就一串串地长，而且增量对比、冲突策略、失败重试全落在传输队列上，
# 不归备份引擎管。
#
# 现在改成调 CD2 自己的接口（见 util/clouddrive）：
#
#     GetToken（账号密码换 JWT）
#         → BackupGetAll                  找到以该源目录为源的那条备份
#         → BackupRestartWalkingThrough   让它立刻重扫一遍
#
# 走的是 CD2 原生备份引擎：增量、冲突策略、失败重试、进度都归它管，面板只负责
# 「点名触发」与读回状态。CD2 容器、它的挂载点、115 的账号都在它自己那儿，
# 面板不需要知道 —— 只需要能连上它的 19798。
#
# 传输细节见 util/clouddrive/client.py：19798 同时提供 gRPC 与 gRPC-Web，
# 后者跑在 HTTP/1.1 上，所以不必为它引入 grpcio。
# ---------------------------------------------------------------------------
SYNC_HOST = os.environ.get("BILI23_CD2_HOST", "192.168.3.36")
SYNC_SOURCE = os.environ.get("BILI23_SYNC_SOURCE", "/Storage/哔哩哔哩")

# 单次调用与状态查询的超时。状态是打开页面时顺带查的，连不上不该把整页拖住
CD2_TIMEOUT = 20.0
CD2_STATUS_TIMEOUT = 5.0

# CD2 把设备令牌写在自己的配置目录里（宿主 /volume1/docker/clouddrive2/config/
# device_token.txt）。那是宿主路径，容器里看不到，除非显式挂进来 —— 挂进来了就
# 不必在面板上存账号密码（设备令牌不随密码变化，也没有过期时间）
SYNC_DEVICE_TOKEN_FILE = os.environ.get("BILI23_CD2_DEVICE_TOKEN_FILE", "")

# 面板「云端同步」页保存过的配置。放在 config.json 旁边（容器里同在挂载卷
# /config 下）：不进 Qt 的 ConfigItem 体系 —— 这些是部署参数而不是应用设置，
# 桌面版没有 CD2，这套配置对它没有意义
SYNC_CONFIG_FILE = appdata_dir / "sync.json"

# 新版配置的字段。🔴 旧实现往同一个文件里写的是 {container, src, dst}，那套字段
# 现在一个都不用 —— 不过滤掉的话，一个残留的旧文件会让页面显示"已保存自定义配置"，
# 而 host/port/source 其实全都来自默认值（用户看到的与实际生效的不是一回事）
SYNC_CONFIG_KEYS = ("host", "port", "source", "username", "password", "totp", "device_token")

def _sync_overrides() -> dict:
    """面板保存过的 CD2 配置。读不出来就当没有（文件不存在 / 手改坏了都容错）"""
    try:
        with open(SYNC_CONFIG_FILE, "r", encoding = "utf-8") as f:
            data = loads(f.read())

        if not isinstance(data, dict):
            return {}

        return {key: value for key, value in data.items() if key in SYNC_CONFIG_KEYS}

    except (OSError, JSONDecodeError):
        return {}

def _pick_port(raw, fallback: int) -> int:
    """端口容错：非数字、越界一律回落到 fallback"""
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback

    return port if 0 < port < 65536 else fallback

def sync_settings() -> dict:
    """备份实际生效的配置：面板保存值 > 环境变量 > 内置默认"""
    saved = _sync_overrides()

    def pick(key: str, fallback: str) -> str:
        value = str(saved.get(key) or "").strip()

        return value or fallback

    return {
        "host": pick("host", SYNC_HOST),
        "port": _pick_port(saved.get("port"), DEFAULT_CD2_PORT),
        "source": pick("source", SYNC_SOURCE),
        "username": pick("username", ""),
        # 密码不走 pick：它不是"有默认值"的部署参数，没存过就是空
        "password": str(saved.get("password") or ""),
        "totp": pick("totp", ""),
        "device_token": pick("device_token", ""),
    }

def _read_device_token_file() -> str:
    if not SYNC_DEVICE_TOKEN_FILE:
        return ""

    try:
        with open(SYNC_DEVICE_TOKEN_FILE, "r", encoding = "utf-8") as f:
            return f.read().strip()

    except OSError:
        return ""

def build_cd2_client(settings: dict = None, timeout: float = None):
    """按配置造一个 CD2 客户端。

    凭据优先级：账号密码 > 面板填的设备令牌 > 挂进来的令牌文件。前一条成立就
    不再往下看 —— 账号密码是用户显式填的，得让它说了算。
    """
    settings = settings or sync_settings()

    client = CloudDriveClient(
        settings["host"], settings["port"],
        timeout = CD2_TIMEOUT if timeout is None else timeout,
    )

    if settings["username"] and settings["password"]:
        client.set_credentials(settings["username"], settings["password"], settings["totp"])

    else:
        client.set_device_token(settings["device_token"] or _read_device_token_file())

    return client

def api_sync(handler, payload: dict) -> dict:
    """触发一次云端备份：让 CD2 重扫那条备份的源目录

    立即返回 —— 扫描与上传由 CD2 自己在后台跑，面板读状态即可（不用 docker exec，
    所以也不会把 HTTP 线程挂在备份上）。
    """
    settings = sync_settings()
    source = settings["source"]

    client = build_cd2_client(settings)

    try:
        backup = client.find_backup(source)

        if backup is None:
            raise PanelError(
                f"CloudDrive2 里没有以 {source} 为源的备份任务。"
                "先在 CD2 里为这个目录建一条备份（源路径要完全一致），面板才能触发它",
                404,
            )

        destinations = [d.destinationPath for d in backup.backup.destinations if d.isEnabled]
        target = "、".join(destinations) or "（这条备份没有启用的目标）"

        client.restart_walking_through(source)

    except CloudDriveAuthError as e:
        raise PanelError(f"登录 CloudDrive2 失败：{e}", 502)

    except CloudDriveError as e:
        logger.warning("云端备份失败：%s", e)
        raise PanelError(str(e), 502)

    finally:
        client.close()

    logger.info("已触发云端备份：%s → %s（CD2 %s:%s）",
                source, target, settings["host"], settings["port"])

    return {"ok": True, "message": f"已让 CloudDrive2 重新扫描 {source}，增量同步到 {target}"}

# 主机名的合法字符集：它会被拼进请求 URL，放行斜杠、@ 这类字符等于让面板
# 往任意地址发请求
CD2_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

def api_sync_config_get(handler, payload: dict) -> dict:
    """CD2 配置 + 那条备份的当前状态。密码只回「设了没」，不回内容"""
    settings = sync_settings()

    result = {
        "ok": True,
        "host": settings["host"],
        "port": settings["port"],
        "source": settings["source"],
        "username": settings["username"],
        # 🔴 凭据不回显：前端拿到空密码框 = 不修改（见 api_sync_config_save）
        "has_password": bool(settings["password"]),
        "has_device_token": bool(settings["device_token"] or _read_device_token_file()),
        "saved": bool(_sync_overrides()),
        "status": None,
    }

    # 状态是顺带查的：连不上就把原因写进 error 让页面提示，而不是整页报错
    client = build_cd2_client(settings, timeout = CD2_STATUS_TIMEOUT)

    try:
        backup = client.find_backup(settings["source"])

        if backup is None:
            result["error"] = f"CloudDrive2 里没有以 {settings['source']} 为源的备份任务"

        else:
            result["status"] = {
                "code": int(backup.status),
                "label": status_label(backup.status),
                "message": backup.statusMessage,
                "enabled": bool(backup.backup.isEnabled),
                "interval": int(backup.backup.walkingThroughIntervalSecs),
                "destinations": [
                    {
                        "path": destination.destinationPath,
                        "enabled": bool(destination.isEnabled),
                        "last_finish": (
                            destination.lastFinishTime.seconds
                            if destination.lastFinishTime.seconds else 0
                        ),
                    }
                    for destination in backup.backup.destinations
                ],
            }

    except CloudDriveError as e:
        result["error"] = str(e)

    finally:
        client.close()

    return result

def api_sync_config_save(handler, payload: dict) -> dict:
    submitted = payload.get("config")

    if not isinstance(submitted, dict):
        raise PanelError("没有要保存的配置", 400)

    host = str(submitted.get("host") or "").strip()
    source = str(submitted.get("source") or "").strip()
    username = str(submitted.get("username") or "").strip()
    totp = str(submitted.get("totp") or "").strip()

    if not CD2_HOST_RE.fullmatch(host):
        raise PanelError("CD2 地址只能是字母、数字、点、下划线或连字符", 400)

    port = _pick_port(submitted.get("port"), 0)

    if not port:
        raise PanelError("CD2 端口必须是 1~65535 之间的整数", 400)

    # 源路径会作为 gRPC 消息的字段值传出去（不经 shell），只要求是绝对路径
    if not source.startswith("/"):
        raise PanelError("源目录必须是 CD2 视角的绝对路径（以 / 开头）", 400)

    current = _sync_overrides()

    # 🔴 密码留空 = 不改。请求体里没带这个键时保持原值 —— 前端拿不到回显，
    # 每次都提交空串会把已存的密码抹掉
    password = submitted.get("password")

    if password is None:
        password = current.get("password") or ""

    config = {
        "host": host,
        "port": port,
        "source": source,
        "username": username,
        "password": str(password),
        "totp": totp,
        "device_token": str(current.get("device_token") or ""),
    }

    # 原子写：与 config.save 同一个套路，断电也留不下半个文件
    SYNC_CONFIG_FILE.parent.mkdir(parents = True, exist_ok = True)
    temp_path = SYNC_CONFIG_FILE.with_name(SYNC_CONFIG_FILE.name + ".tmp")

    with open(temp_path, "w", encoding = "utf-8") as f:
        f.write(dumps_bytes(config).decode("utf-8"))

    os.replace(temp_path, SYNC_CONFIG_FILE)

    # 里面有 CD2 的密码，别让别的用户读得到
    try:
        os.chmod(SYNC_CONFIG_FILE, 0o600)

    except OSError:
        pass

    logger.info("Web 面板保存云端同步配置：CD2 %s:%s，源 %s", host, port, source)

    return api_sync_config_get(handler, {})

# ---------------------------------------------------------------------------
# 日志页
#
# 只读、只按行返回。面板要回答的是"刚才那次下载为什么失败了"，需要的永远
# 是尾部那一段，所以这里从文件末尾往回读固定字节数，而不是整份读进来 ——
# app.log 在容器里跑几个月能长到几十 MB。
# ---------------------------------------------------------------------------
LOG_TAIL_DEFAULT = 300
LOG_TAIL_MAX = 2000

# 单次最多回读的字节数。取 512 KB 是因为 DEBUG 级别下约合 3000+ 行，
# 足够覆盖 LOG_TAIL_MAX 的行数需求还留了余量
LOG_READ_MAX_BYTES = 512 * 1024

# 级别过滤。过滤器写的是"匹配哪些级别"，None 表示不过滤
LOG_LEVELS = {
    "all": None,
    "info": ("INFO",),
    "warn": ("WARNING",),
    "error": ("ERROR", "CRITICAL"),
}

# 排序优先级：常用的排前面，轮转出来的历史文件按名字排在后面
LOG_FILE_ORDER = ("app.log", "crash.log")

# 日志行格式固定为 "[时间] - 模块 - 级别 - at 位置: 消息"，级别在第三个字段。
# 用正则而不是 `"ERROR" in line`：消息体里带 "ERROR" 字样的 INFO 行不该被算进来
LOG_LEVEL_RE = re.compile(r"\] - [^-]+ - ([A-Z]+) - at ")

def _logs_dir():
    """
    日志目录。从 root logger 上已挂的文件处理器反查，而不是自己拼路径 ——
    QStandardPaths 在桌面端与容器里的取值不同，重算一遍很可能指向另一个目录，
    读出来的就是"日志是空的"
    """
    for handler in logging.getLogger().handlers:
        # TimedRotatingFileHandler 会把落盘路径放在 baseFilename 上
        name = getattr(handler, "baseFilename", None)

        if name:
            return Path(name).parent

    return None

def _log_sources() -> dict:
    """可读的日志文件：{文件名: 路径}。目录不存在（比如没打过日志）时给空表"""
    directory = _logs_dir()

    if directory is None or not directory.is_dir():
        return {}

    def order(path: Path):
        name = path.name

        return (LOG_FILE_ORDER.index(name) if name in LOG_FILE_ORDER else len(LOG_FILE_ORDER), name)

    sources = {}

    for path in sorted(directory.glob("*.log*"), key = order):
        # glob 会带上同名目录，这里只要文件
        if path.is_file():
            sources[path.name] = path

    return sources

def _tail_lines(path: Path, max_bytes: int = LOG_READ_MAX_BYTES):
    """
    回读文件尾部若干字节并按行切开

    从中间截断时先丢掉一行：否则首行必定是残缺的半个日志条目，
    看着像日志本身坏了
    """
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()

        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()

        else:
            # 不必回退时也要显式归零：上面刚 seek 到末尾，不回零就读到空数据
            f.seek(0)

        data = f.read()

    return data.decode("utf-8", "replace").splitlines()

def _match_level(line: str, levels) -> bool:
    """levels 为 None 时全放行；否则只留级别命中的行"""
    if levels is None:
        return True

    found = LOG_LEVEL_RE.search(line)

    # 匹配不上格式的行（crash.log 的原始转储就是自由文本）只在"全部"下显示
    return bool(found) and found.group(1) in levels

def api_logs_get(handler, payload: dict) -> dict:
    """
    读日志尾部

    payload 来自查询串（见 parse_query）：file / lines / level
    """
    sources = _log_sources()

    if not sources:
        return {"ok": True, "files": [], "file": "", "lines": [], "matched": 0, "truncated": False,
                "reason": "日志目录不可用或还没有产生日志"}

    names = list(sources)
    key = str(payload.get("file") or "").strip() or names[0]

    if key not in sources:
        raise PanelError(f"没有这个日志文件：{key}", 400)

    try:
        limit = int(payload.get("lines") or LOG_TAIL_DEFAULT)

    except (TypeError, ValueError):
        raise PanelError("行数需要一个整数", 400)

    if not 1 <= limit <= LOG_TAIL_MAX:
        raise PanelError(f"行数需在 1–{LOG_TAIL_MAX} 之间", 400)

    level = str(payload.get("level") or "all").strip().lower()

    if level not in LOG_LEVELS:
        raise PanelError("级别只能是 all / info / warn / error", 400)

    path = sources[key]
    scanned = _tail_lines(path)
    matched = [line for line in scanned if _match_level(line, LOG_LEVELS[level])]
    shown = matched[-limit:]

    stat = path.stat()

    return {
        "ok": True,
        "files": [
            {"key": name, "size": item.stat().st_size}
            for name, item in sources.items()
        ],
        "file": key,
        "level": level,
        "lines": shown,
        "matched": len(matched),
        "truncated": len(matched) > len(shown),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    }

# 路由表。登录接口之外一律要求已登录，白名单与其写在两处，不如让这张表就是白名单
PANEL_ROUTES = {
    PANEL_LOGIN_PATH: api_login,
    PANEL_LOGOUT_PATH: api_logout,
    PANEL_PASSWORD_PATH: api_password,
    PANEL_SESSION_PATH: api_session,
    PANEL_QR_START_PATH: api_qr_start,
    PANEL_QR_POLL_PATH: api_qr_poll,
    PANEL_BILI_LOGOUT_PATH: api_bili_logout,
    PANEL_SETTINGS_SAVE_PATH: api_settings_save,
    PANEL_MCP_TOKEN_PATH: api_mcp_token,
    PANEL_CONFIG_IMPORT_PATH: api_config_import,
    PANEL_CONFIG_RESET_PATH: api_config_reset,
    PANEL_NAMING_PREVIEW_PATH: api_naming_preview,
    PANEL_NAMING_SAVE_PATH: api_naming_save,
    PANEL_IDENTIFY_PREVIEW_PATH: api_identify_preview,
    PANEL_IDENTIFY_SAVE_PATH: api_identify_save,
    PANEL_IDENTIFY_TMDB_PATH: api_identify_tmdb,
    PANEL_DONE_CLEAR_PATH: api_done_clear,
    PANEL_NOTIFY_SAVE_PATH: api_notify_save,
    PANEL_NOTIFY_TEST_PATH: api_notify_test,
    PANEL_SYNC_PATH: api_sync,
    PANEL_SYNC_CONFIG_SAVE_PATH: api_sync_config_save,
}

# GET 侧：只读接口两张表并成一张。单独一张表而不是让一个路径按方法分派，
# 是为了让两个方法各自是一张平表，鉴权与错误处理仍然只有一份实现
PANEL_GET_ROUTES = {
    PANEL_SETTINGS_PATH: api_settings_get,
    PANEL_LOGS_PATH: api_logs_get,
    PANEL_SYNC_CONFIG_PATH: api_sync_config_get,
    PANEL_CONFIG_EXPORT_PATH: api_config_export,
    PANEL_NAMING_PATH: api_naming_get,
    PANEL_IDENTIFY_PATH: api_identify_get,
    PANEL_FAVORITES_PATH: api_favorites_get,
    PANEL_NOTIFY_PATH: api_notify_get,
}

# 不需要登录就能访问的接口
PANEL_PUBLIC_PATHS = (PANEL_LOGIN_PATH,)

class WebPanelHandler(BaseHTTPRequestHandler):
    # 该值会出现在响应的 Server 头里
    server_version = "Bili23-Panel"
    sys_version = ""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        # 本次响应要发的 Set-Cookie。放在实例上而不是塞进 _send 的参数里，
        # 是因为登录成功这类响应是路由函数产生的，它只关心"要种哪个 Cookie"
        self._cookies: list = []

        super().__init__(*args, **kwargs)

    def handle_one_request(self):
        try:
            super().handle_one_request()

        finally:
            # 与 MCP 同一个理由：HTTP/1.1 的默认长连接会让每个空闲连接长期占着
            # 一个线程。面板每 3 秒轮询一次，复用连接省下的握手开销没有意义
            self.close_connection = True

    @property
    def registry(self):
        return self.server.registry

    def log_message(self, format, *args):
        # 默认实现直接写 stderr，绕过了本项目的 logging 配置。
        #
        # 🔴 查询串必须剃掉：面板允许用 `?token=…` 传访问令牌（脚本直接用地址打
        # 接口），那个令牌等于一次免密登录。现在日志级别是 INFO，debug 不落盘 ——
        # 但那是运气不是设计：谁为了排查问题把级别调成 DEBUG，令牌就跟着进了
        # app.log，而日志页会把 app.log 原样回显给浏览器
        logger.debug("Web 面板 %s", redact_request_line(format % args))

    def do_GET(self):
        path = self._path()

        if path in INDEX_PATHS:
            # 用 `?token=…` 打开时先把它换成一枚会话 Cookie，再跳到干净的地址
            if self._exchange_token_for_session():
                return

            # 页面本身不设门禁：它要先加载出来，才知道该显示登录框还是主界面
            self._send(200, "text/html; charset=utf-8", PANEL_HTML.encode("utf-8"))

            return

        if path == HEALTH_PATH:
            self._send(200, "text/plain; charset=utf-8", b"ok")

            return

        if path == WECOM_CALLBACK_PATH:
            # 企微保存回调配置时先发一条 GET 来验证。匿名，见路径定义处的说明
            self._handle_wecom_verify()

            return

        if path in PANEL_GET_ROUTES:
            # GET 不带请求体，跳过读 body 那一层
            self._dispatch_panel(path, PANEL_GET_ROUTES, read_body = False)

            return

        self._send(404, "text/plain; charset=utf-8", b"Not Found")

    def do_POST(self):
        path = self._path()

        if path == WECOM_CALLBACK_PATH:
            self._handle_wecom_message()

            return

        if path in PANEL_ROUTES:
            self._dispatch_panel(path)

            return

        if path != API_PATH:
            self._drain_body()
            self._send(404, "text/plain; charset=utf-8", b"Not Found")

            return

        if not self.authenticated():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized"})

            return

        request = self._read_json()

        if request is None:
            return

        name = request.get("tool")
        arguments = request.get("arguments") or {}

        # 白名单就是工具注册表本身：面板能调的只有 MCP 已经暴露出去的那几个，
        # 不额外开面
        if not isinstance(name, str) or not self.registry.has(name):
            self._send_json(404, {"error": f"unknown tool: {name}"})

            return

        if not isinstance(arguments, dict):
            self._send_json(400, {"error": "'arguments' must be an object"})

            return

        # registry.call 已经把业务异常与主线程超时收敛成 isError 结果，
        # 这里不会再抛
        self._send_json(200, self.registry.call(name, arguments))

    def _exchange_token_for_session(self) -> bool:
        """
        地址栏带 `?token=…` 打开面板时，拿它换一枚会话 Cookie 再跳回干净的地址

        令牌就这么留在地址栏有三个坏处：进浏览器历史、被收藏或转发时一并给出去、
        以及此后每一条同源请求都把它挂在 URL 上。换成 Cookie 后地址栏立刻干净，
        令牌只在这一跳里出现过 —— 而 Cookie 是 HttpOnly 的，页面里的脚本也读不到。

        返回 True 表示已经回过响应，调用方应当直接 return
        """
        _, _, query = self.path.partition("?")
        supplied = parse_query_token(query)

        if not supplied:
            return False

        if not secrets.compare_digest(supplied, self.server.token):
            # 令牌不对就什么都不做：照常给登录页。不回显、也不记是哪个值 ——
            # 这个地址是可能被扫描器扫到的，日志里没必要留他们的尝试内容
            logger.warning("Web 面板收到无效的访问令牌")

            return False

        session = self.server.sessions.issue(self.server.credentials.username)

        self.set_cookie(SESSION_COOKIE, session, max_age = self.server.sessions.ttl)

        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self._write_security_headers()
        self._write_pending_cookies()
        self.end_headers()

        return True

    def _path(self) -> str:
        return self.path.split("?")[0].rstrip("/") or "/"

    def _dispatch_panel(self, path: str, routes: dict = None, read_body: bool = True):
        routes = PANEL_ROUTES if routes is None else routes

        if path not in PANEL_PUBLIC_PATHS and not self.authenticated():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized"})

            return

        if read_body:
            payload = self._read_json(allow_empty = True)

            if payload is None:
                return

        else:
            # GET 没有请求体，参数只能走查询串（日志页的文件名、行数、级别）
            payload = parse_query(self.path)

        try:
            self._send_json(200, routes[path](self, payload))

        except PanelError as e:
            self._send_json(e.status, {"error": str(e)})

        except Exception as e:
            # 路由函数里没预料到的异常：记全栈，但只把摘要交给页面
            logger.exception("Web 面板接口执行失败：%s", path)

            self._send_json(500, {"error": f"服务器错误：{e}"})

    # -------------------------------------------------------------------
    # 企业微信回调（全站唯一的匿名业务端点）
    #
    # 这两扇门不由会话或令牌把守，改由企微签名把守：请求里的 msg_signature 是
    # 用双方约定的 Token 算出来的，算不出来就说明不是企微发来的（或内容被改过）。
    # "谁能进"因此与面板的登录状态无关 —— 企微服务器手里不可能有我们的令牌。
    #
    # 逻辑本身在 util/common/wecom_callback.py，这里只做 HTTP 层的搬运。
    # 这里不做任何网络请求、不进主线程：企微的 GET 验证必须 1 秒内应答完，
    # POST 也只有 5 秒，任何一处阻塞都会让它在后台判我们"配置失败"
    # -------------------------------------------------------------------

    def _wecom_callback_context(self):
        """
        取回调要用的凭据。取不到就回一个统一的 403，返回 None

        （返回 (settings, aes_key)；已回过响应时返回 None）
        """
        settings = notify.load_settings()

        if not settings["wecom_callback_enabled"]:
            # 没开回调却有人来敲：要么是企微后台还留着旧配置，要么是扫描器。
            # 两种都不该给出"这里有个端点"之外的任何信息
            self._reject_wecom("回调未启用")
            return None

        try:
            aes_key = wecom_callback.decode_aes_key(settings["wecom_aes_key"])

        except wecom_callback.CallbackError as e:
            self._reject_wecom(f"EncodingAESKey 不可用：{e}")
            return None

        return settings, aes_key

    def _reject_wecom(self, reason: str, drain: bool = True):
        """
        统一拒绝：403 + 一句不带细节的文案

        细节只写日志。这个端点是全站唯一不设门禁的入口，响应体是任何人都读得到的
        输出 —— 把"是签名错了还是密钥错了"写进去，等于给试探的人递梯子

        🔴 drain 在**已经读过 body** 的调用点上必须传 False：_drain_body 是按
        Content-Length 去 recv 的，body 早已进内存时它会一直等到超时，客户端
        看到的是连接挂死而不是这个 403
        """
        notify.record_callback(False, reason)
        logger.warning("企业微信回调被拒：%s", reason)

        if drain:
            self._drain_body()

        self._send(403, "text/plain; charset=utf-8", b"forbidden")

    def _handle_wecom_verify(self):
        """GET 验证：校验签名 → 解密 echostr → 原样把明文回过去"""
        context = self._wecom_callback_context()

        if context is None:
            return

        settings, aes_key = context
        query = parse_query(self.path)

        try:
            plain = wecom_callback.verify_url(
                settings["wecom_callback_token"], aes_key,
                query.get("timestamp", ""), query.get("nonce", ""),
                query.get("msg_signature", ""), query.get("echostr", ""),
                settings["wecom_corp_id"],
            )

        except wecom_callback.CallbackError as e:
            self._reject_wecom(str(e))
            return

        notify.record_callback(True, "URL 验证通过")
        logger.info("企业微信回调 URL 验证通过")

        # 明文必须原样返回：不加引号、不带 BOM、不加换行。多一个字节企微就判失败
        self._send(200, "text/plain; charset=utf-8", plain.encode("utf-8"))

    def _handle_wecom_message(self):
        """POST 收消息：解密 → 记一笔 → 文本消息则被动回复一条"""
        context = self._wecom_callback_context()

        if context is None:
            return

        settings, aes_key = context
        query = parse_query(self.path)
        body = self._read_body(allow_empty = True)

        if body is None:
            return

        try:
            plain = wecom_callback.decrypt_message(
                settings["wecom_callback_token"], aes_key,
                query.get("timestamp", ""), query.get("nonce", ""),
                query.get("msg_signature", ""), body,
                settings["wecom_corp_id"],
            )

        except wecom_callback.CallbackError as e:
            # body 已经读进内存了，不能再 drain（见 _reject_wecom 的说明）
            self._reject_wecom(str(e), drain = False)
            return

        message = wecom_callback.parse_message(plain)
        summary = wecom_callback.summarize(message)

        notify.record_callback(True, summary)
        logger.info("企业微信回调收到：%s", summary)

        reply = self._wecom_reply(settings, aes_key, message)

        if reply is None:
            # 没有被动回复时按官方建议回 success —— 回空串会被当成"没应答"，
            # 企微会在五秒后重试，最多三次
            self._send(200, "text/plain; charset=utf-8", b"success")

            return

        self._send(200, "application/xml; charset=utf-8", reply.encode("utf-8"))

    def _wecom_reply(self, settings: dict, aes_key: bytes, message: dict):
        """只对文本消息回一条固定文案。别的类型一律回 success，免得答非所问"""
        if message.get("MsgType") != "text":
            return None

        reply_xml = (
            "<xml>"
            f"<ToUserName><![CDATA[{message.get('FromUserName', '')}]]></ToUserName>"
            f"<FromUserName><![CDATA[{message.get('ToUserName', '')}]]></FromUserName>"
            f"<CreateTime>{int(time.time())}</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[已收到。这里是 Bili23 下载器面板，只主动推送下载结果，不接受对话指令。]]></Content>"
            "</xml>"
        )

        try:
            return wecom_callback.encrypt_reply(
                settings["wecom_callback_token"], aes_key, reply_xml, settings["wecom_corp_id"],
            )

        except wecom_callback.CallbackError as e:
            # 回复构造失败不影响"消息已经收到"这件事，别把 200 变成 5xx
            # 让企微白重试三次
            logger.warning("企业微信被动回复构造失败：%s", e)

            return None

    def authenticated(self) -> bool:
        """
        会话 Cookie 或 API 令牌，任一有效即放行

        令牌那条路是留给脚本与自动化的：它不方便先登录拿 Cookie。
        两个入口共用一套鉴权判定，不各自开一套
        """
        session = self.cookie(SESSION_COOKIE)

        if session and self.server.sessions.validate(session):
            return True

        return self.token_authenticated()

    def token_authenticated(self) -> bool:
        """
        校验访问令牌：请求头优先，其次查询参数（便于用链接直接打开发送）

        用 compare_digest 做定时安全比较 —— 面板通常暴露在局域网里，
        逐字符比较会把令牌的长度与前缀泄露给旁听者
        """
        supplied = self.headers.get("X-Panel-Token", "")

        if not supplied:
            _, _, query = self.path.partition("?")
            supplied = parse_query_token(query)

        return bool(supplied) and secrets.compare_digest(supplied, self.server.token)

    def current_user(self) -> str:
        """当前登录的用户名。用令牌进来的请求没有用户名，返回空串"""
        return self.server.sessions.validate(self.cookie(SESSION_COOKIE))

    def cookie(self, name: str) -> str:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))

        except Exception:
            # Cookie 头是客户端随口给的，畸形输入属于正常情况
            return ""

        morsel = jar.get(name)

        return morsel.value if morsel else ""

    def set_cookie(self, name: str, value: str, max_age: int = 0):
        jar = SimpleCookie()
        jar[name] = value
        jar[name]["path"] = "/"
        jar[name]["httponly"] = True
        # Lax 而不是 Strict：面板常被收藏夹或聊天里的链接直接打开，
        # Strict 会让这种"从别的站点点进来"的访问不带 Cookie，表现为刚登录又要求登录
        jar[name]["samesite"] = "Lax"

        # 只在 https 上加 Secure。面板本体是纯 http（TLS 由反代终止），无条件加的话
        # 从局域网 http 直连打开的用户会"登录成功却立刻又要求登录" —— 浏览器不会把
        # Secure Cookie 存进 http 上下文
        if self._via_https():
            jar[name]["secure"] = True

        if max_age:
            jar[name]["max-age"] = max_age

        self._cookies.append(jar[name].OutputString())

    def clear_cookie(self, name: str):
        jar = SimpleCookie()
        jar[name] = ""
        jar[name]["path"] = "/"
        jar[name]["max-age"] = 0

        self._cookies.append(jar[name].OutputString())

    def _read_json(self, allow_empty: bool = False):
        """
        读请求体并解析成对象；失败时已经回过响应，返回 None

        allow_empty 是给面板自己的接口留的：它们多数没有参数（登出、取会话信息），
        页面对这类调用直接发 `{}`，但手工 curl 时常常连 body 都不给
        """
        body = self._read_body(allow_empty)

        if body is None:
            return None

        try:
            payload = loads(body)

        except (JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON"})

            return None

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "expected a JSON object"})

            return None

        return payload

    def _read_body(self, allow_empty: bool = False):
        try:
            length = int(self.headers.get("Content-Length", 0))

        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid Content-Length"})

            return None

        if length <= 0:
            if allow_empty:
                return b"{}"

            self._send_json(400, {"error": "empty request body"})

            return None

        if length > MAX_BODY_SIZE:
            # 先把 body 读掉再回 413：不读的话客户端还在写，撞上的是连接重置
            # 而不是这个响应，它只会看到一个没头没脑的网络错误
            self._drain_body()
            self._send_json(413, {"error": "request body too large"})

            return None

        return self.rfile.read(length)

    def _drain_body(self):
        # 请求被拒时也要把 body 读掉，否则客户端会撞上连接重置而不是收到响应。
        # 上限与 MAX_BODY_SIZE 分开：这一层防的是客户端声明一个天量
        # Content-Length 把服务端拖在读取上，而不是请求本身该有多大
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)

        except (TypeError, ValueError):
            return

        remaining = min(length, DRAIN_LIMIT)

        while remaining > 0:
            chunk = self.rfile.read(min(64 * 1024, remaining))

            if not chunk:
                break

            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict):
        self._send(status, "application/json; charset=utf-8", dumps_bytes(payload))

    def _via_https(self) -> bool:
        """
        请求是不是经由 https 进来的

        只看 X-Forwarded-Proto：面板自己不终止 TLS，是反代（Lucky）加上这一项。
        这个头请求方可以伪造，但伪造它只会让自己的响应带上 Secure / HSTS，
        结果是自己在 http 下拿不到 Cookie —— 收益为负，不构成攻击面
        """
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()

        return forwarded == "https"

    def _write_security_headers(self):
        """页面、接口、重定向共用的安全响应头（放在响应行之后、Set-Cookie 之前）"""
        # 页面与接口都在同一端口；不允许被任何外部页面 iframe 引用，
        # 免得把面板套进钓鱼页
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

        # 整页是内联的 HTML/CSS/JS，不与任何外部源打交道，于是可以收到最紧：
        # 默认什么都不许，只放开内联脚本/样式与同源请求。防的不是面板自己的代码，
        # 而是"有内容被注进来"这种情况（比如某条日志行里带了一段外部图片），
        # 让它在浏览器侧连发都发不出去
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )

        # 只经 https 时声明。面板还有个 http 入口（局域网 23331），在那儿发没意义 ——
        # 浏览器只对 https 响应记 HSTS。不加 includeSubDomains：这台反代上还挂着
        # 别的域名，不该替它们做决定
        if self._via_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _write_pending_cookies(self):
        """把本次响应攒下的 Set-Cookie 都发出去"""
        for line in self._cookies:
            self.send_header("Set-Cookie", line)

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))

        # 面板页与接口都是动态内容，样式与脚本全部内联，没有任何可缓存的静态资源。
        # 不给缓存头的话浏览器会按"启发式缓存"自己决定存多久 —— 后果是面板更新后
        # 旧标签页照旧显示老界面，用户会以为没部署上（实测：新增侧栏页后看不到）
        self.send_header("Cache-Control", "no-store, must-revalidate")

        self._write_security_headers()
        self._write_pending_cookies()

        self.end_headers()
        self.wfile.write(body)

def parse_query_token(query: str) -> str:
    """
    从查询串里取 token

    不用 parse_qs：它会按 `&` 与 `=` 拆解并解码整串，而这里只需要其中一项。
    令牌可能出现在 `?token=xxx` 的位置，其余参数一概无关
    """
    from urllib.parse import unquote

    for item in query.split("&"):
        key, _, value = item.partition("=")

        if key == "token" and value:
            return unquote(value)

    return ""

def parse_query(url: str) -> dict:
    """
    查询串 -> 扁平字典，供 GET 路由当参数用

    同名参数只取最后一个：面板的参数都是单值的，取第一个反而会让
    `?lines=100&lines=5000` 这种构造绕过上限
    """
    _, _, query = url.partition("?")

    if not query:
        return {}

    return {key: values[-1] for key, values in parse_qs(query, keep_blank_values = True).items()}

class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, registry, token: str,
                 credentials: PanelCredentials = None, sessions: SessionStore = None,
                 login_throttle: LoginThrottle = None):
        super().__init__(address, handler)

        self.registry = registry
        self.token = token

        # 三个默认值只为让测试能只传前四个参数就跑起来；
        # 真实启动路径一定会显式传入从配置读出来的那一份
        self.credentials = credentials or PanelCredentials.default()
        self.sessions = sessions or SessionStore()
        self.login_throttle = login_throttle or LoginThrottle()

class WebPanelManager:
    """面板服务器的启停。与 MCPServerManager 结构一致，便于对照维护"""

    def __init__(self):
        self.server: PanelHTTPServer | None = None
        self.thread: Thread | None = None

    def start(self) -> bool:
        if self.server is not None:
            return True

        token = config.get(config.web_panel_token)

        if not token:
            # 首次开启时现生成一个并落盘，脚本与自动化可以拿它免登录调用
            token = generate_token()

            config.set(config.web_panel_token, token)

        # 账号缺失时这一步会补上默认的 admin / password 并落盘。
        # start() 跑在 GUI 线程（main.py 的启动任务里），可以直接调
        username, password_hash = ensure_credentials()

        port = config.get(config.web_panel_port)
        bind_host = resolve_bind_host(WEB_BIND_HOST_ENV)

        # 只在真正启动时才导入：面板关着的时候，解析与下载链路不该进启动路径
        from ..mcp.tools import build_registry

        try:
            # 默认只绑环回地址；容器里由 BILI23_WEB_HOST 显式放开，见 common/net.py
            self.server = PanelHTTPServer(
                (bind_host, port),
                WebPanelHandler,
                build_registry(),
                token,
                credentials = PanelCredentials(username, password_hash),
                sessions = SessionStore(),
            )

        except OSError as e:
            # 端口被占用不能拖垮程序启动
            logger.error("Web 面板启动失败，%s:%d：%s", bind_host, port, e)

            self.server = None

            return False

        self.thread = Thread(target = self.server.serve_forever, name = "web-panel", daemon = True)
        self.thread.start()

        logger.info("Web 面板已启动，监听 %s:%d，账号 %s", bind_host, port, username)

        return True

    def stop(self, timeout: float = 2.0):
        server, thread = self.server, self.thread

        # 先摘引用再关：即便下面抛异常，也不会留下一个已死的 server 让下次 start() 直接返回 True
        self.server = None
        self.thread = None

        if server is None:
            return

        try:
            server.shutdown()
            server.server_close()

        except Exception:
            logger.exception("停止 Web 面板失败")

        if thread is not None:
            thread.join(timeout)

web_panel_manager = WebPanelManager()
