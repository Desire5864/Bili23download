"""
util/clouddrive/schedule.py —— 云端同步的自动方案

全部离线：执行动作、队列判据、延迟与开关都是注入的假货，时间轴用手动 pump 推进
—— 真等上 5 分钟不是测试该干的事。心跳线程也在 fixture 里被摁住不起，
否则它会跑到后面的用例里自己 pump，把断言搅乱。

这一组盯的核心是**边沿检测**：只有"先忙过、再空下来"才算刚下完一批。
队列一直空着就传，会变成每拍传一次。
"""

from util.clouddrive import schedule
import time
import pytest


class Recorder:
    """假的执行器 + 假的队列探针：两者的调用次数就是断言对象"""

    def __init__(self, busy = False):
        self.busy = busy
        self.runs = 0

    def runner(self):
        self.runs += 1

        return "已让 CloudDrive2 重新扫描 /Storage/哔哩哔哩"

    def probe(self):
        return self.busy


def wired(recorder, monkeypatch, delay = 0.0, enabled = True):
    """把一台假排期器接上，并把"设置读取"固定成给定的值"""
    schedule.configure(
        runner = recorder.runner,
        is_busy = recorder.probe,
        get_delay = lambda: delay,
        is_enabled = lambda: enabled,
    )

    return recorder


@pytest.fixture(autouse = True)
def clean_scheduler(monkeypatch):
    """每个用例一台干净的排期器，且不允许它自己起心跳线程"""
    def reset():
        with schedule._LOCK:
            schedule._clear()
            schedule._STATE["last_result"] = None

    monkeypatch.setattr(schedule, "_runner", None)
    monkeypatch.setattr(schedule, "_is_busy", None)
    monkeypatch.setattr(schedule, "_get_delay", None)
    monkeypatch.setattr(schedule, "_is_enabled", None)
    monkeypatch.setattr(schedule, "_thread", None)
    monkeypatch.setattr(schedule, "_ensure_thread", lambda: None)

    reset()

    yield

    reset()


def test_nothing_is_pending_to_begin_with():
    state = schedule.status()

    assert state["pending"] is False
    assert state["waiting_queue"] is False
    assert state["seconds_left"] is None
    assert state["last_result"] is None


def test_an_always_idle_queue_never_triggers_a_sync(monkeypatch):
    """
    🔴 这是整套设计的命门：队列一直空着时**什么都不该发生**

    按"空就传"写会变成每 15 秒传一次 —— 那比没有这个功能还糟
    """
    recorder = wired(Recorder(busy = False), monkeypatch)

    for _ in range(10):
        schedule.pump()

    assert recorder.runs == 0
    assert schedule.status()["pending"] is False


def test_a_busy_queue_is_remembered_but_nothing_runs(monkeypatch):
    recorder = wired(Recorder(busy = True), monkeypatch)

    for _ in range(3):
        schedule.pump()

    assert recorder.runs == 0

    state = schedule.status()

    assert state["waiting_queue"] is True, "还在跑就应当报「在等队列」"
    assert state["pending"] is False


def test_the_sync_fires_after_the_queue_drains_and_the_delay_is_zero(monkeypatch):
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()                    # 忙
    recorder.busy = False
    schedule.pump()                    # 空了，delay=0 当场到点

    assert recorder.runs == 1
    assert schedule.status()["pending"] is False


def test_the_delay_is_actually_waited_out(monkeypatch):
    recorder = wired(Recorder(busy = True), monkeypatch, delay = 0.05)

    schedule.pump()                    # 忙
    recorder.busy = False

    schedule.pump()                    # 空下来：只把起点记下来
    assert recorder.runs == 0

    schedule.pump()
    assert recorder.runs == 0

    time.sleep(0.06)
    schedule.pump()

    assert recorder.runs == 1


def test_only_one_sync_per_batch(monkeypatch):
    """传过一次就得收手：不然队列一直空着，下一拍又会传一遍"""
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 1

    for _ in range(5):
        schedule.pump()

    assert recorder.runs == 1


def test_a_new_download_restarts_the_clock(monkeypatch):
    """
    计时中间又来了一批下载：不能因为它"之前空过"就直接执行 ——
    那批新任务刚下完，正是最需要等收尾落定的时候
    """
    recorder = wired(Recorder(busy = True), monkeypatch, delay = 3600)

    schedule.pump()                    # 第一批忙
    recorder.busy = False
    schedule.pump()                    # 空下来，开始计时

    assert schedule.status()["pending"] is True

    recorder.busy = True               # 又来一批
    schedule.pump()

    assert schedule.status()["pending"] is False
    assert recorder.runs == 0

    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 0, "队列重新空下来应当从头计时"


def test_turning_the_auto_off_stops_it(monkeypatch):
    """关掉自动方案：既不许传，也要把"这一批忙过"的记忆丢掉"""
    recorder = Recorder(busy = True)
    wired(recorder, monkeypatch, delay = 3600)

    schedule.pump()

    schedule.configure(is_enabled = lambda: False)
    schedule.pump()

    assert schedule.status()["auto"] is False

    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 0


def test_the_switch_is_read_live(monkeypatch):
    """
    开关是每拍现读的，不是注入时定死的 —— 否则用户在设置页打开它得重启面板
    """
    recorder = Recorder(busy = True)
    enabled = {"on": True}
    schedule.configure(
        runner = recorder.runner,
        is_busy = recorder.probe,
        get_delay = lambda: 0,
        is_enabled = lambda: enabled["on"],
    )

    enabled["on"] = False
    schedule.pump()

    assert schedule.status()["auto"] is False

    # 重新打开之后是"从下一批算起"，不是补传刚被关掉的那一批 —— 所以得再忙一次
    enabled["on"] = True
    recorder.busy = True
    schedule.pump()

    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 1


def test_a_bad_switch_reader_is_treated_as_off(monkeypatch):
    """开关读不出来时按关处理 —— 宁可不动手，也别在一个坏掉的设置上盲目上传"""
    recorder = Recorder(busy = True)

    def boom():
        raise RuntimeError("配置读不出来")

    schedule.configure(runner = recorder.runner, is_busy = recorder.probe,
                       get_delay = lambda: 0, is_enabled = boom)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 0


def test_the_delay_is_read_live_too(monkeypatch):
    """
    延迟也是每拍现读的：把 5 分钟改成 0，正在等的那一轮应当立刻到点，
    而不是等下一个 5 分钟
    """
    recorder = Recorder(busy = True)
    delay = {"seconds": 3600}
    schedule.configure(
        runner = recorder.runner,
        is_busy = recorder.probe,
        get_delay = lambda: delay["seconds"],
        is_enabled = lambda: True,
    )

    schedule.pump()                    # 忙
    recorder.busy = False
    schedule.pump()                    # 空下来，开始按 3600 秒计时

    assert recorder.runs == 0

    delay["seconds"] = 0
    schedule.pump()

    assert recorder.runs == 1


def test_cancel_calls_off_the_countdown(monkeypatch):
    recorder = wired(Recorder(busy = True), monkeypatch, delay = 3600)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    assert schedule.status()["pending"] is True
    assert schedule.cancel() is True
    assert schedule.status()["pending"] is False

    schedule.pump()

    assert recorder.runs == 0


def test_cancel_while_the_queue_is_still_running_holds(monkeypatch):
    """
    🔴 队列还在跑的时候撤销，必须顶住：否则下一拍又把"忙过"记回来，
    用户撤了个寂寞 —— 这条是取消按钮最容易被写成摆设的地方
    """
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()

    assert schedule.cancel() is True

    schedule.pump()
    schedule.pump()

    recorder.busy = False
    schedule.pump()
    schedule.pump()

    assert recorder.runs == 0, "撤过的那一批不许再传"


def test_cancelling_one_batch_does_not_mute_the_next(monkeypatch):
    """撤的是"这一批"，不是"以后都别传" —— 下一批照常自动同步"""
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()
    schedule.cancel()

    recorder.busy = False
    schedule.pump()                    # 第一批结束，skip 解除

    recorder.busy = True
    schedule.pump()                    # 第二批
    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 1


def test_cancel_reports_when_there_was_nothing_to_cancel(monkeypatch):
    """没在等就得如实说没有 —— 报「已取消」会让用户以为撤掉了什么"""
    wired(Recorder(busy = False), monkeypatch)

    assert schedule.cancel() is False


def test_a_failing_run_neither_escapes_nor_jams_the_watcher(monkeypatch):
    def boom():
        raise RuntimeError("连不上 CloudDrive2")

    schedule.configure(boom, lambda: True, lambda: 0, lambda: True)

    schedule.pump()
    schedule.configure(is_busy = lambda: False)
    schedule.pump()          # 异常不能冒到调用方（这里是心跳线程）

    assert schedule.status()["pending"] is False, "状态要先清掉，别卡在「到点却没跑」"

    recorder = Recorder(busy = True)
    wired(recorder, monkeypatch)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 1, "失败一次之后必须还能再来"


def test_a_probe_that_raises_counts_as_idle(monkeypatch):
    """探针自己炸了宁可早传一次，也不能把计时永远扣在队列里"""
    def bad_probe():
        raise RuntimeError("任务库读不出来")

    recorder = Recorder(busy = True)
    schedule.configure(recorder.runner, recorder.probe, lambda: 0, lambda: True)

    schedule.pump()                    # 先记下"忙过"
    schedule.configure(is_busy = bad_probe)
    schedule.pump()

    assert recorder.runs == 1


def test_a_successful_run_is_kept_for_the_page(monkeypatch):
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is True
    assert "CloudDrive2" in last["message"]
    assert last["at"] > 0


def test_a_failed_run_is_kept_for_the_page(monkeypatch):
    """
    失败必须留下痕迹：这一轮没有 HTTP 请求在旁边看着，异常再不记进状态，
    用户就只剩「下完了、等了几分钟、什么都没发生」这一个观测
    """
    def boom():
        raise RuntimeError("连不上 CloudDrive2")

    schedule.configure(boom, lambda: True, lambda: 0, lambda: True)
    schedule.pump()
    schedule.configure(is_busy = lambda: False)
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is False
    assert "连不上" in last["message"]


def test_the_last_result_outlives_the_next_round(monkeypatch):
    """计时状态会被清，但「上次到底传成没传成」不该跟着消失"""
    recorder = wired(Recorder(busy = True), monkeypatch)

    schedule.pump()
    recorder.busy = False
    schedule.pump()

    assert schedule.status()["last_result"]["ok"] is True

    recorder.busy = True
    schedule.pump()

    assert schedule.status()["last_result"]["ok"] is True


def test_a_missing_runner_is_recorded_rather_than_silently_dropped(monkeypatch):
    """面板没注入执行动作时也要留痕，不能悄悄丢掉"""
    monkeypatch.setattr(schedule, "_runner", None)
    monkeypatch.setattr(schedule, "_is_busy", lambda: True)
    monkeypatch.setattr(schedule, "_get_delay", lambda: 0)
    monkeypatch.setattr(schedule, "_is_enabled", lambda: True)

    schedule.pump()
    schedule.configure(is_busy = lambda: False)
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is False
    assert "没有注册执行动作" in last["message"]


def test_queue_busy_asks_right_now(monkeypatch):
    """手动同步前的提示要现问一次，不能拿心跳那一拍的记忆值糊弄"""
    recorder = Recorder(busy = False)
    wired(recorder, monkeypatch)

    assert schedule.queue_busy() is False

    recorder.busy = True

    assert schedule.queue_busy() is True


def test_configure_starts_the_heartbeat(monkeypatch):
    """自动方案没人点按钮也得有人推进 —— 心跳没起来，它永远不会到点"""
    started = []

    monkeypatch.setattr(schedule, "_ensure_thread", lambda: started.append(1))

    wired(Recorder(busy = False), monkeypatch)

    assert started == [1]
