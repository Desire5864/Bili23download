"""
通知推送 —— 下载完成/失败时推一条到企业微信应用与 Telegram

容器版常年无人值守：任务排进去就不再盯着面板了，「下完了说一声」才是刚需。
两个渠道都只依赖标准库（urllib），不引第三方 SDK —— 容器里少装一个包就少
一份构建期的失败面。

推送时机不是「每完成一集推一条」：一个合集几十集是常态，逐集推会把手机刷爆。
完成通知走两段判定：一是**静默期**（BATCH_DELAY），期间每来一条就重新计时；
二是**队列里还有任务在跑就继续等** —— 合并、后处理会让完成事件之间出现几十秒
空档，光看静默期，一个 25 集的合集会被拆成好几条通知。两条都过了才汇总成一条，
正文按合集分块，一块四行（剧名 / 年份 / 集数 / 状态）。这与桌面版的语义一致 ——
那边也是「下载列表空了才弹一次」。

本模块不碰 Qt、不碰 HTTP 服务，供两处调用：
  · util/download/task/manager.py —— 任务完成时（下载流程，非 GUI 线程）
  · util/web/server.py —— 面板「通知」页的读写与测试发送

线程约束：``send_*`` 都会真的发网络请求，调用方**必须**放到后台线程；
``apply_settings`` 会写配置（config.set 发 Qt 信号），**必须**在 GUI 线程调用。
"""

from .config import config

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

logger = logging.getLogger(__name__)


class NotifyError(ValueError):
    """配置校验不通过。文案直接面向用户，由调用方决定怎么呈现"""


# ---------------------------------------------------------------------------
# 校验规则
#
# 这几条不只是"格式对不对"：值最终会被拼进请求 URL，或作为代理写进 opener。
# 放宽一点就等于给面板开了一条由服务端代为发请求的通道。

# 企业微信 API。走**自建应用**：先 gettoken 换 access_token，再 message/send
# 点名发给指定成员。不用群机器人 webhook 是因为它只能发到它所在的那个群，
# 且 URL 里的 key 泄露即永久有效
WECOM_API_HOST = "https://qyapi.weixin.qq.com"

# 🔴 中转地址只换 host：后面那截路径由我们拼死（{基地址} + /cgi-bin/…），
# 用户改不了 —— 面板会让服务端去打用户填的地址，若路径也能随便写，就等于
# 开了一条 SSRF 通道（填 http://127.0.0.1:23331 再自己指定路径就能让容器去打
# 内网服务）。现在他能左右的只有"打到哪台机器的 /cgi-bin/ 下"，而 host 必须
# 放开：中转服务是用户自建的，IP 不固定（实测那台是 47.117.88.76:3100）。
# 另外不接受查询串与 # 片段，免得参数被塞到别处改写请求语义
WECOM_API_PATH_PREFIX = "/cgi-bin/"

TELEGRAM_API = "https://api.telegram.org/bot%s/sendMessage"

# 企业 ID：形如 ww182aa9502bd5aff3（早期企业是 wx 开头），字母数字
WECOM_CORP_RE = re.compile(r"[A-Za-z0-9]{6,32}")

# 应用 AgentId：纯数字。它要作为 JSON 数字提交，所以不能放宽成任意串
WECOM_AGENT_RE = re.compile(r"\d{1,12}")

# 应用 Secret：一串字母数字（官方是 43 位），留点余量
WECOM_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{10,64}")

# 接收人：企业微信的成员账号（UserID），或 @all 表示全企业。
# API 要求用 | 分隔，但用户习惯敲逗号 —— 几种分隔符都收下，存进去统一成 |
WECOM_TOUSER_SPLIT_RE = re.compile(r"[|,，、;；\s]+")
WECOM_TOUSER_RE = re.compile(r"[A-Za-z0-9_.@\-]{1,64}")
WECOM_ALL = "@all"

# 回调凭据（与上面那组的方向相反：企业微信推给我们）。
# Token 是企微后台里我们自己填的一串字母数字；EncodingAESKey 固定 43 位
# Base64 字符。两者都会被 wecom_callback 拿去算签名/解 AES，字符集收紧一点
# 没有坏处 —— 它们同样不该是"能塞进任何东西"的字段
WECOM_CALLBACK_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,32}")
WECOM_AES_KEY_RE = re.compile(r"[A-Za-z0-9]{43}")

# Bot token 由 Telegram 签发，形如 123456789:AAH...（数字 + 冒号 + 35 位左右）。
# 它会被拼进 URL 路径，所以字符集必须收紧 —— 放行 / 和 .. 就能改写请求目标
TELEGRAM_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# chat_id 可以是数字（群为负数）、私聊数字、或 @频道名
CHAT_ID_RE = re.compile(r"(?:-\d{1,20}|\d{1,20}|@[A-Za-z0-9_]{4,32})")

MAX_URL_LENGTH = 500
MAX_TOKEN_LENGTH = 200
MAX_CHAT_ID_LENGTH = 64
MAX_PROXY_LENGTH = 200
MAX_CORP_ID_LENGTH = 32
MAX_AGENT_ID_LENGTH = 12
MAX_SECRET_LENGTH = 64
MAX_TOUSER_LENGTH = 1024
MAX_TOUSER_COUNT = 100

# 回调（接收消息）的两个凭据。长度由企业微信规定死：Token 不超 32 位，
# EncodingAESKey 恒为 43 位（Base64 编码后的 32 字节 AES 密钥）
MAX_CALLBACK_TOKEN_LENGTH = 32
MAX_AES_KEY_LENGTH = 43

# 单次请求超时。推送是"尽力而为"，卡住十几秒就该放弃 —— 它跑在后台线程上，
# 但线程池不是无限的
REQUEST_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# 批合并与历史

# 完成通知的静默期（秒）。每收到一条完成事件就把计时往后推
BATCH_DELAY = 20.0

# 单条通知里最多列几条片名，超出的折成"等 N 个任务"。
# 只对「认不出合集名」的那些条目生效 —— 认得出的会被压成「西游记 第1-25集」
BATCH_MAX_TITLES = 15

# 发送历史，面板「最近发送记录」用它。环形覆盖，不落盘 —— 这是排查用的
# 即时信息，重启后重新累积即可
HISTORY_SIZE = 20

HISTORY = deque(maxlen = HISTORY_SIZE)

# 收到的回调（验证请求与业务消息）。同样只留在内存里 —— 这些是运维观测数据，
# 没必要落盘，重启后重新积累即可
CALLBACK_HISTORY = deque(maxlen = HISTORY_SIZE)

# 待汇总的完成事件与它的计时器。timer 由 _PENDING_LOCK 保护
_PENDING_LOCK = threading.Lock()
_PENDING = []
_TIMER = None

# 发送动作本身也要串行：两个渠道同时发、面板又在点"测试发送"时，
# 历史里的时间戳与顺序才有意义
_SEND_LOCK = threading.Lock()

# 「整批是否还在跑」的探针。默认实现去任务库点数（见 _batch_still_running），
# 测试里可以直接替换掉它 —— 否则为了几条断言真去建一个任务库，
# 还得把 appdata 目录一并搬进沙箱，得不偿失
RUNNING_PROBE = None

# 算作「还在跑」的状态。**不含暂停与失败**：暂停的任务可能一停一整夜，
# 失败的任务会一直挂在未完成表里等用户处置 —— 把这两种算进去，
# 通知就永远发不出来了
RUNNING_STATUS_NAMES = (
    "QUEUED", "PARSING", "DOWNLOADING",
    "FFMPEG_QUEUED", "MERGING", "CONVERTING", "ADDITIONAL_PROCESSING",
)

CHANNEL_LABELS = {
    "wecom": "企业微信",
    "telegram": "Telegram",
}

# 企业微信的必填凭据与它们的显示名。缺任何一项都发不出去，"启用"的判据就是这四项
WECOM_LABELS = (
    ("wecom_corp_id", "企业 ID"),
    ("wecom_agent_id", "应用 AgentId"),
    ("wecom_secret", "应用 Secret"),
    ("wecom_touser", "指定接收人"),
)

WECOM_REQUIRED = tuple(key for key, _ in WECOM_LABELS)

# 回调（接收消息）的必填项。「对外访问地址」不在里面 —— 它只是给用户复制
# 地址用的，留空时前端会拿浏览器地址栏兜底，缺了不影响回调本身
CALLBACK_LABELS = (
    ("wecom_callback_token", "回调 Token"),
    ("wecom_aes_key", "EncodingAESKey"),
)

# 🔴 真值绝不出服务端的四项。这四项都是"拿到就能冒充你"的东西：
#   · 应用 Secret     —— 能以你的名义给全公司发消息
#   · 回调 Token      —— 能算出合法签名，伪造一条"来自企业微信"的推送
#   · EncodingAESKey —— 同上，且能解开历史上抓到的密文
#   · Bot Token       —— 能以你的机器人身份发言
# 面板下发配置、导出配置、日志打印都按这张表脱敏（导出那侧另有 SENSITIVE_CONFIG_KEYS，
# 那张表用的是 config.py 的 (组, 键) 形式，覆盖范围更大；这张表只管通知页的下发）
SECRET_KEYS = (
    "wecom_secret",
    "wecom_callback_token",
    "wecom_aes_key",
    "telegram_token",
)


# ---------------------------------------------------------------------------
# 配置

def _as_bool(value, label: str) -> bool:
    """只认真正的布尔与 0/1。字符串 "false" 会被 bool() 判成 True，必须挡住"""
    if isinstance(value, bool):
        return value

    if isinstance(value, int) and value in (0, 1):
        return bool(value)

    raise NotifyError(f"{label}只能是开或关")


def _as_text(value, label: str, limit: int, allow_empty: bool = True) -> str:
    text = str(value or "").strip()

    if not text:
        if allow_empty:
            return ""

        raise NotifyError(f"{label}不能为空")

    if len(text) > limit:
        raise NotifyError(f"{label}过长（最多 {limit} 个字符）")

    # 控制字符会把日志行撕开，也可能被下游当成换行注入到消息里
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise NotifyError(f"{label}不能包含换行或控制字符")

    return text


def validate_wecom_corp_id(value) -> str:
    corp_id = _as_text(value, "企业 ID", MAX_CORP_ID_LENGTH)

    if corp_id and not WECOM_CORP_RE.fullmatch(corp_id):
        raise NotifyError("企业 ID 是一串字母数字，形如 ww182aa9502bd5aff3")

    return corp_id


def validate_wecom_agent_id(value) -> str:
    agent_id = _as_text(value, "应用 AgentId", MAX_AGENT_ID_LENGTH)

    if agent_id and not WECOM_AGENT_RE.fullmatch(agent_id):
        raise NotifyError("应用 AgentId 是纯数字，在企业微信应用详情页顶部")

    return agent_id


def validate_wecom_secret(value) -> str:
    secret = _as_text(value, "应用 Secret", MAX_SECRET_LENGTH)

    if secret and not WECOM_SECRET_RE.fullmatch(secret):
        raise NotifyError("应用 Secret 只含字母、数字、下划线与连字符，请从应用详情页复制")

    return secret


def validate_wecom_touser(value) -> str:
    """接收人。存成企业微信要的 | 分隔形式，用户敲逗号也认"""
    raw = _as_text(value, "指定接收人", MAX_TOUSER_LENGTH)

    if not raw:
        return ""

    members = [m for m in WECOM_TOUSER_SPLIT_RE.split(raw) if m]

    if len(members) > MAX_TOUSER_COUNT:
        raise NotifyError(f"指定接收人最多 {MAX_TOUSER_COUNT} 个")

    for member in members:
        if not WECOM_TOUSER_RE.fullmatch(member):
            raise NotifyError(f"接收人「{member}」不合法：填成员账号（不是姓名），或 @all")

    # 去重保序：企业微信那边重复的 UserID 算同一人，但存一个 "a|a" 看着像配错了
    seen, unique = set(), []

    for member in members:
        if member not in seen:
            seen.add(member)
            unique.append(member)

    return "|".join(unique)


def validate_wecom_api_base(value) -> str:
    """
    消息代理地址 —— 企业微信 API 的中转基地址，用来固定出口 IP

    🔴 它与 ``proxy`` 不是一回事：proxy 是 HTTP 代理（跳板），这里是**换基地址**
    （请求原样发给它，由它代发）。实测 ``http://47.117.88.76:3100/cgi-bin/gettoken``
    直接返回企业微信的 JSON，且 errmsg 里写着 ``from ip: 47.117.88.76`` —— 出口
    正是那台机器，这就是"需 IPv4 地址"的那台中转
    """
    base = _as_text(value, "消息代理地址", MAX_URL_LENGTH)

    if not base:
        return ""

    parts = urllib.parse.urlsplit(base)

    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotifyError("消息代理地址要以 http:// 或 https:// 开头，形如 http://1.2.3.4:3100")

    if parts.username or parts.password:
        raise NotifyError("消息代理地址不支持带用户名密码")

    if parts.query or parts.fragment:
        raise NotifyError("消息代理地址不要带 ? 参数或 # 片段，只填到端口即可")

    # 去掉尾斜杠，后面拼 /cgi-bin/... 才不会是 //
    return base.rstrip("/")


def validate_callback_token(value) -> str:
    """
    回调 Token —— 与企微后台「接收消息服务器配置」里填的必须一字不差

    它参与签名计算，写错的表现是回调 URL 一直验证不过（企微只提示"配置失败"，
    不会告诉你错在哪一项），所以这里把格式收紧，先把明显的错拦在保存那一步
    """
    token = _as_text(value, "回调 Token", MAX_CALLBACK_TOKEN_LENGTH)

    if token and not WECOM_CALLBACK_TOKEN_RE.fullmatch(token):
        raise NotifyError("回调 Token 只能是 3~32 位字母或数字")

    return token


def validate_aes_key(value) -> str:
    """EncodingAESKey —— 恒 43 位，多一位少一位都解不开"""
    key = _as_text(value, "EncodingAESKey", MAX_AES_KEY_LENGTH)

    if key and not WECOM_AES_KEY_RE.fullmatch(key):
        raise NotifyError("EncodingAESKey 固定 43 位字母或数字，请从企微后台原样复制")

    return key


def validate_callback_base(value) -> str:
    """
    面板的对外地址，只为拼出完整回调 URL 给用户复制

    留空时前端用浏览器地址栏的 origin 兜底 —— 但从局域网 IP 打开面板时那个
    地址填进企微后台是不通的，所以允许显式写死一个公网地址

    🔴 主机名一律转小写。反代（本机实测是 Lucky）是**按字符串比对域名**的，
    大小写不同会当成两个站：`Bili23.892639.xyz` 走到默认站点吃 404，
    而 `bili23.892639.xyz` 才是那条真正的规则。用户在地址栏里怎么敲的不该
    决定企微那边能不能通，所以这里统一压成小写再存
    """
    base = _as_text(value, "对外访问地址", MAX_URL_LENGTH)

    if not base:
        return ""

    parts = urllib.parse.urlsplit(base)

    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotifyError("对外访问地址要以 http:// 或 https:// 开头，形如 https://bili23.example.com:2662")

    if parts.username or parts.password:
        raise NotifyError("对外访问地址不支持带用户名密码")

    if parts.query or parts.fragment:
        raise NotifyError("对外访问地址不要带 ? 参数或 # 片段，只填到端口即可")

    # 端口显式写出来（urlsplit 会把 :443 之类抹掉），路径前缀保留 ——
    # 有人把面板挂在 /bili23 之下也是合法用法
    netloc = parts.hostname.lower() if parts.port is None else f"{parts.hostname.lower()}:{parts.port}"

    return f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"


def validate_telegram_token(token: str) -> str:
    token = _as_text(token, "Telegram Bot Token", MAX_TOKEN_LENGTH)

    if not token:
        return ""

    if not TELEGRAM_TOKEN_RE.fullmatch(token):
        raise NotifyError("Telegram Bot Token 形如 123456789:AAH...，请向 @BotFather 索取")

    return token


def validate_chat_id(chat_id: str) -> str:
    chat_id = _as_text(chat_id, "Telegram Chat ID", MAX_CHAT_ID_LENGTH)

    if not chat_id:
        return ""

    if not CHAT_ID_RE.fullmatch(chat_id):
        raise NotifyError("Telegram Chat ID 可以是数字、负数（群）或 @频道名")

    return chat_id


def validate_proxy(proxy: str) -> str:
    """
    代理。Telegram 的 API 在境外，容器没有代理根本连不上；
    企业微信在国内，走不走代理都能通，所以这个字段是两个渠道共用的
    """
    proxy = _as_text(proxy, "代理地址", MAX_PROXY_LENGTH)

    if not proxy:
        return ""

    parts = urllib.parse.urlsplit(proxy)

    if parts.scheme not in ("http", "https"):
        raise NotifyError("代理地址要以 http:// 或 https:// 开头")

    if not parts.hostname or not parts.port:
        raise NotifyError("代理地址要写成 http://主机:端口")

    if parts.username or parts.password:
        raise NotifyError("代理地址不支持带用户名密码")

    return proxy


def normalize_settings(values: dict) -> dict:
    """校验并归一化一组提交值。保存与测试发送共用同一条闸门"""
    if not isinstance(values, dict):
        raise NotifyError("没有要保存的设置")

    clean = {
        "wecom_enabled": _as_bool(values.get("wecom_enabled", False), "企业微信通知开关"),
        "wecom_corp_id": validate_wecom_corp_id(values.get("wecom_corp_id")),
        "wecom_agent_id": validate_wecom_agent_id(values.get("wecom_agent_id")),
        "wecom_secret": validate_wecom_secret(values.get("wecom_secret")),
        "wecom_touser": validate_wecom_touser(values.get("wecom_touser")),
        "wecom_api_base": validate_wecom_api_base(values.get("wecom_api_base")),
        "wecom_callback_enabled": _as_bool(values.get("wecom_callback_enabled", False), "企业微信回调开关"),
        "wecom_callback_token": validate_callback_token(values.get("wecom_callback_token")),
        "wecom_aes_key": validate_aes_key(values.get("wecom_aes_key")),
        "wecom_callback_base": validate_callback_base(values.get("wecom_callback_base")),
        "telegram_enabled": _as_bool(values.get("telegram_enabled", False), "Telegram 通知开关"),
        "telegram_token": validate_telegram_token(values.get("telegram_token")),
        "telegram_chat_id": validate_chat_id(values.get("telegram_chat_id")),
        "proxy": validate_proxy(values.get("proxy")),
        "on_complete": _as_bool(values.get("on_complete", True), "下载完成通知开关"),
        "on_fail": _as_bool(values.get("on_fail", False), "下载失败通知开关"),
    }

    # 开着开关却没有凭据，是最容易犯的配置错误：等到真下完一集才发现没推出去。
    # 保存时就拦下来，比事后翻日志友好。四项一起报，免得"填一个提示一次"
    if clean["wecom_enabled"]:
        missing = missing_wecom_fields(clean)

        if missing:
            raise NotifyError("启用企业微信通知前，请先填写：" + "、".join(missing))

    if clean["telegram_enabled"] and not clean["telegram_token"]:
        raise NotifyError("启用 Telegram 通知前，请先填写 Bot Token")

    if clean["telegram_enabled"] and not clean["telegram_chat_id"]:
        raise NotifyError("启用 Telegram 通知前，请先填写 Chat ID")

    # 回调开着却没凭据，企微那边会一直显示"配置失败"，而我们这边只会静默 403。
    # 与其让人对着两个界面猜，不如保存时就说明白缺哪一个
    if clean["wecom_callback_enabled"]:
        missing = [label for key, label in CALLBACK_LABELS if not clean[key]]

        if missing:
            raise NotifyError("启用企业微信回调前，请先填写：" + "、".join(missing))

    return clean


def load_settings() -> dict:
    """读当前生效值。凭据按原值返回 —— 是否掩码由调用方（面板）决定"""
    return {
        "wecom_enabled": bool(config.get(config.notification_wecom_enabled)),
        "wecom_corp_id": str(config.get(config.notification_wecom_corp_id) or "").strip(),
        "wecom_agent_id": str(config.get(config.notification_wecom_agent_id) or "").strip(),
        "wecom_secret": str(config.get(config.notification_wecom_secret) or "").strip(),
        "wecom_touser": str(config.get(config.notification_wecom_touser) or "").strip(),
        "wecom_api_base": str(config.get(config.notification_wecom_api_base) or "").strip(),
        "wecom_callback_enabled": bool(config.get(config.notification_wecom_callback_enabled)),
        "wecom_callback_token": str(config.get(config.notification_wecom_callback_token) or "").strip(),
        "wecom_aes_key": str(config.get(config.notification_wecom_aes_key) or "").strip(),
        "wecom_callback_base": str(config.get(config.notification_wecom_callback_base) or "").strip(),
        "telegram_enabled": bool(config.get(config.notification_telegram_enabled)),
        "telegram_token": str(config.get(config.notification_telegram_token) or "").strip(),
        "telegram_chat_id": str(config.get(config.notification_telegram_chat_id) or "").strip(),
        "proxy": str(config.get(config.notification_proxy) or "").strip(),
        "on_complete": bool(config.get(config.notification_on_complete)),
        "on_fail": bool(config.get(config.notification_on_fail)),
    }


def apply_settings(values: dict) -> dict:
    """
    写进配置并回读。

    🔴 config.set 会发 Qt 信号，只能在 GUI 线程调用 —— 面板侧必须包一层
    call_in_main_thread（与命名规则、名称识别的保存同一条纪律）
    """
    clean = normalize_settings(values)

    config.set(config.notification_wecom_enabled, clean["wecom_enabled"])
    config.set(config.notification_wecom_corp_id, clean["wecom_corp_id"])
    config.set(config.notification_wecom_agent_id, clean["wecom_agent_id"])
    config.set(config.notification_wecom_secret, clean["wecom_secret"])
    config.set(config.notification_wecom_touser, clean["wecom_touser"])
    config.set(config.notification_wecom_api_base, clean["wecom_api_base"])
    config.set(config.notification_wecom_callback_enabled, clean["wecom_callback_enabled"])
    config.set(config.notification_wecom_callback_token, clean["wecom_callback_token"])
    config.set(config.notification_wecom_aes_key, clean["wecom_aes_key"])
    config.set(config.notification_wecom_callback_base, clean["wecom_callback_base"])
    config.set(config.notification_telegram_enabled, clean["telegram_enabled"])
    config.set(config.notification_telegram_token, clean["telegram_token"])
    config.set(config.notification_telegram_chat_id, clean["telegram_chat_id"])
    config.set(config.notification_proxy, clean["proxy"])
    config.set(config.notification_on_complete, clean["on_complete"])
    config.set(config.notification_on_fail, clean["on_fail"])

    return load_settings()


def missing_wecom_fields(settings: dict) -> list:
    """还没填的企微凭据（显示名）。四项一起填的时候，一次报全比一次报一个强"""
    return [
        label for key, label in WECOM_LABELS
        if not str(settings.get(key) or "").strip()
    ]


def wecom_ready(settings: dict) -> bool:
    """企业微信的四项凭据齐了没。判断与实际发送读同一份数据，不会各说各话"""
    return not missing_wecom_fields(settings)


def callback_ready(settings: dict) -> bool:
    """回调的两个凭据齐了没。回调端点的放行判据与它读同一份数据"""
    return not [key for key, _ in CALLBACK_LABELS if not str(settings.get(key) or "").strip()]


def record_callback(ok: bool, detail: str):
    """
    回调事件单独记一条历史

    不并进上面那条发送历史：那个列表回答的是"我推出去的到没到"，
    混进"别人推进来的"会让两边都说不清
    """
    CALLBACK_HISTORY.appendleft({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(ok),
        "detail": detail,
    })


def callback_history() -> list:
    """最近收到的回调（新→旧）"""
    return list(CALLBACK_HISTORY)


def enabled_channels(settings: dict = None) -> list:
    """当前开启了且凭据齐备的渠道。判断与实际发送走同一个函数，不会各说各话"""
    settings = settings or load_settings()
    channels = []

    if settings["wecom_enabled"] and wecom_ready(settings):
        channels.append("wecom")

    if settings["telegram_enabled"] and settings["telegram_token"] and settings["telegram_chat_id"]:
        channels.append("telegram")

    return channels


# ---------------------------------------------------------------------------
# 发送

def _open(request, proxy: str):
    """
    发一个请求

    🔴 显式带一个 ProxyHandler：不带的话 urllib 会读环境变量里的 http_proxy。
    本机开发环境常挂着系统代理（127.0.0.1:52263），容器里则可能是运维留下的
    全局代理 —— 两者都会让"明明填了代理"或"明明没填代理"的预期落空。
    这里把走不走代理完全交给配置决定
    """
    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    else:
        # 空字典 = 所有协议都不用代理
        handlers = [urllib.request.ProxyHandler({})]

    opener = urllib.request.build_opener(*handlers)

    return opener.open(request, timeout = REQUEST_TIMEOUT)


def _read_json(response) -> dict:
    body = response.read().decode("utf-8", "replace")

    try:
        return json.loads(body or "{}")

    except json.JSONDecodeError:
        # 网关拦截页、代理认证页都会走到这里：返回的是一段 HTML 而不是 JSON
        raise NotifyError(f"接口返回的不是 JSON（前 120 字）：{body[:120]}")


def _get_json(url: str, proxy: str = "") -> dict:
    with _open(urllib.request.Request(url, method = "GET"), proxy) as response:
        return _read_json(response)


def _post_json(url: str, payload: dict, proxy: str = "") -> dict:
    data = json.dumps(payload, ensure_ascii = False).encode("utf-8")

    request = urllib.request.Request(
        url, data = data, method = "POST",
        headers = {"Content-Type": "application/json; charset=utf-8"}
    )

    with _open(request, proxy) as response:
        return _read_json(response)


# access_token 缓存：(企业 ID, Secret, 基地址) -> (token, 过期时间戳)
#
# 企业微信的 gettoken 有调用频率限制，而通知是"每下完一批就发"的高频动作 ——
# 不缓存的话一个合集就要去打几十次。官方给的 expires_in 是 7200 秒
_WECOM_TOKEN_LOCK = threading.Lock()
_WECOM_TOKENS = {}


def _wecom_base(settings: dict) -> str:
    """企业微信 API 的基地址。填了中转就用中转"""
    return settings.get("wecom_api_base") or WECOM_API_HOST


def _wecom_access_token(settings: dict) -> str:
    corp_id = settings["wecom_corp_id"]
    secret = settings["wecom_secret"]
    base = _wecom_base(settings)
    key = (corp_id, secret, base)
    now = time.time()

    with _WECOM_TOKEN_LOCK:
        cached = _WECOM_TOKENS.get(key)

        if cached and cached[1] > now:
            return cached[0]

        query = urllib.parse.urlencode({"corpid": corp_id, "corpsecret": secret})
        data = _get_json(f"{base}/cgi-bin/gettoken?{query}", settings["proxy"])

        if data.get("errcode") not in (0, None) or not data.get("access_token"):
            raise NotifyError(
                "获取企业微信 access_token 失败："
                f"{data.get('errcode')} {data.get('errmsg') or '未知错误'}".strip()
            )

        # 提前 5 分钟作废，免得刚好卡在过期边界上用掉一个已失效的 token
        expires_in = data.get("expires_in") or 7200
        _WECOM_TOKENS[key] = (data["access_token"], now + max(60.0, float(expires_in) - 300.0))

        return data["access_token"]


def _send_wecom(settings: dict, title: str, text: str) -> str:
    token = _wecom_access_token(settings)
    content = f"{title}\n{text}" if text else title

    payload = {
        # touser 存的就是 | 分隔的成员账号（validate_wecom_touser 归一过）
        "touser": settings["wecom_touser"],
        "msgtype": "text",
        # agentid 必须是数字，字符串会被企业微信判成参数错误
        "agentid": int(settings["wecom_agent_id"]),
        "text": {"content": content},
        # safe=0：下载完成的通知不是敏感信息，不用"保密消息"那一套（那会让
        # 消息在客户端里带水印、不能转发）
        "safe": 0,
    }

    url = f"{_wecom_base(settings)}/cgi-bin/message/send?access_token={urllib.parse.quote(token)}"
    data = _post_json(url, payload, settings["proxy"])

    if data.get("errcode") not in (0, None):
        raise NotifyError(f"企业微信返回 {data.get('errcode')}：{data.get('errmsg') or '未知错误'}")

    return "已送达"


def _send_telegram(settings: dict, title: str, text: str) -> str:
    url = TELEGRAM_API % settings["telegram_token"]

    payload = {
        "chat_id": settings["telegram_chat_id"],
        "text": f"{title}\n{text}" if text else title,
        "disable_web_page_preview": True,
    }

    data = _post_json(url, payload, settings["proxy"])

    if not data.get("ok"):
        raise NotifyError(f"Telegram 返回：{data.get('description') or '未知错误'}")

    return "已送达"


_SENDERS = {"wecom": _send_wecom, "telegram": _send_telegram}


def _record(channel: str, ok: bool, detail: str):
    HISTORY.appendleft({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": CHANNEL_LABELS.get(channel, channel),
        "ok": bool(ok),
        "detail": detail,
    })


def send(channel: str, settings: dict, title: str, text: str = "") -> tuple:
    """
    向单个渠道发一条，返回 (成功?, 说明)

    不抛异常：调用点大多在下载流程里，推送失败不该影响下载本身。
    失败信息进日志与发送历史，面板上看得见
    """
    sender = _SENDERS.get(channel)

    if sender is None:
        return False, f"未知渠道：{channel}"

    with _SEND_LOCK:
        try:
            detail = sender(settings, title, text)
            logger.info("通知已发送（%s）：%s", CHANNEL_LABELS.get(channel, channel), title)

            _record(channel, True, detail)
            return True, detail

        except NotifyError as e:
            logger.warning("通知发送失败（%s）：%s", CHANNEL_LABELS.get(channel, channel), e)

            _record(channel, False, str(e))
            return False, str(e)

        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}"
            logger.warning("通知发送失败（%s）：%s", CHANNEL_LABELS.get(channel, channel), detail)

            _record(channel, False, detail)
            return False, detail

        except urllib.error.URLError as e:
            # 连不上多半是代理没填/填错 —— Telegram 在国内必须走代理，
            # 所以这一条要把"网络"和"代理"两个可能都说出来
            detail = (f"网络不可达：{e.reason}"
                      "（Telegram 要填可用的代理；企业微信若走了中转，检查中转地址）")
            logger.warning("通知发送失败（%s）：%s", CHANNEL_LABELS.get(channel, channel), detail)

            _record(channel, False, detail)
            return False, detail

        except Exception as e:
            logger.exception("通知发送异常（%s）", CHANNEL_LABELS.get(channel, channel))

            _record(channel, False, repr(e))
            return False, repr(e)


def send_to_channels(channels, settings: dict, title: str, text: str = "") -> dict:
    """向一组渠道发送，返回 {渠道: (成功?, 说明)}"""
    return {ch: send(ch, settings, title, text) for ch in channels}


def send_in_background(channels, settings: dict, title: str, text: str = ""):
    """
    丢到后台线程去发。

    🔴 发送是同步的网络请求，最坏要耗掉整个 REQUEST_TIMEOUT。下载失败的通知
    恰恰是在下载线程上触发的 —— 直接调 send 会把这个线程按住十几秒，正在
    排队的其它任务一起陪着等。通知是尽力而为的旁路，不能反过来影响主线
    """
    thread = threading.Thread(
        target = send_to_channels, args = (channels, settings, title, text),
        name = "notify-send", daemon = True
    )
    thread.start()


def send_test(channel: str, values: dict = None) -> tuple:
    """
    发一条测试消息。

    values 给了就用表单里的现值（还没保存也能先验证连通性），
    没给就用配置里的生效值。返回 (成功?, 说明)
    """
    if values:
        settings = normalize_settings(values)
        # 测试发送要能越过"开关没打开" —— 用户就是想在启用前先验一下
        settings = dict(settings, wecom_enabled = True, telegram_enabled = True)
    else:
        settings = load_settings()

    if channel == "wecom":
        missing = missing_wecom_fields(settings)

        if missing:
            return False, "还没填写：" + "、".join(missing)

    if channel == "telegram" and not (settings["telegram_token"] and settings["telegram_chat_id"]):
        return False, "还没有填写 Telegram Bot Token 或 Chat ID"

    return send(channel, settings, "Bili23 测试通知",
                "如果你看到这条消息，说明通知配置是通的。")


# ---------------------------------------------------------------------------
# 事件入口（下载流程调用）

def _alias_year(season_title: str, series_title: str) -> str:
    """
    去「名称识别」表里查这部剧的年份，查不到回空串

    B站数据里**没有首播年份** —— ``episodes[].release_date`` 恒为空串，``{pub_time}``
    则是该集的**上架**时间（西游记与柯南都上架于 2020，引进老片全错）。所以年份
    唯一的离线来源就是用户在「名称识别」页自己填的那一格。查不到时通知里整行省掉
    —— 宁可少一行，也不编一个看起来像年份的数字。

    🔴 这段**绝不能抛**：它跑在下载完成的核心路径上（mark_as_completed → notify_completed），
    一条没配规则的任务不该因为查表出问题就把整批通知弄没了。
    🔴 entries 特意走 **notify 自己的 ``config``**（而不是让 naming_alias 去读它那份）：
    单元测试把 ``notify.config`` 顶成了 FakeConfig，两处各持一份的话，测试就得去碰
    真实的配置文件。
    """
    if not (season_title or series_title):
        return ""

    try:
        from . import naming_alias

        alias = naming_alias.match_alias(
            {"season_title": season_title, "series_title": series_title},
            config.get(config.naming_alias_list),
        )

    except Exception:
        logger.exception("查询名称识别表获取年份失败，本次通知不显示年份")

        return ""

    if not isinstance(alias, dict):
        return ""

    return str(alias.get("year") or "").strip()


def _completion_entry(source) -> dict:
    """
    把一条完成事件归一成 {title, show, number, year}

    source 既可以是 TaskInfo，也可以是一个纯标题字符串。前者能拆出合集名、集号与
    年份，汇总时拼成一块四行（剧名 / 年份 / 集数 / 状态）；字符串没有这些信息，
    只能原样逐条列出来 —— 保留这条路是因为 MCP/脚本侧还有按标题调用的用法。

    合集名取 season_title、退回 series_title：柯南这类「一部作品多个 season」的，
    season_title 才是「名侦探柯南 第X季」；而央视版四大名著那种 series_title
    是个大帽子（「央视版四大名著」），拿它当合集名会把西游记和红楼梦混成一组。

    年份**必须在这里当场查出来**：entry 是要被塞进待汇总队列（_PENDING）的纯 dict，
    _compose 手上只有这些 dict、拿不到 TaskInfo —— 过了这一站就再没有 season_title
    可查了。
    """
    if isinstance(source, str):
        return {"title": source, "show": "", "number": 0, "year": ""}

    episode = getattr(source, "Episode", None)
    basic = getattr(source, "Basic", None)

    show = ""
    season_title = ""
    series_title = ""
    number = 0

    if episode is not None:
        season_title = str(getattr(episode, "season_title", "") or "").strip()
        series_title = str(getattr(episode, "series_title", "") or "").strip()
        show = season_title or series_title
        # 番剧按集号，普通多 P 稿件按分 P 号 —— 两者都拿不到时退化成 0，
        # 该条就走"逐条列标题"那条路
        number = getattr(episode, "episode_number", 0) or getattr(episode, "part_number", 0) or 0

    return {
        "title": getattr(basic, "show_title", "") if basic is not None else "",
        "show": show,
        "number": int(number),
        "year": _alias_year(season_title, series_title),
    }


def _range_text(numbers: list) -> str:
    """把一串集号压成紧凑写法：1..25 → "1-25"，1,2,3,7,9,10 → "1-3、7、9-10" """
    values = sorted({int(n) for n in numbers if n})

    if not values:
        return ""

    parts = []
    start = previous = values[0]

    for value in values[1:]:
        if value == previous + 1:
            previous = value

            continue

        parts.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = value

    parts.append(f"{start}-{previous}" if start != previous else str(start))

    return "、".join(parts)


def _compose(entries: list) -> str:
    """
    把待汇总的事件拼成通知正文

    认得出合集的按合集分块，一块四行：

        剧名：西游记
        年份：1986
        集数：第1-25集（共25集）
        状态：已完成

    「年份」取自「名称识别」表，没配规则或没填年份时**整行不出现**（不留空值 ——
    「年份：」后面空着比没有这一行更让人犯嘀咕）。认不出的（标题字符串、或拿不到
    集号的条目）仍逐条列出，条目多时按 BATCH_MAX_TITLES 折叠：单集投稿视频本来
    就没有集数与年份可言。
    """
    blocks = []
    grouped = {}
    loose = []

    for entry in entries:
        # 多 P 稿件没有 season_title，但分 P 号是齐的 —— 拿标题当合集名，一样能压成
        # 一块，比把 20 个分 P 标题逐条列出来好读
        key = entry.get("show") or entry.get("title") or ""

        if key and entry.get("number"):
            grouped.setdefault(key, []).append(entry)

        else:
            loose.append(entry.get("title") or "")

    for key, items in grouped.items():
        numbers = [item["number"] for item in items]

        if not (span := _range_text(numbers)):
            continue

        lines = [f"剧名：{items[0].get('show') or key}"]

        if year := next((str(item.get("year") or "") for item in items if item.get("year")), ""):
            lines.append(f"年份：{year}")

        # 🔴 「共 N 集」与上面那个区间同源（都是去重后的集号）：有重复集号时，
        # 拿条目数去数会得到「第1-25集（共26集）」这种自相矛盾的写法
        lines.append(f"集数：第{span}集（共{len({int(n) for n in numbers if n})}集）")
        lines.append("状态：已完成")

        blocks.append("\n".join(lines))

    if loose:
        body = [f"· {title}" for title in loose[:BATCH_MAX_TITLES]]

        if len(loose) > BATCH_MAX_TITLES:
            body.append(f"· …等 {len(loose)} 个任务")

        blocks.append("\n".join(body))

    return "\n\n".join(blocks)


def _batch_still_running() -> bool:
    """
    下载队列里还有任务在跑吗（排队/解析/下载/等 FFmpeg/合并/转换/后处理）

    用来把「整个合集下完才通知」落到实处。查不到时按 False 处理 —— 宁可早发
    一条，也不能因为一次查询异常把通知永远扣在队列里。
    """
    if RUNNING_PROBE is not None:
        return bool(RUNNING_PROBE())

    try:
        # 🔴 这两个相对导入的位置不一样，别看错：本模块在 util/common/ 下，
        # 状态枚举是同级的 `.enum`（写成 `..enum` 会去找根本不存在的 util.enum），
        # 任务管理器在 util/download/ 下，才是 `..download`
        from .enum import DownloadStatus
        from ..download.task.manager import task_manager

        running = {getattr(DownloadStatus, name) for name in RUNNING_STATUS_NAMES}

        for task_info in task_manager.query(False):
            if task_info.Download.status in running:
                return True

    except Exception:
        logger.exception("判断下载队列是否仍有任务在跑失败，本次按已结束处理")

    return False


def _arm_timer():
    """起一个静默期计时器。调用方负责持有 _PENDING_LOCK"""
    global _TIMER

    _TIMER = threading.Timer(BATCH_DELAY, _flush_pending)
    _TIMER.daemon = True
    _TIMER.start()


def notify_completed(source, settings: dict = None):
    """
    一条任务完成。**不立即发送** —— 记进待汇总队列并重置静默期计时

    一次下载动辄几十集，逐集推会把手机刷爆；等安静下来一次性汇总成一条，
    与桌面版「下载列表空了才弹一次」是同一个语义。source 传 TaskInfo 时正文
    按合集分块（剧名 / 年份 / 集数 / 状态），传标题字符串则原样列出。
    """
    settings = settings or load_settings()

    if not settings["on_complete"] or not enabled_channels(settings):
        return

    with _PENDING_LOCK:
        _PENDING.append(_completion_entry(source))

        if _TIMER is not None:
            _TIMER.cancel()

        _arm_timer()


def notify_failed(title: str, reason: str = "", settings: dict = None):
    """
    一条任务失败。立即发送（不合并）—— 失败是要马上知道的事，
    静默期会把「下到一半挂了」拖成二十分钟后才收到
    """
    settings = settings or load_settings()

    if not settings["on_fail"] or not enabled_channels(settings):
        return

    text = f"· {title}"
    if reason:
        text += f"\n原因：{reason}"

    send_in_background(enabled_channels(settings), settings, "B站下载失败", text)


def _flush_pending(force: bool = False):
    """
    静默期到点：把攒下来的完成事件汇总成一条发出去

    🔴 静默期只是「先别急着发」，不是「可以发了」。一次批量下载里，合并与后处理
    会让完成事件之间出现几十秒空档 —— 光看静默期，一个 25 集的合集会被拆成好几条
    通知。所以这里再确认一次队列：还有任务在跑就继续等，整批结束才发一条。
    这也正是桌面版「下载列表空了才弹一次」的判据。

    force 为 True 时跳过上面那次确认（退出前调用，进程马上就要没了，再等没有意义）
    """
    global _TIMER

    # 🔴 先探队列再进锁：任务库查询可能撞上写锁而阻塞（busy_timeout 30s），
    # 按着 _PENDING_LOCK 去做会把 notify_completed 一起堵住，而那是下载线程
    if not force and _batch_still_running():
        with _PENDING_LOCK:
            if not _PENDING:
                _TIMER = None

                return

            logger.info("下载队列仍有任务在跑，完成通知继续等待（已攒 %d 条）", len(_PENDING))

            _arm_timer()

        return

    with _PENDING_LOCK:
        entries = list(_PENDING)
        _PENDING.clear()
        _TIMER = None

    if not entries:
        return

    settings = load_settings()

    # 静默期里用户可能把通知关了或改了渠道，发送前再判一次
    channels = enabled_channels(settings)

    if not settings["on_complete"] or not channels:
        return

    # 不再另起一行「共 N 个任务已完成」：每一块的「集数：…（共N集）」已经带了
    # 这个数，两块以上也各报各的，再补一句总数只会让正文变长而不多给信息
    text = _compose(entries)

    send_to_channels(channels, settings, "B站下载完成", text)


def flush_now():
    """
    立刻把待汇总的事件发出去。

    退出前调用，否则最后一批"下完了"可能还躺在队列里等着 20 秒静默期 ——
    进程一没就永远发不出去了。这里**不等队列跑空**：那一刻队列里大概率还有
    任务，等下去的结果是一条都发不出去
    """
    with _PENDING_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()

    _flush_pending(force = True)


def history() -> list:
    """最近发送记录（新→旧）"""
    return list(HISTORY)
