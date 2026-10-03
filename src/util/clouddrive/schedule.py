"""
云端同步的排期器 —— 只决定「什么时候动手」，不碰 CloudDrive2

为什么需要它：下载还在跑的时候去扫目录，扫到的是一堆半成品 —— FFmpeg 还没合并完、
文件还没按命名规则改名，CD2 把它们传上去，等收尾完成还得再传一遍，白跑一趟带宽。
所以点一下不再立即触发，而是排期到「下载队列彻底安静 + 延迟 N 分钟」，那几分钟
正好留给合并与重命名落定。

队列判据沿用完成通知那一套（util.common.notify.downloads_still_running）：排队、
解析、下载、等 FFmpeg、合并、转换都算在跑，**暂停与失败不算**。两边共用同一套是
刻意的 —— 否则会出现「通知已经发出来了、同步却还认为队列没跑完」这种自相矛盾的
状态，而且两边都不报错，只能靠对时间才发现。

执行动作与队列判据都由外部注入（见 configure），所以本模块既不 import 下载器也不
import web 层：时间轴可以手动推进（见 pump），测试不必真等 5 分钟。

🔴 刻意不做持久化：排期是「这一次点按」的意图，容器重启后重新点一下就行。为此引入
一份带时间戳的状态文件，只会在重启后冒出一个来路不明的待同步任务，比丢掉它更麻烦。
"""

from threading import Lock, Thread
from time import monotonic, sleep, time
import logging

logger = logging.getLogger(__name__)

# 轮询队列的间隔。对「等下载跑完再传」这种需求，秒级精度足够了；
# 调小只是多几次任务库查询，调大则延迟判定的误差跟着变大
POLL_INTERVAL = 15.0

_LOCK = Lock()

# 注入点。未注入 is_busy 时按「不忙」处理（宁可早传一次）；runner 缺失则只记日志
_runner = None
_is_busy = None

_thread = None

_STATE = {
    "armed": False,        # 有没有排期在等
    "idle_since": None,    # 队列第一次被观测为空的时间（None = 队列还在跑）
    "delay": 0.0,          # 要等的秒数
    "requested_at": None,  # 排期时刻（墙钟，只用于页面显示）
    # 上一次执行的结果。**与排期无关，_clear() 不碰它** —— 排期结束了，但"上次那次
    # 到底传成没传成"仍然要答得上来：同步是排到后台跑的，失败时用户根本不在场，
    # 不把结果留在这里，他就只能去翻日志
    "last_result": None,
}


def configure(runner, is_busy, interval: float = None) -> None:
    """注入执行动作与队列判据。面板启动时调一次；测试里可以反复调"""
    global _runner, _is_busy, POLL_INTERVAL

    with _LOCK:
        _runner = runner
        _is_busy = is_busy

        if interval is not None:
            POLL_INTERVAL = float(interval)


def schedule(delay_seconds: float) -> dict:
    """
    登记一次待执行的同步。已有排期则重新计时（用户再点一次 = 「我说的就是这批」）

    队列当前为空时，从这一刻就开始计时 —— 用户点的时候没下载在跑也要等满延迟，
    因为「刚下完正在收尾」与「本来就没在下」在队列状态上完全一样，何况那几分钟
    本来就是留给收尾的。
    """
    delay = max(0.0, float(delay_seconds or 0))

    # 🔴 锁外探队列：_is_busy 要查任务库，持锁调用会把 status() 和别的 schedule() 一起卡住
    busy = _probe()

    now_mono, now_wall = monotonic(), time()

    with _LOCK:
        _STATE.update(
            armed = True,
            idle_since = None if busy else now_mono,
            delay = delay,
            requested_at = now_wall,
        )

    _ensure_thread()

    logger.info("已排期云端同步：%s，之后等 %.0f 秒才执行",
                "等下载任务跑完" if busy else "队列已空", delay)

    return status()


def cancel() -> bool:
    """撤销排期。返回是否真的撤掉了什么 —— 没排期时返回 False，调用方别报「已取消」"""
    with _LOCK:
        if not _STATE["armed"]:
            return False

        _clear()

    logger.info("已取消排期的云端同步")
    return True


def status() -> dict:
    """排期的当前快照，给页面显示用"""
    with _LOCK:
        armed = _STATE["armed"]
        delay = _STATE["delay"]
        idle_since = _STATE["idle_since"]
        requested_at = _STATE["requested_at"]
        last_result = _STATE["last_result"]

    snapshot = {
        "armed": armed,
        "delay_minutes": round(delay / 60, 2),
        "requested_at": requested_at,
        "waiting_queue": False,
        "seconds_left": None,
        "last_result": last_result,
    }

    if not armed:
        return snapshot

    if idle_since is None:
        # 还在等队列空下来，剩余时间无从算起
        snapshot["waiting_queue"] = True
        return snapshot

    snapshot["seconds_left"] = int(max(0.0, delay - (monotonic() - idle_since)))
    return snapshot


def pump() -> None:
    """
    推进一步判定：队列还忙就顺延，空够久了就执行

    正常由后台线程按 POLL_INTERVAL 调它；测试也直接调它 —— 这样时间轴可以手动
    推进，不必为一条断言真等上几分钟。
    """
    with _LOCK:
        if not _STATE["armed"]:
            return

    busy = _probe()
    now = monotonic()

    with _LOCK:
        if not _STATE["armed"]:      # 探测期间被 cancel 了
            return

        if busy:
            # 队列又来任务了：把计时起点抹掉，等它跑完重新开始算
            _STATE["idle_since"] = None
            return

        if _STATE["idle_since"] is None:
            _STATE["idle_since"] = now

        if now - _STATE["idle_since"] < _STATE["delay"]:
            return

        runner = _runner
        _clear()                     # 先清状态再执行：执行抛错也不会把排期卡在"到点未跑"

    if runner is None:
        logger.warning("云端同步排期到点，但没有注册执行动作，本次放弃")
        _remember(False, "没有注册执行动作，这次同步被跳过了")
        return

    logger.info("排期到点，开始云端同步")

    try:
        _remember(True, str(runner() or "已触发云端同步"))

    except Exception as e:
        # 🔴 结果必须留下来：这次执行没有 HTTP 请求在旁边看着，异常再不记进状态，
        # 用户就只剩"点了备份、等了五分钟、什么都没发生"这一个观测
        logger.exception("排期触发的云端同步失败")
        _remember(False, str(e) or e.__class__.__name__)


def _clear() -> None:
    """清空排期状态。调用方负责持有 _LOCK。last_result 不在此列 —— 见 _STATE 的注释"""
    _STATE.update(armed = False, idle_since = None, delay = 0.0, requested_at = None)


def _remember(ok: bool, message: str) -> None:
    """记下这次执行的结果，供页面回看"""
    with _LOCK:
        _STATE["last_result"] = {"ok": bool(ok), "message": message, "at": time()}


def _probe() -> bool:
    """问一次队列还忙不忙。探针本身出错时按「不忙」处理"""
    probe = _is_busy

    if probe is None:
        return False

    try:
        return bool(probe())

    except Exception:
        # 宁可早传一次，也不能因为一次查询异常把排期永远扣在队列里 —— 与通知同一条取舍
        logger.exception("查询下载队列是否仍在运行失败，本次按已结束处理")
        return False


def _ensure_thread() -> None:
    """排期一出现就让心跳线程转起来（只起一次）"""
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
            # 心跳不能因为一次异常就退出 —— 退出以后排期再也没人推进了
            logger.exception("云端同步排期心跳出错")
