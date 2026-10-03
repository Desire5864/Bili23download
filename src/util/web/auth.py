"""
Web 面板自己的登录凭据。

要点在于它和 B 站账号是两回事：这一层拦的是"谁能打开这个网页"，
而不是"用哪个 B 站账号下载"。面板默认账号 admin，密码以
PBKDF2-HMAC-SHA256 加盐哈希落盘，改密码后所有已签发的会话立即作废。

会话只活在内存里，重启进程即全部失效。落盘的会话令牌等于把"永久免密登录"
写进配置文件，而容器部署里这个文件是要挂出来备份、也可能被别的进程读到的。
"""

from ..common.config import config

from threading import Lock
import secrets
import hashlib
import hmac
import time
import logging

logger = logging.getLogger(__name__)

ALGORITHM = "pbkdf2_sha256"

# 迭代次数。本机约 30 毫秒，容器里约 100 毫秒 —— 登录是低频动作，
# 这点延迟换的是离线爆破的成本。改这个值不会让旧哈希失效：
# 轮数写在凭据串里，校验时按存档的那个值算
PBKDF2_ROUNDS = 120_000

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"

# 新密码的长度下限。默认密码是 8 位，别再让人改成 3 位
MIN_PASSWORD_LENGTH = 6

# 会话有效期。面板常开在局域网里，7 天足够省事，又不至于长期有效
SESSION_TTL = 7 * 24 * 3600

def hash_password(password: str, salt: str = "", rounds: int = PBKDF2_ROUNDS) -> str:
    """
    产出 `算法$轮数$盐$摘要` 形式的凭据串

    盐留空时随机生成；测试可以显式传入以获得可复现的结果
    """
    if not salt:
        salt = secrets.token_hex(16)

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)

    return f"{ALGORITHM}${rounds}${salt}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    """
    校验密码

    用 compare_digest 而非 `==`：逐字节比较在第一个不同的字符处提前返回，
    响应时间的差异足以让人把摘要一段段试出来。这里比的是摘要不是明文，
    这条防线理论上没那么要紧，但没有理由不设。

    存档串畸形时一律判失败，不抛异常 —— 这个函数在 HTTP 线程上被调用，
    抛出去只会变成一个谁也看不懂的 500
    """
    try:
        algorithm, rounds, salt, _ = stored.split("$")

        rounds = int(rounds)

    except (AttributeError, ValueError):
        return False

    if algorithm != ALGORITHM or rounds < 1:
        return False

    return hmac.compare_digest(hash_password(password, salt, rounds), stored)

class SessionStore:
    """
    内存会话表

    令牌是 32 字节随机串，用 dict 查找就够了：Python 的字符串哈希每个进程
    都有随机化的前缀，靠响应时间逐字节试探一个哈希表键并不现实，
    不必在这里再做一次定时安全比较
    """

    def __init__(self, ttl: int = SESSION_TTL):
        self.ttl = ttl

        self._items: dict = {}
        self._lock = Lock()

    def issue(self, username: str) -> str:
        token = secrets.token_urlsafe(32)

        with self._lock:
            self._prune()
            self._items[token] = (username, time.time() + self.ttl)

        return token

    def validate(self, token: str) -> str:
        """有效则返回用户名，否则返回空串"""
        if not token:
            return ""

        with self._lock:
            item = self._items.get(token)

            if item is None:
                return ""

            username, expires_at = item

            if expires_at < time.time():
                self._items.pop(token, None)

                return ""

            return username

    def revoke(self, token: str):
        with self._lock:
            self._items.pop(token, None)

    def revoke_all(self):
        with self._lock:
            self._items.clear()

    def count(self) -> int:
        with self._lock:
            self._prune()

            return len(self._items)

    def _prune(self):
        # 过期的条目不必专门起一个清理线程：每次签发时顺手扫一遍，
        # 面板的登录频率低，这个开销可以忽略
        now = time.time()

        for token in [key for key, (_, expires_at) in self._items.items() if expires_at < now]:
            self._items.pop(token, None)

class LoginThrottle:
    """
    登录失败的退避表

    面板常开在局域网里，密码又只有一个，在线爆破是它最现实的威胁。
    PBKDF2 让每次尝试都要花约 100 毫秒，这里再按来源地址做指数退避：
    连续失败超过 FREE_FAILURES 次后，每再失败一次，等待翻倍并封顶 60 秒。
    30 次失败累计要等上 10 分钟以上，每秒试几千个密码的字典攻击直接失去意义

    按来源地址记（TCP 对端地址，`client_address[0]`）：三次握手保证它
    不可伪造，比 X-Forwarded-For 这类请求方可随便填的头可靠。代价是
    NAT 后面多人共用一个出口地址时，一个人试错全家等 —— 面板本来就是
    单账号的场景，这个代价可以接受

    与会话表一样只活在内存里：重启进程即清零，爆破者最坏也就是换来
    一次重启的机会
    """

    # 退避上限。60 秒之后失败次数再涨也不加长：把人锁在门外一小时
    # 不是目的，拖慢自动化的批量尝试才是
    MAX_WAIT = 60.0

    # 免惩罚的失败次数。输错一两次密码就锁 2 秒，对真人太苛刻；
    # 三次之后才进入指数退避，足够让批量尝试失去意义
    FREE_FAILURES = 3

    def __init__(self):
        self._items: dict = {}
        self._lock = Lock()

    def _wait_for(self, fails: int) -> float:
        """失败 fails 次后的等待秒数。免费额度内为 0"""
        if fails <= self.FREE_FAILURES:
            return 0.0

        return min(float(2 ** (fails - self.FREE_FAILURES)), self.MAX_WAIT)

    def wait_seconds(self, key: str) -> float:
        """这个来源当前还要等多久才允许尝试。0 表示放行"""
        with self._lock:
            entry = self._items.get(key)

            if entry is None:
                return 0.0

            fails, until = entry

            if fails <= self.FREE_FAILURES:
                return 0.0

            remaining = until - time.time()

            return remaining if remaining > 0 else 0.0

    def record_failure(self, key: str):
        """记一次失败，并刷新这个来源的下次可尝试时间"""
        with self._lock:
            fails = self._items.get(key, (0, 0.0))[0] + 1

            self._items[key] = (fails, time.time() + self._wait_for(fails))

    def reset(self, key: str):
        """登录成功后清掉这个来源的失败计数"""
        with self._lock:
            self._items.pop(key, None)

class PanelCredentials:
    """
    面板凭据的快照

    启动时从配置读一次，改密码时由主线程回写并更新这里。HTTP 线程不直接读
    config 对象，少一层需要跨线程确认的东西
    """

    def __init__(self, username: str, password_hash: str):
        self._lock = Lock()

        self._username = username
        self._password_hash = password_hash

    @classmethod
    def default(cls) -> "PanelCredentials":
        return cls(DEFAULT_USERNAME, hash_password(DEFAULT_PASSWORD))

    @property
    def username(self) -> str:
        with self._lock:
            return self._username

    def verify(self, username: str, password: str) -> bool:
        with self._lock:
            expected_user = self._username
            stored = self._password_hash

        # 用户名错也要走完哈希：否则"用户名对、密码错"和"用户名不存在"
        # 的耗时明显不同，等于白送一个枚举用户名的接口。
        # 顺序不能反，verify_password 必须先执行
        matched = verify_password(password, stored)

        return matched and hmac.compare_digest(username, expected_user)

    def update(self, username: str, password_hash: str):
        with self._lock:
            self._username = username
            self._password_hash = password_hash

def ensure_credentials() -> tuple:
    """
    读取面板账号，缺失时用默认值补齐并落盘，返回 (用户名, 凭据串)

    必须在 GUI 线程调用：它可能写配置文件
    """
    username = (config.get(config.web_panel_username) or "").strip()
    stored = (config.get(config.web_panel_password) or "").strip()

    if not username:
        username = DEFAULT_USERNAME

        config.set(config.web_panel_username, username)

    if not stored:
        stored = hash_password(DEFAULT_PASSWORD)

        config.set(config.web_panel_password, stored)

        logger.info("Web 面板凭据已初始化，默认账号 %s，请登录后尽快修改密码", username)

    return username, stored
