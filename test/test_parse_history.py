"""
util/mcp/tools/history.py —— 解析记录的读写

面板「解析记录」与桌面端那颗时钟图标用的是同一份数据：`util/misc/history.py` 里
history_manager 那张表（按 URL 去重、只留最近 100 条、写入点在界面收解析结果时）。
面板里解析出来的链接本来就落进这份记录 —— 这里是把同一份数据开出来读写，
**不另建一套存储**，否则两边各记一半、谁都对不上。

测试里一律把它换成替身：真的 history_manager 一碰就建 history.db，
跑一次测试就往用户的配置目录里落一个文件。
"""

from util.mcp.tools import history as history_tool

import sys
import types

import pytest


class FakeHistoryManager:
    def __init__(self, rows = ()):
        self.rows = list(rows)
        self.deleted = []
        self.cleared = 0

    def get_history(self):
        return list(self.rows)

    def delete_history(self, history_id):
        self.deleted.append(history_id)
        self.rows = [row for row in self.rows if row[0] != history_id]

    def clear_history(self):
        self.cleared += 1
        self.rows = []


def sample_rows():
    return [
        ("id-1", "西游记", "https://www.bilibili.com/bangumi/play/ss33622", "TV", 1759500000),
        ("id-2", "", "https://www.bilibili.com/video/BV1xx411c7mD", "USER_UPLOADS", 1759400000),
    ]


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


@pytest.fixture
def manager(monkeypatch):
    fake = FakeHistoryManager(sample_rows())

    module = types.ModuleType("util.misc.history")
    module.history_manager = fake

    monkeypatch.setitem(sys.modules, "util.misc.history", module)

    # Translator 最终落在 QCoreApplication 上，而这里是从 HTTP 工作线程调工具，
    # 测试进程里没有事件循环可等 —— 就地执行。
    #
    # 签名必须与真身一致（timeout 是关键字参数，不透传给被调函数），否则替身会往
    # 无参的 label_all 里塞一个 timeout，抛 TypeError 之后被上层吞掉，
    # 于是"译名有没有取到"这件事测了个寂寞
    def inline(func, *args, timeout = 30.0, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(history_tool, "call_in_main_thread", inline)

    return fake


class TestListParseHistory:
    def test_records_carry_what_the_panel_and_the_model_need(self, manager):
        data = history_tool.tool_list_parse_history({})["structuredContent"]

        assert data["total"] == 2

        first = data["records"][0]

        assert first["history_id"] == "id-1"
        assert first["title"] == "西游记"
        assert first["url"].endswith("ss33622")
        assert first["type"] == "TV"
        assert first["created_time"] == 1759500000

    def test_the_text_names_the_newest_entries(self, manager):
        text = history_tool.tool_list_parse_history({})["content"][0]["text"]

        assert "2" in text
        assert "西游记" in text

    def test_an_untitled_record_falls_back_to_its_type_in_the_text(self, manager):
        """投稿视频那条没有标题，摘要里得拿类型顶上去，不能印一个空字符串"""
        result = history_tool.tool_list_parse_history({})

        records = result["structuredContent"]["records"]

        assert records[1]["title"] == ""

        # 译名跟着运行环境的语言走（容器里是 zh_CN），所以比的是这条记录
        # 自己给出的那个值，而不是写死的英文
        fallback = records[1]["type_label"] or records[1]["type"]

        assert fallback
        assert fallback in result["content"][0]["text"]

    def test_an_empty_history_mentions_the_setting_that_fills_it(self, manager):
        """列表空着多半不是坏了，是这个开关关着 —— 直接说出来，省一轮排查"""
        manager.rows = []

        result = history_tool.tool_list_parse_history({})

        assert result["structuredContent"]["total"] == 0
        assert "settings" in result["content"][0]["text"]

    def test_an_unknown_type_code_leaves_no_label(self, manager):
        """
        类型码翻不出来时 Translator 给的是 None（get_map_method 用的是 dict.get）。
        面板那句 `r.type_label || r.type` 就是为这个准备的
        """
        manager.rows = [("id-9", "x", "https://x", "NOPE_NOT_A_TYPE", 1)]

        record = history_tool.tool_list_parse_history({})["structuredContent"]["records"][0]

        assert not record["type_label"]

    def test_a_dead_main_thread_does_not_break_the_listing(self, manager, monkeypatch):
        """
        回主线程取译名失败，最多是少一个字段，不能把整个列表变成 500

        🔴 这条踩过一次：摘要是按 record["type_label"] 取的，而这个键只在取译名
        成功时才存在 —— 主线程忙（正卡在模态对话框上）时整个「解析记录」直接报错。
        取不到就用类型码顶上
        """

        def boom(*args, **kwargs):
            raise RuntimeError("no main thread")

        monkeypatch.setattr(history_tool, "call_in_main_thread", boom)

        result = history_tool.tool_list_parse_history({})

        assert result["isError"] is False
        assert result["structuredContent"]["total"] == 2
        assert "type_label" not in result["structuredContent"]["records"][0]

        # 摘要也得活着出来 —— 它以前正是在这里抛的 KeyError
        assert "2" in result["content"][0]["text"]


class TestDeleteParseHistory:
    def test_an_unknown_id_is_refused_and_nothing_is_touched(self, manager):
        result = history_tool.tool_delete_parse_history({"history_id": "nope"})

        assert result["isError"] is True
        assert manager.deleted == []

    def test_a_missing_id_is_refused(self, manager):
        assert history_tool.tool_delete_parse_history({})["isError"] is True
        assert manager.deleted == []

    def test_a_blank_id_is_refused(self, manager):
        assert history_tool.tool_delete_parse_history({"history_id": "   "})["isError"] is True
        assert manager.deleted == []

    def test_a_known_id_is_deleted_and_the_rest_survives(self, manager):
        result = history_tool.tool_delete_parse_history({"history_id": "id-2"})

        assert result["isError"] is False
        assert manager.deleted == ["id-2"]
        assert result["structuredContent"]["deleted"] == "id-2"
        assert result["structuredContent"]["total"] == 1


class TestClearParseHistory:
    def test_clear_reports_how_many_it_removed(self, manager):
        result = history_tool.tool_clear_parse_history({})

        assert manager.cleared == 1
        assert result["structuredContent"] == {"removed": 2, "total": 0}

    def test_clearing_an_empty_history_is_harmless(self, manager):
        manager.rows = []

        assert history_tool.tool_clear_parse_history({})["structuredContent"]["removed"] == 0


class TestRegistration:
    def test_all_three_tools_are_registered(self):
        registry = FakeRegistry()

        history_tool.register(registry)

        assert set(registry.tools) == {
            "list_parse_history", "delete_parse_history", "clear_parse_history"
        }

    def test_delete_requires_a_history_id(self):
        registry = FakeRegistry()

        history_tool.register(registry)

        assert registry.tools["delete_parse_history"]["input_schema"]["required"] == ["history_id"]

    def test_the_clear_description_says_it_cannot_be_undone(self):
        registry = FakeRegistry()

        history_tool.register(registry)

        assert "cannot be undone" in registry.tools["clear_parse_history"]["description"]

    def test_the_list_description_names_both_handoffs(self):
        """记录里那串 url 要能直接喂回 parse_url，history_id 要能喂给删除 —— 得写出来"""
        registry = FakeRegistry()

        history_tool.register(registry)

        description = registry.tools["list_parse_history"]["description"]

        assert "parse_url" in description
        assert "delete_parse_history" in description

    def test_the_registry_actually_wires_the_module_in(self):
        """注册表漏挂一个模块，工具全都在、就是调不到 —— 症状是 404，很难猜"""
        from util.mcp.tools import build_registry

        names = {schema["name"] for schema in build_registry().list_schemas()}

        assert {
            "list_parse_history", "delete_parse_history", "clear_parse_history"
        } <= names
