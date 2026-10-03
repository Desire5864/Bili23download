"""
util/web/favorites.py —— 账号收藏（收藏夹 / 订阅合集 / 追番追剧 / 稍后再看 / 历史记录）

全程不打网络：归一化是纯函数，取数那一步把 fetch 换成替身。
B 站改字段、本机断网都不该让这些用例变红 —— 那两件事都不是"我们改坏了"。

真正要守住的三条：
  · 归一化对脏数据要**丢条目而不是抛异常**（缺键是常事，整页 500 最难看）；
  · 「没登录」与「B站报错」要说人话，并且不该发那次注定失败的请求；
  · 后两类（稍后再看 / 历史记录）**没有列表接口**，只回一个"该解析哪个地址"，
    那个地址必须是解析器认得的（见 TestDirectParseKinds 最后一条）。
"""

import pytest

from util.common.data import url_patterns
from util.web import favorites
from util.web.favorites import FavoriteError

# 三种响应各一份，字段取自真接口
CREATED = {
    "code": 0,
    "data": {"count": 2, "list": [
        {"id": 3706531, "fid": 3706531, "mid": 3690988658755978, "title": "默认收藏夹", "media_count": 128},
        {"id": 998877, "mid": 3690988658755978, "title": "稍后细看", "media_count": 7},
    ]},
}

COLLECTED = {
    "code": 0,
    "data": {"count": 1, "list": [
        {"id": 4311, "mid": 1234, "title": "某人的合集", "media_count": 55,
         "cover": "http://i0.hdslb.com/bfs/x.jpg", "upper": {"name": "某 UP"}},
    ]},
}

FOLLOW = {
    "code": 0,
    "data": {"total": 1, "list": [
        {"season_id": 26800, "title": "巴啦啦小魔仙", "season_type_name": "番剧",
         "areas": [{"name": "中国"}], "new_ep": {"index_show": "更新至第 52 话"},
         "progress": "看到第 3 话", "cover": "http://i0.hdslb.com/bfs/y.jpg"},
    ]},
}

class TestNormalize:

    def test_created_folders_point_at_their_favlist_page(self):
        entries = favorites.normalize_entries("favorite", CREATED)

        assert [e["title"] for e in entries] == ["默认收藏夹", "稍后细看"]
        assert entries[0]["url"] == "https://space.bilibili.com/3690988658755978/favlist?fid=3706531"
        assert entries[0]["count"] == 128
        # 收藏夹接口不返回封面，页面拿首字占位 —— 这里就该是空串，不是 None
        assert entries[0]["cover"] == ""

    def test_subscription_keeps_the_owner(self):
        """订阅来的合集常常与自己无关，副标题要标出作者，否则认不出是谁的"""
        entry = favorites.normalize_entries("subscription", COLLECTED)[0]

        assert entry["url"] == "https://space.bilibili.com/1234/lists/4311?type=season"
        assert entry["desc"] == "某 UP"
        assert entry["cover"].endswith("x.jpg")

    def test_follow_prefers_the_update_progress(self):
        entry = favorites.normalize_entries("follow", FOLLOW)[0]

        assert entry["url"] == "https://www.bilibili.com/bangumi/play/ss26800"
        # 有「更新至第 N 话」时正文先给这个，再补「看到第几话」
        assert entry["desc"] == "更新至第 52 话 · 看到第 3 话"

    def test_follow_falls_back_to_type_and_area(self):
        """还没更新也没进度的新番，至少要能看出是什么类型、哪国的"""
        payload = {"code": 0, "data": {"list": [
            {"season_id": 1, "title": "某番", "season_type_name": "国创", "areas": [{"name": "中国"}]},
        ]}}

        assert favorites.normalize_entries("follow", payload)[0]["desc"] == "国创 · 中国"

    def test_drops_entries_that_are_not_clickable(self):
        """
        title 与 url 都拼不出来的条目要丢掉

        那种卡片点不动，留在页面上只会让人以为功能坏了
        """
        payload = {"code": 0, "data": {"list": [
            {"title": "缺 id 与 mid 的收藏夹"},
            {"id": 1, "title": "只有 id、拼不出地址的"},
            {"id": 1, "mid": 9, "title": "好的", "media_count": 3},
        ]}}

        assert [e["title"] for e in favorites.normalize_entries("favorite", payload)] == ["好的"]

    def test_survives_a_payload_without_a_list(self):
        for payload in (None, [], {}, {"data": None}, {"data": {}}, {"data": {"list": None}},
                        {"data": {"list": [None, 3, "x"]}}):
            assert favorites.normalize_entries("favorite", payload) == []

    def test_survives_a_wrong_type_in_a_field(self):
        """media_count 是字符串 / 空值都不该炸，最多显示成 0"""
        payload = {"code": 0, "data": {"list": [
            {"id": 5, "mid": 9, "title": "甲", "media_count": "12"},
            {"id": 6, "mid": 9, "title": "乙"},
        ]}}

        counts = [e["count"] for e in favorites.normalize_entries("favorite", payload)]

        assert counts == [12, 0]

class TestUrl:

    def test_created_folders_ask_for_the_whole_list_at_once(self):
        url = favorites.favorites_url("favorite", 42)

        assert url.startswith(favorites.FAVORITE_CREATED_API)
        assert "up_mid=42" in url
        # list-all 没有分页参数，塞了反而是错的
        assert "pn=" not in url

    def test_subscription_carries_the_page(self):
        url = favorites.favorites_url("subscription", 42, page = 3)

        assert "pn=3" in url and "up_mid=42" in url

    def test_follow_goes_through_the_signer(self):
        """签名是注入的：真签名依赖 config 里那两个密钥，测试不必联网去补"""
        seen = {}

        def sign(params):
            seen.update(params)

            return "wts=1&w_rid=abc"

        url = favorites.favorites_url("follow", 42, sign = sign)

        assert url.endswith("wts=1&w_rid=abc")
        assert seen["vmid"] == 42 and seen["type"] == 1

    def test_unknown_kind_is_refused(self):
        with pytest.raises(FavoriteError):
            favorites.favorites_url("nonsense", 42)

class TestListFavorites:

    def test_not_logged_in_says_so_without_asking_bilibili(self):
        """
        没登录就不该发那次注定失败的请求

        `up_mid` 拿不到，打过去也只是换回一个 -101；直接说人话更省一次往返
        """
        called = []

        def fetch(url):
            called.append(url)

            return CREATED

        got = favorites.list_favorites("favorite", uid = "", fetch = fetch)

        assert got["ok"] is False and got["need_login"] is True
        assert "B站账号" in got["error"]
        assert called == []

    def test_shapes_the_entries_and_the_label(self):
        got = favorites.list_favorites("favorite", uid = 42, fetch = lambda url: CREATED)

        assert got["ok"] is True
        assert got["label"] == "收藏夹"
        assert len(got["entries"]) == 2
        assert got["has_more"] is False

    def test_bilibili_error_code_becomes_a_sentence(self):
        got = favorites.list_favorites("favorite", uid = 42,
                                       fetch = lambda url: {"code": -101, "message": "账号未登录"})

        assert got["ok"] is False
        assert got["need_login"] is True
        assert "扫码登录" in got["error"]

    def test_unknown_error_code_keeps_the_original_message(self):
        got = favorites.list_favorites("favorite", uid = 42,
                                       fetch = lambda url: {"code": -412, "message": "请求被拦截"})

        assert got["ok"] is False
        assert got["error"] == "请求被拦截"

    def test_network_failure_is_reported_not_raised(self):
        """连不上 B站是用户网络的问题，页面要能就地提示，而不是 500"""
        def fetch(url):
            raise OSError("network unreachable")

        got = favorites.list_favorites("favorite", uid = 42, fetch = fetch)

        assert got["ok"] is False
        assert "OSError" in got["error"]

    def test_a_non_json_payload_is_reported(self):
        got = favorites.list_favorites("favorite", uid = 42, fetch = lambda url: "<html>")

        assert got["ok"] is False

    def test_follow_paging_is_reported(self):
        """
        追番接口给 total，页面据此知道还有没有下一页

        签名要注入：真签名依赖 config 里那两个密钥，拿不到会去补取（联网）——
        这条用例不该因为跑测试的机器能不能出网而变红
        """
        payload = {"code": 0, "data": {"total": 100, "list": FOLLOW["data"]["list"]}}

        got = favorites.list_favorites("follow", uid = 42, page = 1,
                                       fetch = lambda url: payload,
                                       sign = lambda params: "wts=1&w_rid=x")

        assert got["has_more"] is True

    def test_follow_reports_a_readable_error_when_signing_is_impossible(self):
        """拿不到签名密钥时要给人话，而不是把 RuntimeError 的名字甩出去"""
        payload = {"code": 0, "data": {"total": 1, "list": FOLLOW["data"]["list"]}}

        def sign(params):
            raise RuntimeError("WBI_KEY_UNAVAILABLE")

        got = favorites.list_favorites("follow", uid = 42, fetch = lambda url: payload, sign = sign)

        assert got["ok"] is False
        assert "签名密钥" in got["error"]

    def test_unknown_kind_is_refused_here_too(self):
        with pytest.raises(FavoriteError):
            favorites.list_favorites("nonsense", uid = 42, fetch = lambda url: CREATED)

class TestDirectParseKinds:
    """
    稍后再看 / 历史记录：桌面端就不出列表，点一下把自定义地址丢给解析器
    （gui/component/widget/flyout.py:246,253）。这一页照做，所以这里守的是
    "这两类不该去取列表"以及"回给前端的地址真的能被解析器认出来"。
    """

    def test_the_page_offers_five_categories_in_a_fixed_order(self):
        """顺序就是 tab 顺序，改这里等于改页面。五个都不能少"""
        assert favorites.KINDS == ("favorite", "subscription", "follow", "watch_later", "history")

    def test_every_category_has_a_label(self):
        """少一个 label 页面会显示成英文 key，静默难看"""
        for kind in favorites.KINDS:
            assert favorites.KIND_LABELS.get(kind), kind

    def test_only_the_first_three_have_list_apis(self):
        assert favorites.LIST_KINDS == ("favorite", "subscription", "follow")
        assert set(favorites.DIRECT_PARSE_KINDS) == {"watch_later", "history"}

    def test_watch_later_answers_with_a_parse_target_not_a_list(self):
        called = []

        def fetch(url):
            called.append(url)

            raise AssertionError("稍后再看不该去取列表")

        got = favorites.list_favorites("watch_later", uid = 42, fetch = fetch)

        assert got["ok"] is True
        assert got["direct_parse"] is True
        assert got["parse_url"] == "bili23://watch_later"
        assert got["label"] == "稍后再看"
        # 一张卡片都不该有：前端据此知道要直接铺条目
        assert got["entries"] == []
        assert called == []

    def test_history_answers_the_same_way(self):
        got = favorites.list_favorites("history", uid = 42, fetch = lambda url: {})

        assert got["direct_parse"] is True
        assert got["parse_url"] == "bili23://history"

    def test_direct_parse_does_not_need_a_logged_in_uid(self):
        """
        没登录也照回这段元数据 —— 登录与否由解析那一步管
        （`ParserBase.check_login`，未登录会抛一句人话）。

        在这里多查一次，只会让"没登录"这一个症状有两个出处，还多一次注定失败的往返
        """
        got = favorites.list_favorites("watch_later", uid = "", fetch = lambda url: CREATED)

        assert got["ok"] is True and got["direct_parse"] is True

    def test_the_parse_targets_are_addresses_the_parsers_actually_accept(self):
        """
        🔴 这条是整个文件里最值钱的一条

        `bili23://watch_later` 这个写法在这里是**硬编码的字符串**，而解析器那边
        认的是 util/common/data/url_pattern.py 里的另一份正则。两处对不上时，
        症状是"点了稍后再看，页面报一句 'not a recognized Bilibili link'" ——
        既不报错在启动时，也不指向任何一处代码。所以直接对着注册表验
        """
        accepted = {name for name, pattern in url_patterns}

        for kind, address in favorites.DIRECT_PARSE_KINDS.items():
            hits = [
                name for name, pattern in url_patterns
                if pattern.search(address)
            ]

            assert hits, "解析器不认这个地址：{0}（{1}）".format(address, kind)
            # 认它的那条必须正好是这一类，认成别的解析器等于解析错东西
            assert kind in accepted and kind in hits, "{0} 落到了 {1}".format(address, hits)

    def test_favorites_url_refuses_a_kind_that_has_no_list(self):
        """万一有人绕过 list_favorites 开头那个分支，报错要指出该去解析哪个地址"""
        with pytest.raises(FavoriteError) as error:
            favorites.favorites_url("history", 42)

        assert "bili23://history" in str(error.value)
