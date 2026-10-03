"""
解析记录（解析历史的读写）

桌面端那个「解析记录」对话框背后是 `util/misc/history.py` 里的 history_manager：
一张 SQLite 表，按 URL 去重、只留最近 100 条、写入点在 `ParseInterface.on_update_parse_list`
（受 config.parse_history 控制，默认开）。面板走的是同一条解析链路，因此
**面板里解析出来的链接本来就进这份记录** —— 这里只是把同一份数据开出来读写，
不另建一套存储，否则两边会各记一半、对不上
"""

from ..invoke import call_in_main_thread

from . import text_result, error_result

def _history_records() -> list:
    from ...misc.history import history_manager

    return [
        {
            "history_id": str(history_id),
            "title": title or "",
            "url": url or "",
            "type": type_name or "",
            "created_time": int(created_time or 0),
        }
        for history_id, title, url, type_name, created_time in history_manager.get_history()
    ]

def _with_type_labels(records: list) -> list:
    """
    给每条记录补一个能直接显示的类型名

    必须回主线程：Translator 最终落在 QCoreApplication.translate 上，而这个工具
    是从 HTTP 工作线程调进来的。取不到就退回类型码本身，不让它把整次调用带崩
    """
    def label_all():
        from ...common.translator import Translator

        for record in records:
            try:
                record["type_label"] = Translator.EPISODE_TYPE(record["type"])

            except Exception:
                record["type_label"] = record["type"]

        return records

    try:
        return call_in_main_thread(label_all, timeout = 5.0)

    except Exception:
        return records

def _summary(records: list) -> str:
    if not records:
        return (
            "Parse history is empty. It only fills up while 'Save parse history' is enabled "
            "in the app's settings; keep the newest 100 entries at most."
        )

    # 🔴 不能直接取 record["type_label"]：回主线程取译名失败时那个键根本不存在，
    # 一取就是 KeyError，本来只是"少一个字段"的降级会变成整个工具 500
    titles = [
        record["title"] or record.get("type_label") or record["type"] or "(untitled)"
        for record in records[:5]
    ]

    return (
        f"{len(records)} parse history record(s), newest first. The most recent are: "
        + "; ".join(titles)
        + "."
    )

def tool_list_parse_history(arguments: dict) -> dict:
    records = _with_type_labels(_history_records())

    return text_result(_summary(records), {
        "total": len(records),
        "records": records,
    })

def tool_delete_parse_history(arguments: dict) -> dict:
    history_id = str(arguments.get("history_id") or "").strip()

    if not history_id:
        return error_result("The 'history_id' argument is required.")

    from ...misc.history import history_manager

    known = {record["history_id"] for record in _history_records()}

    if history_id not in known:
        return error_result(f"No parse history record with history_id '{history_id}'.")

    history_manager.delete_history(history_id)

    return text_result("Deleted one parse history record.", {
        "deleted": history_id,
        "total": len(known) - 1,
    })

def tool_clear_parse_history(arguments: dict) -> dict:
    from ...misc.history import history_manager

    removed = len(_history_records())

    history_manager.clear_history()

    return text_result(f"Cleared {removed} parse history record(s).", {
        "removed": removed,
        "total": 0,
    })

def register(registry):
    registry.register(
        name = "list_parse_history",
        title = "List Parse History",
        description = (
            "List the links that have been parsed recently, newest first. This is the panel's "
            "'parse history' dialog; entries are written by the normal parse flow, including "
            "parses started from the panel or by parse_batch, and at most the newest 100 are kept. "
            "Each record has a 'url' you can hand back to parse_url and a 'history_id' you can "
            "pass to delete_parse_history."
        ),
        input_schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler = tool_list_parse_history,
    )

    registry.register(
        name = "delete_parse_history",
        title = "Delete One Parse History Record",
        description = (
            "Delete a single parse history entry by its 'history_id' (from list_parse_history). "
            "Other entries are untouched."
        ),
        input_schema = {
            "type": "object",
            "properties": {
                "history_id": {
                    "type": "string",
                    "description": "The history_id of the record to delete.",
                },
            },
            "required": ["history_id"],
            "additionalProperties": False,
        },
        handler = tool_delete_parse_history,
    )

    registry.register(
        name = "clear_parse_history",
        title = "Clear Parse History",
        description = "Delete every parse history record. The action cannot be undone.",
        input_schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler = tool_clear_parse_history,
    )
