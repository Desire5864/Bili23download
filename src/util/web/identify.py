"""
Web 面板「名称识别」页（后端）

与「命名规则」页同样的取舍：网页上不另立一套判据。匹配、校验、应用全部落在
util/common/naming_alias.py —— 运行期命名调的就是那几个函数，网页放行而下载时
不生效的规则，用户在界面上看不到任何提示。

本页多做的一件事是**预览**：拿影视类型的样本变量，把季节标题换成这条规则的匹配
内容，再分别按「识别前 / 识别后」渲染一次文件名。渲染走的就是运行期那条路径
（FileNameFormatter + 该类型的当前默认规则），所以预览里出得来，下载时就出得来。

本模块不碰 Qt，跑在 HTTP 线程上；唯一写配置的那一步由 server.py 回到 GUI 线程。
"""

import json
import re

from ..common.config import config
from ..common.data.naming_convention import SampleShape, VariableListFactory
from ..common.enum import ConventionType
from ..common.naming_alias import (
    AliasError, FIELD_LABELS, MATCH_FIELDS, MATCH_MODES, MODE_LABELS,
    apply_to_data, load_aliases, normalize_entries, normalize_entry,
    save_aliases, shadowed_indexes,
)

from copy import deepcopy

# 预览里对比的那几个字段。只列与识别相关的 —— 整份变量表摆出来没人看
SNAPSHOT_KEYS = ("season_title", "series_title", "season_number", "year", "tmdb_id")

def _shape_entry(entry: dict, index: int, shadowed: bool) -> dict:
    """
    把配置里的一条摆成页面要的形状

    用户手改过配置文件时缺键是常事，这里逐个补默认值 —— 页面宁可显示成
    「匹配内容为空」，也不能因为一条脏数据整页 500
    """
    return {
        "id": str(entry.get("id") or ""),
        "index": index,
        "enabled": bool(entry.get("enabled", True)),
        "field": str(entry.get("field") or "season_title"),
        "mode": str(entry.get("mode") or "contains"),
        "match": str(entry.get("match") or ""),
        "title": str(entry.get("title") or ""),
        "season": entry.get("season"),
        "year": str(entry.get("year") or ""),
        "tmdb": str(entry.get("tmdb") or ""),
        "note": str(entry.get("note") or ""),
        # 前面的规则把它遮住了 ⇒ 永远轮不到。不是错误，但要在列表上标出来
        "shadowed": shadowed,
    }

def list_aliases() -> list:
    entries = load_aliases()
    shadowed = set(shadowed_indexes(entries))

    return [
        _shape_entry(entry, index, index in shadowed)
        for index, entry in enumerate(entries)
    ]

def _bangumi_default_rule() -> str:
    """
    影视类型当前用的默认规则串

    预览用它来渲染 —— 名称识别只负责把变量改成什么，落到文件名上长什么样由
    命名规则决定。取不到时给空串，页面据此提示用户先去命名规则页看一眼
    """
    rule_list = config.get(config.naming_rule_list)

    if not isinstance(rule_list, list):
        return ""

    for entry in rule_list:
        if not isinstance(entry, dict):
            continue

        if entry.get("type") == int(ConventionType.BANGUMI) and entry.get("default"):
            return str(entry.get("rule") or "")

    return ""

def build_payload() -> dict:
    """GET 的全部内容：识别表 + 两个下拉的选项 + 影视当前的默认规则"""
    return {
        "ok": True,
        "aliases": list_aliases(),
        "fields": [{"value": value, "label": FIELD_LABELS[value]} for value in MATCH_FIELDS],
        "modes": [{"value": value, "label": MODE_LABELS[value]} for value in MATCH_MODES],
        "bangumi_rule": _bangumi_default_rule(),
    }

def _snapshot(data: dict) -> dict:
    return {key: data.get(key) for key in SNAPSHOT_KEYS}

def _render(rule: str, data: dict) -> str:
    """按给定的规则串渲染一份变量数据。规则为空或渲染失败时给空串"""
    if not rule:
        return ""

    # FileNameFormatter 会把渲染异常吞掉并记一条 traceback，所以先把明显坏掉的
    # 规则挡在外面 —— 这个函数随每次输入触发，不该让 app.log 被刷满
    from ..format.rule_template import compile_rule

    try:
        compile_rule(rule, True)

    except Exception:
        return ""

    from ..format.file_name import FileNameFormatter

    formatter = FileNameFormatter()
    formatter.set_rule(rule)
    formatter.set_variable_data(dict(data))

    return formatter.format() or ""

def preview(entry: dict) -> dict:
    """
    渲染一条识别规则的效果

    样本取自影视类型，并把它当成「B站给出的季节标题就是这条规则的匹配内容」——
    这样预览看到的一定是命中时的样子（没命中时它什么都不做，没什么可看的）。

    样本里的 year / tmdb_id 会先清空：那是命名规则页的示例值，而这里要展示的
    恰恰是「识别之前 B站并没有这两个字段」
    """
    try:
        normalized = normalize_entry(deepcopy(entry))

    except AliasError as error:
        return {
            "ok": True, "valid": False, "error": str(error),
            "before": None, "after": None,
            "path_before": "", "path_after": "", "rule": "",
        }

    data = VariableListFactory().build_variable_data(int(ConventionType.BANGUMI), SampleShape.SINGLE)

    data["year"] = ""
    data["tmdb_id"] = ""

    # 让这条规则一定命中
    field = normalized["field"]

    if field in ("season_title", "any"):
        data["season_title"] = normalized["match"]

    if field in ("series_title", "any"):
        data["series_title"] = normalized["match"]

    before_data = dict(data)
    after_data = apply_to_data(dict(data), [normalized])

    rule = _bangumi_default_rule()

    return {
        "ok": True,
        "valid": True,
        "error": "",
        "before": _snapshot(before_data),
        "after": _snapshot(after_data),
        "path_before": _render(rule, before_data),
        "path_after": _render(rule, after_data),
        "rule": rule,
    }

def normalized_aliases(entries: list) -> list:
    """校验并规范化整张表（供 server.py 回主线程执行前先把错拦下来）"""
    return normalize_entries(deepcopy(entries))

def apply_aliases(entries: list):
    """写回配置。**必须在 GUI 线程上调用** —— config.set 会发 Qt 信号"""
    save_aliases(entries)

# ---------------------------------------------------------------------------
# TMDB 链接 → 剧名 / 年份 / 编号 / 季号
#
# 年份与 TMDB 编号不是 B站给的字段（episodes[].release_date 恒为空串），只能人工填。
# 用户手上一般已经有 TMDB 链接，让程序把链接拆成四个字段，比手抄可靠。
#
# 走 **公开网页**而不是 API：API 要申请 key（还得配到设置里、走代理），而这一页的
# JSON-LD（application/ld+json）本来就带 name 与 startDate，零配置就够用。
# 代价是 TMDB 改版会让它失效 —— 所以解析不出来时一律明确报错，让人工填回去，
# 绝不给一个看着像对的错值。三个解析函数都是纯函数，测试不必碰网络。
# ---------------------------------------------------------------------------

TMDB_HOST = "https://www.themoviedb.org"

# 认得出这些形态：
#   https://www.themoviedb.org/tv/62591
#   https://www.themoviedb.org/tv/62591/season/2
#   https://www.themoviedb.org/movie/12345?language=zh-CN
#   themoviedb.org/tv/62591/    （不带协议、带尾斜杠一样认）
TMDB_LINK_PATTERN = re.compile(
    r"themoviedb\.org\s*/\s*(tv|movie)\s*/\s*(\d+)(?:\s*/\s*season\s*/\s*(\d+))?",
    re.IGNORECASE,
)

# 用户也可能只粘了个编号（从别处复制时常见）。要求整串除数字外只剩空白与 #/，
# 免得把「西游记 1986」这类文字里的数字也认成编号
TMDB_BARE_PATTERN = re.compile(r"^[\s#/]*(\d{2,})[\s#/]*$")

LD_JSON_PATTERN = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)
TITLE_TAG_PATTERN = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
OG_TITLE_PATTERN = re.compile(
    r"""<meta[^>]+property=["']og:title["'][^>]+content=["'](.*?)["']""", re.IGNORECASE
)
# 「巴啦啦小魔仙 (TV Series 2008) — The Movie Database (TMDB)」
# 括号里那截是类型词，长短不定（"TV Series"、"Movie"、有时干脆没有），
# 所以用 [^()]*? 整段吞掉、只掐住末尾的四位年份；(19|20) 是为了别把
# 「(2160p)」这种规格当成发行年
TITLE_YEAR_PATTERN = re.compile(r"^(?P<title>.+?)\s*\(\s*[^()]*?(?P<year>(?:19|20)\d{2})\s*\)")

# JSON-LD 里挑年份的字段，按优先级排：剧集给 startDate，电影给 datePublished，
# 实在没有才退到 endDate（至少不会填成 TMDB 建档日 dateCreated）
LD_YEAR_KEYS = ("startDate", "datePublished", "releaseDate", "endDate")

LD_TYPES = ("TVSeries", "Movie")

TMDB_TIMEOUT = 15.0
TMDB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

class TmdbError(Exception):
    """取 TMDB 失败。消息会直接显示给用户，所以要写清楚下一步做什么"""

def parse_tmdb_link(text) -> dict | None:
    """从用户粘进来的一段文字里认出 TMDB 链接。认不出给 None"""
    if not isinstance(text, str):
        return None

    text = text.strip()

    if not text:
        return None

    matched = TMDB_LINK_PATTERN.search(text)

    if matched:
        season = matched.group(3)

        return {
            "kind": matched.group(1).lower(),
            "tmdb": matched.group(2),
            "season": int(season) if season else None,
        }

    matched = TMDB_BARE_PATTERN.match(text)

    if matched:
        # 只给了编号，没给类型 —— 影视库里剧集占绝大多数，按剧集取页
        return {"kind": "tv", "tmdb": matched.group(1), "season": None}

    return None

def _strip_json_ld_wrapper(block: str) -> str:
    """TMDB 把 JSON-LD 包在 /* <![CDATA[ */ … /* ]]> */ 里，去掉这两段才能 json.loads"""
    block = re.sub(r"^\s*/\*\s*<!\[CDATA\[\s*\*/", "", block.strip())
    block = re.sub(r"/\*\s*\]\]>\s*\*/\s*$", "", block.strip())

    return block.strip()

def _ld_year(data: dict) -> str:
    for key in LD_YEAR_KEYS:
        matched = re.match(r"(\d{4})", str(data.get(key) or ""))

        if matched:
            return matched.group(1)

    return ""

def parse_tmdb_page(html_text: str) -> dict:
    """
    从 TMDB 详情页里取出名字与首播年份

    两条路：JSON-LD（首选，字段干净）→ <title> 标签（兜底）。
    都取不到就给空串，由调用方决定怎么提示 —— 这个函数不抛异常，页面结构变了
    不该表现成"服务出错"
    """
    if not isinstance(html_text, str):
        return {"title": "", "year": ""}

    for block in LD_JSON_PATTERN.findall(html_text):
        try:
            data = json.loads(_strip_json_ld_wrapper(block))

        except (ValueError, TypeError):
            continue

        if not isinstance(data, dict):
            continue

        types = data.get("@type")
        types = types if isinstance(types, list) else [types]

        if not any(entry in LD_TYPES for entry in types):
            continue

        name = data.get("name")
        name = name[0] if isinstance(name, list) and name else name

        if isinstance(name, str) and name.strip():
            return {"title": name.strip(), "year": _ld_year(data)}

    # 样式或结构改过、JSON-LD 没了 —— 退回标题标签：
    # 「巴啦啦小魔仙 (TV Series 2008) — The Movie Database (TMDB)」
    matched = TITLE_TAG_PATTERN.search(html_text)

    if matched:
        title = re.sub(r"\s+", " ", matched.group(1)).strip()
        parsed = TITLE_YEAR_PATTERN.match(title)

        if parsed:
            return {
                "title": parsed.group("title").strip(),
                "year": parsed.group("year"),
            }

        # 没有年份的那种标题（「登录 — The Movie Database (TMDB)」之类不要认），
        # 只在明显不是通用页时才拿来当名字
        if title and "TMDB" not in title and "The Movie Database" not in title:
            return {"title": title, "year": ""}

    matched = OG_TITLE_PATTERN.search(html_text)

    if matched:
        return {"title": matched.group(1).strip(), "year": ""}

    return {"title": "", "year": ""}

def _fetch_tmdb_page(url: str) -> str:
    """
    取 TMDB 详情页

    走项目自己的代理设置（`get_proxy_mounts`）：没配代理就是直连，配了就跟着走 ——
    TMDB 在部分网络下直连不通，用户可以在「设置 → 代理」里解决。SSL 用同一份上下文，
    免得局域网里做证书替换的环境下这里单独报错。
    """
    import httpx

    from ..network.request import get_proxy_mounts, get_ssl_context

    try:
        with httpx.Client(
            mounts = get_proxy_mounts(),
            verify = get_ssl_context(),
            timeout = TMDB_TIMEOUT,
            follow_redirects = True,
            headers = {"User-Agent": TMDB_USER_AGENT},
        ) as client:
            response = client.get(url)

    except Exception as error:
        raise TmdbError(
            "连不上 TMDB（{0}）。可以在「设置 → 代理」里配好代理再试".format(type(error).__name__)
        ) from error

    if response.status_code == 404:
        raise TmdbError("TMDB 上没有这个编号，检查一下链接")

    if response.status_code != 200:
        raise TmdbError("TMDB 返回 {0}，稍后再试".format(response.status_code))

    return response.text

def tmdb_lookup(text, fetch = None) -> dict:
    """
    把一条 TMDB 链接变成识别规则要的四个字段

    永远回 200 + ok 标记（与预览接口同理）：链接粘错、TMDB 打不开都是用户输入
    与网络的问题，不是"请求坏了"，页面据此就地提示即可。

    fetch 可注入 —— 测试拿它顶掉网络，不必真连 TMDB。
    """
    link = parse_tmdb_link(text)

    if not link:
        return {
            "ok": False,
            "error": "没认出 TMDB 链接。形如 https://www.themoviedb.org/tv/62591",
        }

    url = "{0}/{1}/{2}?language=zh-CN".format(TMDB_HOST, link["kind"], link["tmdb"])

    try:
        page = (fetch or _fetch_tmdb_page)(url)

    except TmdbError as error:
        return {"ok": False, "error": str(error)}

    meta = parse_tmdb_page(page)

    if not meta["title"]:
        return {
            "ok": False,
            "error": "页面打开了，但没解析出片名 —— TMDB 结构可能改过，请手工填写",
        }

    return {
        "ok": True,
        "title": meta["title"],
        "year": meta["year"],
        "tmdb": link["tmdb"],
        "kind": link["kind"],
        # 链接里带了 /season/N 就用它；没带按 1 算 —— 名称识别里的季号是
        # B站这一季的序号，一季一个 ss 是常态
        "season": link["season"] or 1,
        "season_from_url": link["season"] is not None,
    }

