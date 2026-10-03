from ...common.config import config
from ...common.runtime import runtime

from ..invoke import call_in_main_thread

from . import text_result, error_result

import logging
import os

logger = logging.getLogger(__name__)

# 任务列表的返回条数上限。比解析列表（100/500）保守得多：解析结果是用户刚点开的
# 一份内容，条数有限；已完成的下载任务却是长期累积的，攒到几百条时全量返回会占掉
# 大量上下文，而调用方要看的几乎总是"在下的"和"最近的"
DEFAULT_TASK_LIMIT = 30
MAX_TASK_LIMIT = 200

def _clamp_limit(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_TASK_LIMIT

    return max(1, min(value, MAX_TASK_LIMIT))

def _clamp_offset(value) -> int:
    # 负数与非法值一律归 0：OFFSET 为负在 SQLite 里等于不偏移，与其让它
    # 静默退化成"第一页"，不如在这里就说清楚
    if not isinstance(value, int) or isinstance(value, bool):
        return 0

    return max(0, value)

def _list_sort_key(task_info):
    """
    未完成的排在前面，各自再按时间排 —— 截断时优先保留最相关的那些

    已完成的以完成时间为准，**倒序**（刚下完的在最上面）；未完成的以创建时间为准，
    **正序** —— 一次批量下载里，排在前面的那几集正是正在下的、以及离完成最近的
    （下载按创建顺序推进）。倒序会把这批最该看的压到列表最底下，也正是「已完成」
    表按 completed_time DESC 分页、本函数取的却是 created_time 会打架的地方，
    所以这里与 SQL 的 ORDER BY 取同一列同一方向。
    """
    completed_time = task_info.Basic.completed_time

    if completed_time:
        return (True, -completed_time)

    return (False, task_info.Basic.created_time)

def _output_file_name(task_info) -> str:
    """
    产物文件名（含扩展名），取不到时返回空串

    优先用 relative_files 里记录的实际文件名。合并完成时那份名字是
    safe_rename 的返回值，重名会带上 " (1)" 之类的后缀，只靠 File.name
    拼是拼不出来的；界面的"打开文件位置"同样以 relative_files[0] 为准。

    下载尚未结束时里面装的还是分片临时文件，此时退回按扩展名拼一个预期路径：
    合并过就用容器扩展名，否则用视频或音频自身的扩展名 —— 视频优先，
    因为不合并而保留两个原始文件时，界面也是把视频排在前面的。
    都取不到才给基名，否则给出的路径连扩展名都没有，模型照着用必然出错。
    """
    from ...common.enum import DownloadStatus

    if task_info.Download.status == DownloadStatus.COMPLETED and task_info.File.relative_files:
        return task_info.File.relative_files[0]

    if not task_info.File.name:
        return ""

    file_info = task_info.File

    if ext := (file_info.merge_file_ext or file_info.video_file_ext or file_info.audio_file_ext):
        return f"{file_info.name}.{ext}"

    return file_info.name

def _status_name(value: int) -> str:
    from ...common.enum import DownloadStatus

    try:
        return DownloadStatus(value).name.lower()

    except ValueError:
        return f"unknown({value})"

def _task_to_dict(task_info, verbose: bool = False) -> dict:
    data = {
        "task_id": task_info.Basic.task_id,
        "title": task_info.Basic.show_title,
        "status": _status_name(task_info.Download.status),
        "progress": task_info.Download.progress,
    }

    if task_info.Download.total_size:
        data["total_size"] = task_info.Download.total_size
        data["downloaded_size"] = task_info.Download.downloaded_size

    if task_info.Download.speed:
        data["speed"] = task_info.Download.speed

    if verbose:
        data["created_time"] = task_info.Basic.created_time
        data["completed_time"] = task_info.Basic.completed_time

        if file_name := _output_file_name(task_info):
            # 不建子文件夹时 folder 是 "."，直接 join 会拼出 "Downloads\.\xxx"；
            # download_path 又是正斜杠，join 后分隔符混用。normpath 一并收拾干净，
            # 免得把这种路径喂给模型后它再原样传回来
            data["file_path"] = os.path.normpath(os.path.join(
                task_info.File.download_path,
                task_info.File.folder,
                file_name,
            ))

        if task_info.Episode.video_quality:
            data["video_quality"] = task_info.Episode.video_quality

        if task_info.Episode.audio_quality:
            data["audio_quality"] = task_info.Episode.audio_quality

        # 实读值：下载完成后、改名交付之前由 Merger 用 FFmpeg 补上
        if task_info.Episode.video_resolution:
            data["video_resolution"] = task_info.Episode.video_resolution

        if task_info.Episode.video_dynamic_range:
            data["video_dynamic_range"] = task_info.Episode.video_dynamic_range

        if task_info.Episode.audio_codec:
            data["audio_codec"] = task_info.Episode.audio_codec

        if task_info.Episode.audio_channels:
            data["audio_channels"] = task_info.Episode.audio_channels

    return data

def _is_stale(task_info) -> bool:
    """
    状态写着「在跑」，内存里却没有下载器在推进它 —— 进程重启后残留的僵尸态

    下载器只活在内存里，容器重建 / 程序重启之后一个都不剩，而任务库里留下的还是
    原来的 queued / downloading / merging…。桌面端由 QueryWorker 统一改写成已暂停，
    面板读的是同一份任务库，必须做同一件事：否则界面上会出现一个进度永远不动、
    既暂停不了也继续不了的「下载中」。
    """
    from ...common.enum import ACTIVE_STATUSES
    from ...download.downloader.manager import downloader_manager

    return (
        downloader_manager.downloaders.get(task_info.Basic.task_id) is None
        and task_info.Download.status in ACTIVE_STATUSES
    )

def _live_task_map() -> dict:
    """
    界面「正在下载」列表里的 TaskInfo，按 task_id 索引

    🔴 未完成任务的**权威副本在界面列表里，不在数据库里**，两个理由：

      ① 排队等槽位的任务（QUEUED）在内存里本来就不该有下载器实例 —— 它只是
         在等前面那个下完。而"没有下载器 + 状态在 ACTIVE_STATUSES"恰好是
         _is_stale() 判断重启残留态的条件，于是一排队就被显示成"已暂停"：
         用户点了「一键开始」，界面上还是一片"已暂停"，只当他没生效
         （2026-10-02 实测：库里的 QUEUED 任务在面板上确实报 paused）。

      ② 正在下载的任务，进度是高频率写内存、异步落盘的，数据库那份必然滞后。

    界面还没建起来时返回空表 —— 那种时候（程序刚起、列表还没拉回来）只有数据库
    可看，调用方按老口径走，退回 _is_stale 那套判断。
    """
    try:
        from .download import _get_downloading_model

        model = _get_downloading_model()

    except Exception:
        # 工具模块之间的循环导入、界面对象还没装配 —— 都不该让"读列表"整个失败
        return {}

    if model is None:
        return {}

    try:
        from PySide6.QtCore import Qt

        # 视图上挂的是代理模型，任务对象在它的源模型里
        source = model.sourceModel() if hasattr(model, "sourceModel") else model

        tasks = {}

        for row in range(source.rowCount()):
            task_info = source.index(row, 0).data(Qt.ItemDataRole.UserRole)

            if task_info is not None:
                tasks[task_info.Basic.task_id] = task_info

        return tasks

    except Exception:
        return {}

def _query_tasks(completed: bool, limit: int = None, offset: int = 0):
    """
    读取任务列表

    正在下载的任务，其进度是高频写入、异步落盘的，数据库里的快照会滞后。
    因此**优先取界面列表里那一份**（见 _live_task_map），其次是内存里下载器持有的
    TaskInfo。两者都没有，说明它其实没在跑，状态要按"已暂停"如实回报。

    limit 会一路下推到 SQL 的 ORDER BY ... LIMIT，不是查回来再切：任务攒到
    几百条时，差别是"反序列化几百条 JSON"和"反序列化几十条"。offset 同理，
    它只在 limit 一起给出时才有意义（分页）。
    """
    from ...common.enum import DownloadStatus
    from ...download.downloader.manager import downloader_manager
    from ...download.task.manager import task_manager

    task_info_list = task_manager.query(completed, limit, offset)

    listed = {} if completed else _live_task_map()

    result = []

    for task_info in task_info_list:
        live = listed.get(task_info.Basic.task_id)

        if live is not None:
            result.append(live)

            continue

        if task_info.Basic.task_id in downloader_manager.downloaders:
            result.append(downloader_manager.downloaders[task_info.Basic.task_id].task_info)

            continue

        if _is_stale(task_info):
            # 只改这一份用于展示的副本，不写回任务库：真下起来的时候下载器会把
            # 状态写回去，这里只是别让界面说谎（与 QueryWorker 的做法一致）
            task_info.Download.status = DownloadStatus.PAUSED

        result.append(task_info)

    return result

def _find_task(task_id: str):
    from ...common.enum import DownloadStatus
    from ...download.downloader.manager import downloader_manager
    from ...download.task.manager import task_manager

    # 与 _query_tasks 同一条优先级：界面列表 > 内存下载器 > 数据库
    if live := _live_task_map().get(task_id):
        return live

    if downloader := downloader_manager.downloaders.get(task_id):
        return downloader.task_info

    # 走 task_id 上的 UNIQUE 约束直接命中，不再把两张表整个读出来逐条比对
    task_info = task_manager.query_by_id(task_id)

    if task_info is not None and _is_stale(task_info):
        task_info.Download.status = DownloadStatus.PAUSED

    return task_info

def tool_list_tasks(arguments: dict) -> dict:
    state = arguments.get("state", "downloading")

    if state not in ("downloading", "completed", "all"):
        return error_result("The 'state' argument must be one of: downloading, completed, all.")

    limit = _clamp_limit(arguments.get("limit"))
    offset = _clamp_offset(arguments.get("offset"))

    def collect():
        # 已完成的任务是历史累积，攒到几百条时全量返回会占掉可观的上下文，
        # 而调用方真正要看的几乎总是"在下的"和"最近的"。这里按此排序后截断，
        # 并把总数一并返回，让模型知道自己只看到了一部分。
        #
        # limit / offset 只下推给已完成表，未完成的一律全取。两者性质不同：
        #
        #   已完成表是历史累积，会长到几百上千条，而调用方只要最近几条；
        #   未完成表是"当前工作集"，且**不能按创建时间截断** —— 正在下载的任务
        #   往往是最早创建的那几个，按 created_time DESC 取前 N 条反而会把它们
        #   丢掉，模型就看不见正在下的东西了。它下完即移入已完成表，不会累积。
        #
        # offset 同理只对已完成表生效：未完成表本来就全量返回，"翻到第二页"
        # 对它没有意义，硬套上去只会让人以为漏了任务
        from ...download.task.manager import task_manager

        tasks = []
        total = 0

        if state in ("downloading", "all"):
            tasks += _query_tasks(False)
            total += task_manager.count(False)

        if state in ("completed", "all"):
            tasks += _query_tasks(True, limit, offset)
            total += task_manager.count(True)

        tasks.sort(key = _list_sort_key)

        return [_task_to_dict(t) for t in tasks[:limit]], total

    tasks, total = call_in_main_thread(collect, timeout = 20.0)

    if total > len(tasks):
        # 翻页时要说明白"这是从第几条开始的一段"，否则 200 条里取第 20 条起
        # 的 10 条会被读成"只有 10 条"
        window = f" from offset {offset}" if offset else ""

        summary = (
            f"{len(tasks)} of {total} task(s){window}; "
            "use a larger 'limit' or a different 'offset' to see more."
        )

    else:
        summary = f"{total} task(s)."

    return text_result(summary, {
        "total": total, "returned": len(tasks), "offset": offset, "tasks": tasks,
    })

def tool_get_task_status(arguments: dict) -> dict:
    task_id = (arguments.get("task_id") or "").strip()

    if not task_id:
        return error_result("The 'task_id' argument is required.")

    task_info = call_in_main_thread(_find_task, task_id, timeout = 20.0)

    if task_info is None:
        return error_result(f"No task found with id '{task_id}'. Use list_tasks to see current tasks.")

    return text_result("", _task_to_dict(task_info, verbose = True))

def tool_get_login_status(arguments: dict) -> dict:
    def collect():
        return {
            "logged_in": bool(config.get(config.is_login)),
            "username": runtime.auth.uname,
            "uid": runtime.auth.uid,
            "session_expired": bool(runtime.auth.is_expired),
        }

    info = call_in_main_thread(collect, timeout = 5.0)

    if not info["logged_in"]:
        summary = "Not logged in. Members-only content and high quality streams are unavailable."

    elif info["session_expired"]:
        summary = "The saved login session has expired; the user needs to sign in again."

    else:
        summary = f"Logged in as {info['username']}."

    return text_result(summary, info)

def register(registry):
    registry.register(
        name = "list_tasks",
        title = "List Download Tasks",
        description = (
            "List download tasks with their current status and progress. Covers every task "
            "in the application, including ones the user created. Unfinished tasks come "
            "first in creation order, then completed ones newest-first; the result is "
            "truncated to 'limit'. 'limit' and 'offset' page through the completed tasks; "
            "unfinished ones are always returned in full."
        ),
        input_schema = {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["downloading", "completed", "all"],
                    "description": "Which tasks to list. Defaults to 'downloading'.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum number of tasks to return (1-{MAX_TASK_LIMIT}, "
                        f"default {DEFAULT_TASK_LIMIT}). The response reports the true total."
                    ),
                    "minimum": 1,
                    "maximum": MAX_TASK_LIMIT,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "How many completed tasks to skip before returning 'limit' of them. "
                        "Use it to page through a long completed list; the response reports "
                        "the true total. Ignored for unfinished tasks."
                    ),
                    "minimum": 0,
                },
            },
            "additionalProperties": False,
        },
        handler = tool_list_tasks,
    )

    registry.register(
        name = "get_task_status",
        title = "Get Download Task Status",
        description = "Get detailed status for a single download task, including its output file path.",
        input_schema = {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id returned by list_tasks or create_download.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        handler = tool_get_task_status,
    )

    registry.register(
        name = "get_login_status",
        title = "Get Login Status",
        description = (
            "Check whether the user is signed in to Bilibili. Members-only content and the "
            "highest quality streams require a valid login."
        ),
        input_schema = {"type": "object", "additionalProperties": False},
        handler = tool_get_login_status,
    )
