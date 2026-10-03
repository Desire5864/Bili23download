"""
util/clouddrive/schedule.py —— 云端同步的排期器

全部离线：执行动作与队列判据都是注入的假货，时间轴用手动 pump 推进 ——
真等上 5 分钟不是测试该干的事。心跳线程也在 fixture 里被摁住不起，
否则它会跑到后面的用例里自己 pump，把断言搅乱。
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


@pytest.fixture(autouse = True)
def clean_scheduler(monkeypatch):
    """每个用例一台干净的排期器，且不允许它自己起心跳线程"""
    def reset():
        with schedule._LOCK:
            schedule._clear()
            schedule._STATE["last_result"] = None

    monkeypatch.setattr(schedule, "_runner", None)
    monkeypatch.setattr(schedule, "_is_busy", None)
    monkeypatch.setattr(schedule, "_thread", None)
    monkeypatch.setattr(schedule, "_ensure_thread", lambda: None)

    reset()

    yield

    reset()


def test_nothing_is_scheduled_to_begin_with():
    state = schedule.status()

    assert state["armed"] is False
    assert state["seconds_left"] is None
    assert state["waiting_queue"] is False
    assert state["last_result"] is None


def test_a_busy_queue_holds_the_sync_off():
    """下载还在跑就不许传 —— 这正是这整套排期的理由"""
    recorder = Recorder(busy = True)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)

    for _ in range(5):
        schedule.pump()

    assert recorder.runs == 0
    assert schedule.status()["waiting_queue"] is True


def test_the_sync_fires_once_the_queue_drains_and_the_delay_is_zero():
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)
    schedule.pump()

    assert recorder.runs == 1
    assert schedule.status()["armed"] is False


def test_the_delay_is_actually_waited_out():
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0.05)

    # 队列是空的，但延迟没到 —— 第一次 pump 只把空闲的起点记下来
    schedule.pump()
    assert recorder.runs == 0

    schedule.pump()
    assert recorder.runs == 0

    time.sleep(0.06)
    schedule.pump()

    assert recorder.runs == 1


def test_a_new_download_restarts_the_clock():
    """
    计时中间又来了一批下载：不能因为它"之前空过"就直接执行 ——
    那批新任务刚下完，正是最需要等收尾落定的时候
    """
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0.05)
    schedule.pump()

    assert schedule.status()["waiting_queue"] is False

    recorder.busy = True
    schedule.pump()

    assert schedule.status()["waiting_queue"] is True
    assert recorder.runs == 0

    recorder.busy = False
    schedule.pump()

    assert recorder.runs == 0, "队列重新空下来应当从头计时"


def test_scheduling_again_pushes_the_deadline_back():
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(3600)
    schedule.pump()

    scheduled_at = schedule.status()["requested_at"]

    assert scheduled_at is not None

    schedule.schedule(0)
    schedule.pump()

    assert recorder.runs == 1, "再点一次应当是重新计时，而不是排队等第二次"


def test_cancel_calls_off_a_pending_sync():
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)

    assert schedule.cancel() is True
    assert schedule.status()["armed"] is False

    schedule.pump()

    assert recorder.runs == 0


def test_cancel_reports_when_there_was_nothing_to_cancel():
    """没排期就得如实说没有 —— 报「已取消」会让用户以为撤掉了什么"""
    assert schedule.cancel() is False


def test_a_failing_run_neither_escapes_nor_jams_the_scheduler():
    def boom():
        raise RuntimeError("连不上 CloudDrive2")

    schedule.configure(boom, lambda: False)

    schedule.schedule(0)
    schedule.pump()          # 异常不能冒到调用方（这里是心跳线程）

    assert schedule.status()["armed"] is False, "状态要先清掉，别卡在「到点却没跑」"

    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)
    schedule.schedule(0)
    schedule.pump()

    assert recorder.runs == 1, "失败一次之后必须还能再排"


def test_a_probe_that_raises_counts_as_idle():
    """探针自己炸了宁可早传一次，也不能把排期永远扣在队列里"""
    def bad_probe():
        raise RuntimeError("任务库读不出来")

    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, bad_probe)

    schedule.schedule(0)
    schedule.pump()

    assert recorder.runs == 1


def test_a_successful_run_is_kept_for_the_page():
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is True
    assert "CloudDrive2" in last["message"]
    assert last["at"] > 0


def test_a_failed_run_is_kept_for_the_page():
    """
    失败必须留下痕迹：这次执行没有 HTTP 请求在旁边看着，异常再不记进状态，
    用户就只剩「点了备份、等了五分钟、什么都没发生」这一个观测
    """
    def boom():
        raise RuntimeError("连不上 CloudDrive2")

    schedule.configure(boom, lambda: False)

    schedule.schedule(0)
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is False
    assert "连不上" in last["message"]


def test_the_last_result_outlives_the_next_schedule():
    """排期状态会被清，但「上次到底传成没传成」不该跟着消失"""
    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)
    schedule.pump()

    busy = Recorder(busy = True)
    schedule.configure(busy.runner, busy.probe)
    schedule.schedule(60)

    state = schedule.status()

    assert state["armed"] is True
    assert state["last_result"]["ok"] is True


def test_a_missing_runner_is_recorded_rather_than_silently_dropped(monkeypatch):
    """面板没注入执行动作时也要留痕，不能悄悄丢掉"""
    monkeypatch.setattr(schedule, "_runner", None)
    monkeypatch.setattr(schedule, "_is_busy", lambda: False)

    schedule.schedule(0)
    schedule.pump()

    last = schedule.status()["last_result"]

    assert last["ok"] is False
    assert "没有注册执行动作" in last["message"]


def test_scheduling_starts_the_heartbeat(monkeypatch):
    """排期一出现就得有人推进 —— 心跳没起来，排期永远不会到点"""
    started = []

    monkeypatch.setattr(schedule, "_ensure_thread", lambda: started.append(1))

    recorder = Recorder(busy = False)
    schedule.configure(recorder.runner, recorder.probe)

    schedule.schedule(0)

    assert started == [1]
