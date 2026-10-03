"""CloudDrive2 客户端（gRPC-Web over HTTP/1.1）。

传输层为什么不用 grpcio
-----------------------
CD2 的 19798 端口同时提供 gRPC 与 gRPC-Web —— 它自己的浏览器 UI 走的就是后者。
gRPC-Web 完全跑在 HTTP/1.1 上：一条消息 = 5 字节前缀（1 字节标志位 + 4 字节大端
长度）+ protobuf 载荷；收尾的 trailer 帧把标志位置 0x80，内容是
`grpc-status:0\\r\\n` 这样的文本行。于是 httpx（已在依赖里）加 protobuf
（已在依赖里）就够了，不必往镜像里再塞 grpcio（约 20 MB）或 h2。

实测（2026-10-02，CD2 1.1.1，proto 1.1.1 生成）：
    GetSystemInfo  → trailer {'grpc-status': '0'}，拿回 CloudDriveSystemInfo
    BackupGetAll   → 5 条备份，哔哩哔哩那条 sourcePath == '/Storage/哔哩哔哩'
    原生 Content-Type: application/grpc 走 HTTP/1.1 会被 400 拒掉（它本来要 HTTP/2）

备份按「源路径」标识
--------------------
proto 里 `Backup` 结构本身没有 id：`BackupRemove` / `BackupGetStatus` /
`BackupRestartWalkingThrough` / `BackupSetEnabledRequest` 传的都是
`sourcePath`。所以这里的每个方法都以 source 为键。

两种凭据
--------
· 账号密码 —— `GetToken` 换 JWT，带有 expiration，过期要重登（本类缓存，并在
  过期前 60 秒自动续）。
· 设备令牌 —— CD2 写在自己配置目录下的 device_token.txt，可以直接当 Bearer
  用；它不随密码变化也不会过期。面板上账号密码留空时就退回它。
"""

import threading
import time
from urllib.parse import unquote

import httpx
from google.protobuf import empty_pb2

from . import clouddrive_pb2 as pb

# proto 里的 `package clouddrive; service CloudDriveFileSrv`
SERVICE = "clouddrive.CloudDriveFileSrv"

# gRPC-Web 帧标志位：最低位表示压缩，最高位表示这是 trailer 帧
FRAME_MESSAGE = 0x00
FRAME_TRAILER = 0x80

# JWT 到期前多久就提前续签，避免"刚好在请求途中过期"
TOKEN_RENEW_MARGIN = 60.0

DEFAULT_PORT = 19798
DEFAULT_TIMEOUT = 20.0
STATUS_TIMEOUT = 5.0


class CloudDriveError(Exception):
    """与 CD2 的通信失败，或 CD2 明确拒绝了这次调用"""


class CloudDriveAuthError(CloudDriveError):
    """凭据不对（或账号开了 2FA 而没给 totpCode）"""


def _frame(payload: bytes) -> bytes:
    return bytes([FRAME_MESSAGE]) + len(payload).to_bytes(4, "big") + payload


def _unframe(body: bytes):
    """拆 gRPC-Web 响应，返回 (protobuf 载荷 或 None, trailers 字典)

    正常响应至少有一个 trailer 帧 —— 哪怕方法返回 Empty（此时没有数据帧）。
    所以"读到 trailer"是判断这次调用到底有没有走完的可靠依据；反过来，一个
    既无数据帧又无 trailer 的空 body（凭据被拒时见过）说明调用根本没成立。
    """
    offset = 0
    payload = None
    trailers = {}

    while offset + 5 <= len(body):
        flag = body[offset]
        length = int.from_bytes(body[offset + 1:offset + 5], "big")
        chunk = body[offset + 5:offset + 5 + length]
        offset += 5 + length

        if flag & FRAME_TRAILER:
            for line in chunk.decode("utf-8", "replace").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    trailers[key.strip().lower()] = value.strip()

        elif not flag & 0x01:
            payload = chunk

    return payload, trailers


class CloudDriveClient:
    """一个 CD2 服务端的连接。用完 close()，或当上下文管理器用。"""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self._base = "http://%s:%d" % (self.host, self.port)

        # trust_env=False 是必须的：容器/桌面上的 http_proxy 环境变量一旦被读到，
        # 访问局域网里的 CD2 就会绕进代理（与通知模块里 ProxyHandler({}) 同一个理由）
        self._client = httpx.Client(
            trust_env = False,
            timeout = timeout,
            follow_redirects = False,
        )

        self._lock = threading.Lock()
        self._token = None
        self._token_expiry = 0.0
        self._credentials = None      # (username, password, totp)
        self._device_token = None

    # -- 凭据 ---------------------------------------------------------------

    def set_credentials(self, username: str, password: str, totp: str = "") -> None:
        """账号密码。真正登录推迟到第一次要用的时候 —— 只读配置不该触发登录"""
        with self._lock:
            self._credentials = (username, password, totp or "")
            self._token = None
            self._token_expiry = 0.0

    def set_device_token(self, token: str) -> None:
        with self._lock:
            self._device_token = (token or "").strip() or None

    def _resolve_token(self) -> str:
        with self._lock:
            credentials = self._credentials
            cached, expiry = self._token, self._token_expiry
            device_token = self._device_token

        if credentials and cached and time.time() < expiry - TOKEN_RENEW_MARGIN:
            return cached

        if credentials:
            return self.login(*credentials)

        if device_token:
            return device_token

        raise CloudDriveAuthError("没有可用的 CD2 凭据：请填写账号密码，或提供设备令牌")

    def login(self, username: str, password: str, totp: str = "") -> str:
        """用账号密码换 JWT。返回可用的 token"""
        request = pb.GetTokenRequest(userName = username, password = password)

        if totp:
            request.totpCode = totp

        # anonymous=True 是必须的：GetToken 本身是公共方法，而且 _resolve_token
        # 在"有账号密码但还没登录"时正是走这里 —— 不绕开就会自己调自己
        reply = self._call("GetToken", request, pb.JWTToken, anonymous = True)

        if not reply.success:
            raise CloudDriveAuthError(reply.errorMessage or "CD2 拒绝了这个账号或密码")

        with self._lock:
            self._token = reply.token
            # 服务端没给 expiration 时给一个保守的默认值，免得每次都重登
            self._token_expiry = (
                reply.expiration.seconds if reply.expiration.seconds else time.time() + 1800
            )

        return reply.token

    # -- 传输 ---------------------------------------------------------------

    def _call(self, method: str, request, response_class = None, anonymous: bool = False):
        headers = {
            "Content-Type": "application/grpc-web+proto",
            "X-Grpc-Web": "1",
            "Accept": "application/grpc-web+proto",
        }

        if not anonymous:
            headers["Authorization"] = "Bearer " + self._resolve_token()

        url = "%s/%s/%s" % (self._base, SERVICE, method)

        try:
            response = self._client.post(url, content = _frame(request.SerializeToString()),
                                         headers = headers)

        except httpx.HTTPError as exc:
            raise CloudDriveError(
                "连不上 CloudDrive2（%s:%s）：%s" % (self.host, self.port, exc)
            ) from exc

        if response.status_code != 200:
            raise CloudDriveError("CloudDrive2 返回 HTTP %s" % response.status_code)

        payload, trailers = _unframe(response.content)
        status = trailers.get("grpc-status")

        if status is None:
            # 凭据不被接受时 CD2 会直接给一个空 body（见过：错误密码调 GetToken）
            raise CloudDriveAuthError(
                "CloudDrive2 没有回应这次调用（凭据可能已失效，或服务尚未就绪）"
            )

        if status != "0":
            detail = unquote(trailers.get("grpc-message") or "")

            if status in ("16", "7"):        # UNAUTHENTICATED / PERMISSION_DENIED
                raise CloudDriveAuthError("CloudDrive2 拒绝授权：%s" % (detail or status))

            raise CloudDriveError("CloudDrive2 返回 %s：%s" % (status, detail or "无详情"))

        if response_class is None:
            return None

        message = response_class()

        if payload:
            message.ParseFromString(payload)

        return message

    # -- 系统信息 -----------------------------------------------------------

    def system_info(self):
        """无需授权。用它做「连得上吗 / 登录了吗」的探针"""
        return self._call("GetSystemInfo", empty_pb2.Empty(), pb.CloudDriveSystemInfo)

    # -- 备份 ---------------------------------------------------------------

    def list_backups(self):
        """全部备份任务（每条是 BackupStatus，任务本体在 .backup 里）"""
        listing = self._call("BackupGetAll", empty_pb2.Empty(), pb.BackupList)

        return list(listing.backups)

    def find_backup(self, source_path: str):
        """按源路径找备份任务。找不到返回 None"""
        for entry in self.list_backups():
            if entry.backup.sourcePath == source_path:
                return entry

        return None

    def backup_status(self, source_path: str):
        return self._call("BackupGetStatus", pb.StringValue(value = source_path),
                          pb.BackupStatus)

    def restart_walking_through(self, source_path: str) -> None:
        """让这条备份立刻重新扫一遍源目录（CD2 的备份引擎自己做增量对比与上传）

        这就是「立即备份」真正干的事 —— 比在容器里 cp 一份强的地方在于：增量、
        失败可重试、能在 CD2 里看到进度，而不是往传输队列里塞一堆单文件任务。
        """
        self._call("BackupRestartWalkingThrough", pb.StringValue(value = source_path))

    def set_backup_enabled(self, source_path: str, enabled: bool) -> None:
        self._call("BackupSetEnabled", pb.BackupSetEnabledRequest(
            sourcePath = source_path, isEnabled = bool(enabled)))

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


# 状态枚举的中文标签。Waiting 见 proto 注释：扫描在排队（等一个 walker 名额或
# 等传输队列排空），界面上说"排队中"比"等待"清楚
BACKUP_STATUS_LABELS = {
    pb.BackupStatus.Idle: "空闲",
    pb.BackupStatus.WalkingThrough: "扫描中",
    pb.BackupStatus.Error: "出错",
    pb.BackupStatus.Disabled: "已停用",
    pb.BackupStatus.Scanned: "已扫描",
    pb.BackupStatus.Finished: "已完成",
    pb.BackupStatus.Waiting: "排队中",
}


def status_label(status: int) -> str:
    return BACKUP_STATUS_LABELS.get(status, "未知(%s)" % status)
