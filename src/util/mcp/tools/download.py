from ...common.data import video_quality_map, audio_quality_map, video_codec_map
from ...common.enum import (
    DuplicateDownloadResolution, DanmakuType, SubtitleType, CoverType, MetadataType, VideoContainer
)
from ...common.config import config
from ...common.runtime import runtime
from ...download.task.options import pick_option
from ...common.signal_bus import signal_bus

from ..invoke import call_in_main_thread

from . import text_result, error_result
from .parse import get_parse_interface, build_item_index, _media_info_error

from threading import Event, Lock
import logging

logger = logging.getLogger(__name__)

MAX_EPISODES_PER_CALL = 200

def _fold(value: str) -> str:
    # 模型不会严格照抄枚举值的写法（hi_res / Hi-Res / HIRES 都会出现），
    # 归一化后再比对，避免因为一个下划线就报错
    return value.strip().upper().replace(" ", "").replace("-", "").replace("_", "")

def _build_lookup(table: dict, extra: dict = None) -> dict:
    lookup = {_fold(name): value for name, value in table.items()}

    if extra:
        lookup.update({_fold(name): value for name, value in extra.items()})

    return lookup

# 编解码器的正式名称带斜杠（"AVC/H.264"），模型多半只写其中一半，两种都认
_VIDEO_QUALITY_LOOKUP = _build_lookup(video_quality_map)
_AUDIO_QUALITY_LOOKUP = _build_lookup(audio_quality_map)
_VIDEO_CODEC_LOOKUP = _build_lookup(video_codec_map, {
    "AVC": 7, "H.264": 7,
    "HEVC": 12, "H.265": 12,
    "AV1": 13,
})

# 只下音频（听歌、做转录）是常见诉求，因此把媒体流的取舍也开放出来
_MEDIA_LOOKUP = {
    "video+audio": (True, True),
    "video": (True, False),
    "audio": (False, True),
}

# 模型可见的名字 → TaskManager 认识的键。这层映射同时也是白名单：
# 下载线程数、下载目录、命名规则等不在其中，模型无从触及
_FLAG_OPTIONS = {
    "danmaku": "download_danmaku",
    "subtitle": "download_subtitle",
    "cover": "download_cover",
    "metadata": "download_metadata",
    "chapter": "embed_chapter",

    "embed_danmaku": "embed_danmaku",
    "embed_subtitle": "embed_subtitle",
    "attach_cover": "attach_cover",
}

# 附加文件的格式与输出容器。值直接用枚举的 value，模型看到的就是文件扩展名
_ENUM_OPTIONS = {
    "danmaku_format": ("danmaku_type", DanmakuType),
    "subtitle_format": ("subtitle_type", SubtitleType),
    "cover_format": ("cover_type", CoverType),
    "metadata_format": ("metadata_type", MetadataType),
    "container": ("video_container", VideoContainer),
}

_OPTION_CHOICES = {
    "video_quality": list(video_quality_map),
    "audio_quality": list(audio_quality_map),
    "video_codec": list(video_codec_map),
}

def _normalize_options(raw):
    """
    把面向模型的选项翻译成 TaskManager 的 options

    返回 (options, 错误信息)。取值写错时直接报错而不是静默忽略 —— 模型以为
    自己下的是 4K，实际拿到默认画质，它没有任何办法发现这件事。
    """
    if raw is None:
        return None, None

    if not isinstance(raw, dict):
        return None, "The 'options' argument must be an object."

    options = {}

    for name, lookup, key in (
        ("video_quality", _VIDEO_QUALITY_LOOKUP, "video_quality_id"),
        ("audio_quality", _AUDIO_QUALITY_LOOKUP, "audio_quality_id"),
        ("video_codec", _VIDEO_CODEC_LOOKUP, "video_codec_id"),
    ):
        value = raw.get(name)

        if value is None:
            continue

        if not isinstance(value, str):
            return None, f"'{name}' must be a string."

        resolved = lookup.get(_fold(value))

        if resolved is None:
            return None, (
                f"'{value}' is not a valid {name}. Valid values are: "
                + ", ".join(_OPTION_CHOICES[name]) + "."
            )

        options[key] = resolved

    for name, key in _FLAG_OPTIONS.items():
        value = raw.get(name)

        if value is None:
            continue

        if not isinstance(value, bool):
            return None, f"'{name}' must be a boolean."

        options[key] = value

    media = raw.get("media")

    if media is not None:
        if not isinstance(media, str) or media.strip().lower() not in _MEDIA_LOOKUP:
            return None, (
                "'media' must be one of: " + ", ".join(_MEDIA_LOOKUP) + "."
            )

        video, audio = _MEDIA_LOOKUP[media.strip().lower()]

        options["download_video_stream"] = video
        options["download_audio_stream"] = audio

    for name, (key, enum_cls) in _ENUM_OPTIONS.items():
        value = raw.get(name)

        if value is None:
            continue

        if not isinstance(value, str):
            return None, f"'{name}' must be a string."

        lookup = {_fold(member.value): member for member in enum_cls}

        resolved = lookup.get(_fold(value))

        if resolved is None:
            return None, (
                f"'{value}' is not a valid {name}. Valid values are: "
                + ", ".join(member.value for member in enum_cls) + "."
            )

        options[key] = resolved

    languages = raw.get("subtitle_languages")

    if languages is not None:
        if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
            return None, "'subtitle_languages' must be an array of language code strings."

        # 空数组表示不作限制，与设置界面「下载全部语言」是同一个含义
        options["subtitle_language"] = {
            "download_specified": bool(languages),
            "specified_language": list(languages),
        }

    if error := _embed_conflict(options):
        return None, error

    return (options or None), None

def _embed_conflict(options: dict):
    """
    嵌入弹幕 / 字幕的前提是否成立，不成立时给出具体原因

    嵌入有三个前提，任何一个不满足，程序都只会静默跳过（见 danmaku.py 的
    _check_embed_danmaku 与 base.py 的 is_embed_available）：字幕轨必须是 ASS、
    输出容器必须是 MKV、而且得真的走一遍合并（只下音频时没有合并步骤）。

    不检查的话，模型开了嵌入开关就会以为嵌进去了，实际什么都没发生，
    它也没有任何途径能发现。这里提前拦下并说清缺的是哪一条。
    """
    for switch, format_key, ass_member, label in (
        ("embed_danmaku", "danmaku_type", DanmakuType.ASS, "danmaku"),
        ("embed_subtitle", "subtitle_type", SubtitleType.ASS, "subtitle"),
    ):
        if not options.get(switch):
            continue

        # 没指定的项按用户当前的设置算，本来就设成 ASS + MKV 时不该报错
        file_format = pick_option(options, format_key, config.get(getattr(config, format_key)))
        container = pick_option(options, "video_container", config.get(config.video_container))

        missing = []

        if file_format != ass_member:
            missing.append(f"{label}_format must be 'ass' (currently '{file_format.value}')")

        if container != VideoContainer.MKV:
            missing.append(f"container must be 'mkv' (currently '{container.value}')")

        # 只下音频时不存在合并步骤，没有容器可供嵌入
        if options.get("download_video_stream") is False:
            missing.append("media must include video")

        if missing:
            return (
                f"Cannot embed the {label}: " + "; ".join(missing) + ". "
                f"Either set those options too, or drop embed_{label} and the {label} "
                "will be saved as a separate file."
            )

    return None

def _collect_episode_info(episode_ids: list):
    """
    按 episode_id 从解析树取出条目数据

    返回 (待下载的 dict 列表, 未找到的 id 列表, 需要二次解析的 id 列表)
    """
    from ...parse.episode.tree import Attribute

    interface = get_parse_interface()

    if interface is None:
        return None, [], []

    # 必须与 parse 侧用同一套编号，否则模型拿到的 id 在这里对不上号
    index = build_item_index(interface.parse_list.get_all_items())

    found = []
    found_ids = []
    missing = []
    needs_reparse = []

    for episode_id in episode_ids:
        item = index.get(episode_id)

        if item is None:
            missing.append(episode_id)

            continue

        # 这类条目（收藏夹、个人空间里的视频）还没有 cid，直接建任务会失败
        if item.attribute & Attribute.NEED_PARSE_BIT:
            needs_reparse.append(episode_id)

            continue

        found.append(item.to_dict())
        found_ids.append(episode_id)

    return found, found_ids, missing, needs_reparse

def _split_duplicates(found: list, found_ids: list):
    """
    分出已经下载过的条目

    返回 (待下载的条目, 对应的 id, 重复条目的标题)。查库在当前线程完成：
    每个线程各持有自己的 SQLite 连接，且这里只读不写
    """
    from ...download.task.manager import task_manager

    fresh = []
    fresh_ids = []
    duplicates = []

    for episode, episode_id in zip(found, found_ids):
        try:
            if task_manager.is_duplicate(episode):
                duplicates.append(episode.get("title", ""))

                continue

        except Exception:
            # 查库失败时按未重复处理，后面 TaskManager 还会再判一次，
            # 顶多是多走一遍流程，总好过把能下的条目挡在门外
            logger.exception("预检重复下载失败：%s", episode.get("title", ""))

        fresh.append(episode)
        fresh_ids.append(episode_id)

    return fresh, fresh_ids, duplicates

# 创建任务同样要互斥：中途会改 runtime.naming.current_starting_number 这个全局编号，
# 并且依赖"解析列表此刻的内容"，两个请求交叠会算错序号、取错条目
_create_lock = Lock()

def tool_create_download(arguments: dict) -> dict:
    if not _create_lock.acquire(timeout = 5.0):
        return error_result("Another download request is being processed. Retry shortly.")

    try:
        return _create_download_locked(arguments)

    finally:
        _create_lock.release()

def _create_download_locked(arguments: dict) -> dict:
    episode_ids = arguments.get("episode_ids")

    if not isinstance(episode_ids, list) or not episode_ids:
        return error_result("The 'episode_ids' argument must be a non-empty array of episode_id strings.")

    if not all(isinstance(item, str) for item in episode_ids):
        return error_result("Every entry in 'episode_ids' must be a string.")

    if len(episode_ids) > MAX_EPISODES_PER_CALL:
        return error_result(
            f"Too many episodes in one call ({len(episode_ids)}); the limit is {MAX_EPISODES_PER_CALL}."
        )

    options, option_error = _normalize_options(arguments.get("options"))

    if option_error:
        return error_result(option_error)

    redownload = arguments.get("redownload")

    if redownload is not None and not isinstance(redownload, bool):
        return error_result("The 'redownload' argument must be a boolean.")

    # 先校验 id 再看媒体信息：传错 id 却收到"媒体信息不可用"会把模型引向
    # 完全无关的方向，它会去重新解析而不是纠正 id
    found, found_ids, missing, needs_reparse = call_in_main_thread(
        _collect_episode_info, episode_ids, timeout = 15.0
    )

    if found is None:
        return error_result("The parse list is unavailable. Call parse_url first.")

    if not found:
        detail = []

        if missing:
            detail.append(f"{len(missing)} id(s) were not found in the parse list")

        if needs_reparse:
            detail.append(f"{len(needs_reparse)} id(s) need to be parsed individually first")

        return error_result(
            "No downloadable episodes matched. " + ("; ".join(detail) + "." if detail else
            "Call get_episodes to see the available episode_id values.")
        )

    if reason := call_in_main_thread(_media_info_error, timeout = 5.0):
        return error_result(
            f"Cannot start a download: {reason}. Try parsing the link again; if it keeps "
            "failing, the content may require a login or be region-restricted."
        )

    # 重复下载必须在这里就地决定，不能交给 TaskManager 按用户设置处理：
    # 那边的 ALWAYS_ASK 会弹窗并无限等待用户点击，而这条链路上没有人在看着。
    # 显式指定后，即便预检与实际创建之间又有任务入库，也不会弹窗
    options = dict(options or {})
    options["duplicate_resolution"] = (
        DuplicateDownloadResolution.CONTINUE if redownload else DuplicateDownloadResolution.SKIP
    )

    duplicates = []

    if not redownload:
        # 自己先查一遍，而不是让 TaskManager 静默跳过：被它跳过的条目不会出现在
        # add_to_downloading_list 里，全部重复时这里只能干等到 60s 超时，
        # 且无从告诉模型是哪几条重复了
        found, found_ids, duplicates = _split_duplicates(found, found_ids)

        if not found:
            return text_result(
                f"Nothing to download: all {len(duplicates)} episode(s) have already been "
                "downloaded. Duplicates are matched by video id alone, so quality and format "
                "are not taken into account. Pass redownload=true to download them again.",
                {"created": 0, "duplicates": duplicates},
            )

    created = {}
    done = Event()

    def on_added(task_info_list):
        created["tasks"] = [
            {"task_id": t.Basic.task_id, "title": t.Basic.show_title}
            for t in task_info_list
        ]
        done.set()

    def start():
        from PySide6.QtCore import QTimer

        # 界面上的"已下载"角标。放在这里而不是校验阶段：校验之后仍可能因为
        # 媒体信息缺失而不创建任何任务，那时标记就是假的。
        #
        # 按位置 id 精确标记，不能拿 item.episode_id 去比对：同一视频的分P
        # 共享那个值，只下了第 3P 也会把 10 个分P 全标成已下载
        if interface := get_parse_interface():
            index = build_item_index(interface.parse_list.get_all_items())

            for item_id in found_ids:
                if item := index.get(item_id):
                    item.downloaded = True

        # 起始编号跟着界面的下载入口走，否则文件名里的序号会从上次的位置续下去
        runtime.naming.current_starting_number = 1

        signal_bus.download.create_task.emit(found, True, options)

        # 与界面的下载入口保持一致：上面改了 item.downloaded，要发一次刷新，
        # 否则"已下载"角标要等用户下次操作才重绘。只发重绘信号，不动勾选数据
        if interface is not None:
            QTimer.singleShot(0, interface.parse_list.update_check_state)

    call_in_main_thread(signal_bus.download.add_to_downloading_list.connect, on_added, timeout = 5.0)

    try:
        call_in_main_thread(start, timeout = 10.0)

        # 任务创建跑在线程池上，还可能被重复下载确认对话框挡住，因此给足时间
        finished = done.wait(60.0)

    finally:
        try:
            call_in_main_thread(
                signal_bus.download.add_to_downloading_list.disconnect, on_added, timeout = 5.0
            )

        except Exception:
            logger.exception("断开下载任务创建信号失败")

    notes = []

    if missing:
        notes.append(f"{len(missing)} id(s) were not found: {', '.join(missing[:5])}")

    if needs_reparse:
        notes.append(f"{len(needs_reparse)} id(s) need to be parsed individually before downloading")

    if duplicates:
        notes.append(
            f"{len(duplicates)} episode(s) were skipped as already downloaded "
            "(pass redownload=true to download them again)"
        )

    if not finished:
        # 全部被判为重复下载时不会有任务入队，信号也就不会发出
        return text_result(
            "Submitted, but no new task was confirmed within 60s. The episodes may already have "
            "been downloaded, or a confirmation dialog may be waiting for the user. "
            "Use list_tasks to check.",
            {"submitted": len(found), "notes": notes},
        )

    tasks = created.get("tasks", [])

    summary = f"Created {len(tasks)} download task(s)."

    if notes:
        summary += " " + "; ".join(notes) + "."

    structured = {"created": len(tasks), "tasks": tasks, "notes": notes}

    if duplicates:
        structured["duplicates"] = duplicates

    return text_result(summary, structured)

def _get_downloading_model():
    """
    取下载界面「正在下载」列表的 model

    控制任务一律经它转发，而不是直接去动 Downloader：界面的
    togglePauseResume / cancelDownload 里还带着并发调度、界面行刷新、
    正在下载时先停分片线程再删文件等一串副作用，绕过去就会出现
    "任务停了但排队的任务不顶上""取消后临时文件残留" 这类问题
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    window = getattr(app, "window", None)

    if window is None:
        return None

    interface = getattr(window, "download_interface", None)

    if interface is None:
        return None

    return interface.downloading_list_view._model

def _find_listed_task(model, task_id: str):
    """
    从界面「正在下载」列表里按 task_id 取回那个 TaskInfo

    必须用列表里的那一份，不能拿数据库重新反序列化一份：暂停 / 继续是按对象就地
    改状态并刷新那一行的，操作另一个副本时列表不会有任何变化（两份对象互不相干）
    """
    from PySide6.QtCore import Qt

    # 视图上挂的是代理模型，任务对象在它的源模型里
    source = model.sourceModel() if hasattr(model, "sourceModel") else model

    for row in range(source.rowCount()):
        task_info = source.index(row, 0).data(Qt.ItemDataRole.UserRole)

        if task_info is not None and task_info.Basic.task_id == task_id:
            return task_info

    return None

def _control_task(task_id: str, action: str):
    """
    对单个任务执行暂停 / 继续 / 取消

    必须在 GUI 线程调用：Downloader 持有 QTimer 与线程池对象

    一律经界面 model 转发（与右键菜单、顶部按钮同一条路）：togglePauseResume /
    cancelDownload 里还带着并发调度、界面行刷新、正在下载时先停分片线程再删文件
    等一串副作用，绕过去就会出现"任务停了但排队的任务不顶上""取消后临时文件残留"。

    🔴 不能再用 downloaders.get(task_id) 先卡一道：它只认"内存里已经有下载器"的任务，
    于是重启后残留的任务、处于暂停的任务、还没被调度起来的排队任务统统被判成
    "No active task" —— 用户点了暂停/继续却什么都没发生，正是这么来的。
    界面的 togglePauseResume 本来就会懒创建 Downloader，走它即可。
    """
    from ...common.enum import RUNNING_STATUSES, DownloadStatus
    from ...download.downloader.manager import downloader_manager
    from ...download.task.manager import task_manager

    model = _get_downloading_model()

    if model is None:
        return "The download interface is not ready yet."

    task_info = _find_listed_task(model, task_id)

    if task_info is None:
        # 不在未完成列表里：可能已经完成（控制不了），也可能确实不存在
        known = task_manager.query_by_id(task_id)

        if known is None:
            return f"No task with id '{task_id}'."

        if known.Download.status == DownloadStatus.COMPLETED:
            return f"Task '{task_id}' has already completed and cannot be controlled."

        return f"Task '{task_id}' is not in the download list."

    if (downloader_manager.downloaders.get(task_id) is None
            and task_info.Download.status in RUNNING_STATUSES):
        # 进程重启后残留的「进行中」状态：内存里没有下载器在推进它。三个动作都是
        # 按状态分派的，不先落到 PAUSED 就会走错分支 —— pause 会去暂停一个并不存在
        # 的下载，resume 落不进"从已暂停继续"那条，cancel 更会挂在"等它自己停下来"上
        # 永远不返回。写回任务库，让数据库与界面说的是同一件事。
        #
        # 🔴 判据用的是 RUNNING_STATUSES（不含 QUEUED）：界面列表里那条 QUEUED 是真的
        # 在排队等槽位，它本来就没有下载器，归一化成 PAUSED 只会让"暂停"这条命令
        # 先把它改成已暂停、再回一句"它没在下载"。
        task_info.Download.status = DownloadStatus.PAUSED

        task_manager.update_async(task_info)

    status = task_info.Download.status
    current = DownloadStatus(status).name.lower()

    match action:
        case "pause":
            if status == DownloadStatus.QUEUED:
                # 还没开始下载，直接落成已暂停（与顶部「全部暂停」同一条路）。
                # togglePauseResume 对 QUEUED 是"启动下载"，不能拿来暂停
                task_info.Download.status = DownloadStatus.PAUSED

                if downloader := downloader_manager.get(task_info, create_if_not_exists = False):
                    downloader.pause()

                model.onUpdateData(task_info)

                task_manager.update_async(task_info)

            elif status == DownloadStatus.DOWNLOADING:
                model.togglePauseResume(task_info)

            else:
                return (
                    f"Task is not downloading (current status: {current}); there is nothing to pause. "
                    "Use resume to start a paused task."
                )

        case "resume":
            if status != DownloadStatus.PAUSED:
                return f"Task is not paused (current status: {current}); there is nothing to resume."

            model.togglePauseResume(task_info)

        case "cancel":
            # 合并 / 转换中的任务界面上不允许取消，这里保持一致；
            # 其余状态交给 cancelDownload，由它决定是否要先等分片线程停下
            if status in (DownloadStatus.MERGING, DownloadStatus.CONVERTING):
                return "Task is being merged or converted and cannot be cancelled. Wait for it to finish."

            model.cancelDownload(task_info)

    return None

# 回执文案里的过去式。不要退回成 f"{action}d" 那种拼接：
# 那样 cancel 会变成 "canceld"，拼错的英文会削弱模型对结果的理解
_ACTION_DONE = {
    "pause": "paused",
    "resume": "resumed",
    "cancel": "cancelled",
}

def _resume_all_tasks(model, tasks):
    """
    「一键开始」：整批交回并发调度器，而不是逐条立刻开跑

    🔴 这条路不能照搬 _control_task 的 resume 分支。那一支对 PAUSED 是直接
    downloader.resume()，语义是"你把暂停的都点一遍，它们就立刻全跑起来"——
    设置里的「同时下载任务数」根本不参与其中（2026-10-02 用户报的正是这个：
    上限写着 2，一键开始却 48 条一起下）。
    正确做法与界面顶部那颗「全部开始」同一条：整批落成 QUEUED，再由
    model.manageConcurrentDownloads() 按 config.download_parallel 放行，
    下完一个补一个。所以这里只改状态，启动交给调度器。

    残留态归一化仍与 _control_task 保持一致，否则容器重建后那批「下载中」既
    继续不了也排不进队。
    """
    from ...common.enum import RUNNING_STATUSES, DownloadStatus
    from ...download.downloader.manager import downloader_manager
    from ...download.task.manager import task_manager

    queued = 0
    skipped = 0

    for task_info in tasks:
        # 内存里没有下载器在推进，状态却写着「在跑」—— 先落 PAUSED 才谈得上继续。
        # 判据不含 QUEUED：那一条是真在排队，本来就该原样留着（见 RUNNING_STATUSES）
        if (downloader_manager.downloaders.get(task_info.Basic.task_id) is None
                and task_info.Download.status in RUNNING_STATUSES):
            task_info.Download.status = DownloadStatus.PAUSED

            task_manager.update_async(task_info)

        if task_info.Download.status != DownloadStatus.PAUSED:
            # 已在跑、已排队的都不必再动；合并中、已失败的也不该被这条拉起来
            skipped += 1

            continue

        task_info.Download.status = DownloadStatus.QUEUED

        model.onUpdateData(task_info)

        queued += 1

    # 真正放行的那一步：它自己数当前几个在跑，只补到上限为止，其余留在 QUEUED，
    # 由下载完成时发出的 auto_manage_concurrent_downloads 信号再调度下一轮
    model.manageConcurrentDownloads()

    running = sum(
        1 for task_info in tasks
        if task_info.Download.status in (DownloadStatus.PARSING, DownloadStatus.DOWNLOADING)
    )

    return {
        "changed": queued,
        "skipped": skipped,
        "total": len(tasks),
        # 队列排了多长、真正在跑几个、上限几个 —— 不报这三个数，用户看到
        # "已开始 48 个"却只有 2 个在动，会以为并发设置根本没生效
        "running": running,
        "limit": config.get(config.download_parallel),
    }

def _control_all_tasks(action: str):
    """
    对未完成列表里的**每一个**任务做同一个动作（面板「一键暂停 / 一键开始」走这里）

    暂停 / 取消逐条复用 _control_task：那里有重启残留态的归一化、有 QUEUED 只落状态
    不启动的特例、有"合并中不许取消"的拒绝 —— 复制一份出来迟早会与单任务版本分叉，
    而分叉出来的差异只会在用户点批量时才暴露。

    ★ 继续（resume）是唯一的例外，走 _resume_all_tasks：单任务那条路是"立刻起跑"，
    整批套上去等于把并发上限整个绕开。理由与做法见那个函数。

    列表先快照成对象再逐个操作：每次控制都会刷新那一行，但行数不变；万一某个任务
    在过程中完成或被别处取消，_control_task 会返回原因，这里记成 skipped 而不是
    中断整批。
    """
    from PySide6.QtCore import Qt

    model = _get_downloading_model()

    if model is None:
        return "The download interface is not ready yet."

    source = model.sourceModel() if hasattr(model, "sourceModel") else model
    tasks = []

    for row in range(source.rowCount()):
        task_info = source.index(row, 0).data(Qt.ItemDataRole.UserRole)

        if task_info is not None:
            tasks.append(task_info)

    if action == "resume":
        return _resume_all_tasks(model, tasks)

    if action == "pause":
        # 🔴 扫描顺序有讲究：先把「还没开跑、在等槽位」的那批落成已暂停，再去暂停
        # 真正在跑的。反过来做就会漏：每一次暂停 DOWNLOADING 都会走进
        # model.togglePauseResume 里那句 manageConcurrentDownloads()，调度器立刻
        # 从队列里补一个上来；等循环按顺序走到那条时它已经是 DOWNLOADING，暂停它
        # 又补下一个 —— 一轮扫完，队列里恰好还剩「上限」那么多条在跑。
        # 2026-10-02 线上实测：上限 1 时一键暂停后仍有 1 条在下载，上限 3 时剩 3 条，
        # 用户看到的就是"点了全部暂停还有东西在下"。先把 QUEUED 清空，调度器就没得补。
        # （QUEUED 那条只是改状态 + 刷行，不触发调度，见 _control_task 的 pause 分支）
        from ...common.enum import DownloadStatus

        tasks.sort(key = lambda item: 0 if item.Download.status == DownloadStatus.QUEUED else 1)

    changed = 0
    skipped = 0

    for task_info in tasks:
        if _control_task(task_info.Basic.task_id, action) is None:
            changed += 1
        else:
            skipped += 1

    return {"changed": changed, "skipped": skipped, "total": len(tasks)}

def _make_control_all_handler(action: str):
    def handler(arguments: dict) -> dict:
        # 批量要一条条过界面 model，30 多条时远比单任务慢：给足超时，别让它半路被掐断
        outcome = call_in_main_thread(_control_all_tasks, action, timeout = 120.0)

        if isinstance(outcome, str):
            return error_result(outcome)

        total = outcome["total"]

        if not total:
            return text_result(
                "There is no unfinished task to control; the download queue is empty.",
                {"action": action, "changed": 0, "skipped": 0, "total": 0},
            )

        summary = f"{outcome['changed']} of {total} task(s) {_ACTION_DONE[action]}"

        if action == "resume":
            summary += (
                f"; {outcome['running']} downloading now, at most {outcome['limit']} "
                "at a time, the rest wait in the queue"
            )

        if outcome["skipped"]:
            summary += f"; {outcome['skipped']} needed no change."

        else:
            summary += "."

        return text_result(summary, {"action": action, **outcome})

    return handler

def _make_control_handler(action: str):
    def handler(arguments: dict) -> dict:
        task_id = (arguments.get("task_id") or "").strip()

        if not task_id:
            return error_result("The 'task_id' argument is required.")

        if reason := call_in_main_thread(_control_task, task_id, action, timeout = 20.0):
            return error_result(reason)

        return text_result(
            f"Task {task_id} {_ACTION_DONE[action]}.", {"task_id": task_id, "action": action}
        )

    return handler

_TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task_id returned by list_tasks or create_download.",
        },
    },
    "required": ["task_id"],
    "additionalProperties": False,
}

def register(registry):
    registry.register(
        name = "create_download",
        title = "Create Download Tasks",
        description = (
            "Create download tasks for episodes currently in the parse list. Call parse_url "
            "first, then pass the episode_id values you want. Anything left out of 'options' "
            "follows the user's own settings, and 'options' applies only to the tasks created "
            "by this call. The output folder and file naming are always the user's and cannot "
            "be changed here. Episodes that were already downloaded are skipped unless "
            "redownload is set."
        ),
        input_schema = {
            "type": "object",
            "properties": {
                "episode_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "episode_id values from parse_url or get_episodes.",
                    "minItems": 1,
                    "maxItems": MAX_EPISODES_PER_CALL,
                },
                "options": {
                    "type": "object",
                    "description": (
                        "Per-task download settings. Omitted fields follow the user's settings. "
                        "Check the 'available' field from parse_url first: requesting a quality "
                        "the content does not offer silently falls back to the closest available "
                        "one, so the download will not match what was asked for."
                    ),
                    "properties": {
                        "video_quality": {
                            "type": "string",
                            "enum": _OPTION_CHOICES["video_quality"],
                            "description": "Video quality; 'auto' follows the user's priority list.",
                        },
                        "audio_quality": {
                            "type": "string",
                            "enum": _OPTION_CHOICES["audio_quality"],
                            "description": "Audio quality; 'auto' follows the user's priority list.",
                        },
                        "video_codec": {
                            "type": "string",
                            "enum": _OPTION_CHOICES["video_codec"],
                            "description": "Video codec; 'auto' follows the user's priority list.",
                        },
                        "media": {
                            "type": "string",
                            "enum": list(_MEDIA_LOOKUP),
                            "description": "Which streams to download. Use 'audio' for audio-only.",
                        },
                        "container": {
                            "type": "string",
                            "enum": [member.value for member in VideoContainer],
                            "description": "Output container for the merged file.",
                        },
                        "danmaku": {"type": "boolean", "description": "Download the danmaku (bullet comments) file."},
                        "danmaku_format": {
                            "type": "string",
                            "enum": [member.value for member in DanmakuType],
                            "description": "Format of the danmaku file.",
                        },
                        "embed_danmaku": {
                            "type": "boolean",
                            "description": (
                                "Embed the danmaku into the video as a subtitle track. Requires "
                                "danmaku_format 'ass' and container 'mkv'."
                            ),
                        },
                        "subtitle": {"type": "boolean", "description": "Download the subtitle file."},
                        "subtitle_format": {
                            "type": "string",
                            "enum": [member.value for member in SubtitleType],
                            "description": "Format of the subtitle file.",
                        },
                        "subtitle_languages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Only download subtitles in these languages, using Bilibili's own "
                                "codes (zh-CN, zh-Hant, en-US, ai-zh ...). An empty array downloads "
                                "every available language. Codes the video does not provide simply "
                                "yield no subtitle file."
                            ),
                        },
                        "embed_subtitle": {
                            "type": "boolean",
                            "description": (
                                "Embed the subtitle into the video as a subtitle track. Requires "
                                "subtitle_format 'ass' and container 'mkv'."
                            ),
                        },
                        "cover": {"type": "boolean", "description": "Download the cover image."},
                        "cover_format": {
                            "type": "string",
                            "enum": [member.value for member in CoverType],
                            "description": "Format of the cover image file.",
                        },
                        "attach_cover": {
                            "type": "boolean",
                            "description": "Attach the cover to the output file as embedded artwork.",
                        },
                        "metadata": {"type": "boolean", "description": "Write a metadata file."},
                        "metadata_format": {
                            "type": "string",
                            "enum": [member.value for member in MetadataType],
                            "description": "Format of the metadata file.",
                        },
                        "chapter": {"type": "boolean", "description": "Embed chapter markers into the output file."},
                    },
                    "additionalProperties": False,
                },
                "redownload": {
                    "type": "boolean",
                    "description": (
                        "Download episodes even if they were downloaded before. Duplicates are "
                        "matched by video id alone, so an episode already downloaded at a "
                        "different quality still counts as a duplicate; set this to download it "
                        "again at another quality."
                    ),
                },
            },
            "required": ["episode_ids"],
            "additionalProperties": False,
        },
        handler = tool_create_download,
    )

    for action, title, description in (
        ("pause", "Pause Download", "Pause a download task that is currently downloading."),
        ("resume", "Resume Download", "Resume a paused download task."),
        ("cancel", "Cancel Download", "Cancel a download task and remove it from the queue. Partial files are deleted. A task that is already merging or converting cannot be cancelled."),
    ):
        registry.register(
            name = f"{action}_task",
            title = title,
            description = description,
            input_schema = _TASK_ID_SCHEMA,
            handler = _make_control_handler(action),
        )

    # 批量版：面板「下载中」页右上角那两颗按钮走这里。单任务接口要 30 多次调用，
    # 每次都要过一遍界面 model 与任务库，批量合成一次调用
    for action, title, description in (
        (
            "pause",
            "Pause All Downloads",
            "Pause every unfinished download task at once. Queued tasks are marked paused "
            "without being started; tasks that are already paused are left alone. Tasks that "
            "are merging or converting keep going until they finish.",
        ),
        (
            "resume",
            "Resume All Downloads",
            "Resume every paused download task at once. Tasks that are not paused are left "
            "alone, so calling this on a running queue changes nothing.",
        ),
        (
            "cancel",
            "Cancel All Downloads",
            "Cancel every unfinished download task at once and drop them from the queue. "
            "Partial files of the cancelled tasks are deleted, so this cannot be undone; "
            "files that finished downloading are untouched. Tasks that are merging or "
            "converting are left alone until they finish.",
        ),
    ):
        registry.register(
            name = f"{action}_all_tasks",
            title = title,
            description = description,
            input_schema = {"type": "object", "properties": {}, "additionalProperties": False},
            handler = _make_control_all_handler(action),
        )
