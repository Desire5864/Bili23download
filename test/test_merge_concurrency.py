"""
合并（FFmpeg）并发调度：上限从设置里读，默认 2。

原先 DownloadListModel._manageConcurrentMerges 把上限硬编码成 1（源码注释写着
"为避免多个合并任务同时进行导致高频资源占用，每次只允许一个"），用户 2026-10-02
要求能同时跑 2 个。这里锁三件事：

  ① 上限真的读 config.merge_parallel —— 设成几就放行几个，不是把 1 改成 2 写死；
  ② 已经在合并的照样计入额度，不会因为"又点了两次"就把并发撑破；
  ③ 并发安全的前提：每个任务的落盘名字都带 task_id，两个 Merger 不会互相覆盖。

用的是**真实的 DownloadListModel**（需要 QApplication，与其它 GUI 用例同一套做法），
只把 togglePauseResume 换成"接住放行"的替身 —— 真身那一步会去起真 FFmpeg。
"""

from PySide6.QtWidgets import QApplication

from gui.component.download_list.model import DownloadListModel

from util.common.config import config
from util.common.enum import DownloadStatus
from util.download.downloader.merger import Merger
from util.download.task.info import TaskInfo

import pytest


@pytest.fixture(scope = "module")
def app():
    # QApplication 全进程只能有一个，模块级复用（与 test_gui_smoke.py 同一条约束）
    return QApplication.instance() or QApplication([])


def make_task(task_id: str, status: DownloadStatus) -> TaskInfo:
    task_info = TaskInfo()

    task_info.Basic.task_id = task_id
    task_info.Download.status = status

    return task_info


def build(app, statuses):
    return DownloadListModel([
        make_task(f"t{index}", status) for index, status in enumerate(statuses)
    ])


class _Starter:
    """接住调度器真正"放行"的那几下，不真去起 FFmpeg"""

    def __init__(self):
        self.started = []

    def __call__(self, task_info):
        self.started.append(task_info.Basic.task_id)

        # 真身 togglePauseResume 对 FFMPEG_QUEUED 是 downloader.start_merge()，
        # 同步就把状态推到 MERGING；调度器正是据此计数的
        task_info.Download.status = DownloadStatus.MERGING


def arranged(app, monkeypatch, statuses, limit):
    model = build(app, statuses)

    monkeypatch.setattr(config, "get", lambda item, default = None: limit)

    starter = _Starter()

    monkeypatch.setattr(model, "togglePauseResume", starter)

    return model, starter


def test_default_limit_is_two():
    """没动过设置时是 2 —— 用户要的就是 2，别让它随上游默认值 1 漂回去"""
    assert config.get(config.merge_parallel) == 2


def test_upper_bound_follows_the_users_ask():
    """
    上限：用户 2026-10-03 要求能在面板设置里自己往上调，从 4 放到 16。

    这个数字与 server.py 的 SETTINGS_FIELDS 里那条 max 是**同一件事的两处写法**
    （GUI 滑块读 config 的 range，网页读 SETTINGS_FIELDS），改一处漏一处会让
    两边打架：网页放行了 16，写回时被 config 的 validator 掐回 4（或反过来，
    滑块够不着网页允许的值）。这里先钉住 config 这一侧，网页那侧由
    test_web_panel 的字段断言守着
    """
    assert config.merge_parallel.range == (1, 16)


def test_merge_scheduler_starts_up_to_the_limit(app, monkeypatch):
    model, starter = arranged(app, monkeypatch, [DownloadStatus.FFMPEG_QUEUED] * 5, limit = 2)

    model.manageConcurrentMerges()

    assert len(starter.started) == 2


# 6、8、16 是放开上限后用户真会填进来的值：调度器必须照单全收，
# 不能在哪一层还留着旧的 4 当暗桩（队列给足 20 条，免得是"没任务可放"混过去）
@pytest.mark.parametrize("limit", [1, 3, 4, 6, 8, 16])
def test_merge_scheduler_follows_the_setting(app, monkeypatch, limit):
    """换成别的值也照办，防止"写死成 2"混过去"""
    model, starter = arranged(app, monkeypatch, [DownloadStatus.FFMPEG_QUEUED] * 20, limit = limit)

    model.manageConcurrentMerges()

    assert len(starter.started) == limit


def test_merge_scheduler_counts_already_merging_tasks(app, monkeypatch):
    """已经在合并的占额度：一条在合 + 上限 2 → 只再补一条"""
    statuses = [DownloadStatus.MERGING] + [DownloadStatus.FFMPEG_QUEUED] * 4

    model, starter = arranged(app, monkeypatch, statuses, limit = 2)

    model.manageConcurrentMerges()

    assert len(starter.started) == 1


def test_merge_scheduler_is_a_no_op_when_the_quota_is_full(app, monkeypatch):
    """额度已满时一条都不放行，等待中的留在 FFMPEG_QUEUED"""
    statuses = [DownloadStatus.MERGING, DownloadStatus.CONVERTING] + [DownloadStatus.FFMPEG_QUEUED] * 3

    model, starter = arranged(app, monkeypatch, statuses, limit = 2)

    model.manageConcurrentMerges()

    assert starter.started == []


def test_merge_scheduler_ignores_tasks_that_are_not_waiting_to_merge(app, monkeypatch):
    """下载中 / 已暂停 / 失败的都不该被合并调度器碰"""
    statuses = [
        DownloadStatus.DOWNLOADING, DownloadStatus.PAUSED, DownloadStatus.QUEUED,
        DownloadStatus.FAILED, DownloadStatus.FFMPEG_FAILED, DownloadStatus.COMPLETED,
    ]

    model, starter = arranged(app, monkeypatch, statuses, limit = 2)

    model.manageConcurrentMerges()

    assert starter.started == []


def test_concurrent_mergers_do_not_share_file_names(app):
    """
    并发的前提：两个任务的临时文件、重封装产物、输出名各带各的 task_id

    原先上限是 1，这层隔离只是"顺带成立"；放开到 2 之后它变成硬前提 ——
    真撞上了是两个 FFmpeg 写同一个文件，产出直接损坏
    """
    first = Merger(make_task("id-a", DownloadStatus.FFMPEG_QUEUED))
    second = Merger(make_task("id-b", DownloadStatus.FFMPEG_QUEUED))

    names = ("temp_video_file_name", "temp_audio_file_name", "temp_output_file_name",
             "temp_remux_audio_file_name")

    for name in names:
        assert getattr(first, name) != getattr(second, name), name
        assert "id-a" in getattr(first, name), name


def test_lists_file_is_per_task(app, tmp_path):
    """concat 清单也一样：并发时两个任务各写各的"""
    task = make_task("id-a", DownloadStatus.FFMPEG_QUEUED)

    task.File.download_path = str(tmp_path)
    task.File.folder = "."
    task.File.video_file_ext = "m4s"

    assert Merger(task).create_lists_file(2) == "lists_id-a.txt"
    assert (tmp_path / "lists_id-a.txt").read_text(encoding = "utf-8") == (
        "file 'video_id-a_0.m4s'\nfile 'video_id-a_1.m4s'\n"
    )
