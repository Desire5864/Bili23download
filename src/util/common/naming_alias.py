"""
名称识别（别名归并）—— 把 B站上名字各异的同剧条目归到同一条命名规则下

B站把一部剧的每一季拆成彼此独立的 season：

  · ``{season_title}`` 拿到的是「西游记续集」这种并列名，而不是「西游记 第二季」
  · ``{season_number}`` 取的是「它在 seasons 列表里排第几」，而那份列表可能横跨
    完全不相干的几部剧 —— 央视版《四大名著》共用一个 seasons 列表，于是
    《红楼梦》排到第 3（见 bangumi.py::determine_season_number）
  · 年份与 TMDB 编号**不是 B站提供的字段**：``episodes[].release_date`` 恒为空串，
    ``{season_id}`` 是 B站的编号（西游记 33622），与 TMDB 的 13923 毫无关系。
    ``{pub_time}`` 也不行 —— 那是该集的上架时间，西游记上架于 2020、柯南上架于
    2020，引进老片全错

别名表把「见到 A 就当成 B」显式记下来：命名时改写 season_title 与 season_number，
并填上 year / tmdb_id 两个变量。规则里配可选段引用即可 —— ``< ({year})>`` 在没
命中时整段消失，不给文件名留下 "()"（可选段语义见 rule_template）。

本模块不碰 Qt、不碰 HTTP，供两处调用：
  · FileNameFormatter.get_variable_data_from_task_info —— 运行期命名（下载与改名）
  · util/web/identify.py —— 面板「名称识别」页的读写、校验与预览
"""

from .config import config

from copy import deepcopy
from uuid import uuid4
import re

class AliasError(ValueError):
    """校验不通过。文案直接面向用户，由调用方决定怎么呈现"""

# 匹配哪个字段。any = season_title 与 series_title 任一命中即可
MATCH_FIELDS = ("season_title", "series_title", "any")

# 匹配方式。contains 是默认 —— B站给的名字常带前后缀（「西游记续集 4K修复版」）
MATCH_MODES = ("contains", "exact", "regex")

FIELD_LABELS = {
    "season_title": "季标题",
    "series_title": "系列标题",
    "any": "任一标题",
}

MODE_LABELS = {
    "contains": "包含",
    "exact": "完全等于",
    "regex": "正则表达式",
}

# 各字段上限。请求体来自网络，不设限等于让任何人往 config.json 里塞任意大的字符串
MAX_MATCH_LENGTH = 120
MAX_TITLE_LENGTH = 120
MAX_NOTE_LENGTH = 200

def load_aliases() -> list:
    """
    读别名表的独立副本

    必须深拷贝：config.get() 返回的是配置里那个 list 本身，元素也是原字典，就地
    编辑等于绕过保存动作改写配置（用户点了取消也已生效）。配置里的值是空表时拿到的
    更是 DefaultValue 上的类属性本身，被污染后进程内的默认值从此带着用户的改动，
    且没有任何报错 —— 与 naming_rules.load_rules 是同一条纪律。
    """
    entries = config.get(config.naming_alias_list)

    return deepcopy(entries) if isinstance(entries, list) else []

def save_aliases(entries: list):
    """写回配置。**必须在 GUI 线程上调用** —— config.set 会发 Qt 信号"""
    config.set(config.naming_alias_list, entries)

def _as_int(value):
    """配置里可能把数字存成字符串；空值与非法值一律给 None"""
    if value in (None, ""):
        return None

    try:
        return int(str(value).strip())

    except (TypeError, ValueError):
        return None

def _candidates(data: dict, field: str) -> list:
    """按匹配字段取出待比对的值。字段缺失或为空时给空串，调用方据此跳过"""
    if field == "any":
        return [str(data.get("season_title") or ""), str(data.get("series_title") or "")]

    return [str(data.get(field) or "")]

def _hit(entry: dict, data: dict) -> bool:
    pattern = str(entry.get("match") or "")

    if not pattern:
        return False

    mode = str(entry.get("mode") or "contains")

    for value in _candidates(data, str(entry.get("field") or "season_title")):
        if not value:
            continue

        if mode == "exact":
            if value == pattern:
                return True

        elif mode == "regex":
            try:
                if re.search(pattern, value):
                    return True

            except re.error:
                # 表里存着坏正则（用户手改过配置文件）：跳过这一条，不影响其余规则。
                # 网页提交的正则已在 normalize_entry 里编译过，走不到这里
                continue

        elif pattern in value:
            return True

    return False

def match_alias(data: dict, entries: list = None):
    """
    按表顺序找第一条命中的规则，没有命中的返回 None

    **顺序即优先级**：contains 模式下「西游记」会吃掉「西游记续集」，所以更具体的
    规则必须排在更宽泛的前面（面板上给了上移 / 下移，另有 shadowed_indexes 提示）
    """
    if entries is None:
        entries = config.get(config.naming_alias_list)

    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue

        if _hit(entry, data):
            return entry

    return None

def apply_to_data(data: dict, entries: list = None) -> dict:
    """
    就地把识别结果写进一份命名变量数据，返回同一个 dict

    ``series_title`` 保持 B站原值不覆盖：它是「央视版四大名著」这类大系列名，
    抹掉之后用户反而看不出这条记录是从哪来的。改名的目标只有 season_title
    """
    alias = match_alias(data, entries)

    if alias is None:
        return data

    title = str(alias.get("title") or "").strip()

    if title:
        data["season_title"] = title

    season = _as_int(alias.get("season"))

    if season is not None:
        data["season_number"] = season

    data["year"] = str(alias.get("year") or "").strip()
    data["tmdb_id"] = str(alias.get("tmdb") or "").strip()

    return data

def normalize_entry(entry: dict, seen_ids: set = None) -> dict:
    """校验并规范化一条识别规则。任何一项不合格就抛 AliasError"""
    if not isinstance(entry, dict):
        raise AliasError("识别规则的格式不正确")

    field = str(entry.get("field") or "season_title").strip()
    mode = str(entry.get("mode") or "contains").strip()
    match = str(entry.get("match") or "").strip()

    if field not in MATCH_FIELDS:
        raise AliasError(f"未知的匹配字段：{entry.get('field')}")

    if mode not in MATCH_MODES:
        raise AliasError(f"未知的匹配方式：{entry.get('mode')}")

    if not match:
        raise AliasError("匹配内容不能为空")

    if len(match) > MAX_MATCH_LENGTH:
        raise AliasError(f"匹配内容不能超过 {MAX_MATCH_LENGTH} 个字符")

    if re.search(r"[\x00-\x1f]", match):
        raise AliasError("匹配内容里不能有换行或控制字符")

    if mode == "regex":
        try:
            re.compile(match)

        except re.error as error:
            raise AliasError(f"正则表达式写错了：{error}")

    title = str(entry.get("title") or "").strip()

    if len(title) > MAX_TITLE_LENGTH:
        raise AliasError(f"归并后的剧名不能超过 {MAX_TITLE_LENGTH} 个字符")

    # 剧名会直接进文件名与目录名。FileNameFormatter 会把非法字符替换成下划线，
    # 但那是兜底、不是允许 —— 在这里拦下来，用户才能知道名字为什么变了
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', title):
        raise AliasError('归并后的剧名里不能有 \\ / : * ? " < > | 这些字符')

    season = _as_int(entry.get("season"))

    if entry.get("season") not in (None, "") and season is None:
        raise AliasError(f"季号必须是数字：{entry.get('season')}")

    if season is not None and not 1 <= season <= 999:
        raise AliasError("季号要在 1 ~ 999 之间")

    year = str(entry.get("year") or "").strip()

    if year and not re.fullmatch(r"\d{4}", year):
        raise AliasError("年份要写成四位数字，例如 1986")

    tmdb = str(entry.get("tmdb") or "").strip()

    if tmdb and not re.fullmatch(r"\d+", tmdb):
        raise AliasError("TMDB 编号必须是纯数字")

    note = str(entry.get("note") or "").strip()

    if len(note) > MAX_NOTE_LENGTH:
        raise AliasError(f"备注不能超过 {MAX_NOTE_LENGTH} 个字符")

    if re.search(r"[\x00-\x1f]", note):
        raise AliasError("备注里不能有换行或控制字符")

    # 一条什么都没改的规则是无害但无用的：它照样会「命中」，让后面真正想生效的
    # 规则永远轮不到，而用户以为已经配好了
    if not title and season is None and not year and not tmdb:
        raise AliasError("这条规则没改任何东西：归并剧名 / 季号 / 年份 / TMDB 编号至少要填一项")

    alias_id = str(entry.get("id") or "").strip()

    # id 由页面留空、服务端补：网页上拿不到 crypto.randomUUID（它不是安全上下文，
    # 局域网 http 下是 undefined），而 id 是页面选中项与删除的唯一依据
    if not alias_id or (seen_ids is not None and alias_id in seen_ids):
        alias_id = str(uuid4())

    if seen_ids is not None:
        seen_ids.add(alias_id)

    return {
        "id": alias_id,
        "enabled": bool(entry.get("enabled", True)),
        "field": field,
        "mode": mode,
        "match": match,
        "title": title,
        "season": season,
        "year": year,
        "tmdb": tmdb,
        "note": note,
    }

def _check_conflicts(entries: list):
    """
    完全相同的匹配条件不能出现两条

    它们的结果必然一字不差，后一条永远轮不到 —— 留着只会在用户以为自己改的是
    另一条时给出错误的结果
    """
    seen = {}

    for index, entry in enumerate(entries):
        key = (entry["field"], entry["mode"], entry["match"])

        if key in seen:
            raise AliasError(
                f"第 {seen[key] + 1} 条与第 {index + 1} 条的匹配条件完全相同"
                f"（{entry['match']}），后一条永远不会生效"
            )

        seen[key] = index

def normalize_entries(entries: list) -> list:
    """校验并规范化整张识别表。任何一条不合格就整份拒收"""
    if not isinstance(entries, list):
        raise AliasError("识别规则表的格式不正确")

    seen_ids = set()
    cleaned = [normalize_entry(entry, seen_ids) for entry in entries]

    _check_conflicts(cleaned)

    return cleaned

def shadowed_indexes(entries: list) -> list:
    """
    找出被前面的规则遮住、永远轮不到的条目下标

    这不是错误（顺序是用户自己排的），但要点出来：contains 模式下把「西游记」排在
    「西游记续集」前面，后者就成了摆设
    """
    indexes = []

    for index, entry in enumerate(entries):
        if entry.get("mode") != "contains":
            continue

        current = str(entry.get("match") or "")

        for earlier in entries[:index]:
            if earlier.get("mode") != "contains":
                continue

            # 两条的字段得够得着同一处：field 相同，或者其中一条用 any 通吃
            reachable = (
                earlier.get("field") == entry.get("field")
                or "any" in (earlier.get("field"), entry.get("field"))
            )

            if not reachable:
                continue

            pattern = str(earlier.get("match") or "")

            # 前面那条的匹配内容本事就是当前这条的子串 ⇒ 当前这条永远轮不到
            if pattern and pattern in current:
                indexes.append(index)
                break

    return indexes
