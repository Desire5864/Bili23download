"""
云端同步的自动方案 —— 下载彻底跑完 N 分钟之后，自己让 CloudDrive2 重扫一遍

两条路职责分开：

  · **手动**：面板上那颗按钮 → 立即执行（见  `server.api_sync`），**不走本模块**。
    用户 2026-10-04 明确要求「点这个按钮要立即执行」，所以手动那条路不看队列、
    不看延迟，点了就传。
  · **自动**：本模块盯着下载队列，**观测到「忙 → 空」这个边沿**才开始计时，
    等满设置里那个延迟（默认 5 分钟）就让 CD2 重扫。那几分钟留给 FFmpeg 合并与
    文件重命名落定，免得把半成品传上去。

🔴 为什么盯「边沿」而不是盯「队列空」：队列本来就空着的时候没有任何东西要传，
按"空就传"会变成每 15 秒传一次。只有先忙过、再空下来，才意味着「刚下完一批」。

队列判据沿用完成通知那一套（util.common.notify.downloads_still_running）：排队、
解析、下载、等 FFmpeg、合并、转换都算在跑，**暂停与失败不算**。两边共用同一套是
刻意的 —— 否则会出现「通知已经发出来了、同步却还认为队列没跑完」这种自相矛盾的
状态，而且两边都不报错，只能靠对时间才发现。

执行动作、队列判据、延迟与开关全部由外部注入（见 configure），所以本模块既不
import 下载器也不 import web 层：时间轴可以手动推进（见 pump），测试不必真等 5 分钟，
设置改了也不必重新配置 —— 延迟与开关是每拍现读的。

🔴 刻意不做持久化：本轮计时是运行期事实，容器重启后从下一批算起就行。为此引入
一份带时间戳的状态文件，只会在重启后冒出一个来路不明的待同步任务，比丢掉它更麻烦。
"""

from threading import Lock, Thread
from time import monotonic, sleep, time
import logging

logger = logging.getLogger(__name__)

# 轮询队列的间隔。秒级精度对"等下载跑完再传"足够了；调小只是多几次任务库查询，
# 调大则延迟判定的误差跟着变大（短批次甚至可能整批被跳过）
POLL_INTERVAL = 15.0

_LOCK = Lock()

# 注入点。一个都没注入时 pump() 直接返回 —— 不做任何事，也不会误触发
_runner = None      # 执行动作：让 CD2 重扫
_is_busy = None     # 队列判据：有没有任务在跑
_get_delay = None   # 自动方案的延迟（秒）
_is_enabled = None  # 自动方案的开关

_thread = None

_STATE = {
    "enabled": False,    # 最近一拍读到的开关值（只用于展示）
    "saw_busy": False,   # 本轮观测到队列忙过（"忙 → 空"的边沿记忆）
    "idle_since": None,  # 队列刚空下来的时刻（None = 还没开始计时）
    "skip": False,       # 用户撤了本轮，且队列还在跑 —— 顶住别把它认回来
    "delay": 0.0,        # 最近一拍读到的延迟（秒，只用于展示）
    # 上一次执行的结果。**与计时无关，_clear() 不碰它** —— 一轮结束了，但"上次那次
    # 到底传成没传成"仍然要答得上来：同步是跑到后台的，失败时用户根本不在场，
    # 不把结果留在这里，他就只能去翻日志
    "last_result": None,
}


def configure(runner = None, is_busy = None, get_delay = None, is_enabled = None,
              interval: float = None) -> None:
    """注入执行动作、队列判据、延迟与开关。面板启动时调一次；测试里可以反复调"""
    global _runner, _is_busy, _get_delay, _is_enabled, POLL_INTERVAL

    with _LOCK:
        if runner is not None:
            _runner = runner

        if is_busy is not None:
            _is_busy = is_busy

        if get_delay is not None:
            _get_delay = get_delay

        if is_enabled is not None:
            _is_enabled = is_enabled

        if interval is not None:
            POLL_INTERVAL = float(interval)

    # 自动方案没人点按钮也得转起来 —— 心跳随注入一起起（只起一次）
    _ensure_thread()


def pump() -> None:
    """
    推进一步判定：忙过又空下来就开始计时，计满就执行

    正常由后台线程按 POLL_INTERVAL 调它；测试也直接调它 —— 这样时间轴可以手动
    推进，不必为一条断言真等上几分钟。
    """
    with _LOCK:
        enabled = _probe_flag(_is_enabled, False)

        _STATE["enabled"] = enabled
        _STATE["delay"] = _probe_delay(_get_delay)

        if not enabled:
            # 关掉自动方案 = 连"忙过"这个记忆一起丢掉：
            # 重新打开时应当从下一批算起，而不是补传上一批
            _STATE["saw_busy"] = False
            _STATE["idle_since"] = None
            _STATE["skip"] = False
            return

    # 🔴 锁外探队列：_is_busy 要查任务库，持锁调用会把 status() 一起卡住
    busy = _probe()
    now = monotonic()

    with _LOCK:
        if not _STATE["enabled"]:      # 探测期间被关掉了
            return

        if busy:
            # 队列又来任务了：把计时起点抹掉，等它跑完重新开始算。
            # skip 是"用户撤了本轮"的记号，这种情况下别再把它认回来
            if not _STATE["skip"]:
                _STATE["saw_busy"] = True

            _STATE["idle_since"] = None
            return

        if _STATE["skip"]:
            # 这一批彻底结束了，"别传"的记号到此为止：下一批照常自动传
            _STATE["skip"] = False
            return

        if not _STATE["saw_busy"]:
            # 压根没忙过 —— 队列一直是空的，没有"刚下完的一批"要传
            return

        if _STATE["idle_since"] is None:
            _STATE["idle_since"] = now
            logger.info("下载队列已空，为自动同步计时：%.0f 秒后执行", _STATE["delay"])

        if now - _STATE["idle_since"] < _STATE["delay"]:
            return

        runner = _runner

        # 一轮只传一次：把"忙过"的记号抹掉，下次再忙才会重新计时。
        # 先清状态再执行 —— 执行抛错也不会把计时卡在"到点未跑"
        _STATE["saw_busy"] = False
        _STATE["idle_since"] = None

    if runner is None:
        logger.warning("自动同步到点，但没有注册执行动作，本次放弃")
        remember(False, "没有注册执行动作，这次同步被跳过了")
        return

    logger.info("自动方案的延迟已满，开始云端同步")

    try:
        remember(True, str(runner() or "已触发云端同步"))

    except Exception as e:
        # 🔴 结果必须留下来：这次执行没有 HTTP 请求在旁边看着，异常再不记进状态，
        # 用户就只剩"下完了、等了五分钟、什么都没发生"这一个观测
        logger.exception("自动触发的云端同步失败")
        remember(False, str(e) or e.__class__.__name__)


def cancel() -> bool:
    """
    撤销**本轮**的自动同步。返回是否真的撤掉了什么 —— 没在计时时返回 False，
    调用方别报「已取消」让用户以为撤掉了什么

    队列还在跑的时候撤，得用 skip 顶住：不然下一拍又把"忙过"记回来，
    用户撤了个寂寞
    """
    busy = _probe()          # 锁外探，理由同 pump()

    with _LOCK:
        if not _STATE["saw_busy"]:
            return False

        _STATE["saw_busy"] = False
        _STATE["idle_since"] = None
        _STATE["skip"] = busy

    logger.info("已取消本轮的自动云端同步")
    return True


def queue_busy() -> bool:
    """现问一次队列忙不忙，不等心跳那一拍 —— 手动同步前的弹窗提示要用它"""
    return _probe()


def status() -> dict:
    """自动方案的当前快照，给页面显示用"""
    # 🔴 开关与延迟都**现读**，不用 _STATE 里那份缓存：缓存是心跳写进去的，容器刚
    # 起来时那一拍要等 15 秒 —— 页面上会先显示"自动方案关着 / 等 0 分钟"，
    # 然后再自己变回来
    enabled = _probe_flag(_is_enabled, False)
    delay = _probe_delay(_get_delay)

    with _LOCK:
        saw_busy = _STATE["saw_busy"]
        idle_since = _STATE["idle_since"]
        last_result = _STATE["last_result"]

    # 「在等队列」与「在倒计时」是两种不同的等待，页面文案不一样
    waiting_queue = bool(enabled and saw_busy and idle_since is None)
    pending = bool(enabled and saw_busy and idle_since is not None)

    snapshot = {
        "auto": enabled,
        "pending": pending,
        "waiting_queue": waiting_queue,
        "delay_minutes": round(delay / 60, 2),
        "seconds_left": None,
        "last_result": last_result,
    }

    if pending:
        snapshot["seconds_left"] = int(max(0.0, delay - (monotonic() - idle_since)))

    return snapshot


def remember(ok: bool, message: str) -> None:
    """记下一次执行的结果，供页面回看。手动那条路也写这里，两边的"上次同步"是同一个"""
    with _LOCK:
        _STATE["last_result"] = {"ok": bool(ok), "message": message, "at": time()}


def _clear() -> None:
    """清空本轮计时。调用方负责持有 _LOCK。last_result 不在此列 —— 见 _STATE 的注释"""
    _STATE.update(saw_busy = False, idle_since = None, skip = False,
                  delay = 0.0, enabled = False)


def _probe() -> bool:
    """问一次队列还忙不忙。探针本身出错时按「不忙」处理"""
    probe = _is_busy

    if probe is None:
        return False

    try:
        return bool(probe())

    except Exception:
        # 宁可早传一次，也不能因为一次查询异常把计时永远扣在队列里 —— 与通知同一条取舍
        logger.exception("查询下载队列是否仍在运行失败，本次按已结束处理")
        return False


def _probe_flag(probe, default: bool) -> bool:
    """读一个布尔型设置。没注入时按 default（默认关，免得测试里误触发真同步）"""
    if probe is None:
        return bool(default)

    try:
        return bool(probe())

    except Exception:
        logger.exception("读取云端同步开关失败，按默认值处理")
        return bool(default)


def _probe_delay(probe) -> float:
    """读延迟秒数。读不出来按 0 —— 与队列探针同一条取舍：宁可早传一次"""
    if probe is None:
        return 0.0

    try:
        return max(0.0, float(probe() or 0))

    except Exception:
        logger.exception("读取云端同步延迟失败，按 0 处理")
        return 0.0


def _ensure_thread() -> None:
    """让心跳转起来（只起一次）。自动方案全靠它推进"""
    global _thread

    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return

        _thread = Thread(target = _loop, name = "cloud-sync-scheduler", daemon = True)
        _thread.start()


def _loop() -> None:
    while True:
        sleep(POLL_INTERVAL)

        try:
            pump()

        except Exception:
            # 心跳不能因为一次异常就退出 —— 退出以后自动同步再也没人推进了
            logger.exception("云端同步自动方案的心跳出错")
