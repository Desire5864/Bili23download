"""
util/mcp/tools/parse.py —— 解析结果里的「同系列还有哪几部」与「这是第几页」

这两段以前是丢掉的：`_do_parse` 只从 worker 的 success 里取了 category，
番剧那一整份 `season_data`（整个系列的所有季）和分页信息就烂在了返回值里。
面板要靠它们画「同系列多季」的下拉（桌面端那个能"解析好几部"的控件）
与「稍后再看 / 历史记录」的翻页按钮；模型也靠它知道"这部不止一季"。

守的是容错：脏数据只该少几个选项，**绝不能让整次解析失败** ——
把异常抛到 `_parse_url_locked` 外面，一次本来成功的解析就变成 500，那才是真的亏。
"""

from util.mcp.tools import parse as parse_tool

SEASON_EXTRA = {
    "seasons": True,
    "season_data": {
        "season_list": [
            {"title": "机智的医生生活", "season_id": 41348,
             "url": "https://www.bilibili.com/bangumi/play/ss41348"},
            {"title": "机智的医生生活 第二季", "season_id": 41349,
             "url": "https://www.bilibili.com/bangumi/play/ss41349"},
        ],
        "series_title": "机智的医生生活",
        "season_id": 41349,
    },
}

class FakeRegistry:
    """只记账，不碰真实注册表的其它工具"""

    def __init__(self):
        self.tools = {}

    def register(self, name, title, description, input_schema, handler):
        self.tools[name] = {
            "title": title,
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }

class TestSeasonData:

    def test_a_bangumi_carries_its_whole_series(self):
        got = parse_tool._season_data(SEASON_EXTRA)

        assert [s["title"] for s in got["seasons"]] == ["机智的医生生活", "机智的医生生活 第二季"]
        assert got["series_title"] == "机智的医生生活"
        assert got["current_season_id"] == 41349

    def test_the_season_id_stays_a_bilibili_number(self):
        """
        透出的 season_id 是 **B站编号**，不是 TMDB id

        项目里踩过这个坑（西游记 33622 vs 真 TMDB 13923），所以这里钉死：不做任何换算
        """
        got = parse_tool._season_data(SEASON_EXTRA)

        assert got["seasons"][1]["season_id"] == 41349
        assert got["seasons"][1]["url"].endswith("ss41349")

    def test_a_season_without_a_url_is_dropped(self):
        """没有 url 就换不过去，那种选项点了没反应 —— 不如不出现"""
        extra = {"season_data": {"season_list": [
            {"title": "没有地址的", "season_id": 1},
            {"title": "正常的", "season_id": 2, "url": "https://x/ss2"},
        ]}}

        got = parse_tool._season_data(extra)

        assert [s["title"] for s in got["seasons"]] == ["正常的"]

    def test_a_series_title_is_optional(self):
        """系列名可以缺，缺了就不透出这个键，而不是给个空串让前端画个空标题"""
        extra = {"season_data": {"season_list": [
            {"title": "甲", "url": "https://x/ss1"},
        ], "season_id": 1}}

        got = parse_tool._season_data(extra)

        assert "series_title" not in got
        assert got["seasons"][0]["title"] == "甲"

    def test_a_single_season_still_comes_through(self):
        """
        只有一季也照样透出

        "有没有得选"是界面的事（<2 就不画下拉），这里替它做决定的话，
        模型就拿不到"这部属于某个系列"这条信息了
        """
        extra = {"season_data": {"season_list": [{"title": "唯一一季", "url": "https://x/ss9"}]}}

        assert len(parse_tool._season_data(extra)["seasons"]) == 1

    def test_junk_is_ignored_not_raised(self):
        """
        脏数据只该少几个选项，不该让整次解析失败

        这些输入如果抛出去，症状是"番剧解析时好时坏"，而日志里只有一句 TypeError
        """
        for extra in (
            None, [], "x", 0,
            {},
            {"season_data": None},
            {"season_data": []},
            {"season_data": {}},
            {"season_data": {"season_list": None}},
            {"season_data": {"season_list": []}},
            {"season_data": {"season_list": [None, 3, "x", []]}},
            {"season_data": {"season_list": [{"title": "没地址"}]}},
        ):
            assert parse_tool._season_data(extra) == {}, extra

class TestPaginationData:

    def test_the_page_numbers_come_through(self):
        got = parse_tool._pagination_data({"pagination_data": {
            "current_page": 2, "total_pages": 5, "total_items": 93,
        }})

        assert got == {"current_page": 2, "total_pages": 5, "total_items": 93}

    def test_a_boolean_is_not_a_page_number(self):
        """Python 里 True == 1，会在 `isinstance(x, int)` 上蒙混过关"""
        got = parse_tool._pagination_data({"pagination_data": {
            "current_page": True, "total_pages": 5,
        }})

        assert got == {"total_pages": 5}

    def test_a_listing_without_pages_has_none(self):
        for extra in (None, [], {}, {"pagination_data": None}, {"pagination_data": []}):
            assert parse_tool._pagination_data(extra) == {}

class TestClampPage:

    def test_a_real_page_is_kept(self):
        assert parse_tool._clamp_page(3) == 3

    def test_anything_odd_falls_back_to_the_first_page(self):
        """
        第 1 页总是存在的，退回它最安全

        这里不像 limit 那样夹到今天花板：页数取决于内容，夹了反而是编造
        """
        for value in (0, -3, None, "2", 1.5, True, False, [], {}):
            assert parse_tool._clamp_page(value) == 1, value

class TestNotes:

    def test_the_series_note_says_how_many_seasons(self):
        note = parse_tool._series_note({"seasons": [{"title": "一"}, {"title": "二"}],
                                        "series_title": "某剧"})

        assert "某剧" in note and "2" in note

    def test_the_series_note_survives_a_missing_title(self):
        assert "series" in parse_tool._series_note({"seasons": [{"title": "一"}]})

    def test_the_pagination_note_is_silent_on_a_single_page(self):
        """只有一页时什么都不用说，说了反而让模型以为漏了什么"""
        assert parse_tool._pagination_note({"current_page": 1, "total_pages": 1}) == ""
        assert parse_tool._pagination_note({}) == ""

    def test_the_pagination_note_points_at_the_page_argument(self):
        note = parse_tool._pagination_note({"current_page": 2, "total_pages": 5})

        assert "2" in note and "5" in note and "page" in note

class TestSchema:

    def test_parse_url_takes_a_page(self):
        registry = FakeRegistry()
        parse_tool.register(registry)

        schema = registry.tools["parse_url"]["input_schema"]

        assert "page" in schema["properties"]
        assert schema["properties"]["page"]["minimum"] == 1
        # 只有 url 是必填的：分页是可选能力
        assert schema["required"] == ["url"]

    def test_the_description_tells_the_model_about_seasons_and_pages(self):
        """
        结构化结果不是所有客户端都交给模型，文本描述是唯一保证送达的那一份

        所以"这部还有别的季"与"还有下一页"必须写进 description，
        光放在 structuredContent 里等于没说
        """
        registry = FakeRegistry()
        parse_tool.register(registry)

        description = registry.tools["parse_url"]["description"]

        assert "seasons" in description and "current_season_id" in description
        assert "pagination" in description and "page" in description
        # 顺带把两个没有列表接口的入口也交代清楚
        assert "bili23://watch_later" in description and "bili23://history" in description
