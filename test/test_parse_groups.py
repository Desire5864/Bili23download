"""
util/mcp/tools/parse.py —— 解析结果的分组层级与批量解析的入口校验

面板要把「电视剧 / 西游记 → 章节 / 正片 → N 条」画成可折叠的分组，而不是把
157 条摊成一片 —— 摊平之后「正片」与「相关推荐」混在一起，该不该下根本没法判断。
分组的数据源就是解析树本身：外面还包着一层不可见的根（`update_episode_list`
特意包的，免得顶层节点信息丢失），屏幕上显示的第一行是它的第一个子节点。

守三条：
  · 树里**只透出 id**，条目标题一律走 episodes 那条平表（同一份数据不进两次负载）；
  · 空章节不占一行（番剧的「UP主陪你看」被剔除后就只剩个空壳）；
  · 取不到树、数据是脏的，退化成「没有分组」，**绝不能**让整次解析失败。

批量解析另有一条底线：解析器类型由**第一条**链接决定，混着放别的类型会拿同一个
解析器去解析后面每一条，静默产出一片错数据。所以入口就得拒收。
"""

from util.mcp.tools import parse as parse_tool

import pytest


class Node:
    """够用的 TreeItem 替身：_group_tree 只用到 title / number / children"""

    def __init__(self, number = "", title = "", children = ()):
        self.number = number
        self.title = title
        self.children = list(children)

    def count(self):
        return len(self.children)

    def child(self, row):
        return self.children[row]


class FakeParseList:
    def __init__(self, root):
        self._model = type("FakeModel", (), {"root_node": root})()


class FakeInterface:
    def __init__(self, root):
        self.parse_list = FakeParseList(root)


def bangumi_tree():
    """
    番剧的真实形状（西游记那种）：

      包装根（不可见）→ 电视剧 / 西游记 → 正片   → 3 条
                                      → 相关推荐 → 2 条

    返回 (包装根, 叶子列表)。叶子顺序就是面板上从上到下的顺序
    """
    main_eps = [Node("1", "第一集"), Node("2", "第二集"), Node("3", "第三集")]
    related_eps = [Node("4", "相关一"), Node("5", "相关二")]

    root = Node("电视剧", "西游记", [
        Node("章节", "正片", main_eps),
        Node("章节", "相关推荐", related_eps),
    ])

    return Node("", "", [root]), main_eps + related_eps


def flatten_ids(group):
    """按面板的渲染顺序取出所有叶子 id"""
    ids = list(group["episode_ids"])

    for child in group["children"]:
        ids.extend(flatten_ids(child))

    return ids


class TestGroupTree:
    def test_the_sections_become_nested_nodes(self):
        wrapper, items = bangumi_tree()

        group = parse_tool._group_tree(wrapper.child(0), parse_tool.build_item_index(items))

        assert group["number"] == "电视剧"
        assert group["title"] == "西游记"
        assert [section["title"] for section in group["children"]] == ["正片", "相关推荐"]
        assert group["children"][0]["episode_ids"] == ["1", "2", "3"]
        assert group["children"][1]["episode_ids"] == ["4", "5"]

        # 条目挂在章节下面，没有既算进章节又算进根节点
        assert group["episode_ids"] == []

    def test_every_leaf_appears_exactly_once_in_index_order(self):
        wrapper, items = bangumi_tree()

        group = parse_tool._group_tree(wrapper.child(0), parse_tool.build_item_index(items))

        assert flatten_ids(group) == ["1", "2", "3", "4", "5"]

    def test_item_titles_stay_out_of_the_tree(self):
        """条目标题是 episodes 那条平表的事，在树里再来一份只是让负载翻倍"""
        wrapper, items = bangumi_tree()

        group = parse_tool._group_tree(wrapper.child(0), parse_tool.build_item_index(items))

        titles = [group["title"]] + [
            node["title"]
            for section in group["children"]
            for node in (section,)
        ]

        assert "第一集" not in titles
        assert "相关一" not in titles

    def test_an_empty_section_does_not_take_a_row(self):
        """
        「UP主陪你看」这一类没有 bvid / cid，解析侧本就把整个章节剔掉；
        剔掉之后剩下的空壳节点不该在面板上占一行空标题
        """
        root = Node("电视剧", "西游记", [
            Node("章节", "正片", [Node("1", "第一集")]),
            Node("章节", "UP主陪你看", []),
        ])

        group = parse_tool._group_tree(root, parse_tool.build_item_index([root.children[0].children[0]]))

        assert [section["title"] for section in group["children"]] == ["正片"]

    def test_a_single_video_is_one_group_with_one_leaf(self):
        leaf = Node("1", "标题")
        root = Node("投稿视频", "", [leaf])

        group = parse_tool._group_tree(root, parse_tool.build_item_index([leaf]))

        assert group["episode_ids"] == ["1"]
        assert group["children"] == []

    def test_a_deeper_tree_keeps_every_level(self):
        """收藏夹 / 合集是三层结构，多出来的那层不该被拍平"""
        leaf = Node("1", "视频一")
        collection = Node("合集", "合辑名", [leaf])
        root = Node("收藏夹", "默认收藏夹", [collection])

        group = parse_tool._group_tree(root, parse_tool.build_item_index([leaf]))

        assert group["children"][0]["title"] == "合辑名"
        assert flatten_ids(group) == ["1"]


class TestCollectGroup:
    def test_the_invisible_wrapper_root_is_skipped(self):
        """屏幕上第一行是包装根的子节点，包装根本身不显示，别把它当成组标题"""
        wrapper, items = bangumi_tree()

        group = parse_tool._collect_group(FakeInterface(wrapper), parse_tool.build_item_index(items))

        assert group["title"] == "西游记"

    def test_an_empty_tree_gives_no_group(self):
        assert parse_tool._collect_group(FakeInterface(Node("", "", [])), {}) == {}

    def test_a_visible_root_without_items_gives_no_group(self):
        wrapper = Node("", "", [Node("电视剧", "西游记", [])])

        assert parse_tool._collect_group(FakeInterface(wrapper), {}) == {}

    def test_a_parse_list_without_a_model_gives_no_group(self):
        """_model 是私有属性，取不到就退化成"没有分组"，而不是抛出去"""

        class Bare:
            pass

        class Interface:
            parse_list = Bare()

        assert parse_tool._collect_group(Interface(), {}) == {}


class FakeRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, name, title, description, input_schema, handler):
        self.tools[name] = {
            "name": name,
            "title": title,
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }


class TestBatchRegistration:
    def test_the_tool_is_registered_and_wants_an_array(self):
        registry = FakeRegistry()

        parse_tool.register(registry)

        assert "parse_batch" in registry.tools

        schema = registry.tools["parse_batch"]["input_schema"]

        assert schema["required"] == ["urls"]
        assert schema["properties"]["urls"]["type"] == "array"
        assert schema["additionalProperties"] is False

    def test_the_limit_in_the_description_matches_the_guard(self):
        """描述里报的数字与真正卡住的那个常量必须是同一个，否则模型会照错的数去发"""
        registry = FakeRegistry()

        parse_tool.register(registry)

        assert str(parse_tool.BATCH_MAX_LINKS) in registry.tools["parse_batch"]["description"]

    def test_the_description_only_promises_upload_videos(self):
        registry = FakeRegistry()

        parse_tool.register(registry)

        description = registry.tools["parse_batch"]["description"]

        assert "av / BV" in description
        assert "Only upload videos" in description


class TestBatchTarget:
    @pytest.mark.parametrize("url", [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "BV1xx411c7mD",
        "av12345678",
    ])
    def test_upload_video_links_are_the_supported_kind(self, url):
        assert parse_tool._batch_parse_target(url) in parse_tool.BATCH_PARSER_TYPES

    @pytest.mark.parametrize("url", [
        "https://www.bilibili.com/bangumi/play/ss41349",
        "https://space.bilibili.com/1234567/favlist",
        "https://www.bilibili.com/cheese/play/ss100",
        "bili23://history",
        "bili23://watch_later",
        "am123456",
    ])
    def test_other_kinds_are_not_supported(self, url):
        assert parse_tool._batch_parse_target(url) not in parse_tool.BATCH_PARSER_TYPES

    def test_an_unrecognized_link_targets_nothing(self):
        assert parse_tool._batch_parse_target("https://example.com/x") == ""


def ok_result():
    return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class TestBatchGuard:
    def test_a_multiline_block_is_split_and_trimmed(self, monkeypatch):
        """面板给的是 textarea：空行与两边的空格都不算链接"""
        seen = {}

        def fake_locked(urls, arguments):
            seen["urls"] = urls
            return ok_result()

        monkeypatch.setattr(parse_tool, "_parse_batch_locked", fake_locked)

        parse_tool.tool_parse_batch({"urls": "BV1xx411c7mD\n\n   BV1yy411c7mE   \n"})

        assert seen["urls"] == ["BV1xx411c7mD", "BV1yy411c7mE"]

    def test_a_json_array_is_accepted_too(self, monkeypatch):
        seen = {}

        def fake_locked(urls, arguments):
            seen["urls"] = urls
            return ok_result()

        monkeypatch.setattr(parse_tool, "_parse_batch_locked", fake_locked)

        parse_tool.tool_parse_batch({"urls": ["BV1xx411c7mD", "av12345678"]})

        assert seen["urls"] == ["BV1xx411c7mD", "av12345678"]

    def test_no_links_is_refused(self):
        result = parse_tool.tool_parse_batch({"urls": ["", "   "]})

        assert result["isError"] is True

    def test_a_missing_argument_is_refused(self):
        assert parse_tool.tool_parse_batch({})["isError"] is True

    def test_a_number_is_refused(self):
        assert parse_tool.tool_parse_batch({"urls": 42})["isError"] is True

    def test_too_many_links_are_refused(self):
        urls = [f"BV1xx411c7{i:02d}" for i in range(parse_tool.BATCH_MAX_LINKS + 1)]

        result = parse_tool.tool_parse_batch({"urls": urls})

        assert result["isError"] is True
        assert str(parse_tool.BATCH_MAX_LINKS) in result["content"][0]["text"]

    def test_a_bangumi_link_is_refused_and_named(self):
        """
        批量走的是 DynamicParser：解析器类型由第一条链接决定。混进一条番剧链接，
        后面每一条都会被拿投稿视频的解析器去解析，安安静静出一片错数据
        """
        url = "https://www.bilibili.com/bangumi/play/ss41349"

        result = parse_tool.tool_parse_batch({"urls": ["BV1xx411c7mD", url]})

        assert result["isError"] is True
        assert url in result["content"][0]["text"]

    def test_the_parse_lock_is_released_even_when_refused(self, monkeypatch):
        """拒收也要放锁，否则一次手滑的调用会把后面的解析全堵死"""
        monkeypatch.setattr(
            parse_tool, "_parse_batch_locked",
            lambda urls, arguments: ok_result(),
        )

        assert parse_tool.tool_parse_batch({"urls": ["https://example.com/x"]})["isError"] is True

        # 锁还活着的话这里会拿到"另一个解析正在跑"
        assert parse_tool._parse_lock.acquire(timeout = 1.0) is True
        parse_tool._parse_lock.release()
