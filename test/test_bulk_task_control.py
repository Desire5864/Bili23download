"""
面板「下载中」页的「一键暂停 / 一键取消 / 一键开始」（批量控制）。

它们是 pause_all_tasks / cancel_all_tasks / resume_all_tasks 三个 MCP 工具，
面板那三颗按钮走的就是它们（页面里由 action + "_all_tasks" 拼出来）。

- **暂停 / 取消**逐条复用 _control_task，而不是另写一套批量分派 —— 单任务那条路里有
  "重启残留态先归一化成已暂停""QUEUED 只落状态不启动""合并中不许取消"这些特例，
  复制一份出来迟早会分叉，而分叉只会在用户点批量的时候暴露。

- **继续是个例外**：单任务那条路对 PAUSED 是直接 downloader.resume()，整批套上去等于
  "点几条起几条"，设置里的「同时下载任务数」完全不参与。本文件另一半重量就在这儿：
  一键开始必须是"整批落队，交给并发调度器按上限放行"。

真实 DownloadListModel 需要真窗口与真下载器，这里用替身：要测的恰恰是
"还没轮到下载器"的那段分派逻辑。替身里的 manageConcurrentDownloads 复刻了真身的
调度规则（数出在跑的几个，只把队列补到上限为止），并发上限从 config 里真读。
"""

from util.common.config import config
from util.common.enum import DownloadStatus
from util.download.downloader.manager import downloader_manager
from util.download.task.info import TaskInfo
from util.download.task.manager import task_manager
from util.mcp.tools import download as download_tools

import pytest

RUNNING = (DownloadStatus.PARSING, DownloadStatus.DOWNLOADING)


def make_task(task_id: str, status: DownloadStatus) -> TaskInfo:
    task_info = TaskInfo()

    task_info.Basic.task_id = task_id
    task_info.Basic.show_title = f"任务 {task_id}"
    task_info.Download.status = status

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
    """
    代理模型替身

    togglePauseResume 复刻真实语义（QUEUED → 起跑、DOWNLOADING ↔ PAUSED 互换）；
    manageConcurrentDownloads 复刻真身的调度：数出在跑的几个，只把队首的 QUEUED
    补到 config.download_parallel 为止。少了这一条，"一键开始会不会守上限"根本测不出来。
    """

    def __init__(self, task_list, cancellable = False):
        self._source = _StubSourceModel(task_list)
        self.toggled = []
        self.scheduled = 0
        # 取消默认是关着的：暂停 / 继续的用例走到这里就说明分派错了（见下面的 raise）。
        # 测「一键取消」时才打开
        self.allow_cancel = cancellable
        self.cancelled = []

    def sourceModel(self):
        return self._source

    def togglePauseResume(self, task_info):
        status = DownloadStatus(task_info.Download.status)

        self.toggled.append(status)

        if status == DownloadStatus.QUEUED:
            task_info.Download.status = DownloadStatus.DOWNLOADING

        elif status == DownloadStatus.DOWNLOADING:
            task_info.Download.status = DownloadStatus.PAUSED

            # 🔴 真身就是这样：暂停一条在跑的任务之后会立刻补位（见 model.py 里
            # togglePauseResume 的 DOWNLOADING 分支）。少了这一句，"一键暂停会不会
            # 一边停一边把排队的补上来"根本测不出来
            self.manageConcurrentDownloads()

        elif status == DownloadStatus.PAUSED:
            task_info.Download.status = DownloadStatus.DOWNLOADING

        else:
            raise AssertionError(f"不该对 {status} 调用 togglePauseResume")

    def onUpdateData(self, task_info):
        pass

    def cancelDownload(self, task_info):
        """
        复刻真身 cancelDownload 的分派（见 gui/component/download_list/model.py）

        合并 / 转换中直接返回、什么都不做 —— 半成品删了就白跑，产品就是这么定的；
        其余状态交给 task_manager.cancel_async，在界面上的结果是**那一行消失**。
        替身用"从列表里摘掉 + 记账"表示这两件事：断言既能看取消了几条，也能看
        列表里还剩谁。
        """
        if not self.allow_cancel:
            raise AssertionError("批量暂停 / 继续不该走到取消")

        if DownloadStatus(task_info.Download.status) in (
            DownloadStatus.MERGING, DownloadStatus.CONVERTING
        ):
            return

        self.cancelled.append(task_info.Basic.task_id)

        self._source._task_list.remove(task_info)

    def manageConcurrentDownloads(self):
        self.scheduled += 1

        limit = config.get(config.download_parallel)

        tasks = self._source._task_list

        running = sum(1 for t in tasks if DownloadStatus(t.Download.status) in RUNNING)

        for task_info in tasks:
            if running >= limit:
                break

            if DownloadStatus(task_info.Download.status) == DownloadStatus.QUEUED:
                self.togglePauseResume(task_info)

                if DownloadStatus(task_info.Download.status) in RUNNING:
                    running += 1


@pytest.fixture
def control_env(monkeypatch):
    env = {"downloaders": {}, "updated": [], "model": None, "limits": {
        config.download_parallel: 2, config.merge_parallel: 2,
    }}

    monkeypatch.setattr(downloader_manager, "downloaders", env["downloaders"])
    # 落库存的是**那一刻的状态快照**：记对象引用会被后续动作改写成同一个值，
    # 断言就永远看到最后的结果（第一版就是这样误判"没有落库"的）
    monkeypatch.setattr(
        task_manager, "update_async",
        lambda task_info: env["updated"].append(DownloadStatus(task_info.Download.status)),
    )
    monkeypatch.setattr(task_manager, "query_by_id", lambda task_id: None)
    monkeypatch.setattr(download_tools, "_get_downloading_model", lambda: env["model"])

    # 并发上限按设置的读法走 config.get(...)。用真对象做键，读的就是
    # _resume_all_tasks 里那一句，设置项换名字这里立刻会炸
    monkeypatch.setattr(config, "get", lambda item, default = None: env["limits"].get(item, default))

    return env


class _FakeDownloader:
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True


def listed(env, tasks, stale = (), cancellable = False):
    """
    把任务摆进"正在下载"列表

    🔴 默认给每条任务配一个下载器：**"内存里有下载器"才是"真在跑"**，
    少了它，_control_task 会把这些任务当成重启残留态先归一化成已暂停，
    动作自然就落错了分支（第一版就是这么红的）。stale 里列出的 task_id
    刻意不给下载器，用来复现"进程重启后库里还留着 downloading"那种情况。
    """
    for task in tasks:
        task_id = task.Basic.task_id

        if task_id not in stale:
            env["downloaders"][task_id] = _FakeDownloader()

    env["model"] = _StubModel(tasks, cancellable = cancellable)

    return env["model"]


def run_inline(func, *args, timeout = None, **kwargs):
    """call_in_main_thread 的替身：就地执行，只吃掉 timeout（真身拿它当等待预算）"""
    return func(*args, **kwargs)


def statuses(tasks):
    return [DownloadStatus(t.Download.status) for t in tasks]


def counts(result):
    """把返回体折成 (changed, skipped, total) —— 断言只关心这三个数时读起来更清楚"""
    assert isinstance(result, dict), result

    return result["changed"], result["skipped"], result["total"]


# ---------------------------------------------------------------------------
# 暂停
# ---------------------------------------------------------------------------

def test_pause_all_pauses_every_running_task(control_env):
    tasks = [make_task(f"t{index}", DownloadStatus.DOWNLOADING) for index in range(3)]
    model = listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("pause")) == (3, 0, 3)

    # 三个都真的走了一次切换，而不是只改状态
    assert model.toggled == [DownloadStatus.DOWNLOADING] * 3
    assert statuses(tasks) == [DownloadStatus.PAUSED] * 3


def test_pause_all_marks_queued_without_starting_them(control_env):
    """
    排队中的任务只落状态，**不调 togglePauseResume**

    那边对 QUEUED 的语义是"开始下载" —— 拿它暂停等于点了开始
    """
    queued = [make_task("q1", DownloadStatus.QUEUED), make_task("q2", DownloadStatus.QUEUED)]
    model = listed(control_env, queued)

    assert counts(download_tools._control_all_tasks("pause")) == (2, 0, 2)

    assert model.toggled == []
    assert statuses(queued) == [DownloadStatus.PAUSED] * 2
    assert len(control_env["updated"]) == 2


def test_pause_all_skips_tasks_that_are_already_paused(control_env):
    tasks = [
        make_task("p1", DownloadStatus.PAUSED),
        make_task("d1", DownloadStatus.DOWNLOADING),
        make_task("p2", DownloadStatus.PAUSED),
    ]
    listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("pause")) == (1, 2, 3)
    assert statuses(tasks) == [
        DownloadStatus.PAUSED, DownloadStatus.PAUSED, DownloadStatus.PAUSED
    ]


def test_pause_all_drains_the_queue_before_stopping_the_running_one(control_env):
    """
    一键暂停扫完之后，队列里不能还剩东西在跑

    🔴 扫描顺序反了就会漏：暂停一条 DOWNLOADING 会走进 model.togglePauseResume 里
    那句 manageConcurrentDownloads()，调度器立刻把队首那条 QUEUED 补上来；而那次
    补位发生在循环已经越过它之后，于是它一直下到底。2026-10-02 线上受控复现
    （上限 1、42 条队列）：暂停前在跑的是 0c1dfba1，队首排队的是 fd696962；
    扫完之后 0c1dfba1 停了，fd696962 却在下，15 秒后跑完进了合并。
    先把 QUEUED 全落成 PAUSED，调度器就没得补了。
    """
    control_env["limits"][config.download_parallel] = 1

    tasks = [
        make_task("d1", DownloadStatus.DOWNLOADING),
        make_task("q1", DownloadStatus.QUEUED),
        make_task("q2", DownloadStatus.QUEUED),
    ]
    model = listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("pause")) == (3, 0, 3)
    assert statuses(tasks) == [DownloadStatus.PAUSED] * 3

    # 真正的判据：调度器一次都不该把排队的补起来。只看最终状态是抓不到的 ——
    # 补上来那条在同一轮里也会被后面的循环暂停，"先起再停"看上去和没起一样
    assert model.toggled == [DownloadStatus.DOWNLOADING]


def test_pause_all_leaves_merging_tasks_running(control_env):
    """合并中不拦：它本来也没法"暂停"，拦下来只会让用户以为按钮坏了"""
    merging = make_task("m1", DownloadStatus.MERGING)
    listed(control_env, [merging])

    assert counts(download_tools._control_all_tasks("pause")) == (0, 1, 1)
    assert merging.Download.status == DownloadStatus.MERGING


def test_pause_all_with_a_queued_task_that_has_a_downloader(control_env):
    """已经起了下载器但还挂着 QUEUED 的：状态照落，同时把那个下载器停掉"""
    task = make_task("q1", DownloadStatus.QUEUED)
    listed(control_env, [task])

    downloader = control_env["downloaders"]["q1"]

    assert counts(download_tools._control_all_tasks("pause")) == (1, 0, 1)
    assert task.Download.status == DownloadStatus.PAUSED
    assert downloader.paused is True


# ---------------------------------------------------------------------------
# 取消 —— 「一键取消」（三颗按钮里唯一不可逆的那颗）
# ---------------------------------------------------------------------------

def test_cancel_all_cancels_every_unfinished_task(control_env):
    """不管在跑、排队还是已暂停，一键取消一视同仁地把它们摘掉"""
    tasks = [
        make_task("t1", DownloadStatus.DOWNLOADING),
        make_task("t2", DownloadStatus.QUEUED),
        make_task("t3", DownloadStatus.PAUSED),
        make_task("t4", DownloadStatus.FAILED),
    ]
    model = listed(control_env, tasks, cancellable = True)

    assert counts(download_tools._control_all_tasks("cancel")) == (4, 0, 4)
    assert sorted(model.cancelled) == ["t1", "t2", "t3", "t4"]
    # 摘掉之后列表里不该还留着它们 —— 判据不能只看返回值
    assert model._source.rowCount() == 0


def test_cancel_all_leaves_merging_tasks_alone(control_env):
    """
    合并 / 转换中的不动，且如实记成 skipped

    半成品删了就白跑，产品里一直是这样（cancelDownload 的 MERGING|CONVERTING
    分支直接 return）。批量这条不能"更狠" —— 否则用户点一次就把 45 分钟的白下了
    """
    downloading = make_task("t1", DownloadStatus.DOWNLOADING)
    merging = make_task("m1", DownloadStatus.MERGING)
    converting = make_task("c1", DownloadStatus.CONVERTING)
    listed(control_env, [downloading, merging, converting], cancellable = True)

    assert counts(download_tools._control_all_tasks("cancel")) == (1, 2, 3)


def test_cancel_all_ignores_the_download_parallel_limit(control_env):
    """
    取消不看并发上限：上限是"同时下几个"的约束，取消是往下减，没有上限可言

    这条是在守一个容易改错的地方 —— 别照着 _resume_all_tasks 的样子给 cancel
    也套一层"先排队再调度"，那会让取消变成"先起几条再取消"
    """
    tasks = [make_task(f"t{index}", DownloadStatus.DOWNLOADING) for index in range(5)]
    model = listed(control_env, tasks, cancellable = True)

    assert counts(download_tools._control_all_tasks("cancel")) == (5, 0, 5)
    assert model.scheduled == 0
    assert len(model.cancelled) == 5


def test_cancel_all_resumes_nothing(control_env):
    """取消完之后不许有任务被顺手拉起来（那等于"取消即重启"，最难查）"""
    tasks = [make_task("t1", DownloadStatus.DOWNLOADING), make_task("t2", DownloadStatus.QUEUED)]
    model = listed(control_env, tasks, cancellable = True)

    download_tools._control_all_tasks("cancel")

    assert model.toggled == []


# ---------------------------------------------------------------------------
# 继续 —— 「一键开始」必须守并发上限
# ---------------------------------------------------------------------------

def test_resume_all_queues_everything_and_lets_the_scheduler_start_them(control_env):
    """
    3 条暂停、上限 2：三条都从"已暂停"转成"排队"，但只有 2 条真跑起来

    这是 2026-10-02 用户报的那个 bug 的正面：一键开始曾经把 PAUSED 逐条
    downloader.resume()，也就是 48 条一起下，设置里的上限形同虚设
    """
    tasks = [make_task(f"p{index}", DownloadStatus.PAUSED) for index in range(3)]
    model = listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("resume")) == (3, 0, 3)

    assert statuses(tasks) == [
        DownloadStatus.DOWNLOADING, DownloadStatus.DOWNLOADING, DownloadStatus.QUEUED,
    ]

    # 起跑不是这里逐条点的，而是整批交给调度器一次
    assert model.scheduled == 1
    assert model.toggled == [DownloadStatus.QUEUED, DownloadStatus.QUEUED]


def test_resume_all_honours_the_configured_limit(control_env):
    """上限设成几，就只起几个 —— 换个数再验一遍，防止"写死成 2"蒙混过关"""
    control_env["limits"][config.download_parallel] = 1

    tasks = [make_task(f"p{index}", DownloadStatus.PAUSED) for index in range(4)]
    listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("resume")) == (4, 0, 4)
    assert statuses(tasks) == [
        DownloadStatus.DOWNLOADING,
        DownloadStatus.QUEUED, DownloadStatus.QUEUED, DownloadStatus.QUEUED,
    ]


def test_resume_all_leaves_already_running_tasks_alone(control_env):
    """
    一队里面已经有在跑的了：只补到上限为止，其余留在队列

    对着一队正在下的任务点「一键开始」不该把它们弄乱，也不该把上限撑破
    """
    tasks = [
        make_task("d1", DownloadStatus.DOWNLOADING),
        make_task("p1", DownloadStatus.PAUSED),
        make_task("p2", DownloadStatus.PAUSED),
        make_task("p3", DownloadStatus.PAUSED),
    ]
    model = listed(control_env, tasks)

    # changed 只算这次从暂停转成排队的；已在跑的那条记 skipped
    assert counts(download_tools._control_all_tasks("resume")) == (3, 1, 4)

    assert statuses(tasks) == [
        DownloadStatus.DOWNLOADING,     # 本来就在跑
        DownloadStatus.DOWNLOADING,     # 补进来的那一个
        DownloadStatus.QUEUED,
        DownloadStatus.QUEUED,
    ]
    assert model.toggled == [DownloadStatus.QUEUED]


def test_resume_all_revives_stale_tasks(control_env):
    """
    重启残留态（内存里没有下载器，库里还是 downloading）：先落成已暂停再排队

    不先归一化的话这两条既继续不了也排不进队 —— 用户点了没反应
    """
    stale = [make_task(f"s{index}", DownloadStatus.DOWNLOADING) for index in range(2)]
    model = listed(control_env, stale, stale = [t.Basic.task_id for t in stale])

    outcome = download_tools._control_all_tasks("resume")

    assert counts(outcome) == (2, 0, 2)

    # 落库时写的是已暂停（库与界面说同一件事），随后才被排进队
    assert control_env["updated"] == [DownloadStatus.PAUSED, DownloadStatus.PAUSED]
    assert model.toggled == [DownloadStatus.QUEUED, DownloadStatus.QUEUED]
    assert statuses(stale) == [DownloadStatus.DOWNLOADING] * 2


def test_resume_all_starts_paused_tasks_that_have_no_downloader(control_env):
    """
    界面显示已暂停、内存里也没有下载器的一条，仍然可以一键开始

    这是面板上最常见的样子：用户前一天点过暂停，今天回来说"全部继续"
    """
    paused = [make_task(f"p{index}", DownloadStatus.PAUSED) for index in range(2)]
    listed(control_env, paused, stale = [t.Basic.task_id for t in paused])

    assert counts(download_tools._control_all_tasks("resume")) == (2, 0, 2)
    assert statuses(paused) == [DownloadStatus.DOWNLOADING] * 2


def test_resume_all_does_not_retry_failed_tasks(control_env):
    """
    下载失败 / 合并失败的**不**被一键开始拉起来

    桌面端那颗「全部开始」确实会把它们转成排队（batchStart 里含 FAILED），
    面板这条刻意保持原语义：失败的任务要用户自己重试，批量按钮不该替人做决定
    """
    tasks = [
        make_task("f1", DownloadStatus.FAILED),
        make_task("f2", DownloadStatus.FFMPEG_FAILED),
        make_task("p1", DownloadStatus.PAUSED),
    ]
    listed(control_env, tasks)

    assert counts(download_tools._control_all_tasks("resume")) == (1, 2, 3)
    assert statuses(tasks) == [
        DownloadStatus.FAILED, DownloadStatus.FFMPEG_FAILED, DownloadStatus.DOWNLOADING,
    ]


def test_resume_all_reports_running_and_limit(control_env):
    """回执里要带上"真在跑几个 / 上限几个"，面板 toast 拿它说人话"""
    control_env["limits"][config.download_parallel] = 2

    tasks = [make_task(f"p{index}", DownloadStatus.PAUSED) for index in range(5)]
    listed(control_env, tasks)

    outcome = download_tools._control_all_tasks("resume")

    assert outcome["running"] == 2
    assert outcome["limit"] == 2
    assert outcome["changed"] == 5


def test_resume_all_with_an_empty_queue_still_reports_the_limit(control_env):
    listed(control_env, [])

    outcome = download_tools._control_all_tasks("resume")

    assert counts(outcome) == (0, 0, 0)
    assert outcome["running"] == 0
    assert outcome["limit"] == 2


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_empty_queue_reports_zero(control_env):
    listed(control_env, [])

    assert counts(download_tools._control_all_tasks("pause")) == (0, 0, 0)


def test_missing_interface_is_reported(control_env):
    control_env["model"] = None

    assert isinstance(download_tools._control_all_tasks("pause"), str)
    assert isinstance(download_tools._control_all_tasks("resume"), str)


# ---------------------------------------------------------------------------
# 工具外壳
# ---------------------------------------------------------------------------

def test_handler_returns_counts(control_env, monkeypatch):
    listed(control_env, [make_task("d1", DownloadStatus.DOWNLOADING),
                         make_task("p1", DownloadStatus.PAUSED)])

    monkeypatch.setattr(download_tools, "call_in_main_thread", run_inline)

    result = download_tools._make_control_all_handler("pause")({})

    assert result["isError"] is False
    assert result["structuredContent"] == {"action": "pause", "changed": 1, "skipped": 1, "total": 2}
    assert "1 of 2 task(s) paused" in result["content"][0]["text"]


def test_resume_handler_explains_the_queue(control_env, monkeypatch):
    """
    文案里不能只说"已开始 N 个"：排了 3 个而只跑 2 个，不解释上限就像设置失效
    """
    listed(control_env, [make_task(f"p{index}", DownloadStatus.PAUSED) for index in range(3)])

    monkeypatch.setattr(download_tools, "call_in_main_thread", run_inline)

    result = download_tools._make_control_all_handler("resume")({})

    assert result["isError"] is False
    assert result["structuredContent"]["running"] == 2
    assert result["structuredContent"]["limit"] == 2

    text = result["content"][0]["text"]

    assert "3 of 3 task(s) resumed" in text
    assert "at most 2 at a time" in text


def test_handler_says_so_when_the_queue_is_empty(control_env, monkeypatch):
    listed(control_env, [])

    monkeypatch.setattr(download_tools, "call_in_main_thread", run_inline)

    result = download_tools._make_control_all_handler("resume")({})

    assert result["structuredContent"]["total"] == 0
    assert "queue is empty" in result["content"][0]["text"]


def test_cancel_handler_says_what_happened(control_env, monkeypatch):
    """回执要能区分"取消了"和"还在合并、没动"，否则用户以为按钮失灵"""
    listed(
        control_env,
        [make_task("t1", DownloadStatus.PAUSED), make_task("m1", DownloadStatus.MERGING)],
        cancellable = True,
    )

    monkeypatch.setattr(download_tools, "call_in_main_thread", run_inline)

    result = download_tools._make_control_all_handler("cancel")({})

    assert result["isError"] is False

    text = result["content"][0]["text"]

    assert "1 of 2 task(s) cancelled" in text
    assert "1 needed no change" in text


def test_handler_reports_a_missing_interface_as_an_error(control_env, monkeypatch):
    control_env["model"] = None

    monkeypatch.setattr(download_tools, "call_in_main_thread", run_inline)

    result = download_tools._make_control_all_handler("pause")({})

    assert result["isError"] is True


def test_tools_are_registered_without_arguments():
    """三颗按钮都不带参数：整批就是"列表里全部"这个语义"""
    registered = {}

    class _Registry:
        def register(self, **kwargs):
            registered[kwargs["name"]] = kwargs

    download_tools.register(_Registry())

    assert "pause_all_tasks" in registered
    assert "resume_all_tasks" in registered
    # 「一键取消」也是同一套批量接口，面板三颗按钮一颗都不能少
    assert "cancel_all_tasks" in registered

    for name in ("pause_all_tasks", "resume_all_tasks", "cancel_all_tasks"):
        schema = registered[name]["input_schema"]

        assert schema["properties"] == {}
        assert "required" not in schema
