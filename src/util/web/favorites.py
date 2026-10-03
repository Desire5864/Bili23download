"""
Web 面板「收藏」页（后端）

桌面端的收藏浮层有五类（`gui/component/widget/flyout.py`），而它们分成**两种行为**：

- 收藏夹 / 订阅合集 / 追番追剧 —— "点开看列表"。本模块把 B 站那三个接口的响应
  归一成页面直接能画的一行一条。
- 稍后再看 / 历史记录 —— 桌面端**根本不出列表**，点一下就把 `bili23://watch_later`
  或 `bili23://history` 丢给解析器（flyout.py:246,253），由解析页列条目。
  这一页照做：不替它们编一套卡片，只把"该解析哪个地址"告诉前端。

几处刻意的取舍：

- **不另立判据**。条目里的 `url` 全是能直接喂给 `parse_url` 的标准地址，
  点开看点、要下载，走的都是面板上那条已经验过的解析/建任务链路 ——
  这一页只负责"列出收藏夹"，不重复实现解析。
- **自定义地址写在服务端**（`DIRECT_PARSE_KINDS`），前端只认这一份。两边各写一遍，
  迟早会有一边改了另一边没跟上，而症状是"点了没反应"，最难查。
- **归一化是纯函数**（`normalize_entries`），取数那一步可注入（`fetch`），
  所以测试不必打真网络：B 站改字段、本机断网都不该让测试变红。
- **未登录不发请求**。`up_mid` 拿不到就无从查起，直接回一句能读的提示，
  比让 B 站回一个 -101 再翻译成人话更省事。稍后再看 / 历史记录也照此 ——
  它们连列表都没有，登录与否由解析那一步去管（`ParserBase.check_login`）。
- 本模块不碰 Qt，跑在 HTTP 线程上。
"""

from urllib.parse import urlencode

# 自建收藏夹。注意这是 list-all：一次给全，没有分页
FAVORITE_CREATED_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"

# 订阅（收藏）别人的合集，有分页
FAVORITE_COLLECTED_API = "https://api.bilibili.com/x/v3/fav/folder/collected/list"

# 追番追剧，走 wbi 签名
FOLLOW_API = "https://api.bilibili.com/x/space/bangumi/follow/list"

# 五类分类，顺序即页面 tab 顺序。前三类取列表，后两类走解析 —— 与桌面端对齐
KINDS = ("favorite", "subscription", "follow", "watch_later", "history")

KIND_LABELS = {
    "favorite": "收藏夹",
    "subscription": "订阅合集",
    "follow": "追番追剧",
    "watch_later": "稍后再看",
    "history": "历史记录",
}

# 需要"点开看列表"的三类，也就是本模块真的去取数的那些
LIST_KINDS = ("favorite", "subscription", "follow")

# 自定义协议地址 → 解析器。值的来源是两个解析器自己认的地址
# （`util/common/data/url_pattern.py` 里的 `bili23://…` 两条），写法与桌面端浮层一致。
#
# 🔴 不在这里拼 `pn`：这两个地址解析出来的就是**分页的第一页**，
# 翻页靠 parse_url 的 `page` 参数，见 panel.py 里那一页的翻页按钮
DIRECT_PARSE_KINDS = {
    "watch_later": "bili23://watch_later",
    "history": "bili23://history",
}

PAGE_SIZE = 50
FOLLOW_PAGE_SIZE = 24

# B 站在未登录 / 登录失效时的 code，翻成人话给页面
BILI_CODE_MESSAGES = {
    -101: "B站那边说账号没登录，去「B站账号」页扫码登录后再试",
    -400: "B站拒绝了这次请求，稍后再试",
    -403: "B站说没有权限看这个列表",
}


class FavoriteError(Exception):
    """能给用户看的一句话。取数失败一律走它，调用方不必再翻译"""

def favorites_url(kind: str, uid, page: int = 1, sign = None) -> str:
    """
    拼某一类收藏的列表地址

    `sign` 只给 follow 用（那个接口要 wbi 签名）。做成参数是为了**可注入**：
    签名依赖 `config` 里的两个密钥，测试拿一个假的顶掉就不必联网补密钥
    """
    if kind == "favorite":
        return "{0}?{1}".format(FAVORITE_CREATED_API, urlencode({"up_mid": uid}))

    if kind == "subscription":
        return "{0}?{1}".format(FAVORITE_COLLECTED_API, urlencode({
            "pn": page,
            "ps": PAGE_SIZE,
            "up_mid": uid,
            "platform": "web",
            "web_location": "333.1387",
        }))

    if kind == "follow":
        if sign is None:
            # 惰性导入：base.py 顶层挂着 PySide6 与 signal_bus，
            # 而这个模块的纯函数部分要能在不碰 Qt 的前提下被导入
            from ..parse.parser.base import enc_wbi

            sign = enc_wbi

        params = {
            "vmid": uid,
            "type": 1,
            "pn": page,
            "ps": FOLLOW_PAGE_SIZE,
            "playform": "web",
            "follow_status": 0,
            "web_location": "333.1387",
        }

        try:
            query = sign(params)

        except Exception as error:
            # 签名要拿 config 里的两个密钥，拿不到时 enc_wbi 会去补取一次（网络）。
            # 那是这条路独有的前置条件，值得单独给一句人话 ——
            # 否则用户看到的是一句英文异常名，无从归因
            raise FavoriteError(
                "拿不到 B站签名密钥，稍后再试（追番列表要签名，首次可能需要联网补一次）"
            ) from error

        return "{0}?{1}".format(FOLLOW_API, query)

    if kind in DIRECT_PARSE_KINDS:
        # 走到这儿说明调用方漏了 list_favorites 开头那个分支。直接说清楚，
        # 免得报成"不认识的分类" —— 分类是认识的，只是它没有列表接口
        raise FavoriteError("「{0}」没有列表接口，要解析 {1}".format(
            KIND_LABELS[kind], DIRECT_PARSE_KINDS[kind]))

    raise FavoriteError("不认识的收藏分类：{0}".format(kind))

def _entry(title, url, count = 0, cover = "", desc = "") -> dict:
    """页面画一张卡片要的就这几个字段，多的不往后端塞"""
    return {
        "title": str(title or ""),
        "url": str(url or ""),
        "count": int(count or 0),
        "cover": str(cover or ""),
        "desc": str(desc or ""),
    }

def normalize_entries(kind: str, payload) -> list:
    """
    把 B 站的响应摆成页面要的形状

    容错按"宁可少一条也别整页 500"来：字段缺、类型不对都当空值，
    但 `title` 与 `url` 同时为空的条目直接丢掉 —— 那种卡片点不动，
    留在页面上只会让人以为功能坏了
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if not isinstance(data, dict):
        return []

    raw = data.get("list")

    if not isinstance(raw, list):
        return []

    entries = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        shaped = None

        if kind == "favorite":
            fid = item.get("id")
            mid = item.get("mid")

            if fid is None or mid is None:
                continue

            shaped = _entry(
                item.get("title"),
                "https://space.bilibili.com/{0}/favlist?fid={1}".format(mid, fid),
                item.get("media_count"),
            )

        elif kind == "subscription":
            media_id = item.get("id")
            mid = item.get("mid")

            if media_id is None:
                continue

            # 合集作者名：订阅来的合集常常与自己无关，标出作者省得认错
            upper = item.get("upper") or {}
            owner = upper.get("name") if isinstance(upper, dict) else ""

            shaped = _entry(
                item.get("title"),
                "https://space.bilibili.com/{0}/lists/{1}?type=season".format(mid, media_id),
                item.get("media_count"),
                item.get("cover"),
                owner,
            )

        elif kind == "follow":
            season_id = item.get("season_id")

            if season_id is None:
                continue

            areas = item.get("areas") or []
            area = areas[0].get("name", "") if areas and isinstance(areas[0], dict) else ""
            season_type = item.get("season_type_name") or ""
            new_ep = item.get("new_ep") or {}
            progress = item.get("progress") or ""

            # 副标题按信息量排：更新到第几话 > 看到第几话 > 类型 · 地区
            desc = ""
            new_ep_text = new_ep.get("index_show", "") if isinstance(new_ep, dict) else ""

            if new_ep_text:
                desc = "{0} · {1}".format(new_ep_text, progress) if progress else new_ep_text
            elif progress:
                desc = progress
            elif season_type or area:
                desc = " · ".join(part for part in (season_type, area) if part)

            shaped = _entry(
                item.get("title"),
                "https://www.bilibili.com/bangumi/play/ss{0}".format(season_id),
                0,
                item.get("cover"),
                desc,
            )

        if shaped and (shaped["title"] or shaped["url"]):
            entries.append(shaped)

    return entries

def _fetch_json(url: str) -> dict:
    """同步取一份 JSON。带全局 client 的 Cookie —— 这三个接口都要登录态"""
    from ..network.request import SyncNetWorkRequest

    return SyncNetWorkRequest(url).run()

def current_uid() -> str:
    from ..common.runtime import runtime

    return str(runtime.auth.uid or "")

def direct_parse_payload(kind: str) -> dict:
    """
    「稍后再看 / 历史记录」的应答：不取列表，只告诉前端该解析哪个地址

    形状与列表接口**保持同构**（都有 ok / kind / label），前端一套分支就能处理两类；
    多出来的 `direct_parse` + `parse_url` 就是"这一类要解析"的全部信息。

    不在这里查登录态：这两类本来就没有列表可判，真正的登录检查在解析器里
    （`ParserBase.check_login`，未登录时会抛一句人话）。在这里多查一次，
    只会让"没登录"这一个症状有两个出处
    """
    return {
        "ok": True,
        "kind": kind,
        "label": KIND_LABELS[kind],
        "direct_parse": True,
        "parse_url": DIRECT_PARSE_KINDS[kind],
        "entries": [],
    }

def list_favorites(kind: str, uid = None, page: int = 1, fetch = None, sign = None) -> dict:
    """
    取一类收藏的列表

    与面板其它接口一样：**用户/网络的问题一律回 200 + ok=false**，
    由页面就地提示；只有"参数本身不对"才该是 400，那个判断在 server 层做。
    """
    if kind not in KINDS:
        raise FavoriteError("不认识的收藏分类：{0}".format(kind))

    if kind in DIRECT_PARSE_KINDS:
        return direct_parse_payload(kind)

    uid = current_uid() if uid is None else str(uid)

    if not uid:
        return {
            "ok": False,
            "need_login": True,
            "error": "还没登录 B站账号，去「B站账号」页扫码登录后再看收藏",
        }

    try:
        url = favorites_url(kind, uid, page, sign)
        payload = (fetch or _fetch_json)(url)

    except FavoriteError as error:
        # 「签名密钥暂时拿不到」这类：页面就地提示即可，不该变成 500
        return {"ok": False, "error": str(error)}

    except Exception as error:
        return {
            "ok": False,
            "error": "取收藏列表失败（{0}）：可以在「设置 → 代理」里检查网络".format(
                type(error).__name__),
        }

    if not isinstance(payload, dict):
        return {"ok": False, "error": "B站返回的不是一份 JSON，稍后再试"}

    code = payload.get("code", 0)

    if code != 0:
        return {
            "ok": False,
            "need_login": code == -101,
            "error": BILI_CODE_MESSAGES.get(code, payload.get("message") or "B站返回错误码 {0}".format(code)),
        }

    entries = normalize_entries(kind, payload)
    data = payload.get("data") or {}
    total = data.get("total")

    # list-all（收藏夹）没有分页，永远算作到底
    has_more = bool(total) and page * (FOLLOW_PAGE_SIZE if kind == "follow" else PAGE_SIZE) < int(total)

    return {
        "ok": True,
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind),
        "entries": entries,
        "page": page,
        "has_more": has_more,
    }
