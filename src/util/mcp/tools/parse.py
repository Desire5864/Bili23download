from ...common.data import url_patterns

from ..invoke import call_in_main_thread

from . import text_result, error_result

from threading import Event, Lock
import logging

logger = logging.getLogger(__name__)

# 解析结果可能有上千项（合集、个人空间），一次全塞给模型既超长又没用
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

def get_parse_interface():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    window = getattr(app, "window", None)

    if window is None:
        return None

    return getattr(window, "parse_interface", None)

def build_item_index(items) -> dict:
    """
    条目在解析列表中的位置就是它对外的 id

    不能拿 TreeItem.episode_id 当键：那是 EpisodeData 里**视频级**元数据的缓存键，
    同一个视频的所有分P 共享同一个值（分P 之间只有 cid 不同），多集番剧同理。
    用它建索引，10 个分P 会在字典里互相覆盖只剩最后一条 —— 模型想下第 3P，
    实际拿到第 10P，而且全程没有任何报错。

    位置是纯局部标识，只在本次解析结果内有效；解析新链接后列表整体替换，
    编号自然跟着换，与"id 指向当前列表"的语义一致。
    """
    return {str(position): item for position, item in enumerate(items, 1)}

def _episode_to_dict(item_id: str, item, is_link_target: bool = False) -> dict:
    from ...parse.episode.tree import Attribute

    data = {
        "episode_id": item_id,
        "title": item.title,
        "number": item.number,
        "duration": item.duration,
    }

    # 解析的链接精确指向的就是这一条。界面上它会被自动滚动到、自动勾选，
    # 模型手上却只有一份看起来齐平的列表：一个视频的所有分P 共享同一个 bvid，
    # 番剧一整季的条目在导出字段上也毫无差别，不点出来就无从分辨
    if is_link_target:
        data["is_link_target"] = True

    if item.badge:
        data["badge"] = item.badge

    if item.bvid:
        data["bvid"] = item.bvid

    # 需要二次解析的条目（个人空间、收藏夹里的视频）不能直接下载，
    # 必须让模型看见，否则它会拿着这些 id 去创建任务然后困惑于失败
    if item.attribute & Attribute.NEED_PARSE_BIT:
        data["needs_reparse"] = True

    if item.downloaded:
        data["already_downloaded"] = True

    return data

def _media_info_error():
    """媒体信息是否可用，不可用时给出原因（供 create_download 复用同一判断）"""
    from ...parse.preview.info import PreviewerInfo

    if PreviewerInfo.error_occurred:
        return PreviewerInfo.error_message or "media information was not retrieved"

    return None

def _media_info_summary():
    """
    本次解析的内容实际提供哪些清晰度、音质与编码，以及这些信息取自哪个视频

    模型要在 create_download 里指定画质，但它无从知道这个视频有没有 4K。
    不告诉它的话，它只能凭标题猜，猜错了会被静默降级到最接近的档位 ——
    它以为下到了 4K，实际是 1080P，而且没有任何迹象可循。

    首选项是充电专属、付费等取不到媒体信息的视频时，Previewer 会自动换用备选，
    于是这些档位其实属于列表里的**另一个**视频。界面把这件事写在下载选项对话框上，
    模型同样需要知道，否则它会把别人的 4K 当成自己要下的那一集的。

    这几个 choice_data 由 Previewer 填充，解析开始时会被重置成空，
    因此只有在媒体信息就绪后取到的值才有意义。一次取齐是为了让选项与来源
    出自同一个快照：分两次回主线程取，中间用户切换剧集就会把两者对不上。
    """
    from ...parse.preview.info import PreviewerInfo

    options = {}

    for key, data in (
        ("video_quality", PreviewerInfo.video_quality_choice_data),
        ("audio_quality", PreviewerInfo.audio_quality_choice_data),
        ("video_codec", PreviewerInfo.video_codec_choice_data),
    ):
        # 重置时置成的是空列表，解析后才是 {名称: id} 的字典，两种都能取键
        if names := list(data):
            options[key] = names

    source = {}

    if PreviewerInfo.episode_title:
        source["title"] = PreviewerInfo.episode_title

        if PreviewerInfo.episode_number:
            source["number"] = PreviewerInfo.episode_number

        if PreviewerInfo.from_fallback:
            source["from_fallback"] = True

    return options, source

def _season_data(extra) -> dict:
    """
    把解析器交代的"这个链接属于哪个系列、系列里还有哪几部"拣出来

    只有番剧会带这一段（`BangumiParser.get_extra_data` 里的 `season_data`：
    整个系列的所有季、系列名、当前季的 B站编号）。其它解析器没有，没有就是空字典。

    容错按"宁可少一个选项也别让整次解析失败"来：字段缺、类型不对都跳过。
    面板要靠它画「同系列多季」的下拉 —— 少一项顶多少一个选项，
    而抛出去会让整个 parse_url 变成失败，那才是真的亏

    透出的 `season_id` 是 **B站编号**，不是 TMDB id（见项目长期记忆：两者经常被搞混）。
    它在这里只有一个用途：让界面知道下拉该选中哪一项
    """
    if not isinstance(extra, dict):
        return {}

    data = extra.get("season_data")

    if not isinstance(data, dict):
        return {}

    seasons = []

    for entry in data.get("season_list") or []:
        if not isinstance(entry, dict):
            continue

        url = str(entry.get("url") or "")

        if not url:
            continue

        seasons.append({
            "title": str(entry.get("title") or ""),
            "url": url,
            "season_id": entry.get("season_id"),
        })

    if not seasons:
        return {}

    result = {"seasons": seasons}

    if series_title := str(data.get("series_title") or ""):
        result["series_title"] = series_title

    if (current := data.get("season_id")) is not None:
        result["current_season_id"] = current

    return result

def _pagination_data(extra) -> dict:
    """分页信息（个人空间、稍后再看、历史记录那类接口才给），没有就是空字典"""
    if not isinstance(extra, dict):
        return {}

    data = extra.get("pagination_data")

    if not isinstance(data, dict):
        return {}

    return {
        key: data[key]
        for key in ("current_page", "total_pages", "total_items")
        if isinstance(data.get(key), int) and not isinstance(data[key], bool)
    }

def _group_tree(node, index: dict) -> dict:
    """
    把解析树拍成"分组 + 条目 id"的结构

    解析结果是棵三层树：类别/剧名 → 章节 → 条目（投稿视频、合集还会再多一层）。
    界面照着它分组显示，模型也据此知道哪几条同属一个章节。

    只透出 id，标题一律走 episodes 那条平表 —— 同一份数据不进两次负载。
    容错按"宁可少一层分组，也别让整次解析失败"来
    """
    reverse = {id(item): item_id for item_id, item in index.items()}

    def walk(current):
        own = []
        children = []

        for child in current.children:
            if child.children:
                sub = walk(child)

                # 空章节不必占一行：番剧里「UP主陪你看」这类段落没有 bvid / cid，
                # 解析侧本就会把它们剔除，剔除后就只剩一个空壳节点
                if sub["episode_ids"] or sub["children"]:
                    children.append(sub)

            elif (item_id := reverse.get(id(child))) is not None:
                own.append(item_id)

        return {
            "number": current.number,
            "title": current.title,
            "episode_ids": own,
            "children": children,
        }

    return walk(node)

def _collect_group(interface, index: dict) -> dict:
    """
    解析列表的分组层级；没有可用的树时返回空字典

    树外面还包着一层不可见的根（见 `EpisodeParserBase.update_episode_list`：它特意
    再包一层，免得顶层节点信息丢失），屏幕上显示的第一行是那个根的第一个子节点 ——
    番剧是「电视剧 / 西游记」，章节则是它的子节点。

    `_model` 是私有属性，取不到就退化成"没有分组"：面板少一层折叠，
    比整个 parse_url 报错划算
    """
    model = getattr(interface.parse_list, "_model", None)
    root = getattr(model, "root_node", None)

    if root is None or not root.count():
        return {}

    visible = root.child(0)

    if visible is None or not visible.count():
        return {}

    return _group_tree(visible, index)

def _collect_episodes(limit: int):
    """
    汇总解析列表的当前内容，界面不可用时返回 None

    返回的字典直接构成工具结果的主体，键序即模型读到的顺序：
    先是规模与链接指向哪一项，再是条目本身
    """
    interface = get_parse_interface()

    if interface is None:
        return None

    items = interface.parse_list.get_all_items()

    # 先按完整列表编号再截断，保证 limit 不会改变任何条目的 id
    index = build_item_index(items)

    link_target_id = _locate_link_target(interface, index)

    episodes = [
        _episode_to_dict(item_id, item, is_link_target = item_id == link_target_id)
        for item_id, item in list(index.items())[:limit]
    ]

    result = {
        "total": len(items),
        "returned": len(episodes),
    }

    if link_target_id is not None:
        result["link_target_episode_id"] = link_target_id

        # 目标项落在 limit 之外时单独附上它的完整信息。这恰恰是最需要它的场景 ——
        # 三百集的合集，链接指向第 250 集 —— 只给一个编号，模型没法跟用户确认
        # 要下的是哪一集，还得再花一轮 get_episodes 去捞
        if not any(episode["episode_id"] == link_target_id for episode in episodes):
            result["link_target_episode"] = _episode_to_dict(
                link_target_id, index[link_target_id], is_link_target = True
            )

    result["episodes"] = episodes

    # 分组层级（类别/剧名 → 章节 → 条目）。面板照它画可折叠的分组，
    # 不再把番剧的「正片 / 相关推荐 / 花絮」摊成一片
    if group := _collect_group(interface, index):
        result["group"] = group

    return result

def _locate_link_target(interface, index: dict):
    """
    解析的链接精确指向的那一项在列表中的位置，链接未指向具体视频时为 None

    投稿视频按 cid、番剧与课程按 ep_id、会员购课程按 section_id 定位，解析侧已经
    算好并交给了解析树（见 ParseTreeView.update_tree），这里只是把结果取出来。

    按对象身份比对而不是比字段：分P 之间除了 cid 什么都一样，番剧一整季的条目
    在导出字段上同样难分彼此，只有对象本身能唯一确定是哪一条。index 里的对象与
    get_current_episode_item() 返回的都来自同一棵树，比对是可靠的
    """
    current_item = interface.parse_list.get_current_episode_item()

    if current_item is None:
        return None

    return next(
        (item_id for item_id, item in index.items() if item is current_item), None
    )

# MCP 上一次解析结束时界面的状态指纹：(全部条目 id, 被勾选的条目 id)
#
# 用户开着自动选择（config.auto_select_mode）时，解析完成后条目会被自动勾上，
# 于是"有勾选项"这个信号会被我们自己上一次解析污染 —— 不加区分的话，
# 第二次 parse_url 起就会永远被守卫拒绝，模型只能解析一次。
#
# 用指纹把两种勾选分开：与指纹完全一致，说明是上次解析留下的自动勾选、
# 用户没碰过，可以安全覆盖；对不上，才是用户真的在挑东西。
_last_snapshot = None

# 解析的互斥锁。解析全程都在动全局状态，两个解析交叠必然互相破坏，
# 详见 tool_parse_url 里的说明。它同时也保护了上面那个指纹变量
_parse_lock = Lock()

def _take_snapshot(interface):
    parse_list = interface.parse_list

    return (
        frozenset(item.episode_id for item in parse_list.get_all_items()),
        frozenset(item.episode_id for item in parse_list.get_checked_items()),
    )

def _parse_busy_reason():
    """
    检查当前是否适合发起解析

    EpisodeData 是全局缓存，解析开始时会 clear_cache()，界面上的解析树也会被
    整棵替换。用户正勾选着一批要下载的条目时，AI 的解析会把它们连同缓存一起冲掉，
    且不可撤销。这里在入口拦下来，让模型知道要等用户。

    注意不能用 EpisodeData._active_parsers 判断：那个计数只防并发写互相擦除数据，
    防不了"新解析覆盖旧结果"这件事本身。
    """
    interface = get_parse_interface()

    if interface is None:
        return "The application window is not ready yet."

    if interface.parse_list.get_checked_items_count() == 0:
        return None

    # 勾选状态与上次解析结束时完全一致 —— 是自动选择留下的，不是用户挑的
    if _last_snapshot is not None and _take_snapshot(interface) == _last_snapshot:
        return None

    return (
        "The user currently has items selected in the parse list. Parsing a new link "
        "would discard that selection. Ask the user to finish or clear it first."
    )

def _record_snapshot():
    global _last_snapshot

    def take():
        interface = get_parse_interface()

        return _take_snapshot(interface) if interface is not None else None

    try:
        _last_snapshot = call_in_main_thread(take, timeout = 5.0)

    except Exception:
        # 取不到就置空，退回"有勾选就拒绝"的保守行为
        logger.exception("记录解析列表状态失败")

        _last_snapshot = None

def _do_parse(url: str, timeout: float, preview_timeout: float = 30.0, page: int = 1):
    """
    发起解析，并等到媒体信息（清晰度、音质）也就绪

    解析成功只代表拿到了剧集列表。清晰度、音质是随后由 Previewer 异步取的，
    而创建下载任务依赖它 —— 界面上用户要花时间勾选，等于天然等过了这一步，
    但模型是连着调 parse_url 和 create_download 的，不等就必然撞上
    "Media information is not available"。
    """
    from ...common.signal_bus import signal_bus
    from ...thread.async_ import AsyncTask
    from ...parse.worker import ParseWorker

    outcome = {}
    done = Event()
    preview_done = Event()

    def on_success(category_name, extra_data):
        outcome["category"] = category_name
        # extra_data 里有这个解析器愿意额外交代的东西：番剧的整个系列（seasons）、
        # 分页信息（个人空间、稍后再看那类）。以前只有 category 有用，就把它丢了；
        # 现在面板要靠它画"同系列还有哪几部"的下拉，得原样带出去
        outcome["extra"] = extra_data
        done.set()

    def on_error(message):
        outcome["error"] = message
        done.set()

    def on_preview_finish():
        preview_done.set()

    def start():
        interface = get_parse_interface()

        # 预览信号必须赶在解析发起之前接上：媒体信息可能来得很快，
        # 晚一步连接就会彻底错过这次通知
        signal_bus.parse.preview_finish.connect(on_preview_finish)

        # 复刻 ParseInterface.on_parse 的启动步骤，额外挂上自己的回调。
        # 不直接调 reparse()：那样拿不到 worker 的 success / error 信号，
        # 解析失败时只能干等到超时，模型看不到真正的原因
        interface.url_box.setText(url)
        interface.parse_btn.setIndeterminateState(True)

        worker = ParseWorker(url, page)

        worker.success.connect(interface.on_parse_success)
        worker.error.connect(interface.on_parse_error)

        # 这两个回调在解析线程上直连执行，只写字典和置位 Event，不碰 Qt 对象
        worker.success.connect(on_success)
        worker.error.connect(on_error)

        AsyncTask.run(worker)

    call_in_main_thread(start, timeout = 10.0)

    try:
        if not done.wait(timeout):
            return {"error": f"Parsing timed out after {timeout:.0f}s."}

        if "error" not in outcome:
            # 拿不到媒体信息不算解析失败：剧集列表仍然可用，
            # 只是下载会受影响，交由 create_download 去报告
            if not preview_done.wait(preview_timeout):
                logger.warning("等待媒体信息超时，链接：%s", url)

        return outcome

    finally:
        try:
            call_in_main_thread(
                signal_bus.parse.preview_finish.disconnect, on_preview_finish, timeout = 5.0
            )

        except Exception:
            logger.exception("断开预览完成信号失败")

        # 无论成功、失败还是超时都重新取一次指纹：它记的是"界面此刻的样子"，
        # 只有反映真实状态才能在下次解析时正确区分自动勾选与用户勾选。
        # 放在等预览之后，此时自动选择已经应用完毕
        _record_snapshot()

def tool_parse_url(arguments: dict) -> dict:
    url = (arguments.get("url") or "").strip()

    if not url:
        return error_result("The 'url' argument is required.")

    if not any(pattern.search(url) for _, pattern in url_patterns):
        return error_result(
            f"'{url}' is not a recognized Bilibili link. Accepted forms include a full "
            "bilibili.com URL, a b23.tv short link, or a bare av / BV / ep / ss / md id."
        )

    # 服务器改成每连接一个线程后，并发的解析请求会真正并行跑进来，而解析全程
    # 都在动全局状态：EpisodeData 缓存、界面上那一棵解析树、预览完成信号的
    # 连接与断开。两个解析交叠会互相擦掉结果，preview_finish 更会同时唤醒
    # 两边的等待，各自都以为自己的媒体信息已经就绪。
    #
    # 这类串行是数据结构决定的，不是线程调度能优化掉的，所以直接互斥。
    # 只读的工具（任务列表、任务状态、登录状态）不受这把锁影响，仍可并发。
    if not _parse_lock.acquire(timeout = 2.0):
        return error_result(
            "Another parse is already running. Only one parse can run at a time because "
            "it replaces the application's parse list. Retry once it finishes."
        )

    try:
        return _parse_url_locked(url, arguments)

    finally:
        _parse_lock.release()

def _parse_url_locked(url: str, arguments: dict) -> dict:
    if reason := call_in_main_thread(_parse_busy_reason, timeout = 5.0):
        return error_result(reason)

    limit = _clamp_limit(arguments.get("limit"))
    page = _clamp_page(arguments.get("page"))

    outcome = _do_parse(url, timeout = 90.0, page = page)

    if error := outcome.get("error"):
        return error_result(f"Parsing failed: {error}")

    # 这次调用会排在界面的 on_update_parse_list 之后执行（两者都投递到 GUI
    # 线程的事件循环，先进先出），所以读到的一定是已经更新过的树
    collected = call_in_main_thread(_collect_episodes, limit, timeout = 15.0)

    if collected is None:
        return error_result("The parse list is unavailable.")

    structured = {"category": outcome.get("category", "")}
    structured.update(collected)

    # 同系列还有别的季时把整个系列带上：模型据此知道"这部不是全部"，
    # 界面据此画那个能"解析好几部"的下拉
    series = _season_data(outcome.get("extra"))

    if series:
        structured.update(series)

    # 分页（稍后再看、历史记录、个人空间这类）。没有这一段说明该链接一次给全
    pagination = _pagination_data(outcome.get("extra"))

    if pagination:
        structured["pagination"] = pagination

    total = collected["total"]
    returned = collected["returned"]

    media_error = call_in_main_thread(_media_info_error, timeout = 5.0)

    source = {}

    if media_error:
        # 明确告诉模型下载会失败，省得它拿着 episode_id 去撞 create_download
        structured["media_info_available"] = False
        structured["media_info_error"] = media_error

    else:
        # 供 create_download 的 options 使用：只有这里列出的档位是真实可选的
        options, source = call_in_main_thread(_media_info_summary, timeout = 5.0)

        if options:
            structured["available"] = options

        if source:
            structured["media_info_source"] = source

    summary = f"Parsed {total} item(s) from {url}." + _link_target_note(collected)

    if series:
        summary += " " + _series_note(series)

    if pagination:
        summary += " " + _pagination_note(pagination)

    if media_error:
        summary += f" Media information is unavailable ({media_error}), so downloads cannot be created yet."

    else:
        summary += _fallback_note(source)

    if returned < total:
        summary += f" Showing the first {returned}; call get_episodes with a higher limit to see more."

    return text_result(summary, structured)

# 批量解析一次最多收这么多条链接
#
# 解析链路是逐条串行的，每条之间还固定歇 0.5 秒（见 DynamicParser.parse_url_list），
# 50 条就要跑两三分钟。再多只是把一次工具调用的等待时间拉到不可用的量级
BATCH_MAX_LINKS = 50

# 批量解析只收投稿视频
#
# 与桌面端「批量解析」对话框的约定一致（那里的占位文字写着 only av and BV links
# are supported）。批量走的是 DynamicParser：解析器类型由**第一条**链接决定，
# 后面每条都拿同一个解析器去 parse(url, get_info_data=True)，混着放别的类型
# 会静默按错的解析器解析。宁可在这里拒收并说清楚，也不要让它悄悄解析成一片错数据
BATCH_PARSER_TYPES = ("video",)

def _batch_parse_target(url: str) -> str:
    """这条链接会落到哪个解析器上（取第一个命中的模式，与 get_parser_type 同序）"""
    for name, pattern in url_patterns:
        if pattern.search(url):
            return name

    return ""

def _set_batch_auto_add(enabled: bool):
    """
    批量解析"每条解析完自动加入下载列表"的开关

    消费点在 `DynamicEpisodeParser.update_page_node` 里，读的是同一个配置项，
    所以这里改一次就够了。桌面端那个复选框也是直接写这个键
    """
    from ...common.config import config

    config.set(config.auto_add_to_download_list, bool(enabled))

def _do_parse_batch(url_list: list, timeout: float):
    """
    逐条解析一批链接，等整批跑完

    🔴 批量是"**先把列表换成一棵空树，再把每条链接的节点挂上去**"：第一条走到
    `DynamicEpisodeParser.update_page_node` 时 `root_node_initialized` 还是 False，
    于是 `init_root_node()` 发一次全量更新（空根）把列表**替换**掉，之后每条才
    靠 `append_nodes()` 往这棵新根上挂。所以结果 = 这一批链接的并集，
    **原来列表里的内容会没**（实测：先解析 157 条的番剧，再批量两条视频 → 43 条）。

    由此也决定了：既不能复用 _do_parse，也不能按条去读列表 —— 中途读到的只是半成品
    """
    from ...common.data.auto_parse import AutoParsePayload
    from ...common.enum import ParserType
    from ...parse.worker import ProgressParseWorker
    from ...thread.async_ import AsyncTask

    outcome = {}
    done = Event()
    finished = Event()

    def on_success(category_name, extra_data):
        outcome["category"] = category_name
        done.set()

    def on_error(message):
        outcome["error"] = message
        done.set()

    def on_finished():
        finished.set()

    def start():
        interface = get_parse_interface()

        # 与单条解析一样把第一条写进地址栏：解析历史记的就是这一条，
        # 界面上也看得出刚才是从哪儿开始的
        interface.url_box.setText(url_list[0])
        interface.parse_btn.setIndeterminateState(True)

        worker = ProgressParseWorker(AutoParsePayload(
            url = url_list[0],
            url_list = list(url_list),
            parser_type = ParserType.BATCH,
        ))

        # 这几个回调在解析线程上直连执行，只写字典和置位 Event，不碰 Qt 对象
        worker.success.connect(on_success)
        worker.error.connect(on_error)
        worker.finished.connect(on_finished)

        AsyncTask.run(worker)

    call_in_main_thread(start, timeout = 10.0)

    try:
        outcome["finished"] = finished.wait(timeout)

    finally:
        # 批量解析没有接 on_parse_success，按钮的不确定态得自己收回来，
        # 否则界面上那颗「解析」会一直转下去
        def reset():
            interface = get_parse_interface()

            if interface is not None:
                interface.parse_btn.setIndeterminateState(False)

        try:
            call_in_main_thread(reset, timeout = 5.0)

        except Exception:
            logger.exception("恢复解析按钮状态失败")

        # 与 _do_parse 同理：无论成败都重取一次指纹，它记的是"界面此刻的样子"
        _record_snapshot()

    return outcome

def tool_parse_batch(arguments: dict) -> dict:
    raw = arguments.get("urls")

    # 允许多行文本：面板给的是 textarea，模型更可能直接给一个列表，两种都收
    if isinstance(raw, str):
        raw = raw.splitlines()

    if not isinstance(raw, list):
        return error_result("The 'urls' argument must be a list of links, or one link per line.")

    urls = [str(line).strip() for line in raw if str(line).strip()]

    if not urls:
        return error_result("No links provided.")

    if len(urls) > BATCH_MAX_LINKS:
        return error_result(
            f"Too many links: {len(urls)}. At most {BATCH_MAX_LINKS} links can be parsed in one batch."
        )

    for url in urls:
        if _batch_parse_target(url) not in BATCH_PARSER_TYPES:
            return error_result(
                f"'{url}' is not supported by batch parsing. Only upload videos (av / BV links) "
                "can be batched, one per line. Parse anything else one link at a time."
            )

    # 与 parse_url 共用同一把锁：两者都在动全局的解析树与 EpisodeData 缓存
    if not _parse_lock.acquire(timeout = 2.0):
        return error_result(
            "Another parse is already running. Only one parse can run at a time because "
            "it replaces the application's parse list. Retry once it finishes."
        )

    try:
        return _parse_batch_locked(urls, arguments)

    finally:
        _parse_lock.release()

def _parse_batch_locked(urls: list, arguments: dict) -> dict:
    if reason := call_in_main_thread(_parse_busy_reason, timeout = 5.0):
        return error_result(reason)

    limit = _clamp_limit(arguments.get("limit"))

    # 只有明确传了布尔值才动这个开关：桌面端的复选框也写它，
    # 不传就是"按用户原来的设置来"，不该被一次调用悄悄改掉
    if isinstance(arguments.get("auto_add"), bool):
        call_in_main_thread(_set_batch_auto_add, arguments["auto_add"], timeout = 5.0)

    timeout = min(900.0, 30.0 * len(urls) + 30.0)

    outcome = _do_parse_batch(urls, timeout = timeout)

    if error := outcome.get("error"):
        return error_result(f"Batch parsing failed: {error}")

    if not outcome.get("finished"):
        return error_result(f"Batch parsing did not finish within {timeout:.0f}s.")

    collected = call_in_main_thread(_collect_episodes, limit, timeout = 15.0)

    if collected is None:
        return error_result("The parse list is unavailable.")

    structured = {
        "category": outcome.get("category", ""),
        "parsed_links": len(urls),
    }

    structured.update(collected)

    total = collected["total"]

    # 🔴 措辞不能说成"追加到原来的列表上"：批量是**先重建一棵空树再逐条往里挂**
    # （DynamicEpisodeParser.update_page_node 里第一次会 init_root_node），
    # 原来列表里的东西会被整棵换掉。说反了，模型就会以为"我批一批就又多了一批"，
    # 而用户看到的是上一次的结果不见了
    summary = (
        f"Parsed {len(urls)} link(s) into the parse list; the list now holds {total} item(s) "
        "from these links, combined into one tree. This replaced whatever the list held before."
    )

    if total > collected["returned"]:
        summary += (
            f" Showing the first {collected['returned']}; call get_episodes with a higher "
            "limit to see more."
        )

    return text_result(summary, structured)

def _series_note(series: dict) -> str:
    """
    把"属于某系列、还有哪几季"写进文本摘要

    结构化结果里已经有 seasons，但并非所有 MCP 客户端都会把 structuredContent
    交给模型；而"这部还有第二季"恰恰是模型最该主动告诉用户的一句话
    """
    seasons = series.get("seasons") or []
    title = series.get("series_title") or ""

    head = f"It is part of the series '{title}'" if title else "It is part of a series"
    return f"{head}, which has {len(seasons)} season(s) listed in 'seasons'."

def _pagination_note(pagination: dict) -> str:
    """分页时点明"这只是其中一页"，否则模型会以为这就是全部"""
    current = pagination.get("current_page")
    total = pagination.get("total_pages")

    if current is None or total is None:
        return ""

    return (
        f"This is page {current} of {total}; pass 'page' to parse another one."
        if total > 1 else ""
    )


def tool_get_episodes(arguments: dict) -> dict:
    limit = _clamp_limit(arguments.get("limit"))

    collected = call_in_main_thread(_collect_episodes, limit, timeout = 15.0)

    if collected is None:
        return error_result("The parse list is unavailable.")

    total = collected["total"]

    if not total:
        return text_result("The parse list is empty. Call parse_url first.", {
            "total": 0,
            "returned": 0,
            "episodes": [],
        })

    structured = collected

    options, source = call_in_main_thread(_media_info_summary, timeout = 5.0)

    if options:
        structured["available"] = options

    if source:
        structured["media_info_source"] = source

    summary = f"{total} item(s) in the parse list."

    return text_result(summary + _link_target_note(collected) + _fallback_note(source), structured)

def _link_target_note(collected: dict) -> str:
    """
    把"链接指向的是哪一项"也写进文本摘要

    结构化结果里已经有 is_link_target 与 link_target_episode_id，但并非所有
    MCP 客户端都会把 structuredContent 交给模型，文本是唯一保证送达的那一份
    """
    item_id = collected.get("link_target_episode_id")

    if item_id is None:
        return ""

    episode = collected.get("link_target_episode") or next(
        (item for item in collected["episodes"] if item["episode_id"] == item_id), None
    )

    if title := (episode or {}).get("title"):
        return f" The link points to episode_id {item_id} ({title})."

    return f" The link points to episode_id {item_id}."

def _fallback_note(source: dict) -> str:
    """媒体信息取自别的视频时明说，别让模型把这些档位当成目标视频的"""
    if not source.get("from_fallback"):
        return ""

    return (
        " Note: the linked item did not provide media information, so the qualities in "
        f"'available' are those of another item ({source.get('title', '')}) and may differ "
        "from what the item you download actually offers."
    )

def _clamp_limit(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_LIMIT

    return max(1, min(value, MAX_LIMIT))

def _clamp_page(value) -> int:
    """只要是个正常的正整数就照用。非法值退回第 1 页 —— 分页类链接的第一页总是存在的"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return 1

    return value

_LIMIT_SCHEMA = {
    "type": "integer",
    "description": f"Maximum number of episodes to return (1-{MAX_LIMIT}, default {DEFAULT_LIMIT}).",
    "minimum": 1,
    "maximum": MAX_LIMIT,
}

_PAGE_SCHEMA = {
    "type": "integer",
    "description": (
        "Which page of a paginated listing to parse (1-based, default 1). Only meaningful "
        "for listings the result reports as paginated, such as the watch-later list, watch "
        "history or a user's uploads; for everything else page 1 is the whole thing."
    ),
    "minimum": 1,
}

def register(registry):
    registry.register(
        name = "parse_url",
        title = "Parse Bilibili Link",
        description = (
            "Parse a Bilibili link and load its episodes into the application's parse list. "
            "Accepts a full URL, a b23.tv short link, a bare av / BV / ep / ss / md id, or one "
            "of the app's own entry points: 'bili23://watch_later' and 'bili23://history'. "
            "This replaces whatever is currently in the parse list, and is refused while the "
            "user has items selected there. Call this before create_download. The result's "
            "'available' field lists the qualities and codecs this content actually offers, "
            "which are the valid values for create_download's options. When the link points at "
            "one specific item of a multi-part video, season or collection, 'link_target_episode_id' "
            "names it (and that item carries 'is_link_target'); prefer it over guessing from titles, "
            "since every part of a video shares one bvid. Its absence means the link addressed the "
            "whole listing rather than a single item. For a season of a series, 'seasons' lists "
            "every season of that series and 'current_season_id' says which one was just parsed - "
            "so you can tell the user the series continues and parse another season if they want. "
            "When 'pagination' is present the listing has more pages; pass 'page' to load another. "
            "'group' mirrors the parse list's own hierarchy (category/season -> section -> item): "
            "each node carries 'episode_ids' for the items directly beneath it, so 'episodes' is "
            "the flat view of the same set and 'group' is how those items are grouped on screen."
        ),
        input_schema = {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The Bilibili link or id to parse.",
                },
                "limit": _LIMIT_SCHEMA,
                "page": _PAGE_SCHEMA,
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler = tool_parse_url,
    )

    registry.register(
        name = "get_episodes",
        title = "List Parsed Episodes",
        description = (
            "List the episodes currently loaded in the application's parse list, including "
            "the episode_id values needed by create_download, and 'link_target_episode_id' "
            "identifying which item the parsed link pointed at, when it pointed at one."
        ),
        input_schema = {
            "type": "object",
            "properties": {"limit": _LIMIT_SCHEMA},
            "additionalProperties": False,
        },
        handler = tool_get_episodes,
    )

    registry.register(
        name = "parse_batch",
        title = "Parse Several Links At Once",
        description = (
            "Parse several Bilibili upload-video links (av / BV) in one call - this is the "
            "panel's batch-parse button, and the desktop app's equivalent dialog. All of the "
            "links end up in the application's parse list as ONE combined tree: the links are "
            "parsed one after another and every link's items are added to the same root. "
            "🔴 Like parse_url, this REPLACES whatever the list held before the call - the "
            "several links are combined with each other, not with the previous content. "
            f"Only upload videos are accepted, at most {BATCH_MAX_LINKS} links per call, one per "
            "line or as a JSON array. The result reports the whole list's totals, and 'group' "
            "carries the whole list, so read it to see what came out of this batch. Pass "
            "'auto_add' to control whether each link is added to the download list as soon as "
            "it is parsed; omitting it leaves the user's own setting untouched."
        ),
        input_schema = {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The av / BV links to parse, one entry per link.",
                },
                "limit": _LIMIT_SCHEMA,
                "auto_add": {
                    "type": "boolean",
                    "description": (
                        "Add each link's items to the download list as soon as that link is "
                        "parsed. Leave unset to keep the user's configured behaviour."
                    ),
                },
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
        handler = tool_parse_batch,
    )
