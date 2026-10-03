"""
面板对下载任务的控制（暂停 / 继续 / 取消），以及「进程重启后残留状态」的处理。

背景：Downloader 与 Merger 都只活在内存里，容器重建或程序重启之后一个都不剩，
而任务库里留下的仍是 queued / downloading / merging…。面板以前直接读库原样显示，
于是界面上出现一个进度永远不动、既暂停不了也继续不了的「下载中」；点暂停时还会
撞上 _control_task 里那道 downloaders.get(task_id) 的检查，收到
"No active task with id ..." —— 2026-10-02 用户报的「暂停不了，也没继续下载」就是它。

本文件锁三件事：
  1. ACTIVE_STATUSES 覆盖全部"需要活的驱动对象才成立"的状态；
  2. 展示层把这类残留状态如实报成已暂停（与桌面端 QueryWorker 同一套语义）；
  3. 控制层不再被"内存里没有下载器"卡住，且先把状态落到已暂停再分派动作。

控制层用替身模型测：真实的 DownloadListModel 需要真窗口与真下载器，而这里要锁的
恰恰是"还没轮到下载器"的那段分派逻辑。
"""

from util.common.enum import ACTIVE_STATUSES, RUNNING_STATUSES, DownloadStatus
from util.download.downloader.manager import downloader_manager
from util.download.task.info import TaskInfo
from util.download.task.manager import task_manager
from util.mcp.tools import download as download_tools
from util.mcp.tools import task as task_tools

import pytest


def make_task(task_id: str, status: DownloadStatus, **download) -> TaskInfo:
    task_info = TaskInfo()

    task_info.Basic.task_id = task_id
    task_info.Basic.show_title = f"任务 {task_id}"
    task_info.Download.status = status

    for key, value in download.items():
        setattr(task_info.Download, key, value)

    return task_info


class _StubIndex:
    def __init__(self, task_info):
        self._task_info = task_info

    def data(self, role = None):
        return self._task_info


class _StubSourceModel:
    def __init__(self, task_list):
        self._task_list = task_list

    def rowCount(self, parent = None):
        return len(self._task_list)

    def index(self, row, column = 0, parent = None):
        return _StubIndex(self._task_list[row])


class _StubModel:
    """代理模型替身：只记录被调用的动作，以及调用那一刻的状态"""

    def __init__(self, task_list):
        self._source = _StubSourceModel(task_list)
        self.calls = []

    def sourceModel(self):
        return self._source

    def togglePauseResume(self, task_info):
        self.calls.append(("toggle", DownloadStatus(task_info.Download.status)))

    def cancelDownload(self, task_info):
        self.calls.append(("cancel", DownloadStatus(task_info.Download.status)))

    def onUpdateData(self, task_info):
        self.calls.append(("update", DownloadStatus(task_info.Download.status)))

    def actions(self):
        return [name for name, _ in self.calls]

    def first_status(self, name):
        for call_name, status in self.calls:
            if call_name == name:
                return status

        return None


@pytest.fixture
def control_env(monkeypatch):
    """
    把控制层的外部依赖换成替身：下载器表、任务库、界面模型

    返回一个可以随后注入任务与模型的容器
    """
    env = {
        "downloaders": {},
        "updated": [],
        "model": None,
        "listed": [],
        "in_db": {},
    }

    monkeypatch.setattr(downloader_manager, "downloaders", env["downloaders"])
    monkeypatch.setattr(
        task_manager, "update_async", lambda task_info: env["updated"].append(task_info)
    )
    monkeypatch.setattr(task_manager, "query_by_id", lambda task_id: env["in_db"].get(task_id))
    monkeypatch.setattr(
        download_tools, "_get_downloading_model", lambda: env["model"]
    )

    return env


class _FakeDownloader:
    def __init__(self, task_info):
        self.task_info = task_info

    def pause(self):
        self.task_info.Download.status = DownloadStatus.PAUSED


class TestActiveStatuses:
    def test_covers_every_status_that_needs_a_live_worker(self):
        assert ACTIVE_STATUSES == frozenset({
            DownloadStatus.QUEUED,
            DownloadStatus.PARSING,
            DownloadStatus.DOWNLOADING,
            DownloadStatus.FFMPEG_QUEUED,
            DownloadStatus.MERGING,
            DownloadStatus.CONVERTING,
            DownloadStatus.ADDITIONAL_PROCESSING,
        })

    @pytest.mark.parametrize("status", [
        DownloadStatus.PAUSED,
        DownloadStatus.COMPLETED,
        DownloadStatus.FAILED,
        DownloadStatus.FFMPEG_FAILED,
        DownloadStatus.INVALID,
    ])
    def test_leaves_settled_states_alone(self, status):
        # 这些状态不依赖"内存里有东西在跑"，归一化时不能碰：
        # 已暂停要原样保留（否则界面上的「继续」会莫名其妙消失），
        # 失败与已完成更是终态
        assert status not in ACTIVE_STATUSES

    def test_control_normalisation_excludes_queued(self):
        """
        控制层归一化用的集合要把 QUEUED 摘出去

        排队等槽位本来就不需要活的驱动对象，而"没有下载器 + 状态在 ACTIVE_STATUSES"
        正是判残留态的条件 —— 不摘掉的话，排队中的任务会被当成重启残留：显示上
        被归一成"已暂停"，用户点「暂停」还会先被偷偷改状态、再收到"它没在下载"
        """
        assert RUNNING_STATUSES == ACTIVE_STATUSES - {DownloadStatus.QUEUED}

    @pytest.mark.parametrize("status", [
        DownloadStatus.PARSING,
        DownloadStatus.DOWNLOADING,
        DownloadStatus.FFMPEG_QUEUED,
        DownloadStatus.MERGING,
        DownloadStatus.CONVERTING,
        DownloadStatus.ADDITIONAL_PROCESSING,
    ])
    def test_control_normalisation_still_covers_every_running_state(self, status):
        assert status in RUNNING_STATUSES


class TestStaleNormalisation:
    def test_status_without_downloader_is_reported_paused(self, control_env, monkeypatch):
        stale = make_task("t-stale", DownloadStatus.DOWNLOADING, progress = 60, speed = 49735286)

        monkeypatch.setattr(task_manager, "query", lambda completed, limit = None, offset = 0: [stale])

        result = task_tools._query_tasks(False)

        assert result[0].Download.status == DownloadStatus.PAUSED

    def test_paused_and_failed_keep_their_status(self, control_env, monkeypatch):
        paused = make_task("t-paused", DownloadStatus.PAUSED)
        failed = make_task("t-failed", DownloadStatus.FAILED)

        monkeypatch.setattr(
            task_manager, "query", lambda completed, limit = None, offset = 0: [paused, failed]
        )

        result = task_tools._query_tasks(False)

        assert [t.Download.status for t in result] == [DownloadStatus.PAUSED, DownloadStatus.FAILED]

    def test_live_task_wins_over_db_snapshot(self, control_env, monkeypatch):
        # 界面上的那份是实时的（进度、速度高频更新），数据库快照会滞后，
        # 内存里有下载器时必须用内存那份，而不是把库里的旧值回给调用方
        snapshot = make_task("t-live", DownloadStatus.DOWNLOADING, progress = 40)
        live = make_task("t-live", DownloadStatus.DOWNLOADING, progress = 61)

        control_env["downloaders"]["t-live"] = _FakeDownloader(live)

        monkeypatch.setattr(
            task_manager, "query", lambda completed, limit = None, offset = 0: [snapshot]
        )

        result = task_tools._query_tasks(False)

        assert result[0] is live
        assert result[0].Download.progress == 61

    def test_find_task_normalises_stale_status(self, control_env, monkeypatch):
        stale = make_task("t-stale", DownloadStatus.MERGING)

        monkeypatch.setattr(task_manager, "query_by_id", lambda task_id: stale)

        assert task_tools._find_task("t-stale").Download.status == DownloadStatus.PAUSED

    def test_is_stale_requires_both_conditions(self, control_env):
        from util.mcp.tools.task import _is_stale

        running = make_task("t-a", DownloadStatus.DOWNLOADING)

        assert _is_stale(running) is True

        control_env["downloaders"]["t-a"] = _FakeDownloader(running)

        assert _is_stale(running) is False

        settled = make_task("t-b", DownloadStatus.COMPLETED)

        assert _is_stale(settled) is False


class TestListedCopyWins:
    """
    未完成任务的权威副本在**界面列表**里，不在数据库里

    最要紧的一条：排队等槽位的任务（QUEUED）在内存里本来就不该有下载器实例 ——
    它只是在等前面那个下完。而"状态在 ACTIVE_STATUSES + 内存里没有下载器"恰好是
    _is_stale() 判重启残留态的条件，于是它一排队就被显示成「已暂停」：
    用户点了「一键开始」，界面上还是一片"已暂停"，只当他没生效。
    2026-10-02 实测确认过这一点（库里 QUEUED 的两条，面板报的是 paused）。
    """

    def _prepare(self, control_env, *tasks):
        control_env["model"] = _StubModel(list(tasks))

        return control_env["model"]

    def _db_returns(self, monkeypatch, *tasks):
        monkeypatch.setattr(
            task_manager, "query", lambda completed, limit = None, offset = 0: list(tasks)
        )

    def test_queued_task_waiting_for_a_slot_is_reported_as_queued(self, control_env, monkeypatch):
        waiting = make_task("t-waiting", DownloadStatus.QUEUED)

        self._prepare(control_env, waiting)
        self._db_returns(monkeypatch, waiting)

        result = task_tools._query_tasks(False)

        assert result[0] is waiting
        assert result[0].Download.status == DownloadStatus.QUEUED

    def test_listed_copy_wins_over_the_database_snapshot(self, control_env, monkeypatch):
        # 库里写的还是用户上次点的「已暂停」，而界面上它已经被调度器排进队了
        snapshot = make_task("t-q", DownloadStatus.PAUSED)
        listed = make_task("t-q", DownloadStatus.QUEUED)

        self._prepare(control_env, listed)
        self._db_returns(monkeypatch, snapshot)

        assert task_tools._query_tasks(False)[0] is listed

    def test_listed_copy_wins_over_the_database_for_a_running_task(self, control_env, monkeypatch):
        # 界面那一份是实时的（进度高频更新），库里的快照必然滞后
        snapshot = make_task("t-run", DownloadStatus.DOWNLOADING, progress = 12)
        listed = make_task("t-run", DownloadStatus.DOWNLOADING, progress = 71)

        self._prepare(control_env, listed)
        self._db_returns(monkeypatch, snapshot)

        assert task_tools._query_tasks(False)[0].Download.progress == 71

    def test_completed_list_never_consults_the_interface(self, control_env, monkeypatch):
        """已完成的早就移出下载列表了，那条路必须仍以数据库为准"""
        done = make_task("t-done", DownloadStatus.COMPLETED)

        self._prepare(control_env, make_task("t-done", DownloadStatus.QUEUED))
        self._db_returns(monkeypatch, done)

        assert task_tools._query_tasks(True)[0] is done

    def test_find_task_prefers_the_listed_copy(self, control_env, monkeypatch):
        waiting = make_task("t-waiting", DownloadStatus.QUEUED)

        self._prepare(control_env, waiting)

        monkeypatch.setattr(
            task_manager, "query_by_id",
            lambda task_id: make_task(task_id, DownloadStatus.PAUSED),
        )

        assert task_tools._find_task("t-waiting") is waiting

    def test_a_broken_interface_does_not_break_the_listing(self, control_env, monkeypatch):
        """取界面那一份失败时必须退回数据库口径，而不是让整个列表报错"""

        class _ExplodingModel:
            def sourceModel(self):
                raise RuntimeError("界面还没装配好")

        stale = make_task("t-stale", DownloadStatus.DOWNLOADING)

        control_env["model"] = _ExplodingModel()

        self._db_returns(monkeypatch, stale)

        result = task_tools._query_tasks(False)

        assert result[0].Download.status == DownloadStatus.PAUSED


class TestControlTask:
    def _prepare(self, control_env, *tasks):
        control_env["model"] = _StubModel(list(tasks))
        control_env["in_db"] = {t.Basic.task_id: t for t in tasks}

        return control_env["model"]

    def test_stale_task_is_normalised_and_persisted(self, control_env):
        stale = make_task("t-stale", DownloadStatus.DOWNLOADING, progress = 60)

        self._prepare(control_env, stale)

        # 先暂停它：此时它其实没在跑，归一到已暂停之后应被拒
        reason = download_tools._control_task("t-stale", "pause")

        assert DownloadStatus.PAUSED == stale.Download.status
        assert "not downloading" in reason
        # 归一化必须写回任务库，否则数据库与界面说的不是同一件事
        assert control_env["updated"] == [stale]

    def test_stale_task_can_be_resumed_and_starts_from_paused(self, control_env):
        # 用户实际会走的路径：面板把残留状态显示成「已暂停」，他点「继续」
        stale = make_task("t-stale", DownloadStatus.DOWNLOADING, progress = 60)
        model = self._prepare(control_env, stale)

        assert download_tools._control_task("t-stale", "resume") is None
        assert model.actions() == ["toggle"]
        # 分派时必须已经是 PAUSED —— Downloader 的 resume 只从这条分支走，
        # 带着 DOWNLOADING 进去会落到 pause 那一支，点「继续」反而把任务停掉
        assert model.first_status("toggle") == DownloadStatus.PAUSED

    def test_live_downloading_task_pauses_through_the_model(self, control_env):
        running = make_task("t-live", DownloadStatus.DOWNLOADING, progress = 30)
        model = self._prepare(control_env, running)

        control_env["downloaders"]["t-live"] = _FakeDownloader(running)

        assert download_tools._control_task("t-live", "pause") is None
        assert model.actions() == ["toggle"]
        assert model.first_status("toggle") == DownloadStatus.DOWNLOADING

    def test_paused_task_resumes(self, control_env):
        paused = make_task("t-paused", DownloadStatus.PAUSED)
        model = self._prepare(control_env, paused)

        assert download_tools._control_task("t-paused", "resume") is None
        assert model.first_status("toggle") == DownloadStatus.PAUSED

    def test_queued_task_is_paused_directly_not_started(self, control_env):
        # togglePauseResume 对 QUEUED 是"启动下载"，拿它暂停等于反着来
        queued = make_task("t-queued", DownloadStatus.QUEUED)
        model = self._prepare(control_env, queued)

        # 内存里有下载器与否都走这一支：归一化判据 RUNNING_STATUSES 不含 QUEUED
        control_env["downloaders"]["t-queued"] = _FakeDownloader(queued)

        assert download_tools._control_task("t-queued", "pause") is None
        assert ("toggle", DownloadStatus.QUEUED) not in model.calls
        assert queued.Download.status == DownloadStatus.PAUSED
        assert model.actions() == ["update"]

    def test_queued_task_without_a_downloader_is_not_treated_as_stale(self, control_env):
        """
        排队等槽位的任务本来就没有下载器，那不是"重启残留"

        🔴 归一化判据一度用 ACTIVE_STATUSES（含 QUEUED），于是面板上「排队中」的任务
        被当成僵尸态改写成「已暂停」：用户按了「一键开始」，看到 48 条里只有 1 条在跑、
        其余全变「已暂停」，以为一键开始没生效。实测线上库是 QUEUED、列表却报 paused。
        """
        queued = make_task("t-queued", DownloadStatus.QUEUED)
        model = self._prepare(control_env, queued)

        # 没有 downloaders 条目，也仍然走"直接落 PAUSED"那条正常分支
        assert download_tools._control_task("t-queued", "pause") is None
        assert ("toggle", DownloadStatus.QUEUED) not in model.calls
        assert queued.Download.status == DownloadStatus.PAUSED
        assert model.actions() == ["update"]

    def test_queued_task_stays_queued_when_only_read(self, control_env):
        # 只查状态不该改动它：一键开始刚排好队，列表刷新不能把它抹成已暂停
        queued = make_task("t-queued", DownloadStatus.QUEUED)
        model = self._prepare(control_env, queued)

        reason = download_tools._control_task("t-queued", "resume")

        assert reason is not None and "not paused" in reason
        assert queued.Download.status == DownloadStatus.QUEUED
        assert model.calls == []

    def test_stale_task_cancels_without_waiting_for_itself(self, control_env):
        # cancelDownload 对 DOWNLOADING 是"先等分片线程停下再删文件"。
        # 一个并没有在跑的任务永远等不到那一刻 —— 之前它会挂到超时
        stale = make_task("t-stale", DownloadStatus.DOWNLOADING, progress = 60)
        model = self._prepare(control_env, stale)

        assert download_tools._control_task("t-stale", "cancel") is None
        assert model.actions() == ["cancel"]
        assert model.first_status("cancel") == DownloadStatus.PAUSED

    def test_merging_task_cannot_be_cancelled(self, control_env):
        merging = make_task("t-merging", DownloadStatus.MERGING)
        model = self._prepare(control_env, merging)

        # 内存里有下载器才说明它真在合并
        control_env["downloaders"]["t-merging"] = _FakeDownloader(merging)

        reason = download_tools._control_task("t-merging", "cancel")

        assert "cannot be cancelled" in reason
        assert model.calls == []

    def test_stale_merging_task_can_be_cancelled(self, control_env):
        # 重启后卡在「合并中」的任务其实早没有 Merger 在跑了。
        # 若不做归一化，cancelDownload 会把它当成"正在合并"直接拒绝，
        # 用户就会看到一个既删不掉也动不了的死任务
        merging = make_task("t-merging", DownloadStatus.MERGING)
        model = self._prepare(control_env, merging)

        assert download_tools._control_task("t-merging", "cancel") is None
        assert model.first_status("cancel") == DownloadStatus.PAUSED

    def test_unknown_task_is_rejected(self, control_env):
        self._prepare(control_env)

        reason = download_tools._control_task("t-missing", "resume")

        assert "No task with id" in reason

    def test_completed_task_is_rejected(self, control_env):
        # 已完成的任务已经移出「正在下载」列表，控制不了，但要说清楚原因
        done = make_task("t-done", DownloadStatus.COMPLETED)

        model = self._prepare(control_env)
        control_env["in_db"]["t-done"] = done

        reason = download_tools._control_task("t-done", "resume")

        assert "already completed" in reason
        assert model.calls == []

    def test_missing_interface_is_reported(self, control_env):
        control_env["model"] = None

        reason = download_tools._control_task("t-any", "pause")

        assert "not ready" in reason
