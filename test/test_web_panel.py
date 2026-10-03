"""
util/web/server.py —— Web 面板的令牌校验与路由。

起一个真实的 HTTP 服务在随机端口上测：这些逻辑的价值全在"浏览器与它之间
真的发生了什么"，用 mock 替掉 socket 就只能测到自己写的假象。

工具注册表换成假的：本文件不关心解析与下载，只关心 HTTP 这一层。
（真实的注册表会拉起 Qt 主线程调用，那是 MCP 侧测试的职责。）
"""

from util.web.server import (
    API_PATH, PANEL_BILI_LOGOUT_PATH, PANEL_CONFIG_EXPORT_PATH, PANEL_CONFIG_IMPORT_PATH,
    PANEL_CONFIG_RESET_PATH, PANEL_DONE_CLEAR_PATH, PANEL_FAVORITES_PATH, PANEL_IDENTIFY_PATH,
    PANEL_IDENTIFY_PREVIEW_PATH, PANEL_IDENTIFY_SAVE_PATH, PANEL_IDENTIFY_TMDB_PATH,
    PANEL_LOGIN_PATH, PANEL_LOGS_PATH,
    PANEL_LOGOUT_PATH, PANEL_MCP_TOKEN_PATH, PANEL_NAMING_PATH, PANEL_NAMING_PREVIEW_PATH,
    PANEL_NAMING_SAVE_PATH, PANEL_NOTIFY_PATH, PANEL_NOTIFY_SAVE_PATH, PANEL_NOTIFY_TEST_PATH,
    PANEL_PASSWORD_PATH, PANEL_QR_POLL_PATH, PANEL_SESSION_PATH,
    PANEL_SETTINGS_PATH, PANEL_SETTINGS_SAVE_PATH, PANEL_SYNC_CONFIG_PATH,
    PANEL_SYNC_CONFIG_SAVE_PATH, PANEL_SYNC_PATH, SESSION_COOKIE, WECOM_CALLBACK_PATH,
    PanelHTTPServer, WebPanelHandler, _tail_lines, parse_query, parse_query_token
)
from util.web import favorites
from util.web import identify
from util.web import naming
from util.web import server as web_server
from util.web.auth import (
    LoginThrottle, PanelCredentials, SessionStore, ensure_credentials, hash_password,
    verify_password
)
from util.web.bili_login import parse_login_url, render_svg

from util.common.config import DefaultValue
from util.common.data.naming_convention import convention_type_map
from util.common.naming_alias import (
    AliasError, apply_to_data, match_alias, normalize_entries, normalize_entry,
    shadowed_indexes,
)
from util.common import notify
from util.common import wecom_callback
from util.common.notify import NotifyError
from util.clouddrive import CloudDriveAuthError, CloudDriveError
from util.clouddrive import clouddrive_pb2

from copy import deepcopy
from http.client import HTTPConnection
from threading import Thread
from urllib.parse import quote
import base64
import importlib
import json
import logging
import re
import struct
import time
import pytest

TOKEN = "test-token-abcdefghijklmnop"

TEST_USER = "admin"
TEST_PASS = "secret-pass"

class FakeRegistry:
    def __init__(self, known = ("list_tasks",)):
        self.known = set(known)
        self.calls = []

    def has(self, name):
        return name in self.known

    def call(self, name, arguments):
        self.calls.append((name, arguments))

        return {"tool": name, "arguments": arguments}

class FakeItem:
    def __init__(self, key, default = "", group = "Fake"):
        self.key = key
        self.name = key          # 与 qfluentwidgets ConfigItem 对齐：条目名走 .name
        self.default = default
        self.defaultValue = default
        # 真配置项有 group，配置导出/导入按 (group, name) 索引，fake 也要长着它
        self.group = group

class FakeConfig:
    """只认对象的字典，避开真实配置会落盘这件事"""

    def __init__(self):
        self.store = {}

        self.web_panel_username = FakeItem("username", "admin")
        # 敏感项的 group 必须与真配置一致：导出/导入的脱敏判定按 (group, name) 走
        self.web_panel_password = FakeItem("web_panel_password", "", group = "Web Panel")
        self.web_panel_token = FakeItem("web_panel_token", "panel-token-live", group = "Web Panel")

        # B 站会话 Cookie：导出脱敏清单里的四个
        self.SESSDATA = FakeItem("SESSDATA", "sess-live-secret", group = "Cookie")
        self.bili_jct = FakeItem("bili_jct", "jct-live-secret", group = "Cookie")
        self.DedeUserID = FakeItem("DedeUserID", "42", group = "Cookie")
        self.DedeUserID__ckMd5 = FakeItem("DedeUserID__ckMd5", "md5", group = "Cookie")

        # 设置页会读这些项的默认值，缺了会 AttributeError
        self.download_parallel = FakeItem("download_parallel", 1)
        self.merge_parallel = FakeItem("merge_parallel", 2)
        self.download_thread = FakeItem("download_thread", 4)
        self.speed_limit_enabled = FakeItem("speed_limit_enabled", False)
        self.speed_limit_rate = FakeItem("speed_limit_rate", 10.0)
        self.auto_retry_enabled = FakeItem("auto_retry_enabled", True)
        self.auto_retry_max_count = FakeItem("auto_retry_max_count", 5)

        self.download_path = FakeItem("download_path", "/downloads")

        # 「其他高级设置」里那个解析开关：面板的批量解析弹窗要靠它给复选框做回填
        self.auto_add_to_download_list = FakeItem("auto_add_to_download_list", False)

        # 「媒体选项」五个：四个开关 + 一个存枚举的
        self.download_video_stream = FakeItem("download_video_stream", True)
        self.download_audio_stream = FakeItem("download_audio_stream", True)
        self.merge_video_audio = FakeItem("merge_video_audio", True)
        self.keep_original_files = FakeItem("keep_original_files", False)
        # 存的是枚举成员，面板侧会转成下标显示
        self.keep_original_files_type = FakeItem("keep_original_files_type", 0)

        # 「画质、音质和编码优先级」三个
        self.video_quality_priority = FakeItem("video_quality_priority", [80, 64])
        self.audio_quality_priority = FakeItem("audio_quality_priority", [30251, 30250])
        self.video_codec_priority = FakeItem("video_codec_priority", [7, 12])

        # 「下载格式」两个
        self.video_container = FakeItem("video_container", 0)
        self.m4a_to_mp3 = FakeItem("m4a_to_mp3", False)

        # 「MCP 服务器」：开关与端口在设置页可改，令牌走独立接口
        self.mcp_enabled = FakeItem("mcp_enabled", False)
        self.mcp_port = FakeItem("mcp_port", 23330)
        self.mcp_token = FakeItem("mcp_token", "mcp-token-live", group = "MCP")

        # 「通知」组：通知页与下载流程都要读。两个凭据的 group 必须与真配置一致 ——
        # 导出脱敏按 (group, name) 判定，group 写错等于让凭据漏进导出的 JSON
        self.notification_wecom_enabled = FakeItem("notification_wecom_enabled", False, group = "Notification")
        self.notification_wecom_corp_id = FakeItem("notification_wecom_corp_id", "", group = "Notification")
        self.notification_wecom_agent_id = FakeItem("notification_wecom_agent_id", "", group = "Notification")
        self.notification_wecom_secret = FakeItem("notification_wecom_secret", "", group = "Notification")
        self.notification_wecom_touser = FakeItem("notification_wecom_touser", "", group = "Notification")
        self.notification_wecom_api_base = FakeItem("notification_wecom_api_base", "", group = "Notification")
        # 回调（接收消息）那四个。两个凭据同样是敏感键，group 必须与真配置一致
        self.notification_wecom_callback_enabled = FakeItem("notification_wecom_callback_enabled", False, group = "Notification")
        self.notification_wecom_callback_token = FakeItem("notification_wecom_callback_token", "", group = "Notification")
        self.notification_wecom_aes_key = FakeItem("notification_wecom_aes_key", "", group = "Notification")
        self.notification_wecom_callback_base = FakeItem("notification_wecom_callback_base", "", group = "Notification")
        self.notification_telegram_enabled = FakeItem("notification_telegram_enabled", False, group = "Notification")
        self.notification_telegram_token = FakeItem("notification_telegram_token", "", group = "Notification")
        self.notification_telegram_chat_id = FakeItem("notification_telegram_chat_id", "", group = "Notification")
        self.notification_proxy = FakeItem("notification_proxy", "", group = "Notification")
        self.notification_on_complete = FakeItem("notification_on_complete", True, group = "Notification")
        self.notification_on_fail = FakeItem("notification_on_fail", False, group = "Notification")

        # 「CDN 设置」与「代理设置」
        from util.common.enum import Area, ProxyMode

        self.prefer_cdn_server_provider = FakeItem("prefer_cdn_server_provider", True)
        self.area = FakeItem("area", Area.CN)
        # 两份列表各给两项就够验顺序与按地区落键了
        self.cn_cdn_server_list = FakeItem("cn_cdn_server_list", [
            {"host": "upos-sz-mirror08c.bilivideo.com", "provider": "HUAWEI"},
            {"host": "upos-sz-mirrorali.bilivideo.com", "provider": "ALIBABA"},
        ])
        self.ov_cdn_server_list = FakeItem("ov_cdn_server_list", [
            {"host": "upos-mirror_ov.bilivideo.com", "provider": "HUAWEI"},
        ])
        self.proxy_mode = FakeItem("proxy_mode", ProxyMode.SYSTEM)
        self.proxy_server = FakeItem("proxy_server", "")
        self.proxy_port = FakeItem("proxy_port", 80)
        self.proxy_uname = FakeItem("proxy_uname", "")
        self.proxy_password = FakeItem("proxy_password", "proxy-pass-live", group = "Advanced")

        # 「其他高级设置」
        self.user_agent = FakeItem("user_agent", "Mozilla/5.0 (Test)", group = "Advanced")

        # 「命名规则」页：默认值取真实的内置规则表 —— 页面上的往返（读 → 改 →
        # 存回）只有对着真表跑才有意义，自己编一张短表验不出类型覆盖
        self.naming_rule_list = FakeItem(
            "naming_rule_list", deepcopy(DefaultValue.naming_rule_list), group = "File Naming"
        )

        # 「名称识别」页：出厂是空表，与 DefaultValue 一致
        self.naming_alias_list = FakeItem(
            "naming_alias_list", deepcopy(DefaultValue.naming_alias_list), group = "File Naming"
        )

    def get(self, item):
        return self.store.get(item.key, item.default)

    def set(self, item, value):
        self.store[item.key] = value

@pytest.fixture
def panel():
    registry = FakeRegistry()

    server = PanelHTTPServer(
        ("127.0.0.1", 0),
        WebPanelHandler,
        registry,
        TOKEN,
        # 用 1 轮 PBKDF2：真实的 12 万轮会让每个用例白白多花几十毫秒，
        # 而这里要验的是"密码对得上/对不上"，不是哈希强度
        credentials = PanelCredentials(TEST_USER, hash_password(TEST_PASS, rounds = 1)),
        sessions = SessionStore(),
    )

    Thread(target = server.serve_forever, daemon = True).start()

    server.registry = registry

    yield server

    server.shutdown()
    server.server_close()

def request(server, method, path, body = None, token = None, cookie = None):
    status, raw, _ = full_request(server, method, path, body = body, token = token, cookie = cookie)

    return status, raw

def full_request(server, method, path, body = None, token = None, cookie = None, extra = None):
    """
    带响应头的版本。登录与登出要看 Set-Cookie，二元组不够用

    extra 用于塞 X-Forwarded-Proto 这类头 —— 反代才会写，本地测安全头时要自己造
    """
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout = 5)

    try:
        headers = {}

        if token is not None:
            headers["X-Panel-Token"] = token

        if cookie is not None:
            headers["Cookie"] = cookie

        if extra:
            headers.update(extra)

        connection.request(method, path, body = body, headers = headers)

        response = connection.getresponse()

        return response.status, response.read(), dict(response.getheaders())

    finally:
        connection.close()

def login(server, username = TEST_USER, password = TEST_PASS):
    """登录并返回可用的 Cookie 头；失败时返回空串"""
    body = json.dumps({"username": username, "password": password}).encode()

    status, _, headers = full_request(server, "POST", PANEL_LOGIN_PATH, body = body)

    if status != 200:
        return ""

    # 只取 `名=值` 那一段，属性（Path/HttpOnly/…）不进请求头
    pair = headers.get("Set-Cookie", "").split(";")[0]

    return pair

class TestParseQueryToken:
    def test_reads_the_token(self):
        assert parse_query_token("token=abc123") == "abc123"

    def test_takes_the_token_among_other_parameters(self):
        assert parse_query_token("x=1&token=abc&y=2") == "abc"

    def test_absent_token_is_empty(self):
        assert parse_query_token("") == ""
        assert parse_query_token("a=1&b=2") == ""

    def test_url_encoded_value_is_decoded(self):
        assert parse_query_token("token=a%2Bb") == "a+b"

    def test_key_without_value_is_ignored(self):
        assert parse_query_token("token=") == ""
        assert parse_query_token("token") == ""

class TestRoutes:
    def test_index_serves_the_panel(self, panel):
        status, body = request(panel, "GET", "/")

        assert status == 200
        assert b"Bili23 Downloader" in body

    def test_index_html_also_serves_the_panel(self, panel):
        status, _ = request(panel, "GET", "/index.html")

        assert status == 200

    def test_health_is_plain_ok(self, panel):
        # compose 的 healthcheck 依赖它，不需要令牌
        status, body = request(panel, "GET", "/health")

        assert status == 200
        assert body == b"ok"

    def test_unknown_path_is_404(self, panel):
        status, _ = request(panel, "GET", "/nope")

        assert status == 404

    def test_post_to_unknown_path_is_404(self, panel):
        status, _ = request(panel, "POST", "/nope", body = b"{}", token = TOKEN)

        assert status == 404

    def test_security_headers_are_set(self, panel):
        # 面板常暴露在局域网里，这几个头要一直在
        connection = HTTPConnection("127.0.0.1", panel.server_address[1], timeout = 5)

        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()

            assert response.getheader("X-Frame-Options") == "DENY"
            assert response.getheader("X-Content-Type-Options") == "nosniff"

        finally:
            connection.close()

    def test_page_is_not_cached_by_the_browser(self, panel):
        # 没有缓存头时浏览器会启发式缓存整页 —— 面板一更新，旧标签页就"看不到新页面"，
        # 看起来像功能没部署上（2026-10-02 实测：新增侧栏页后用户没强制刷新就看不到）
        connection = HTTPConnection("127.0.0.1", panel.server_address[1], timeout = 5)

        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()

            assert "no-store" in response.getheader("Cache-Control", "")

            # 接口同样不能缓存，否则切页拿到的可能是上一次的数据
            connection.close()
            connection = HTTPConnection("127.0.0.1", panel.server_address[1], timeout = 5)
            connection.request("GET", f"{PANEL_SETTINGS_PATH}?token={TOKEN}")
            response = connection.getresponse()
            response.read()

            assert "no-store" in response.getheader("Cache-Control", "")

        finally:
            connection.close()

class TestSecurityHardening:
    """
    加固项：响应头、Cookie 属性、请求日志脱敏、令牌不进地址栏

    这几条的共同点是"平时看不出效果，漏了也不会有人报错"，所以必须有用例钉住 ——
    否则下次改 _send 或 set_cookie 时很容易顺手把某一项弄丢
    """

    def test_every_response_carries_the_security_headers(self, panel):
        """
        页面、健康检查、未认证的 401 都要带同一套头

        只在页面上加、接口上漏掉是最容易犯的错。_send 是所有响应的唯一出口，
        覆盖这三类就够把口子钉住
        """
        # 第三类不带 Cookie，走的是"未认证 → 401"这条早退分支
        for method, path in (("GET", "/"), ("GET", "/health"), ("GET", PANEL_SETTINGS_PATH)):
            _status, _raw, headers = full_request(panel, method, path)

            assert headers["X-Content-Type-Options"] == "nosniff", path
            assert headers["X-Frame-Options"] == "DENY", path
            assert headers["Referrer-Policy"] == "no-referrer", path
            assert "no-store" in headers["Cache-Control"], path

            policy = headers["Content-Security-Policy"]

            # 默认全禁，只放开内联脚本/样式与同源请求
            assert policy.startswith("default-src 'none'"), path
            assert "script-src 'unsafe-inline'" in policy, path
            assert "connect-src 'self'" in policy, path
            assert "frame-ancestors 'none'" in policy, path
            assert "base-uri 'none'" in policy, path

    def test_hsts_only_when_the_request_came_in_over_https(self, panel):
        """
        HSTS 按"这一跳是不是 https"发

        面板本体是纯 http（TLS 由反代终止），判据只能是反代写的 X-Forwarded-Proto。
        局域网 http 入口上发这条没有意义 —— 浏览器只对 https 响应记 HSTS
        """
        _status, _raw, headers = full_request(panel, "GET", "/")

        assert "Strict-Transport-Security" not in headers

        _status, _raw, headers = full_request(panel, "GET", "/", extra = {"X-Forwarded-Proto": "https"})

        assert headers["Strict-Transport-Security"] == "max-age=31536000"

    def test_session_cookie_is_httponly_and_lax(self, panel):
        """JS 读不到（HttpOnly），跨站请求不带它（Lax）"""
        status, _raw, headers = self.login_headers(panel)

        assert status == 200

        cookie = headers["Set-Cookie"]

        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie

    def test_secure_flag_is_added_only_over_https(self, panel):
        """
        无条件加 Secure 会踩到局域网用户：浏览器不把 Secure Cookie 存进 http 上下文，
        表现是"登录成功却立刻又要求登录"
        """
        _status, _raw, headers = self.login_headers(panel)

        assert "Secure" not in headers["Set-Cookie"]

        _status, _raw, headers = self.login_headers(panel, https = True)

        assert "Secure" in headers["Set-Cookie"]

    @staticmethod
    def login_headers(panel, https = False):
        body = json.dumps({"username": TEST_USER, "password": TEST_PASS}).encode("utf-8")
        extra = {"X-Forwarded-Proto": "https"} if https else None

        return full_request(panel, "POST", PANEL_LOGIN_PATH, body = body, extra = extra)

    def test_request_log_redacts_the_query_string(self):
        """
        用 `?token=…` 打接口时，BaseHTTPRequestHandler 交给日志的是整条 requestline

        当前日志级别是 INFO，debug 不落盘 —— 但谁为了排查问题把级别调成 DEBUG，
        令牌就跟着进了 app.log，而日志页会把 app.log 原样回显给浏览器
        """
        line = ('1.2.3.4 - - [02/Oct/2026 14:00:00] '
                '"GET /api/panel/logs?file=app.log&token=s3cret-token HTTP/1.1" 200 -')

        redacted = web_server.redact_request_line(line)

        assert "s3cret-token" not in redacted
        assert "token=" not in redacted
        # 路径与协议要留着，不然这类日志就没有排查价值了
        assert "/api/panel/logs" in redacted
        assert "HTTP/1.1" in redacted

    def test_redaction_leaves_a_clean_request_line_alone(self):
        line = '1.2.3.4 - - [02/Oct/2026 14:00:00] "GET / HTTP/1.1" 200 -'

        assert web_server.redact_request_line(line) == line

    def test_opening_the_page_with_a_token_exchanges_it_for_a_session(self, panel):
        """
        用 `?token=…` 打开面板时先换一枚会话 Cookie，再跳回干净的地址

        不换的话令牌会一直留在地址栏：进浏览器历史、被收藏或转发时一起给出去、
        此后每条同源请求都挂着它。换成 Cookie 后它只在那一跳里出现过，
        而 Cookie 是 HttpOnly 的，页面脚本读不到
        """
        status, _raw, headers = full_request(panel, "GET", f"/?token={TOKEN}")

        assert status == 302
        assert headers["Location"] == "/"

        cookie = headers["Set-Cookie"]

        assert cookie.startswith(f"{SESSION_COOKIE}=")

        # 换来的会话必须真能用，否则这一跳就白跳了
        status, raw = request(panel, "POST", PANEL_SESSION_PATH, cookie = cookie.split(";")[0])

        assert status == 200
        assert json.loads(raw)["username"] == TEST_USER

    def test_a_wrong_token_shows_the_login_page_without_echoing_it(self, panel):
        """令牌不对时不做任何区分：照常给登录页，且不回显送进来的那一串"""
        status, raw = request(panel, "GET", "/?token=wrong-token-here")

        assert status == 200

        page = raw.decode("utf-8")

        assert "<!DOCTYPE html>" in page
        assert "wrong-token-here" not in page

    def test_plain_page_load_does_not_redirect(self, panel):
        """没带令牌参数时不能平白多出一次跳转"""
        status, _raw, headers = full_request(panel, "GET", "/")

        assert status == 200
        assert "Location" not in headers

class TestApiAuth:
    def call(self, panel, token = None, body = b'{"tool":"list_tasks","arguments":{}}'):
        return request(panel, "POST", "/api/call", body = body, token = token)

    def test_missing_token_is_rejected(self, panel):
        status, _ = self.call(panel)

        assert status == 401

    def test_wrong_token_is_rejected(self, panel):
        status, _ = self.call(panel, token = "wrong")

        assert status == 401

    def test_empty_token_is_rejected(self, panel):
        status, _ = self.call(panel, token = "")

        assert status == 401

    def test_header_token_is_accepted(self, panel):
        status, body = self.call(panel, token = TOKEN)

        assert status == 200
        assert b"list_tasks" in body

    def test_query_token_is_accepted(self, panel):
        # 便于用一条链接直接把面板带令牌打开
        status, _ = request(
            panel, "POST", f"/api/call?token={TOKEN}",
            body = b'{"tool":"list_tasks","arguments":{}}',
        )

        assert status == 200

    def test_token_prefix_is_rejected(self, panel):
        # 逐字符比较会把前缀泄出去，所以用的是定时安全比较
        status, _ = self.call(panel, token = TOKEN[:-1])

        assert status == 401

class TestApiDispatch:
    def call(self, panel, body):
        return request(panel, "POST", "/api/call", body = body, token = TOKEN)

    def test_unknown_tool_is_refused(self, panel):
        status, body = self.call(panel, b'{"tool":"delete_everything","arguments":{}}')

        assert status == 404
        assert b"delete_everything" in body

    def test_tool_is_not_called_when_not_whitelisted(self, panel):
        self.call(panel, b'{"tool":"delete_everything","arguments":{}}')

        assert panel.registry.calls == []

    def test_arguments_reach_the_tool(self, panel):
        self.call(panel, b'{"tool":"list_tasks","arguments":{"state":"all","limit":5}}')

        assert panel.registry.calls == [("list_tasks", {"state": "all", "limit": 5})]

    def test_missing_arguments_defaults_to_empty(self, panel):
        status, _ = self.call(panel, b'{"tool":"list_tasks"}')

        assert status == 200
        assert panel.registry.calls == [("list_tasks", {})]

    def test_invalid_json_is_400(self, panel):
        status, _ = self.call(panel, b"{not json")

        assert status == 400

    def test_non_object_body_is_400(self, panel):
        status, _ = self.call(panel, b'["list_tasks"]')

        assert status == 400

    def test_non_object_arguments_is_400(self, panel):
        status, _ = self.call(panel, b'{"tool":"list_tasks","arguments":"x"}')

        assert status == 400

    def test_empty_body_is_400(self, panel):
        status, _ = self.call(panel, b"")

        assert status == 400

    def test_oversized_body_is_refused(self, panel):
        # 超限时不能把整个 body 读进内存
        status, _ = request(
            panel, "POST", "/api/call",
            body = b"x" * (256 * 1024 + 1),
            token = TOKEN,
        )

        assert status == 413

class TestPasswordHashing:
    def test_roundtrip(self):
        assert verify_password("hunter2", hash_password("hunter2", rounds = 1))

    def test_wrong_password_is_rejected(self):
        assert not verify_password("hunter3", hash_password("hunter2", rounds = 1))

    def test_each_hash_uses_a_fresh_salt(self):
        # 同一个密码两次哈希必须不同，否则从配置里一眼就能看出哪些账号同密码
        assert hash_password("same", rounds = 1) != hash_password("same", rounds = 1)

    def test_rounds_are_read_back_from_the_stored_value(self):
        # 轮数写在凭据串里，将来调整默认值不该让已有哈希全部失效
        stored = hash_password("hunter2", salt = "abc", rounds = 3)

        assert stored.split("$")[1] == "3"
        assert verify_password("hunter2", stored)

    @pytest.mark.parametrize("stored", [
        "",
        "not-a-hash",
        "pbkdf2_sha256$abc$salt$digest",    # 轮数不是数字
        "md5$1000$salt$digest",             # 算法不认识
        "pbkdf2_sha256$0$salt$digest",      # 轮数为 0
        "$1$2$3",                           # 段数不够
    ])
    def test_malformed_stored_value_fails_closed(self, stored):
        # 畸形存档一律判失败，且不能抛异常 —— 这个函数跑在 HTTP 线程上
        assert not verify_password("hunter2", stored)

class TestPanelCredentials:
    def test_correct_pair_passes(self):
        credentials = PanelCredentials(TEST_USER, hash_password(TEST_PASS, rounds = 1))

        assert credentials.verify(TEST_USER, TEST_PASS)

    def test_wrong_password_fails(self):
        credentials = PanelCredentials(TEST_USER, hash_password(TEST_PASS, rounds = 1))

        assert not credentials.verify(TEST_USER, "nope")

    def test_wrong_username_fails(self):
        # 密码对但用户名不对，一样不能放进来
        credentials = PanelCredentials(TEST_USER, hash_password(TEST_PASS, rounds = 1))

        assert not credentials.verify("root", TEST_PASS)

    def test_update_replaces_both_fields(self):
        credentials = PanelCredentials(TEST_USER, hash_password(TEST_PASS, rounds = 1))

        credentials.update("other", hash_password("another", rounds = 1))

        assert credentials.username == "other"
        assert credentials.verify("other", "another")
        assert not credentials.verify(TEST_USER, TEST_PASS)

class TestSessionStore:
    def test_issued_token_validates(self):
        store = SessionStore()

        assert store.validate(store.issue("admin")) == "admin"

    def test_unknown_token_is_rejected(self):
        assert SessionStore().validate("whatever") == ""

    def test_empty_token_is_rejected(self):
        assert SessionStore().validate("") == ""

    def test_revoke_invalidates_the_token(self):
        store = SessionStore()
        token = store.issue("admin")

        store.revoke(token)

        assert store.validate(token) == ""

    def test_revoke_all_clears_every_session(self):
        # 改密码时用得上：别的设备上开着的会话要一并失效
        store = SessionStore()
        tokens = [store.issue("admin") for _ in range(3)]

        store.revoke_all()

        assert [store.validate(t) for t in tokens] == ["", "", ""]

    def test_expired_session_is_rejected(self):
        store = SessionStore(ttl = -1)

        assert store.validate(store.issue("admin")) == ""

    def test_tokens_are_unique(self):
        store = SessionStore()

        assert store.issue("admin") != store.issue("admin")

class TestLoginThrottle:
    """登录失败的退避表：免费额度、指数增长、封顶、按来源隔离"""

    def test_no_failure_means_no_wait(self):
        assert LoginThrottle().wait_seconds("1.2.3.4") == 0

    def test_first_failures_are_free(self):
        # 输错一两次就锁 2 秒对真人太苛刻：免费额度内不等待
        throttle = LoginThrottle()

        for _ in range(LoginThrottle.FREE_FAILURES):
            throttle.record_failure("a")

        assert throttle.wait_seconds("a") == 0

    def test_failures_grow_the_wait_exponentially(self):
        throttle = LoginThrottle()

        for _ in range(LoginThrottle.FREE_FAILURES + 1):
            throttle.record_failure("a")
        first = throttle.wait_seconds("a")

        throttle.record_failure("a")
        second = throttle.wait_seconds("a")

        assert 0 < first < second

    def test_wait_is_capped(self):
        throttle = LoginThrottle()

        for _ in range(20):
            throttle.record_failure("a")

        assert throttle.wait_seconds("a") <= LoginThrottle.MAX_WAIT + 0.001

    def test_sources_are_isolated(self):
        throttle = LoginThrottle()

        throttle.record_failure("a")

        assert throttle.wait_seconds("b") == 0

    def test_reset_clears_the_entry(self):
        throttle = LoginThrottle()

        for _ in range(LoginThrottle.FREE_FAILURES + 1):
            throttle.record_failure("a")

        assert throttle.wait_seconds("a") > 0

        throttle.reset("a")

        assert throttle.wait_seconds("a") == 0

class TestEnsureCredentials:
    def test_fills_in_defaults(self, monkeypatch):
        fake = FakeConfig()

        monkeypatch.setattr("util.web.auth.config", fake)

        username, stored = ensure_credentials()

        assert username == "admin"
        assert verify_password("password", stored)
        # 顺手落盘，免得下次启动再算一遍
        assert fake.store["web_panel_password"]

    def test_existing_values_are_kept(self, monkeypatch):
        fake = FakeConfig()
        stored = hash_password("mine", rounds = 1)

        fake.set(fake.web_panel_username, "someone")
        fake.set(fake.web_panel_password, stored)

        monkeypatch.setattr("util.web.auth.config", fake)

        username, result = ensure_credentials()

        assert username == "someone"
        assert result == stored

class TestQrCodeSvg:
    def test_renders_an_svg(self):
        svg = render_svg("https://example.com/scan")

        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")
        assert 'fill="#ffffff"' in svg
        assert 'fill="#000000"' in svg

    def test_matrix_is_merged_into_a_single_path(self):
        # 逐个模块一个 <rect> 会产生几千个节点，这里必须是合并成游程的一条 path
        svg = render_svg("https://example.com/scan")

        assert svg.count("<path") == 1
        assert svg.count("<rect") == 1

    def test_different_content_gives_different_svg(self):
        assert render_svg("a") != render_svg("b")

class TestParseLoginUrl:
    def test_takes_the_login_cookies(self):
        url = (
            "https://passport.biligame.com/crossDomain?DedeUserID=42&SESSDATA=abc"
            "&bili_jct=token&DedeUserID__ckMd5=md5"
        )

        assert parse_login_url(url) == {
            "DedeUserID": "42",
            "SESSDATA": "abc",
            "bili_jct": "token",
            "DedeUserID__ckMd5": "md5",
        }

    def test_ignores_unrelated_parameters(self):
        assert parse_login_url("https://x/y?gourl=https%3A%2F%2Fwww.bilibili.com") == {}

    def test_url_without_query_gives_nothing(self):
        assert parse_login_url("") == {}
        assert parse_login_url("https://x/y") == {}

class TestPanelAuth:
    def test_page_is_served_without_login(self, panel):
        # 登录框要先加载出来，页面本身不能设门禁
        status, body = request(panel, "GET", "/")

        assert status == 200
        assert b"Bili23 Downloader" in body

    def test_api_requires_login(self, panel):
        status, _ = request(panel, "POST", API_PATH, body = b'{"tool":"list_tasks"}')

        assert status == 401

    def test_token_still_works_for_automation(self, panel):
        # 脚本与 MCP 客户端不方便先登录换 Cookie，令牌这条路必须留着
        status, _ = request(panel, "POST", API_PATH, body = b'{"tool":"list_tasks"}', token = TOKEN)

        assert status == 200

class TestPanelLogin:
    def test_login_issues_a_session_cookie(self, panel):
        body = json.dumps({"username": TEST_USER, "password": TEST_PASS}).encode()

        status, _, headers = full_request(panel, "POST", PANEL_LOGIN_PATH, body = body)

        assert status == 200
        assert SESSION_COOKIE in headers.get("Set-Cookie", "")

    def test_wrong_password_is_rejected(self, panel):
        body = json.dumps({"username": TEST_USER, "password": "nope"}).encode()

        assert request(panel, "POST", PANEL_LOGIN_PATH, body = body)[0] == 401

    def test_wrong_username_is_rejected(self, panel):
        body = json.dumps({"username": "root", "password": TEST_PASS}).encode()

        assert request(panel, "POST", PANEL_LOGIN_PATH, body = body)[0] == 401

    def test_missing_fields_are_rejected(self, panel):
        assert request(panel, "POST", PANEL_LOGIN_PATH, body = b"{}")[0] == 401

    def test_brute_force_is_throttled(self, panel):
        """连续失败超过免费额度后来源地址进入退避，连正确密码也要等"""
        wrong = json.dumps({"username": TEST_USER, "password": "nope"}).encode()

        # 免费额度内的失败照常回 401：输错一两次就被锁对真人太苛刻。
        # 第 4 次 401 时表里才攒够免费额度外的记录，第 5 次起进入退避
        for _ in range(4):
            assert request(panel, "POST", PANEL_LOGIN_PATH, body = wrong)[0] == 401

        status, raw = request(panel, "POST", PANEL_LOGIN_PATH, body = wrong)

        assert status == 429
        assert "频繁" in json.loads(raw)["error"]

        # 退避判定在密码校验之前：等待期内正确的密码也进不来，
        # 否则爆破者可以拿"响应码差异"当密码试探的信号
        good = json.dumps({"username": TEST_USER, "password": TEST_PASS}).encode()

        assert request(panel, "POST", PANEL_LOGIN_PATH, body = good)[0] == 429

    def test_success_clears_the_throttle(self, panel):
        wrong = json.dumps({"username": TEST_USER, "password": "nope"}).encode()

        # 失败到进入退避（免费额度内清零不产生效果，多失败几次）
        for _ in range(LoginThrottle.FREE_FAILURES + 1):
            request(panel, "POST", PANEL_LOGIN_PATH, body = wrong)

        assert panel.login_throttle.wait_seconds("127.0.0.1") > 0

        panel.login_throttle.reset("127.0.0.1")

        good = json.dumps({"username": TEST_USER, "password": TEST_PASS}).encode()

        assert request(panel, "POST", PANEL_LOGIN_PATH, body = good)[0] == 200

    def test_session_cookie_grants_api_access(self, panel):
        cookie = login(panel)

        assert cookie

        status, body = request(panel, "POST", API_PATH, body = b'{"tool":"list_tasks"}', cookie = cookie)

        assert status == 200
        assert b"list_tasks" in body

    def test_forged_session_cookie_is_rejected(self, panel):
        # 会话令牌是 32 字节随机串，这里确认它确实被校验而不是照单全收
        status, _ = request(
            panel, "POST", API_PATH, body = b'{"tool":"list_tasks"}',
            cookie = f"{SESSION_COOKIE}=forged",
        )

        assert status == 401

    def test_session_endpoint_reports_the_user(self, panel):
        status, body = request(panel, "POST", PANEL_SESSION_PATH, body = b"{}", cookie = login(panel))

        assert status == 200
        assert json.loads(body)["username"] == TEST_USER

    def test_logout_revokes_the_session(self, panel):
        cookie = login(panel)

        assert request(panel, "POST", PANEL_LOGOUT_PATH, body = b"{}", cookie = cookie)[0] == 200

        # Cookie 还是那一枚，但它已经作废了
        assert request(panel, "POST", PANEL_SESSION_PATH, body = b"{}", cookie = cookie)[0] == 401

    def test_logout_only_kills_the_current_session(self, panel):
        first = login(panel)
        second = login(panel)

        request(panel, "POST", PANEL_LOGOUT_PATH, body = b"{}", cookie = first)

        assert request(panel, "POST", PANEL_SESSION_PATH, body = b"{}", cookie = second)[0] == 200

class TestPanelPassword:
    @pytest.fixture(autouse = True)
    def isolate_config(self, monkeypatch):
        # 改密码要回 GUI 线程写配置，而单测里没有在跑的事件循环：
        # 这里把"排队到主线程"换成直接执行，再把配置对象换掉，
        # 于是路由与鉴权照测，又不会真去写用户的配置文件
        # timeout 之类是调度用的参数，不该转交给被调函数，这里直接丢掉
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))
        monkeypatch.setattr("util.web.server.config", FakeConfig())

    def change(self, panel, cookie, current, new):
        body = json.dumps({"current": current, "password": new}).encode()

        return request(panel, "POST", PANEL_PASSWORD_PATH, body = body, cookie = cookie)

    def test_changes_the_password(self, panel):
        status, _ = self.change(panel, login(panel), TEST_PASS, "another-pass")

        assert status == 200
        # 旧密码作废，新密码可用
        assert not login(panel, password = TEST_PASS)
        assert login(panel, password = "another-pass")

    def test_wrong_current_password_is_rejected(self, panel):
        assert self.change(panel, login(panel), "nope", "another-pass")[0] == 401

    def test_short_password_is_rejected(self, panel):
        status, body = self.change(panel, login(panel), TEST_PASS, "abc")

        assert status == 400
        assert "6" in json.loads(body)["error"]

    def test_same_password_is_rejected(self, panel):
        assert self.change(panel, login(panel), TEST_PASS, TEST_PASS)[0] == 400

    def test_old_sessions_are_revoked(self, panel):
        # 改密码要能把别处的会话一并踢掉，否则改密码只是给自己看
        stale = login(panel)
        current = login(panel)

        self.change(panel, current, TEST_PASS, "another-pass")

        assert request(panel, "POST", PANEL_SESSION_PATH, body = b"{}", cookie = stale)[0] == 401

    def test_current_session_survives(self, panel):
        # 改密码的人自己不该被踢回登录框
        body = json.dumps({"current": TEST_PASS, "password": "another-pass"}).encode()

        status, _, headers = full_request(panel, "POST", PANEL_PASSWORD_PATH, body = body, cookie = login(panel))
        fresh = headers.get("Set-Cookie", "").split(";")[0]

        assert status == 200
        assert fresh

        assert request(panel, "POST", PANEL_SESSION_PATH, body = b"{}", cookie = fresh)[0] == 200

class TestPanelEndpoints:
    def test_panel_endpoints_require_login(self, panel):
        assert request(panel, "POST", PANEL_QR_POLL_PATH, body = b'{"qrcode_key":"x"}')[0] == 401

    def test_qr_poll_requires_a_key(self, panel):
        status, body = request(panel, "POST", PANEL_QR_POLL_PATH, body = b"{}", cookie = login(panel))

        assert status == 400
        assert "qrcode_key" in json.loads(body)["error"]

    def test_unknown_panel_endpoint_is_404(self, panel):
        # /api/panel/ 下没见过的路径不该落到工具调用那条分支上
        status, _ = request(panel, "POST", "/api/panel/nope", body = b"{}", cookie = login(panel))

        assert status == 404

    def test_bili_logout_is_dispatched(self, panel, monkeypatch):
        called = []

        monkeypatch.setattr("util.web.server.bili_login.logout", lambda: called.append(True))

        status, _ = request(panel, "POST", PANEL_BILI_LOGOUT_PATH, body = b"{}", cookie = login(panel))

        assert status == 200
        assert called == [True]

class TestSettingsApi:
    """设置页的读写接口（server.py 的 SETTINGS_FIELDS 那一段）"""

    def prepare(self, monkeypatch):
        """
        换成假配置，并把写配置的跨线程调用折叠成直接执行

        真实的 call_in_main_thread 要 Qt 事件循环在转，测试里没有；
        折叠掉它，验的是"校验与落值"，不是线程调度
        """
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        return fake

    def save(self, panel, settings):
        body = json.dumps({"settings": settings}).encode()

        return request(panel, "POST", PANEL_SETTINGS_SAVE_PATH, body = body, cookie = login(panel))

    def test_read_requires_login(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert request(panel, "GET", PANEL_SETTINGS_PATH)[0] == 401

    def test_save_requires_login(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        body = json.dumps({"settings": {"download_parallel": 2}}).encode()

        assert request(panel, "POST", PANEL_SETTINGS_SAVE_PATH, body = body)[0] == 401

    def test_token_also_opens_the_read_path(self, panel, monkeypatch):
        # 令牌那条路与 Cookie 共用一套鉴权，不该只有 Panel 的 POST 接口认它
        self.prepare(monkeypatch)

        assert request(panel, "GET", PANEL_SETTINGS_PATH, token = TOKEN)[0] == 200

    def test_fields_carry_value_and_range(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}

        assert fields["download_parallel"]["value"] == 1
        assert fields["download_parallel"]["min"] == 1
        assert fields["download_parallel"]["max"] == 10
        assert fields["merge_parallel"]["value"] == 2
        assert fields["merge_parallel"]["min"] == 1
        assert fields["merge_parallel"]["max"] == 4
        assert fields["download_thread"]["value"] == 4

    def test_the_parse_auto_add_switch_is_readable(self, panel, monkeypatch):
        """
        🔴 面板的批量解析弹窗要拿它给复选框做回填。它不在 SETTINGS_FIELDS 里的话前端
        根本读不到，只能从"未勾选"起步 —— 于是每点一次批量解析就把用户开着的
        「解析完自动加入下载列表」关一次（桌面端那个对话框是读配置再回填的）
        """
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}
        field = fields["auto_add_to_download_list"]

        assert field["type"] == "bool"
        assert field["label"]
        # 读出来的必须是布尔值：读失败时回的是 null，前端拿它当"关着"照样会改坏设置
        assert field["value"] is False

    def test_readonly_exposes_download_path(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))
        readonly = {r["label"]: r["value"] for r in json.loads(raw)["readonly"]}

        assert status == 200
        assert readonly["下载目录"] == "/downloads"

    def test_readonly_reports_free_space_of_the_download_volume(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))
        readonly = json.loads(raw)["readonly"]

        assert status == 200

        # 顺序也得钉住：概览页那张指标卡靠它取「下载目录」和「剩余空间」，
        # 虽然前端已经改成按 label 找，但设置页那栏是按列表原样铺的
        assert [r["label"] for r in readonly] == ["下载目录", "剩余空间"]
        assert re.fullmatch(r"\d+\.\d (TB|GB)", readonly[1]["value"]), readonly[1]

    def test_disk_free_text_uses_whichever_unit_reads_better(self, tmp_path):
        """够 1T 报 T、不够报 G —— 盘快满时「287.4 GB」比「0.3 TB」有用得多"""
        text = web_server._disk_free_text(str(tmp_path))

        assert re.fullmatch(r"\d+\.\d (TB|GB)", text), text

    def test_disk_free_text_falls_back_to_an_existing_ancestor(self, tmp_path):
        """目录可能还没建（首次部署就是），上溯到存在的祖先照样给得出数"""
        missing = tmp_path / "not-yet" / "downloads"

        assert not missing.exists()
        assert re.fullmatch(r"\d+\.\d (TB|GB)", web_server._disk_free_text(str(missing)))

    def test_disk_free_text_gives_up_quietly(self, monkeypatch):
        """一个候选都查不到就给「—」，不能抛异常拖垮整页设置"""
        def boom(_path):
            raise OSError("nope")

        monkeypatch.setattr(web_server.shutil, "disk_usage", boom)

        assert web_server._disk_free_text("/downloads") == "—"

    def test_save_persists_and_echoes_back(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, raw = self.save(panel, {"download_parallel": 3, "download_thread": 8})

        assert status == 200
        assert fake.store["download_parallel"] == 3
        assert fake.store["download_thread"] == 8

        # 返回值直接就是最新设置，前端据此重渲染，省掉一次 GET
        values = {f["key"]: f["value"] for f in json.loads(raw)["fields"]}

        assert values["download_parallel"] == 3

    # ---- MCP 服务器组 ----

    @staticmethod
    def fake_manager(monkeypatch):
        """
        换掉 MCP 管理器单例：测试里不该真的去绑端口，
        只需要记录 restart 被调了几次、并报告一个确定的运行状态
        """
        import types

        manager = types.SimpleNamespace(running = False, restarts = 0)

        manager.restart = lambda: manager.__dict__.update(restarts = manager.restarts + 1) or True

        monkeypatch.setattr("util.mcp.server.mcp_server_manager", manager)

        return manager

    def test_settings_carry_mcp_status(self, panel, monkeypatch):
        self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))
        mcp = json.loads(raw)["mcp"]

        assert status == 200
        assert mcp["running"] is False
        assert mcp["port"] == 23330
        assert mcp["token"] == "mcp-token-live"
        assert mcp["error"] == ""

    def test_saving_mcp_keys_restarts_the_server(self, panel, monkeypatch):
        """开关与端口不是改完就完：要重启 MCP 服务器线程才吃得上"""
        fake = self.prepare(monkeypatch)
        manager = self.fake_manager(monkeypatch)

        status, raw = self.save(panel, {"mcp_enabled": True, "mcp_port": 24444})

        assert status == 200
        assert fake.store["mcp_enabled"] is True
        assert fake.store["mcp_port"] == 24444
        assert manager.restarts == 1

    def test_saving_other_keys_leaves_mcp_alone(self, panel, monkeypatch):
        manager = self.fake_manager(monkeypatch)

        self.prepare(monkeypatch)
        self.save(panel, {"download_parallel": 2})

        assert manager.restarts == 0

    def record_scheduler(self, monkeypatch):
        """
        记下「让调度器重排」那一次信号

        persist() 里是 `from ..common.signal_bus import signal_bus` —— 每次调用现取，
        所以换掉模块属性就够了，不必去动真实信号对象
        """
        import types

        import util.common.signal_bus as bus_module

        hits = []

        fake_bus = types.SimpleNamespace(
            download = types.SimpleNamespace(
                auto_manage_concurrent_downloads = types.SimpleNamespace(
                    emit = lambda: hits.append(1)
                )
            )
        )

        monkeypatch.setattr(bus_module, "signal_bus", fake_bus)

        return hits

    def test_saving_parallel_limits_reshuffles_the_scheduler(self, panel, monkeypatch):
        """
        两个并发上限改完都要立刻重排

        不 emit 的话得等某个任务状态变化才会重新评估，用户会觉得"改了半天没反应"；
        调**低**时尤其明显 —— 多出来的那几个还在跑，界面上看不出任何变化
        """
        for key in ("download_parallel", "merge_parallel"):
            self.prepare(monkeypatch)
            self.fake_manager(monkeypatch)

            hits = self.record_scheduler(monkeypatch)

            status, _ = self.save(panel, {key: 3})

            assert status == 200, key
            assert hits == [1], key

    def test_saving_unrelated_keys_does_not_reshuffle_the_scheduler(self, panel, monkeypatch):
        self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        hits = self.record_scheduler(monkeypatch)

        assert self.save(panel, {"download_thread": 6})[0] == 200
        assert hits == []

    def test_mcp_token_regenerate(self, panel, monkeypatch):
        """重新生成要落盘并返回新令牌；旧令牌在存进 fake 前后必须不同"""
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        fake.store["mcp_token"] = "old-token"

        status, raw = request(
            panel, "POST", PANEL_MCP_TOKEN_PATH, body = b"{}", cookie = login(panel)
        )
        data = json.loads(raw)

        assert status == 200
        assert data["ok"] is True
        assert data["token"] != "old-token"
        assert fake.store["mcp_token"] == data["token"]
        assert data["mcp"]["token"] == data["token"]

    def test_mcp_token_requires_login(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert request(panel, "POST", PANEL_MCP_TOKEN_PATH, body = b"{}")[0] == 401

    # ---- CDN 设置与代理设置 ----

    def test_cdn_list_follows_area(self, panel, monkeypatch):
        """GET 按当前地区取列表，options 带服务商名"""
        self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))
        fields = {f["key"]: f for f in json.loads(raw)["fields"]}
        cdn = fields["cdn_server_list"]

        assert status == 200
        assert cdn["type"] == "cdnlist"
        assert cdn["value"][0] == "upos-sz-mirror08c.bilivideo.com"
        assert cdn["options"][0] == ["upos-sz-mirror08c.bilivideo.com", "HUAWEI"]

    def test_cdn_reorder_persists_as_dicts(self, panel, monkeypatch):
        """提交的是 host 顺序，服务端负责还原成 {host, provider} 写回"""
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        hosts = ["upos-sz-mirrorali.bilivideo.com", "upos-sz-mirror08c.bilivideo.com"]

        status, raw = self.save(panel, {"cdn_server_list": hosts})

        assert status == 200

        stored = fake.store["cn_cdn_server_list"]

        assert [e["host"] for e in stored] == hosts
        assert stored[0]["provider"] == "ALIBABA"

    def test_cdn_list_submitted_as_json_string(self, panel, monkeypatch):
        """
        线上报过「服务商 CDN 列表格式不正确」：前端隐藏框交的是 JSON 字符串，
        cdnlist 分支却只认数组。字符串要先解开再验，与 priority 同一条链路
        """
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        hosts = ["upos-sz-mirror08c.bilivideo.com", "upos-sz-mirrorali.bilivideo.com"]

        status, raw = self.save(panel, {"cdn_server_list": json.dumps(hosts)})

        assert status == 200
        assert [e["host"] for e in fake.store["cn_cdn_server_list"]] == hosts

    def test_cdn_reorder_rejected_when_hosts_do_not_match(self, panel, monkeypatch):
        self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        status, raw = self.save(panel, {"cdn_server_list": ["not-a-host"]})

        # 409：host 集合与现有列表对不上，多半是页面停留太久、配置已被别处改过
        assert status == 409

    def test_cdn_list_writes_overseas_key_when_area_changed_together(self, panel, monkeypatch):
        """同一次提交里改地区 + 列表：列表要落到新地区的键上"""
        from util.common.enum import Area

        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        status, raw = self.save(panel, {
            "area": 1,  # 海外
            "cdn_server_list": ["upos-mirror_ov.bilivideo.com"],
        })

        assert status == 200
        assert fake.store["area"] == Area.OV
        assert [e["host"] for e in fake.store["ov_cdn_server_list"]] == ["upos-mirror_ov.bilivideo.com"]
        # 国内那份不该被动过
        assert "cn_cdn_server_list" not in fake.store

    def test_proxy_save_rebuilds_the_network_client(self, panel, monkeypatch):
        """代理改动不用重启：persist 会丢弃共享客户端，下个请求按新配置重建"""
        from util.common.enum import ProxyMode
        import util.network.request as net_request

        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        resets = []
        monkeypatch.setattr(net_request, "reset_client", lambda: resets.append(1))

        status, raw = self.save(panel, {"proxy_mode": 2, "proxy_server": "192.168.3.25", "proxy_port": 7890})

        assert status == 200
        assert fake.store["proxy_mode"] == ProxyMode.MANUAL
        assert fake.store["proxy_server"] == "192.168.3.25"
        assert fake.store["proxy_port"] == 7890
        assert resets == [1]

    def test_saving_unrelated_keys_leaves_client_alone(self, panel, monkeypatch):
        import util.network.request as net_request

        self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        resets = []
        monkeypatch.setattr(net_request, "reset_client", lambda: resets.append(1))

        self.save(panel, {"download_parallel": 2})

        assert resets == []

    def test_proxy_password_is_never_sent_back_to_the_page(self, panel, monkeypatch):
        """
        密文字段只回"设了没"，不回真值

        代理密码与面板令牌同性质 —— 一旦落在 DOM 的 value 里，整页截图、剪贴板历史、
        浏览器扩展都能把它带走。前端于是留空 + placeholder 说明状态，靠 f.set 那一位
        """
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        fake.store["proxy_password"] = "proxy-secret-live"

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}
        proxy = fields["proxy_password"]

        assert proxy["secret"] is True
        assert proxy["set"] is True
        assert proxy["value"] == ""
        # 真值一个字符都不许出现在响应里
        assert "proxy-secret-live" not in raw.decode("utf-8")

    def test_blank_secret_keeps_the_stored_value(self, panel, monkeypatch):
        """留空 = 不改。不回填的话，用户只改一个别的设置就会把代理密码一起抹掉"""
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        fake.store["proxy_password"] = "proxy-secret-live"

        status, _ = self.save(panel, {"proxy_password": ""})

        assert status == 200
        assert fake.store["proxy_password"] == "proxy-secret-live"

    def test_a_new_secret_overwrites_the_stored_one(self, panel, monkeypatch):
        """填了新值就以新值为准 —— "留空不改"不能变成"永远改不了" """
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        fake.store["proxy_password"] = "proxy-secret-live"

        status, _ = self.save(panel, {"proxy_password": "next-pass"})

        assert status == 200
        assert fake.store["proxy_password"] == "next-pass"

    def test_user_agent_saves_and_rejects_empty(self, panel, monkeypatch):
        """桌面版 UA 不允许为空，面板版同语义：空串 400，正常串落盘"""
        fake = self.prepare(monkeypatch)
        self.fake_manager(monkeypatch)

        status, raw = self.save(panel, {"user_agent": "Bili23/2.20"})

        assert status == 200
        assert fake.store["user_agent"] == "Bili23/2.20"

        status, raw = self.save(panel, {"user_agent": "  "})

        assert status == 400
        assert "不能为空" in json.loads(raw)["error"]

    def test_digits_sent_as_string_are_accepted(self, panel, monkeypatch):
        # 浏览器里 <input type=number> 拿到的就是字符串，后端要认
        fake = self.prepare(monkeypatch)

        status, _ = self.save(panel, {"download_parallel": "4"})

        assert status == 200
        assert fake.store["download_parallel"] == 4

    @pytest.mark.parametrize("value", [0, 11, -3, 100])
    def test_out_of_range_is_refused(self, panel, monkeypatch, value):
        fake = self.prepare(monkeypatch)

        status, raw = self.save(panel, {"download_parallel": value})

        assert status == 400
        assert "download_parallel" not in fake.store
        assert "同时下载任务数" in json.loads(raw)["error"]

    def test_decimal_is_refused_for_an_int_field(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.save(panel, {"download_parallel": 2.5})[0] == 400

    def test_non_numeric_is_refused(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, _ = self.save(panel, {"download_thread": "多来点"})

        assert status == 400
        assert "download_thread" not in fake.store

    def test_unknown_key_is_refused(self, panel, monkeypatch):
        # 白名单之外一律拒绝：能任意写配置就等于把令牌与 B 站 Cookie 交出去
        fake = self.prepare(monkeypatch)

        status, raw = self.save(panel, {"web_panel_token": "x"})

        assert status == 400
        assert "不认识的设置项" in json.loads(raw)["error"]
        assert not fake.store

    def test_bool_field_accepts_a_switch(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        assert self.save(panel, {"speed_limit_enabled": True})[0] == 200
        assert fake.store["speed_limit_enabled"] is True

    def test_bool_field_refuses_a_number(self, panel, monkeypatch):
        # 7 既不是 True 也不是 0/1，说明请求构造错了，不能默默当成长度
        self.prepare(monkeypatch)

        assert self.save(panel, {"speed_limit_enabled": 7})[0] == 400

    def test_float_field_accepts_a_decimal(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        assert self.save(panel, {"speed_limit_rate": 2.5})[0] == 200
        assert fake.store["speed_limit_rate"] == 2.5

    def test_empty_settings_is_refused(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.save(panel, {})[0] == 400

    def test_missing_settings_key_is_refused(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        body = json.dumps({"nope": 1}).encode()

        assert request(panel, "POST", PANEL_SETTINGS_SAVE_PATH, body = body, cookie = login(panel))[0] == 400

    # ---- 媒体选项 / 下载格式：开关与枚举 ----

    def test_media_option_switches_persist(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, raw = self.save(panel, {"download_video_stream": False, "keep_original_files": True})

        assert status == 200
        assert fake.store["download_video_stream"] is False
        assert fake.store["keep_original_files"] is True
        # 回显里要带着新值，前端据此重绘
        fields = {f["key"]: f for f in json.loads(raw)["fields"]}
        assert fields["download_video_stream"]["value"] is False

    def test_enum_field_is_stored_as_an_enum(self, panel, monkeypatch):
        # 只下「仅视频」是 index 1；存回去必须是 OriginalFileType.VIDEO，
        # 存成裸 1 会让真机上的 OptionsValidator 拦下来
        fake = self.prepare(monkeypatch)

        status, _ = self.save(panel, {"keep_original_files_type": 1})

        assert status == 200

        from util.common.enum import OriginalFileType

        assert fake.store["keep_original_files_type"] is OriginalFileType.VIDEO

    def test_enum_field_reads_back_as_an_index(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}

        assert fields["keep_original_files_type"]["value"] == 0
        # 枚举值（"mp4"/"mkv"）绝不能漏出去，前端要的是下标
        assert fields["video_container"]["type"] == "enum"
        assert fields["video_container"]["options"] == ["mp4", "mkv"]
        assert fields["video_container"]["value"] == 0

    def test_enum_field_refuses_an_out_of_range_index(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        # mkv 是 1，2 不存在
        assert self.save(panel, {"video_container": 2})[0] == 400

    # ---- 优先级：只调序，不改档位集合 ----

    def test_priority_reorder_is_accepted(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, _ = self.save(panel, {"video_quality_priority": "[64, 80]"})

        assert status == 200
        assert fake.store["video_quality_priority"] == [64, 80]

    def test_priority_accepts_the_hidden_input_as_a_json_string(self, panel, monkeypatch):
        # 前端把排序结果塞在隐藏框里收集，走的是同一条 el.value
        fake = self.prepare(monkeypatch)

        status, _ = self.save(panel, {"video_codec_priority": "[12, 7]"})

        assert status == 200
        assert fake.store["video_codec_priority"] == [12, 7]

    def test_priority_refuses_a_dropped_quality(self, panel, monkeypatch):
        # 丢一档会让选择逻辑在余下档位里挑，静默降级成另一种画质
        self.prepare(monkeypatch)

        assert self.save(panel, {"video_quality_priority": "[80]"})[0] == 400

    def test_priority_refuses_a_duplicated_quality(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.save(panel, {"video_quality_priority": "[80, 80]"})[0] == 400

    def test_priority_refuses_an_unknown_quality(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        # 999 是编出来的档位 id
        assert self.save(panel, {"video_quality_priority": "[80, 999]"})[0] == 400

    def test_priority_options_only_list_the_configured_qualities(self, panel, monkeypatch):
        # 画质表里还有 auto/200 这种从没进过优先级列表的档位：
        # 一旦列进候选，用户原样保存也会被"档位组合变了"拒绝
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}
        options = fields["video_quality_priority"]["options"]

        assert [row[0] for row in options] == [80, 64]
        # 显示的是译过的档位名，不是裸 id
        assert all(len(row) == 2 and row[1] for row in options)
        assert "200" not in [str(row[0]) for row in options]

    def test_priority_names_are_translated_exactly_once(self, panel, monkeypatch):
        # 回归：这里曾把译名再当 key 查一次（双层翻译），真实译名环境里
        # 画质回退成裸 id（127/126…）、音质/编码把整张映射表发出去变成
        # [object Object]。假翻译模拟真 Translator 的约定——key 为 None
        # 时返回整张表——双层翻译下必然出现"表"或错位文案，一次译则精确匹配
        from util.common import data as media_data
        from util.common import translator

        def fake_method(section):
            def method(key = None):
                if key is None:
                    return {"整张映射表": "不该出现"}
                return f"{section}·{key}"
            return method

        monkeypatch.setattr(translator.Translator, "VIDEO_QUALITY", fake_method("画质"))
        monkeypatch.setattr(translator.Translator, "AUDIO_QUALITY", fake_method("音质"))
        monkeypatch.setattr(translator.Translator, "VIDEO_CODEC", fake_method("编码"))

        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = login(panel))

        assert status == 200

        fields = {f["key"]: f for f in json.loads(raw)["fields"]}

        for key, reversed_map, section in (
            ("video_quality_priority", media_data.reversed_video_quality_map, "画质"),
            ("audio_quality_priority", media_data.reversed_audio_quality_map, "音质"),
            ("video_codec_priority", media_data.reversed_video_codec_map, "编码"),
        ):
            options = fields[key]["options"]

            assert [row[1] for row in options] == [
                f"{section}·{reversed_map[quality_id]}" for quality_id, _ in options
            ]


class TestConfigFile:
    """「配置文件设置」：导出 / 导入 / 恢复默认，逐键写入立即生效"""

    def prepare(self, monkeypatch):
        """与 TestPanelSettings.prepare 同一套假配置与跨线程折叠

        auth.config 也要换掉：恢复默认的接口会按启动路径补默认凭据，
        不 patch 的话它会写进真实的 config.json；reset 还会重启 MCP
        对齐开关状态，一并换成假的
        """
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        monkeypatch.setattr("util.web.auth.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        # 与 TestSettingsApi.fake_manager 同一招：reset 会重启 MCP，换成假的
        import types

        manager = types.SimpleNamespace(running = False, restarts = 0)
        manager.restart = lambda: manager.__dict__.update(restarts = manager.restarts + 1) or True

        monkeypatch.setattr("util.mcp.server.mcp_server_manager", manager)

        return fake

    def test_export_returns_grouped_config(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_CONFIG_EXPORT_PATH, cookie = login(panel))

        assert status == 200

        data = json.loads(raw)["data"]

        assert isinstance(data, dict)
        # FakeItem 的 group 默认是 "Fake"，导出要按组归拢
        assert "Fake" in data
        assert "user_agent" in data.get("Advanced", {})

    def test_export_requires_login(self, panel):
        assert request(panel, "GET", PANEL_CONFIG_EXPORT_PATH)[0] == 401

    def test_import_writes_known_keys_and_skips_unknown(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        payload = {
            "data": {
                "Advanced": {"user_agent": "Imported/1.0"},
                "Nope": {"missing_key": 1},
            }
        }

        status, raw = request(panel, "POST", PANEL_CONFIG_IMPORT_PATH,
                              body = json.dumps(payload).encode(), cookie = login(panel))

        assert status == 200

        result = json.loads(raw)

        assert result["applied"] == 1
        assert fake.store["user_agent"] == "Imported/1.0"
        assert any("Nope.missing_key" in i for i in result["ignored"])

    def test_import_rejects_non_dict(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "POST", PANEL_CONFIG_IMPORT_PATH,
                              body = json.dumps({"data": [1, 2]}).encode(), cookie = login(panel))

        assert status == 400

    def test_reset_restores_defaults(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        fake.store["user_agent"] = "Changed/9.9"
        fake.store["download_parallel"] = 7

        status, raw = request(panel, "POST", PANEL_CONFIG_RESET_PATH, body = b"{}", cookie = login(panel))

        assert status == 200
        # 全部回到 FakeItem 的 default
        assert fake.store["user_agent"] == "Mozilla/5.0 (Test)"
        assert fake.store["download_parallel"] == 1

    # ---- 安全：敏感数据不进浏览器、不进日志、爆破有人管 ----

    def test_export_masks_sensitive_keys(self, panel, monkeypatch):
        """B 站 Cookie、面板与 MCP 令牌、代理密码、通知凭据：导出件里只能是掩码"""
        fake = self.prepare(monkeypatch)

        # 密码哈希给个非空值：空值按约定保持空串不掩码
        fake.store["web_panel_password"] = "pbkdf2$120000$salt$deadbeef"
        # 通知凭据同理 —— 企微应用 Secret 与 TG bot token 都能以你的名义发消息
        fake.store["notification_wecom_secret"] = "wecom-secret-live"
        fake.store["notification_telegram_token"] = "tg-token-live"
        # 回调凭据：拿这两项就能伪造一条"来自企业微信"的推送
        fake.store["notification_wecom_callback_token"] = "cbtokenlive"
        fake.store["notification_wecom_aes_key"] = "A" * 43

        status, raw = request(panel, "GET", PANEL_CONFIG_EXPORT_PATH, cookie = login(panel))

        assert status == 200

        result = json.loads(raw)
        data, redacted = result["data"], set(result["redacted"])

        masked = {
            ("Cookie", "SESSDATA"), ("Cookie", "bili_jct"),
            ("Cookie", "DedeUserID"), ("Cookie", "DedeUserID__ckMd5"),
            ("Web Panel", "web_panel_password"), ("Web Panel", "web_panel_token"),
            ("MCP", "mcp_token"), ("Advanced", "proxy_password"),
            ("Notification", "notification_wecom_secret"),
            ("Notification", "notification_telegram_token"),
            ("Notification", "notification_wecom_callback_token"),
            ("Notification", "notification_wecom_aes_key"),
        }

        for group, key in masked:
            assert data[group][key] == web_server.SENSITIVE_MASK
            assert f"{group}.{key}" in redacted

        # 真值一个都不许出现 —— 导出件是要被分享的东西
        whole = raw.decode("utf-8")

        for secret in ("sess-live-secret", "jct-live-secret", "panel-token-live",
                       "mcp-token-live", "proxy-pass-live", "pbkdf2$120000$salt$deadbeef",
                       "wecom-secret-live", "tg-token-live", "cbtokenlive", "A" * 43):
            assert secret not in whole

    def test_export_keeps_empty_sensitive_keys_empty(self, panel, monkeypatch):
        """没设置的敏感项保持空串：掩码会让人误以为"有值但被藏了" """
        fake = self.prepare(monkeypatch)

        fake.store["SESSDATA"] = ""

        status, raw = request(panel, "GET", PANEL_CONFIG_EXPORT_PATH, cookie = login(panel))

        assert status == 200

        result = json.loads(raw)

        assert result["data"]["Cookie"]["SESSDATA"] == ""
        assert "Cookie.SESSDATA" not in result["redacted"]

    def test_import_refuses_sensitive_keys(self, panel, monkeypatch):
        """导入不能成为"把令牌写进配置"的后门，也不能把掩码覆盖到真值上"""
        fake = self.prepare(monkeypatch)

        payload = {
            "data": {
                "MCP": {"mcp_token": "attacker-token"},
                "Web Panel": {"web_panel_token": web_server.SENSITIVE_MASK},
                "Cookie": {"SESSDATA": "stolen-sess"},
                "Notification": {"notification_wecom_secret": "attacker-secret"},
                "Advanced": {"user_agent": "Imported/1.0"},
            }
        }

        status, raw = request(panel, "POST", PANEL_CONFIG_IMPORT_PATH,
                              body = json.dumps(payload).encode(), cookie = login(panel))

        assert status == 200

        result = json.loads(raw)

        assert result["applied"] == 1
        assert fake.store["user_agent"] == "Imported/1.0"
        # 五个敏感键全部被拒：真令牌没被换掉，掩码也没盖住真值
        assert fake.store.get("mcp_token", "mcp-token-live") == "mcp-token-live"
        assert fake.store.get("SESSDATA", "sess-live-secret") == "sess-live-secret"
        assert fake.store.get("notification_wecom_secret", "") == ""

        ignored = " ".join(result["ignored"])

        for key in ("MCP.mcp_token", "Web Panel.web_panel_token", "Cookie.SESSDATA",
                    "Notification.notification_wecom_secret"):
            assert key in ignored

        assert "敏感项" in ignored

    def test_reset_rotates_tokens_and_sessions(self, panel, monkeypatch):
        """恢复默认后：令牌换新、账号回默认、旧会话全部作废"""
        fake = self.prepare(monkeypatch)

        old_token = panel.token

        cookie = login(panel)

        fake.store["web_panel_token"] = old_token

        status, _ = request(panel, "POST", PANEL_CONFIG_RESET_PATH, body = b"{}", cookie = cookie)

        assert status == 200

        # 令牌换了一枚新的并且落了盘
        assert fake.store["web_panel_token"]
        assert fake.store["web_panel_token"] != old_token
        assert panel.token == fake.store["web_panel_token"]

        # 旧会话作废：拿着 reset 前的 Cookie 再请求就是 401
        status, _ = request(panel, "GET", PANEL_SETTINGS_PATH, cookie = cookie)

        assert status == 401

        # 凭据快照回到默认账号，能直接用默认密码登录
        status, raw = request(panel, "POST", PANEL_LOGIN_PATH,
                              body = json.dumps({"username": "admin", "password": "password"}).encode())

        assert status == 200

        # 旧的面板令牌也一并作废
        status, _ = request(panel, "GET", PANEL_SETTINGS_PATH, token = old_token)

        assert status == 401


class TestNamingValidation:
    """
    命名规则的校验判据。

    与桌面端 EditRuleDialog.validate_rule 一一对应 —— 网页放行、运行期却渲染不
    出来的规则，用户在界面上看不到任何提示：任务建得出来，文件名却是空的
    """

    def test_accepts_a_normal_rule(self):
        assert naming.validate_rule("{leaf_title}", 11) == ""

    def test_rejects_empty_rule(self):
        assert "不能为空" in naming.validate_rule("", 11)

    def test_rejects_leading_or_trailing_separator(self):
        assert naming.validate_rule("/{leaf_title}", 11)
        assert naming.validate_rule("{leaf_title}.", 11)
        assert naming.validate_rule("{leaf_title}/", 11)

    def test_rejects_an_unmatched_delimiter(self):
        assert "配对" in naming.validate_rule("{leaf_title", 11)

    def test_rejects_illegal_literal_characters(self):
        assert "非法字符" in naming.validate_rule("{leaf_title}?x", 11)

    def test_rejects_unknown_variable(self):
        assert "nope" in naming.validate_rule("{nope}", 11)

    def test_rejects_an_optional_segment_without_variables(self):
        assert "可选段" in naming.validate_rule("<abc>{leaf_title}", 11)

    def test_rejects_a_format_spec_that_does_not_fit_the_value(self):
        # 文本变量套 :02d，渲染期必炸。这条只有真渲染一次才拦得住
        assert "渲染不出" in naming.validate_rule("{leaf_title:02d}", 11)

    def test_accepts_a_variable_outside_the_types_recommended_list(self):
        """
        推荐表与键空间是两回事

        个人空间（50）的推荐清单里没有 {parent_title}，但运行期的键空间里恒有它
        （get_variable_data_from_task_info 无条件填齐）。拿推荐表当白名单，会把
        一条本来能用的规则拒掉
        """
        assert naming.validate_rule("{parent_title}/{leaf_title}", 50) == ""

    def test_rejects_an_overlong_rule(self):
        assert naming.validate_rule("{leaf_title}" + "a" * 600, 11)

    def test_optional_segment_is_dropped_when_the_variable_is_blank(self):
        """可选段的语义：段内变量取空值时整段连同前后缀一起丢弃"""
        rows = naming.render_preview("<P{p:02d}->{leaf_title}", 11)

        # 单个视频形态下 p 为 0，P00- 不该留在文件名里
        assert rows[0]["path"] == "游戏科学新作《黑神话：钟馗》先导预告"

    def test_preview_covers_every_shape_of_the_type(self):
        assert len(naming.render_preview("{leaf_title}", 13)) == 2     # 合集：单P + 多P
        assert len(naming.render_preview("{leaf_title}", 11)) == 1     # 单个视频

    def test_shape_labels_tell_the_two_collection_rows_apart(self):
        """合集类型下两行说的是「这一集在稿件内部是单P还是多P」"""
        labels = [row["label"] for row in naming.render_preview("{leaf_title}", 13)]

        assert labels == ["单P条目", "多P条目"]


class TestNamingApi:
    """「命名规则」页的接口：读整表、预览一条规则串、整表保存"""

    def prepare(self, monkeypatch):
        """
        换掉假配置并把跨线程调用折叠成直调

        naming 模块读配置走的是 naming_rules 里那份 `config` 绑定（load_rules /
        save_rules 都在用它），与 server 自己那份是两个名字，两处都得换
        """
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        monkeypatch.setattr("util.common.naming_rules.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        return fake

    def get_payload(self, panel):
        status, raw = request(panel, "GET", PANEL_NAMING_PATH, cookie = login(panel))

        assert status == 200

        return json.loads(raw)

    def post(self, panel, path, body):
        return request(panel, "POST", path, body = json.dumps(body).encode(), cookie = login(panel))

    # ---- 读 ----

    def test_get_returns_rules_types_and_builtin(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        d = self.get_payload(panel)

        assert len(d["rules"]) == len(DefaultValue.naming_rule_list)
        assert len(d["builtin"]) == len(DefaultValue.naming_rule_list)

        # 每条规则都带页面需要的六样：name 是显示名，name_key 是存回去的原值
        for rule in d["rules"]:
            assert set(rule) == {"id", "name", "name_key", "type", "rule", "default"}

        # 类型目录覆盖全部 ConventionType，且每个类型都有变量表与预览形态
        assert [t["value"] for t in d["types"]] == [
            int(value) for value in convention_type_map.values()
        ]

        for entry in d["types"]:
            assert entry["label"]
            assert entry["vars"] and all(v["var"].startswith("{") for v in entry["vars"])
            assert entry["shapes"] and all(s["label"] for s in entry["shapes"])

    def test_get_requires_login(self, panel):
        assert request(panel, "GET", PANEL_NAMING_PATH)[0] == 401

    def test_mixed_entry_types_are_flagged(self, panel, monkeypatch):
        """来源类类型要告诉页面：混在里面的影视 / 课程条目不走这条规则"""
        self.prepare(monkeypatch)

        types = {t["value"]: t for t in self.get_payload(panel)["types"]}

        assert types[60]["mixed"]        # 历史记录里混着影视与课程
        assert types[20]["mixed"] == []  # 影视类型自己没混别的

    # ---- 预览 ----

    def test_preview_renders_every_shape(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NAMING_PREVIEW_PATH,
                                {"rule": "{parent_title}/P{p:02d}-{leaf_title}", "type": 12})

        assert status == 200

        d = json.loads(raw)

        assert d["valid"] is True
        assert d["error"] == ""
        assert len(d["rows"]) == 1
        assert "P04-" in d["rows"][0]["path"]

    def test_preview_reports_an_invalid_rule_as_200(self, panel, monkeypatch):
        """
        规则串不合法回的是 200 + valid=false

        用户打字打到一半必然经过一串中间态，那不是"请求错了"，
        页面也不该把它当成失败处理
        """
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NAMING_PREVIEW_PATH, {"rule": "{nope}", "type": 11})

        assert status == 200

        d = json.loads(raw)

        assert d["valid"] is False
        assert "nope" in d["error"]
        assert d["rows"] == []

    def test_preview_requires_a_type(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_NAMING_PREVIEW_PATH, {"rule": "{leaf_title}"})

        assert status == 400

    def test_preview_rejects_an_unknown_type(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_NAMING_PREVIEW_PATH, {"rule": "{leaf_title}", "type": 999})

        assert status == 400

    def test_preview_requires_login(self, panel):
        status, _ = request(panel, "POST", PANEL_NAMING_PREVIEW_PATH,
                            body = json.dumps({"rule": "{leaf_title}", "type": 11}).encode())

        assert status == 401

    # ---- 保存 ----

    def test_save_writes_the_whole_table(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["rule"] = "{leaf_title} [{aid}]"

        status, _ = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 200

        saved = fake.store["naming_rule_list"]

        assert saved[0]["rule"] == "{leaf_title} [{aid}]"
        # 顺序原样保留：它是下载选项下拉框里的呈现次序
        assert [r["id"] for r in saved] == [r["id"] for r in rules]

    def test_save_assigns_ids_to_new_rules(self, panel, monkeypatch):
        """id 留空由服务端补：网页上拿不到 crypto.randomUUID（它不是安全上下文）"""
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules.append({
            "id": "", "name": "我的规则", "name_key": "",
            "type": 11, "rule": "{bvid}_{leaf_title}", "default": False,
        })

        status, raw = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 200

        new_id = json.loads(raw)["rules"][-1]["id"]

        assert len(new_id) == 36
        assert new_id == fake.store["naming_rule_list"][-1]["id"]

    def test_save_keeps_the_translation_key_when_the_name_is_untouched(self, panel, monkeypatch):
        """
        内置规则的名字存的是翻译键

        页面上看到的是译名，原样存回时必须把键还原 —— 存了译名，界面语言一换
        这条规则的名字就固定在旧语言上了（与桌面端 resolve_name() 同一条纪律）
        """
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]

        assert rules[0]["name_key"] == "DEFAULT_FOR_NORMAL"
        assert rules[0]["name"] != rules[0]["name_key"]

        self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert fake.store["naming_rule_list"][0]["name"] == "DEFAULT_FOR_NORMAL"

    def test_save_stores_the_literal_when_the_name_changed(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["name"] = "我的单视频规则"

        self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert fake.store["naming_rule_list"][0]["name"] == "我的单视频规则"

    def test_save_rejects_an_unknown_variable(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["rule"] = "{nope}"

        status, raw = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 400
        assert "nope" in json.loads(raw)["error"]
        # 整份拒收：写进去一半会让配置停在自相矛盾的状态（同类型两条默认规则）
        assert "naming_rule_list" not in fake.store

    def test_save_rejects_duplicate_names_within_a_type(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules.append({
            "id": "", "name": rules[0]["name"], "name_key": "",
            "type": rules[0]["type"], "rule": "{leaf_title}", "default": False,
        })

        status, raw = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 400
        assert "重名" in json.loads(raw)["error"]

    def test_save_rejects_an_empty_table(self, panel, monkeypatch):
        """清空之后每个类型都没有规则可用，运行期会退化成 {leaf_title}"""
        self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": []})

        assert status == 400

    def test_save_requires_one_default_rule_per_type(self, panel, monkeypatch):
        """
        每个有规则的类型必须有且只有一条默认规则

        一条都没有时 FileNameFormatter 按「type 相同且 default 为真」取不到规则，
        会退化成 {leaf_title}：文件名全错，且整个过程不报错
        """
        self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["default"] = False

        status, raw = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 400
        assert "默认规则" in json.loads(raw)["error"]

    def test_save_converges_duplicate_defaults(self, panel, monkeypatch):
        """同类型提交两条默认时只留最后一条，与桌面端 _set_default_rule 一致"""
        fake = self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["default"] = True
        rules.append({
            "id": "", "name": "另一条", "name_key": "",
            "type": rules[0]["type"], "rule": "{bvid}", "default": True,
        })

        status, _ = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 200

        saved = fake.store["naming_rule_list"]

        assert [r["default"] for r in saved if r["type"] == rules[0]["type"]] == [False, True]

    def test_save_rejects_control_characters_in_the_name(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        rules = [dict(r) for r in self.get_payload(panel)["rules"]]
        rules[0]["name"] = "换\n行"

        status, _ = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": rules})

        assert status == 400

    def test_save_rejects_a_malformed_entry(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_NAMING_SAVE_PATH, {"rules": ["不是字典"]})

        assert status == 400

    def test_save_requires_login(self, panel):
        status, _ = request(panel, "POST", PANEL_NAMING_SAVE_PATH,
                            body = json.dumps({"rules": []}).encode())

        assert status == 401


def alias(**kwargs):
    """一条识别规则，字段都给了默认值，用例里只写关心的那几个"""
    entry = {
        "id": "", "enabled": True,
        "field": "season_title", "mode": "contains",
        "match": "西游记续集", "title": "西游记",
        "season": 2, "year": "1986", "tmdb": "13923", "note": "",
    }
    entry.update(kwargs)

    return entry


class TestAliasMatching:
    """
    名称识别的匹配与应用（util/common/naming_alias.py）

    B站把一部剧的每一季拆成独立 season，`{season_title}` 拿到「西游记续集」这种
    并列名，`{season_number}` 是它在 seasons 列表里的序号，年份与 TMDB 编号则
    根本不是 B站提供的字段 —— 这些只能由用户写下的识别规则来纠正
    """

    def data(self, **kwargs):
        base = {
            "season_title": "西游记续集",
            "series_title": "央视版四大名著",
            "season_number": 2,
            "year": "",
            "tmdb_id": "",
        }
        base.update(kwargs)

        return base

    def test_empty_table_matches_nothing(self):
        assert match_alias(self.data(), []) is None

    def test_contains_hits_the_substring(self):
        assert match_alias(self.data(), [alias()]) is not None

    def test_exact_does_not_hit_a_longer_title(self):
        entry = alias(mode="exact")

        assert match_alias(self.data(season_title="西游记续集 4K修复"), [entry]) is None

    def test_regex_hits(self):
        assert match_alias(self.data(), [alias(mode="regex", match="西游.*续集")]) is not None

    def test_a_broken_regex_is_skipped_instead_of_raising(self):
        """手改配置写坏正则时跳过这一条，不能让整次命名失败"""
        assert match_alias(self.data(), [alias(mode="regex", match="西游[")]) is None

    def test_matches_on_series_title(self):
        entry = alias(field="series_title", match="央视版四大名著")

        assert match_alias(self.data(), [entry]) is not None

    def test_any_field_matches_either_side(self):
        entry = alias(field="any", match="央视版四大名著")

        assert match_alias(self.data(), [entry]) is not None

    def test_first_match_wins(self):
        """顺序即优先级：contains 下「西游」会吃掉「西游记续集」"""
        broad = alias(match="西游", title="宽泛")
        narrow = alias(match="西游记续集", title="具体")

        assert match_alias(self.data(), [broad, narrow])["title"] == "宽泛"
        assert match_alias(self.data(), [narrow, broad])["title"] == "具体"

    def test_disabled_entries_are_skipped(self):
        assert match_alias(self.data(), [alias(enabled=False)]) is None

    def test_apply_rewrites_title_season_and_the_two_new_variables(self):
        data = apply_to_data(self.data(), [alias()])

        assert data["season_title"] == "西游记"
        assert data["season_number"] == 2
        assert data["year"] == "1986"
        assert data["tmdb_id"] == "13923"

    def test_apply_keeps_series_title(self):
        """系列标题是「央视版四大名著」这类来源信息，抹掉用户反而看不出这条从哪来"""
        assert apply_to_data(self.data(), [alias()])["series_title"] == "央视版四大名著"

    def test_apply_without_a_title_only_fills_the_numbers(self):
        data = apply_to_data(self.data(), [alias(title="", season=3)])

        assert data["season_title"] == "西游记续集"   # 没给剧名就不改名
        assert data["season_number"] == 3
        assert data["year"] == "1986"

    def test_apply_does_nothing_when_nothing_matches(self):
        original = self.data(season_title="轻音少女 第二季")

        assert apply_to_data(original, [alias()]) == original


class TestAliasValidation:
    """识别规则的校验（normalize_entry / normalize_entries）"""

    def test_normalizes_a_complete_entry(self):
        cleaned = normalize_entry(alias())

        assert cleaned["field"] == "season_title"
        assert cleaned["mode"] == "contains"
        assert cleaned["season"] == 2
        assert cleaned["year"] == "1986"
        assert cleaned["tmdb"] == "13923"
        assert len(cleaned["id"]) == 36              # 服务端补的 uuid4

    def test_rejects_an_empty_match(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(match=""))

    def test_rejects_an_unknown_field(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(field="nope"))

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(mode="nope"))

    def test_rejects_a_broken_regex(self):
        with pytest.raises(AliasError) as info:
            normalize_entry(alias(mode="regex", match="西游["))

        assert "正则" in str(info.value)

    def test_rejects_a_non_numeric_season(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(season="二"))

    def test_rejects_an_out_of_range_season(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(season=0))

        with pytest.raises(AliasError):
            normalize_entry(alias(season=1000))

    def test_rejects_a_year_that_is_not_four_digits(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(year="86"))

        with pytest.raises(AliasError):
            normalize_entry(alias(year="1986年"))

    def test_rejects_a_non_numeric_tmdb_id(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(tmdb="tt13923"))

    def test_rejects_an_entry_that_changes_nothing(self):
        """只写了个匹配内容、什么都没改的规则是有害的：它会吃掉后面的规则"""
        with pytest.raises(AliasError) as info:
            normalize_entry(alias(title="", season="", year="", tmdb=""))

        assert "没改任何东西" in str(info.value)

    def test_rejects_illegal_characters_in_the_title(self):
        with pytest.raises(AliasError):
            normalize_entry(alias(title="西游/记"))

    def test_keeps_an_existing_id(self):
        assert normalize_entry(alias(id="fixed-id"))["id"] == "fixed-id"

    def test_replaces_a_duplicated_id(self):
        seen = set()

        first = normalize_entry(alias(id="same"), seen)
        second = normalize_entry(alias(id="same", match="另一条"), seen)

        assert first["id"] != second["id"]

    def test_an_empty_season_is_stored_as_null(self):
        assert normalize_entry(alias(season=""))["season"] is None

    def test_rejects_two_entries_with_the_same_condition(self):
        with pytest.raises(AliasError) as info:
            normalize_entries([alias(), alias(id="x")])

        assert "完全相同" in str(info.value)

    def test_accepts_an_empty_table(self):
        """清空是合法操作：识别表出厂就是空的"""
        assert normalize_entries([]) == []

    def test_rejects_a_non_list(self):
        with pytest.raises(AliasError):
            normalize_entries({"not": "a list"})


class TestAliasShadowing:
    """顺序即优先级：被上面的规则遮住、永远轮不到的条目要点出来"""

    def test_detects_a_shadowed_entry(self):
        entries = normalize_entries([alias(match="西游"), alias(match="西游记续集")])

        assert shadowed_indexes(entries) == [1]

    def test_specific_first_has_no_shadow(self):
        entries = normalize_entries([alias(match="西游记续集"), alias(match="西游记")])

        assert shadowed_indexes(entries) == []

    def test_exact_mode_is_never_shadowed_by_a_contains(self):
        entries = normalize_entries([
            alias(match="西游记"),
            alias(mode="exact", match="西游记续集"),
        ])

        assert shadowed_indexes(entries) == []

    def test_different_fields_do_not_shadow_each_other(self):
        entries = normalize_entries([
            alias(match="西游记", field="season_title"),
            alias(match="西游记续集", field="series_title"),
        ])

        assert shadowed_indexes(entries) == []


class TestDoneClearApi:
    """「已完成」页的清理记录接口

    语义是**只删记录、不删文件**，所以既要验它删对了表（completed=True，
    误删成 download_task 会把正在下载的任务抹掉），也要验空表时不白提交一次事务
    """

    def prepare(self, monkeypatch, tasks):
        import sys
        import types

        calls = []
        module = types.ModuleType("util.download.task.manager")
        module.task_manager = types.SimpleNamespace(
            query = lambda completed = False: list(tasks),
            delete_many = lambda task_list, completed = False: calls.append((list(task_list), completed)),
        )

        # 真的那个模块在 import 时就会 TaskManager()，测试环境里没有它需要的运行时。
        # 预置 sys.modules 让函数内的 `from ..download.task.manager import task_manager`
        # 直接命中这份替身
        monkeypatch.setitem(sys.modules, "util.download.task.manager", module)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        return calls

    def post(self, panel, **kw):
        return request(panel, "POST", PANEL_DONE_CLEAR_PATH, body = b"{}", **kw)

    def test_requires_login(self, panel):
        assert self.post(panel)[0] == 401

    def test_clears_the_completed_table_only(self, panel, monkeypatch):
        calls = self.prepare(monkeypatch, [object(), object()])

        status, raw = self.post(panel, cookie = login(panel))

        assert status == 200
        assert json.loads(raw)["removed"] == 2
        assert len(calls) == 1
        assert len(calls[0][0]) == 2

        # completed=True 是要害：写成 False 就会去删 download_task 表
        assert calls[0][1] is True

    def test_empty_table_skips_the_write(self, panel, monkeypatch):
        calls = self.prepare(monkeypatch, [])

        status, raw = self.post(panel, cookie = login(panel))

        assert status == 200
        assert json.loads(raw)["removed"] == 0
        assert calls == []


class TestIdentifyApi:
    """「名称识别」页的接口：读整表、预览一条规则、整表保存"""

    def prepare(self, monkeypatch):
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        # match_alias / apply_to_data 读的是它们自己模块里那份 config 绑定
        monkeypatch.setattr("util.common.naming_alias.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        return fake

    def get_payload(self, panel):
        status, raw = request(panel, "GET", PANEL_IDENTIFY_PATH, cookie = login(panel))

        assert status == 200

        return json.loads(raw)

    def post(self, panel, path, body):
        return request(panel, "POST", path, body = json.dumps(body).encode(), cookie = login(panel))

    # ---- 读 ----

    def test_get_returns_the_table_and_the_two_dropdowns(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        d = self.get_payload(panel)

        assert d["aliases"] == []
        assert [f["value"] for f in d["fields"]] == ["season_title", "series_title", "any"]
        assert [m["value"] for m in d["modes"]] == ["contains", "exact", "regex"]

        # 预览要用它渲染，取的是配置里影视类型的默认规则
        assert "{season_title}" in d["bangumi_rule"]

    def test_get_requires_login(self, panel):
        assert request(panel, "GET", PANEL_IDENTIFY_PATH)[0] == 401

    def test_get_shapes_a_dirty_entry_without_blowing_up(self, panel, monkeypatch):
        """用户手改过配置文件、条目缺键时页面也得能打开"""
        fake = self.prepare(monkeypatch)

        fake.store["naming_alias_list"] = [{"match": "西游记续集"}]

        entries = self.get_payload(panel)["aliases"]

        assert len(entries) == 1
        assert entries[0]["field"] == "season_title"
        assert entries[0]["enabled"] is True

    # ---- 预览 ----

    def test_preview_renders_before_and_after(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_IDENTIFY_PREVIEW_PATH, {"alias": alias()})

        assert status == 200

        d = json.loads(raw)

        assert d["valid"] is True
        assert d["error"] == ""
        assert d["before"]["season_title"] == "西游记续集"
        assert d["after"]["season_title"] == "西游记"
        assert d["after"]["year"] == "1986"
        assert d["after"]["tmdb_id"] == "13923"

    def test_preview_exposes_the_blank_year_before_matching(self, panel, monkeypatch):
        """识别前 year / tmdb_id 是空的 —— 样本里的示例值必须先清掉"""
        self.prepare(monkeypatch)

        _, raw = self.post(panel, PANEL_IDENTIFY_PREVIEW_PATH, {"alias": alias()})
        d = json.loads(raw)

        assert d["before"]["year"] == ""
        assert d["before"]["tmdb_id"] == ""

    def test_preview_returns_200_with_valid_false_on_a_bad_rule(self, panel, monkeypatch):
        """校验不过是 200 + valid=false：用户打字打到一半必然经过非法中间态"""
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_IDENTIFY_PREVIEW_PATH, {"alias": alias(match="")})

        assert status == 200

        d = json.loads(raw)

        assert d["valid"] is False
        assert d["error"]
        assert d["path_after"] == ""

    def test_preview_requires_the_alias(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.post(panel, PANEL_IDENTIFY_PREVIEW_PATH, {})[0] == 400

    def test_preview_requires_login(self, panel):
        status, _ = request(panel, "POST", PANEL_IDENTIFY_PREVIEW_PATH,
                            body = json.dumps({"alias": alias()}).encode())

        assert status == 401

    # ---- 保存 ----

    def test_save_writes_the_table(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_IDENTIFY_SAVE_PATH, {"aliases": [alias()]})

        assert status == 200

        saved = fake.store["naming_alias_list"]

        assert len(saved) == 1
        assert saved[0]["match"] == "西游记续集"
        assert saved[0]["season"] == 2

        # 回给页面的那份带上服务端补的 id 与遮蔽标记
        body = json.loads(raw)

        assert len(body["aliases"][0]["id"]) == 36
        assert body["aliases"][0]["shadowed"] is False

    def test_save_rejects_a_bad_entry_and_writes_nothing(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_IDENTIFY_SAVE_PATH,
                              {"aliases": [alias(), alias(id="x", match="")]})

        assert status == 400
        assert "naming_alias_list" not in fake.store

    def test_save_rejects_a_duplicated_condition(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_IDENTIFY_SAVE_PATH,
                                {"aliases": [alias(), alias(id="x")]})

        assert status == 400
        assert "完全相同" in json.loads(raw)["error"]

    def test_save_accepts_an_empty_table(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        assert self.post(panel, PANEL_IDENTIFY_SAVE_PATH, {"aliases": []})[0] == 200
        assert fake.store["naming_alias_list"] == []

    def test_save_rejects_a_non_list(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.post(panel, PANEL_IDENTIFY_SAVE_PATH, {"aliases": "nope"})[0] == 400

    def test_save_marks_a_shadowed_entry(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        _, raw = self.post(panel, PANEL_IDENTIFY_SAVE_PATH,
                           {"aliases": [alias(match="西游"), alias(match="西游记续集")]})

        assert [a["shadowed"] for a in json.loads(raw)["aliases"]] == [False, True]

    def test_save_requires_login(self, panel):
        status, _ = request(panel, "POST", PANEL_IDENTIFY_SAVE_PATH,
                            body = json.dumps({"aliases": []}).encode())

        assert status == 401


class TestTmdbLookup:
    """
    TMDB 链接 → 剧名 / 年份 / 编号 / 季号

    解析与拼装都是纯函数，一律离线测：夹具是 2026-10 从 TMDB 真页面上裁下来的
    片段。真去连 TMDB 的话，测试会随对方改版或本机断网而红 —— 那不是"我们改坏了"，
    却要在每次跑测试时解释一遍。真链路由验收脚本单独跑。
    """

    # themoviedb.org/tv/62591 那页里的一截，连外面的 CDATA 注释一起裁下来 ——
    # 真页面就是长这样，去掉包装反而测不到 _strip_json_ld_wrapper
    PAGE = (
        '<!DOCTYPE html><html><head>'
        '<title>巴啦啦小魔仙 (TV Series 2008) &#8212; The Movie Database (TMDB)</title>'
        '<script type="application/ld+json">'
        '/* <![CDATA[ */'
        '{"@context":"https://schema.org","@type":"TVSeries","name":"巴啦啦小魔仙",'
        '"numberOfEpisodes":52,"startDate":"2008-04-12T00:00:00Z","endDate":"2008-06-02T00:00:00Z"}'
        '/* ]]> */'
        '</script></head><body></body></html>'
    )

    def post(self, panel, body):
        return request(panel, "POST", PANEL_IDENTIFY_TMDB_PATH,
                       body = json.dumps(body).encode(), cookie = login(panel))

    # ---- 链接解析 ----

    def test_takes_a_plain_tv_link(self):
        assert identify.parse_tmdb_link("https://www.themoviedb.org/tv/62591") == {
            "kind": "tv", "tmdb": "62591", "season": None,
        }

    def test_takes_the_season_from_the_link(self):
        link = identify.parse_tmdb_link("https://www.themoviedb.org/tv/62591/season/2?language=zh-CN")

        assert link["tmdb"] == "62591"
        assert link["season"] == 2

    def test_takes_a_movie_link_without_a_scheme(self):
        assert identify.parse_tmdb_link("themoviedb.org/movie/12345/") == {
            "kind": "movie", "tmdb": "12345", "season": None,
        }

    def test_takes_a_bare_id(self):
        assert identify.parse_tmdb_link("  62591 ")["tmdb"] == "62591"

    def test_refuses_text_that_is_not_a_link(self):
        """
        没认出链接时必须给 None，不能猜

        「西游记 1986」里也有数字，随便挑一个当编号会**默默写到别的片子**上 ——
        页面还会显示"已填入"，用户根本不知道填错了
        """
        assert identify.parse_tmdb_link("西游记 1986") is None
        assert identify.parse_tmdb_link("") is None
        assert identify.parse_tmdb_link(None) is None

    # ---- 页面解析 ----

    def test_reads_name_and_premiere_year_from_json_ld(self):
        assert identify.parse_tmdb_page(self.PAGE) == {"title": "巴啦啦小魔仙", "year": "2008"}

    def test_falls_back_to_the_title_tag(self):
        """JSON-LD 没了（改版）也要能撑住 —— 退回标题标签里的「名字 (年份)」"""
        page = '<title>让子弹飞 (2010) &#8212; The Movie Database (TMDB)</title>'

        assert identify.parse_tmdb_page(page) == {"title": "让子弹飞", "year": "2010"}

    def test_does_not_mistake_a_placeholder_page_for_a_title(self):
        """登录页 / 出错页的标题里也有 TMDB 字样，别把它当成片名填进去"""
        assert identify.parse_tmdb_page("<title>登录 — The Movie Database (TMDB)</title>")["title"] == ""

    def test_survives_a_broken_json_ld_block(self):
        page = '<script type="application/ld+json">{不是 json}</script><title>甲 (2020) — x</title>'

        assert identify.parse_tmdb_page(page) == {"title": "甲", "year": "2020"}

    def test_gives_empty_strings_when_nothing_can_be_read(self):
        assert identify.parse_tmdb_page("<html><body>空</body></html>") == {"title": "", "year": ""}
        assert identify.parse_tmdb_page(None) == {"title": "", "year": ""}

    # ---- 串起来（fetch 注入，不碰网络）----

    def test_lookup_returns_the_fields_the_form_needs(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return self.PAGE

        d = identify.tmdb_lookup("https://www.themoviedb.org/tv/62591", fetch = fetch)

        assert d == {
            "ok": True, "title": "巴啦啦小魔仙", "year": "2008", "tmdb": "62591",
            "kind": "tv", "season": 1, "season_from_url": False,
        }

        # 必须带语言参数，否则拿到的是 "Balala the Fairies"
        assert urls == ["https://www.themoviedb.org/tv/62591?language=zh-CN"]

    def test_lookup_prefers_the_season_written_in_the_link(self):
        d = identify.tmdb_lookup("https://www.themoviedb.org/tv/62591/season/2",
                                 fetch = lambda url: self.PAGE)

        assert d["season"] == 2
        assert d["season_from_url"] is True

    def test_lookup_reports_a_link_it_cannot_read(self):
        d = identify.tmdb_lookup("随便什么", fetch = lambda url: self.PAGE)

        assert d["ok"] is False
        assert "TMDB 链接" in d["error"]

    def test_lookup_reports_a_fetch_failure(self):
        def fetch(url):
            raise identify.TmdbError("连不上 TMDB")

        d = identify.tmdb_lookup("https://www.themoviedb.org/tv/62591", fetch = fetch)

        assert d["ok"] is False
        assert d["error"] == "连不上 TMDB"

    def test_lookup_reports_a_page_it_cannot_parse(self):
        """页面能打开但解析不出片名 —— 要明说让人工填，不能回一份空字段当成功"""
        d = identify.tmdb_lookup("https://www.themoviedb.org/tv/62591", fetch = lambda url: "<html/>")

        assert d["ok"] is False
        assert "手工填写" in d["error"]

    # ---- HTTP 层 ----

    def test_api_wires_the_link_through(self, panel, monkeypatch):
        monkeypatch.setattr(identify, "_fetch_tmdb_page", lambda url: self.PAGE)

        status, raw = self.post(panel, {"link": "https://www.themoviedb.org/tv/62591"})

        assert status == 200

        d = json.loads(raw)

        assert d["ok"] is True
        assert d["title"] == "巴啦啦小魔仙"
        assert d["year"] == "2008"
        assert d["tmdb"] == "62591"

    def test_api_requires_login(self, panel):
        status, _ = request(panel, "POST", PANEL_IDENTIFY_TMDB_PATH,
                            body = json.dumps({"link": "x"}).encode())

        assert status == 401

    def test_api_rejects_a_missing_link(self, panel):
        assert self.post(panel, {})[0] == 400
        assert self.post(panel, {"link": "   "})[0] == 400
        assert self.post(panel, {"link": 62591})[0] == 400


class TestFavoritesApi:
    """
    GET /api/panel/favorites

    取数那一步在这里被换掉：这一层要验的是"HTTP 这一层怎么应答"，
    真去连 B站是 work 脚本的职责
    """

    LIST = {"code": 0, "data": {"list": [
        {"id": 3706531, "mid": 3690988658755978, "title": "默认收藏夹", "media_count": 128},
    ]}}

    def get(self, panel, query = "", cookie = None):
        return request(
            panel, "GET", PANEL_FAVORITES_PATH + query,
            cookie = login(panel) if cookie is None else cookie,
        )

    def test_requires_the_panel_session(self, panel):
        assert self.get(panel, "?kind=favorite", cookie = "")[0] == 401

    def test_rejects_an_unknown_kind(self, panel):
        status, raw = self.get(panel, "?kind=nonsense")

        assert status == 400
        assert "分类" in raw.decode("utf-8")

    def test_rejects_a_page_that_is_not_a_number(self, panel):
        assert self.get(panel, "?kind=follow&page=abc")[0] == 400
        assert self.get(panel, "?kind=follow&page=0")[0] == 400

    def test_defaults_to_the_favorites_tab(self, panel, monkeypatch):
        """不带 kind 时按「收藏夹」算，页面第一次进来就是这一档"""
        monkeypatch.setattr(favorites, "current_uid", lambda: "42")
        monkeypatch.setattr(favorites, "_fetch_json", lambda url: self.LIST)

        status, raw = self.get(panel)

        assert status == 200

        body = json.loads(raw)

        assert body["ok"] is True and body["kind"] == "favorite"
        assert body["entries"][0]["title"] == "默认收藏夹"

    def test_says_login_is_needed_instead_of_failing(self, panel, monkeypatch):
        """
        没登录 B站账号不是"服务器出错了"：要回 200 + 一句能照做的提示

        这里回 500 的话，页面上只会显示"服务器错误"，用户不知道要去扫码
        """
        monkeypatch.setattr(favorites, "current_uid", lambda: "")

        status, raw = self.get(panel, "?kind=favorite")

        assert status == 200

        body = json.loads(raw)

        assert body["ok"] is False
        assert body["need_login"] is True
        assert "B站账号" in body["error"]

    def test_the_list_less_categories_answer_without_asking_bilibili(self, panel, monkeypatch):
        """
        稍后再看 / 历史记录没有列表接口，回的是"该去解析哪个地址"

        顺带守住"别发那次注定无用的请求"：取数用会爆炸的替身顶掉，
        真发了这条用例就红，而不是悄悄多打一次 B站
        """
        def boom(url):
            raise AssertionError("这两类不该去取列表")

        monkeypatch.setattr(favorites, "_fetch_json", boom)
        monkeypatch.setattr(favorites, "current_uid", lambda: "42")

        for kind, address in (("watch_later", "bili23://watch_later"),
                              ("history", "bili23://history")):
            status, raw = self.get(panel, "?kind=" + kind)

            assert status == 200, kind

            body = json.loads(raw)

            assert body["ok"] is True and body["direct_parse"] is True, kind
            assert body["parse_url"] == address, kind
            assert body["label"], kind
            # 一张卡片都不该有：前端据此直接铺条目
            assert body["entries"] == [], kind

    def test_the_list_less_categories_do_not_require_a_bili_login(self, panel, monkeypatch):
        """
        回这段元数据不需要登录态 —— 登录与否由解析那一步管（解析器里的 check_login）

        在这里先拦一道的话，"没登录"会多出一个出处，还多一次注定失败的往返
        """
        monkeypatch.setattr(favorites, "current_uid", lambda: "")

        status, raw = self.get(panel, "?kind=watch_later")

        assert status == 200
        assert json.loads(raw)["direct_parse"] is True


class TestAliasInFileName:
    """
    识别结果要真的出现在运行期渲染的文件名里

    这一条走的是完整链路：TaskInfo → get_variable_data_from_task_info →
    apply_to_data → FileNameFormatter.format，与下载时建任务用的是同一条。
    只测 apply_to_data 的话，"接上了没有"这件事没人验
    """

    # 带年份与 TMDB 段的影视规则：两处都用可选段包着，没命中时整段消失
    RULE = ("{season_title}< ({year})>< {{tmdb-{tmdb_id}}}>/Season {season_number}"
            "/{season_title}<.{year}>.S{season_number:02d}E{episode_number:02d}.{video_quality}")

    def prepare(self, monkeypatch, entries):
        fake = FakeConfig()
        fake.store["naming_alias_list"] = entries

        monkeypatch.setattr("util.common.naming_alias.config", fake)

        return fake

    def render(self, monkeypatch, season_title, entries):
        self.prepare(monkeypatch, entries)

        from util.download.task.info import TaskInfo
        from util.format.file_name import FileNameFormatter
        from util.parse.episode.tree import Attribute

        info = TaskInfo()
        info.Episode.attribute = Attribute.BANGUMI_BIT
        info.Episode.season_title = season_title
        info.Episode.series_title = "央视版四大名著"
        info.Episode.season_number = 2
        info.Episode.episode_number = 1
        info.Episode.episode_title = "第1话"
        info.Episode.video_quality = "AI Upscale"

        formatter = FileNameFormatter()
        formatter.set_rule(self.RULE)
        formatter.set_variable_data(info)

        return formatter.format() or ""

    def test_a_matched_entry_lands_in_the_rendered_path(self, monkeypatch):
        path = self.render(monkeypatch, "西游记续集", normalize_entries([alias()]))

        assert path == "西游记 (1986) {tmdb-13923}/Season 2/西游记.1986.S02E01.AI Upscale"

    def test_optional_segments_vanish_when_nothing_matches(self, monkeypatch):
        """没命中时年份与 TMDB 段整段消失，不留 "()" 也不留 "{}" """
        path = self.render(monkeypatch, "轻音少女 第二季", normalize_entries([alias()]))

        assert path == "轻音少女 第二季/Season 2/轻音少女 第二季.S02E01.AI Upscale"

    def test_the_rule_itself_passes_validation(self):
        """上面的规则必须是命名规则页也放行的 —— 否则用户照着配会被拒"""
        assert naming.validate_rule(self.RULE, 20) == ""


class TestParseQuery:
    """GET 路由的参数来源：查询串 -> 扁平字典"""

    def test_reads_plain_pairs(self):
        assert parse_query("/api/panel/logs?file=app.log&lines=100") == {"file": "app.log", "lines": "100"}

    def test_no_query_is_empty(self):
        assert parse_query("/api/panel/logs") == {}
        assert parse_query("/api/panel/logs?") == {}

    def test_last_value_wins(self):
        # 取第一个会让 ?lines=100&lines=99999 绕过上限
        assert parse_query("/x?lines=100&lines=5000")["lines"] == "5000"

    def test_values_are_url_decoded(self):
        assert parse_query("/x?file=app%2Elog")["file"] == "app.log"

class TestLogsApi:
    """
    日志页接口。

    可读范围由服务端自己列出来（日志目录里的 *.log*），请求方给的是文件名
    而不是路径 —— 「读日志」这个功能不该顺带变成任意文件读取
    """

    SAMPLE = "\n".join((
        "[2026-10-01 20:00:00.000000] - util.web.server - INFO - at server.py:1 in f: 启动",
        "[2026-10-01 20:00:01.000000] - util.web.server - WARNING - at server.py:2 in f: 慢",
        "[2026-10-01 20:00:02.000000] - util.web.server - ERROR - at server.py:3 in f: 挂了",
        "[2026-10-01 20:00:03.000000] - util.web.server - INFO - at server.py:4 in f: 消息里提到 ERROR 字样",
    ))

    def prepare(self, monkeypatch, tmp_path):
        """造一个假日志目录，并让服务端认为它就是日志目录"""
        logs = tmp_path / "logs"
        logs.mkdir()

        (logs / "app.log").write_text(self.SAMPLE, encoding = "utf-8")
        (logs / "crash.log").write_text("原始转储，不是日志的行格式", encoding = "utf-8")

        monkeypatch.setattr("util.web.server._logs_dir", lambda: logs)

        return logs

    def read(self, panel, query = ""):
        return request(panel, "GET", PANEL_LOGS_PATH + query, cookie = login(panel))

    def test_requires_login(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        assert request(panel, "GET", PANEL_LOGS_PATH)[0] == 401

    def test_token_also_opens_it(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        assert request(panel, "GET", PANEL_LOGS_PATH, token = TOKEN)[0] == 200

    def test_lists_the_files_app_log_first(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        status, raw = self.read(panel)

        assert status == 200

        data = json.loads(raw)

        assert [f["key"] for f in data["files"]] == ["app.log", "crash.log"]
        assert data["file"] == "app.log"

    def test_returns_the_tail(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        data = json.loads(self.read(panel)[1])

        assert data["matched"] == 4
        assert data["truncated"] is False
        assert data["lines"][-1].endswith("消息里提到 ERROR 字样")

    def test_line_limit_keeps_the_newest(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        data = json.loads(self.read(panel, "?lines=2")[1])

        assert len(data["lines"]) == 2
        assert data["matched"] == 4
        assert data["truncated"] is True
        assert data["lines"][0].endswith("挂了")

    def test_level_filter_matches_the_level_field_not_the_message(self, panel, monkeypatch, tmp_path):
        # 末行是 INFO，只是消息里出现了 "ERROR" 字样，不该被当成错误行捞出来
        self.prepare(monkeypatch, tmp_path)

        data = json.loads(self.read(panel, "?level=error")[1])

        assert len(data["lines"]) == 1
        assert "挂了" in data["lines"][0]

    def test_warn_filter(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        data = json.loads(self.read(panel, "?level=warn")[1])

        assert len(data["lines"]) == 1
        assert "慢" in data["lines"][0]

    def test_free_form_file_is_only_visible_under_all(self, panel, monkeypatch, tmp_path):
        # crash.log 是 faulthandler 的原始转储，没有级别字段
        self.prepare(monkeypatch, tmp_path)

        assert len(json.loads(self.read(panel, "?file=crash.log")[1])["lines"]) == 1
        assert json.loads(self.read(panel, "?file=crash.log&level=error")[1])["lines"] == []

    def test_unknown_file_is_refused(self, panel, monkeypatch, tmp_path):
        self.prepare(monkeypatch, tmp_path)

        assert self.read(panel, "?file=nope.log")[0] == 400

    def test_traversal_is_refused(self, panel, monkeypatch, tmp_path):
        # 文件名必须命中服务端列出的那份，路径穿不出去
        self.prepare(monkeypatch, tmp_path)

        assert self.read(panel, "?file=../../etc/passwd")[0] == 400

    @pytest.mark.parametrize("query", ["?lines=0", "?lines=99999", "?lines=abc", "?level=trace"])
    def test_bad_parameters_are_refused(self, panel, monkeypatch, tmp_path, query):
        self.prepare(monkeypatch, tmp_path)

        assert self.read(panel, query)[0] == 400

    def test_missing_log_directory_is_reported_not_crashed(self, panel, monkeypatch):
        # 还没打过日志时目录可能不存在，界面要能给出提示而不是 500
        monkeypatch.setattr("util.web.server._logs_dir", lambda: None)

        status, raw = self.read(panel)

        assert status == 200

        data = json.loads(raw)

        assert data["files"] == []
        assert data["lines"] == []
        assert data["reason"]

class FakeCd2Client:
    """替掉 util.clouddrive.CloudDriveClient：只记下被要求做了什么"""

    def __init__(self, backup = None):
        self.backup = backup
        self.restarted = []
        self.closed = False
        self.find_error = None
        self.restart_error = None

    def find_backup(self, source):
        if self.find_error:
            raise self.find_error

        return self.backup

    def restart_walking_through(self, source):
        if self.restart_error:
            raise self.restart_error

        self.restarted.append(source)

    def close(self):
        self.closed = True


def make_backup_entry(source = None, destination = "/115open/云下载/哔哩哔哩"):
    """一条 BackupStatus，形状对得上 BackupGetAll 的返回"""
    entry = clouddrive_pb2.BackupStatus()
    entry.backup.sourcePath = source or web_server.SYNC_SOURCE
    entry.status = clouddrive_pb2.BackupStatus.Scanned
    entry.statusMessage = "Backup scanned"

    slot = entry.backup.destinations.add()
    slot.destinationPath = destination
    slot.isEnabled = True

    return entry


class TestSyncApi:
    """
    云端备份接口。

    它的职责只是"触发"：调 CD2 的 BackupRestartWalkingThrough 让那条备份重扫
    一遍就返回，扫描与上传由 CD2 自己后台跑。测试里把 build_cd2_client 换掉 ——
    否则一次测试就真的会去连 CD2 的 19798，还会顺手启动一次真实备份。
    """

    def fake_client(self, monkeypatch, backup = None, tmp_path = None):
        client = FakeCd2Client(backup)
        monkeypatch.setattr(web_server, "build_cd2_client", lambda *a, **k: client)

        # 备份用的地址/源目录可能被面板保存过的 sync.json 覆盖，
        # 指到一个不存在的文件上，这些用例断言的才是内置默认值
        if tmp_path is None:
            import pathlib

            tmp_path = pathlib.Path("/nonexistent-sync-config-test")

        monkeypatch.setattr(web_server, "SYNC_CONFIG_FILE", tmp_path / "sync.json")

        return client

    def test_requires_login(self, panel):
        assert request(panel, "POST", PANEL_SYNC_PATH, body = b"{}")[0] == 401

    def test_token_can_trigger(self, panel, monkeypatch):
        client = self.fake_client(monkeypatch, backup = make_backup_entry())

        status, raw = request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert status == 200

        data = json.loads(raw)

        assert data["ok"] is True
        assert web_server.SYNC_SOURCE in data["message"]

        # 触发的是"那条备份"，不是逐文件复制
        assert client.restarted == [web_server.SYNC_SOURCE]
        assert client.closed is True

    def test_missing_backup_maps_to_404(self, panel, monkeypatch):
        """CD2 里没有这条备份时要说清楚，而不是静默什么都不做"""
        client = self.fake_client(monkeypatch, backup = None)

        status, raw = request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert status == 404
        assert web_server.SYNC_SOURCE in json.loads(raw)["error"]
        assert client.restarted == []

    def test_unreachable_cd2_maps_to_502(self, panel, monkeypatch):
        client = self.fake_client(monkeypatch)
        client.find_error = CloudDriveError("连不上 CloudDrive2")

        status, raw = request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert status == 502
        assert "连不上" in json.loads(raw)["error"]

    def test_bad_credentials_are_reported_as_such(self, panel, monkeypatch):
        client = self.fake_client(monkeypatch)
        client.find_error = CloudDriveAuthError("账号或密码不对")

        status, raw = request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert status == 502
        assert "账号或密码不对" in json.loads(raw)["error"]

    def test_client_is_closed_even_when_it_fails(self, panel, monkeypatch):
        client = self.fake_client(monkeypatch)
        client.find_error = CloudDriveError("boom")

        request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert client.closed is True

class TestSyncConfig:
    """
    云端同步的 CD2 配置接口（地址 / 源目录 / 账号密码）。

    配置存成 config.json 旁边的 sync.json，生效优先级：面板保存值 >
    环境变量 > 内置默认 —— 备份必须跟着这套优先级走，否则面板上改了地址、
    点「立即备份」却还在连旧的。
    """

    def patched_file(self, monkeypatch, tmp_path):
        target = tmp_path / "sync.json"
        monkeypatch.setattr(web_server, "SYNC_CONFIG_FILE", target)

        # 读接口会顺带查一次备份状态：不拦住就会真去连 CD2
        monkeypatch.setattr(web_server, "build_cd2_client", lambda *a, **k: FakeCd2Client())

        return target

    def test_requires_login(self, panel):
        assert request(panel, "POST", PANEL_SYNC_CONFIG_SAVE_PATH, body = b"{}")[0] == 401

    def test_get_returns_defaults_before_anything_saved(self, panel, monkeypatch, tmp_path):
        self.patched_file(monkeypatch, tmp_path)

        status, raw = request(panel, "GET", PANEL_SYNC_CONFIG_PATH, token = TOKEN)

        assert status == 200

        data = json.loads(raw)

        assert data["host"] == web_server.SYNC_HOST
        assert data["port"] == 19798
        assert data["source"] == web_server.SYNC_SOURCE
        assert data["saved"] is False
        assert data["has_password"] is False

    def test_an_old_format_file_does_not_count_as_saved(self, panel, monkeypatch, tmp_path):
        """
        🔴 旧实现往同一个 sync.json 里写的是 {container, src, dst}。留着它不算"已保存"
        —— 那三个键现在一个都不生效，页面却会显示"已保存自定义配置"，用户看到的
        host/port/source 其实全来自默认值（2026-10-02 线上就是这个状态）
        """
        target = self.patched_file(monkeypatch, tmp_path)
        target.write_text(json.dumps({
            "container": "clouddrive2",
            "src": "/Storage/哔哩哔哩",
            "dst": "/CloudNAS/CloudDrive/115open/云下载/哔哩哔哩",
        }), encoding = "utf-8")

        status, raw = request(panel, "GET", PANEL_SYNC_CONFIG_PATH, token = TOKEN)

        assert status == 200

        data = json.loads(raw)

        assert data["saved"] is False
        assert data["host"] == web_server.SYNC_HOST
        assert data["source"] == web_server.SYNC_SOURCE

    def test_save_then_get_roundtrip(self, panel, monkeypatch, tmp_path):
        target = self.patched_file(monkeypatch, tmp_path)

        body = json.dumps({"config": {
            "host": "10.0.0.9",
            "port": 20000,
            "source": "/Storage/测试源",
            "username": "me@example.com",
            "password": "s3cret",
        }}).encode()

        status, raw = request(panel, "POST", PANEL_SYNC_CONFIG_SAVE_PATH, body = body, token = TOKEN)

        assert status == 200

        data = json.loads(raw)

        assert data["saved"] is True
        assert data["host"] == "10.0.0.9"
        assert data["port"] == 20000
        assert data["source"] == "/Storage/测试源"

        # 🔴 密码进了文件，但绝不回给页面 —— 只回"设过没有"
        assert "password" not in data
        assert data["has_password"] is True

        saved = json.loads(target.read_text("utf-8"))

        assert saved["password"] == "s3cret"
        assert saved["username"] == "me@example.com"

    def test_omitting_the_password_keeps_the_stored_one(self, panel, monkeypatch, tmp_path):
        """密码框留空 = 不改。页面拿不到回显，每次都提交空串会把已存的密码抹掉"""
        target = self.patched_file(monkeypatch, tmp_path)
        target.write_text(json.dumps({"host": "old", "password": "keepme"}), encoding = "utf-8")

        body = json.dumps({"config": {
            "host": "10.0.0.9", "port": 19798, "source": "/s", "username": "u",
        }}).encode()

        request(panel, "POST", PANEL_SYNC_CONFIG_SAVE_PATH, body = body, token = TOKEN)

        saved = json.loads(target.read_text("utf-8"))

        assert saved["password"] == "keepme"
        assert saved["host"] == "10.0.0.9"

    def test_saved_config_drives_the_backup(self, panel, monkeypatch, tmp_path):
        """面板上改了地址之后，触发备份必须用新地址去连"""
        self.patched_file(monkeypatch, tmp_path)

        seen = []

        def build(settings = None, timeout = None):
            seen.append((settings or {}).get("host"))

            return FakeCd2Client(make_backup_entry(source = "/Storage/哔哩哔哩"))

        monkeypatch.setattr(web_server, "build_cd2_client", build)

        body = json.dumps({"config": {
            "host": "10.0.0.9", "port": 20000, "source": "/Storage/哔哩哔哩",
        }}).encode()

        request(panel, "POST", PANEL_SYNC_CONFIG_SAVE_PATH, body = body, token = TOKEN)
        request(panel, "POST", PANEL_SYNC_PATH, body = b"{}", token = TOKEN)

        assert "10.0.0.9" in seen

    def test_broken_saved_file_falls_back_to_defaults(self, panel, monkeypatch, tmp_path):
        """手改坏了 sync.json 不该拖垮整页：回落默认值"""
        target = self.patched_file(monkeypatch, tmp_path)
        target.write_text("{ not json", encoding = "utf-8")

        status, raw = request(panel, "GET", PANEL_SYNC_CONFIG_PATH, token = TOKEN)

        assert status == 200

        data = json.loads(raw)

        assert data["host"] == web_server.SYNC_HOST
        assert data["saved"] is False

    def test_rejects_bad_values(self, panel, monkeypatch, tmp_path):
        """host 会拼进请求 URL、source 会进 gRPC 消息：畸形值一律不收"""
        target = self.patched_file(monkeypatch, tmp_path)

        for bad in (
            {"host": "bad/host", "port": 19798, "source": "/a"},
            {"host": "", "port": 19798, "source": "/a"},
            {"host": "ok", "port": 0, "source": "/a"},
            {"host": "ok", "port": 99999, "source": "/a"},
            {"host": "ok", "port": 19798, "source": "relative"},
            {"host": "ok", "port": 19798, "source": ""},
        ):
            body = json.dumps({"config": bad}).encode()

            status, _ = request(panel, "POST", PANEL_SYNC_CONFIG_SAVE_PATH, body = body, token = TOKEN)

            assert status == 400, f"这个配置不该被接受：{bad}"

        # 一条都没写进去
        assert not target.exists()

# ---------------------------------------------------------------------------
# 通知（企业微信应用 / Telegram Bot）
# ---------------------------------------------------------------------------

WECOM_CORP_ID = "ww182aa9502bd5aff3"
WECOM_AGENT_ID = "1000005"
WECOM_SECRET = "Kd8sN2pQwErTyUiOpAsDfGhJkLzXcVbNmQwErTyU"
WECOM_TOUSER = "zhangsan|lisi"
# 那个固定出口 IPv4 的中转（实测 gettoken 的 errmsg 里 from ip 就是它）
WECOM_API_BASE = "http://47.117.88.76:3100"

# 企微发送是**两个**请求（先换 token，再发消息），替身要按 URL 分派应答
WECOM_TOKEN_BODY = b'{"errcode":0,"errmsg":"ok","access_token":"tok-abc","expires_in":7200}'
WECOM_OK_BODY = b'{"errcode":0,"errmsg":"ok"}'

TELEGRAM_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
TELEGRAM_CHAT_ID = "-1001234567890"

# 回调（接收消息）那套。Token 与 EncodingAESKey 的格式由企微规定死
CALLBACK_TOKEN = "VCGWG"
CALLBACK_AES_KEY = "0XlyvMcFO3ZTQ3QIy48m14eC8qeYmCRN1uzVEZdQIVX"
CALLBACK_BASE = "https://bili23.892639.xyz:2662"


def wecom_kwargs(**kw):
    """一套齐备的企微应用凭据，用例里按需覆盖单项"""
    values = {
        "wecom_corp_id": WECOM_CORP_ID,
        "wecom_agent_id": WECOM_AGENT_ID,
        "wecom_secret": WECOM_SECRET,
        "wecom_touser": WECOM_TOUSER,
    }

    values.update(kw)

    return values


def notify_values(**kw):
    """一份全空的合法通知配置，按需覆盖单项"""
    values = {
        "wecom_enabled": False,
        "wecom_corp_id": "",
        "wecom_agent_id": "",
        "wecom_secret": "",
        "wecom_touser": "",
        "wecom_api_base": "",
        "wecom_callback_enabled": False,
        "wecom_callback_token": "",
        "wecom_aes_key": "",
        "wecom_callback_base": "",
        "telegram_enabled": False,
        "telegram_token": "",
        "telegram_chat_id": "",
        "proxy": "",
        "on_complete": True,
        "on_fail": False,
    }

    values.update(kw)

    return values


def callback_kwargs(**kw):
    """一套齐备的回调凭据，用例里按需覆盖单项"""
    values = {
        "wecom_callback_enabled": True,
        "wecom_callback_token": CALLBACK_TOKEN,
        "wecom_aes_key": CALLBACK_AES_KEY,
        "wecom_callback_base": CALLBACK_BASE,
    }

    values.update(kw)

    return values


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """替掉 urllib 的 opener —— 测试里一条都不许真往企微或 Telegram 发出去"""

    def __init__(self, body = b"{}"):
        self.body = body
        self.requests = []
        self.routes = {}

    def route(self, fragment, body):
        """按 URL 片段分派应答。企微要先 gettoken 再 message/send，两者应答不同"""
        self.routes[fragment] = body

        return self

    def open(self, request, timeout = None):
        self.requests.append(request)

        for fragment, body in self.routes.items():
            if fragment in request.full_url:
                return FakeResponse(body)

        return FakeResponse(self.body)


def fake_build_opener(monkeypatch, body, routes = None):
    """拦下 build_opener：返回替身，同时把传进来的 handler 留下来（要验代理）"""
    opener = FakeOpener(body)
    captured = {}

    for fragment, payload in (routes or {}).items():
        opener.route(fragment, payload)

    def build(*handlers):
        captured["handlers"] = list(handlers)
        return opener

    monkeypatch.setattr(notify.urllib.request, "build_opener", build)

    return opener, captured


def fake_wecom(monkeypatch, send_body = WECOM_OK_BODY):
    """企微要过两道：先换 token，再发消息"""
    return fake_build_opener(monkeypatch, b"{}", routes = {
        "gettoken": WECOM_TOKEN_BODY,
        "message/send": send_body,
    })


class TestNotifyValidation:
    """保存与测试发送共用的那道闸门（notify.normalize_settings）"""

    def test_accepts_a_complete_configuration(self):
        clean = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs(),
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = TELEGRAM_CHAT_ID,
        ))

        assert clean["wecom_corp_id"] == WECOM_CORP_ID
        assert clean["wecom_agent_id"] == WECOM_AGENT_ID
        assert clean["telegram_chat_id"] == TELEGRAM_CHAT_ID

    def test_a_disabled_channel_needs_no_credentials(self):
        assert notify.normalize_settings(notify_values())["wecom_enabled"] is False

    def test_enabling_wecom_without_credentials_is_refused(self):
        """"开了开关却没凭据"要到真下完一集才暴露，最难查"""
        with pytest.raises(NotifyError, match = "企业 ID"):
            notify.normalize_settings(notify_values(wecom_enabled = True))

    def test_only_the_missing_wecom_field_is_named(self):
        """四项只缺一个时要指名道姓 —— 把四项全列一遍等于没说"""
        with pytest.raises(NotifyError, match = "指定接收人"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True, **wecom_kwargs(wecom_touser = "")))

    def test_the_receiver_list_is_normalized_to_pipes(self):
        """企业微信 API 要的是 | 分隔，用户敲的是逗号"""
        clean = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_touser = "zhangsan, lisi，wangwu")))

        assert clean["wecom_touser"] == "zhangsan|lisi|wangwu"

    def test_a_receiver_that_is_a_name_not_an_account_is_refused(self):
        """填姓名而不是成员账号是极常见的错，企业微信那边只会回一句参数错误"""
        with pytest.raises(NotifyError, match = "不合法"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True, **wecom_kwargs(wecom_touser = "张三")))

    def test_enabling_telegram_without_a_token_is_refused(self):
        with pytest.raises(NotifyError, match = "Bot Token"):
            notify.normalize_settings(notify_values(telegram_enabled = True))

    def test_enabling_telegram_without_a_chat_id_is_refused(self):
        with pytest.raises(NotifyError, match = "Chat ID"):
            notify.normalize_settings(notify_values(
                telegram_enabled = True, telegram_token = TELEGRAM_TOKEN))

    def test_an_api_base_without_a_scheme_is_refused(self):
        with pytest.raises(NotifyError, match = "http"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True, **wecom_kwargs(wecom_api_base = "47.117.88.76:3100")))

    def test_an_api_base_with_a_query_is_refused(self):
        """
        中转地址只换 host —— 路径是我们拼死的（{基地址} + /cgi-bin/…）。
        允许查询串，等于给"改写请求目标"留了口子
        """
        with pytest.raises(NotifyError, match = "参数"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True,
                **wecom_kwargs(wecom_api_base = "http://47.117.88.76:3100/?target=evil")))

    def test_an_api_base_with_credentials_is_refused(self):
        with pytest.raises(NotifyError, match = "用户名密码"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True,
                **wecom_kwargs(wecom_api_base = "http://u:p@47.117.88.76:3100")))

    def test_the_api_base_keeps_the_port_and_drops_the_trailing_slash(self):
        """拼 /cgi-bin/… 时不能出现 //（nginx 会 301，POST 就丢了）"""
        clean = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_api_base = "http://47.117.88.76:3100/")))

        assert clean["wecom_api_base"] == WECOM_API_BASE

    def test_a_corp_id_with_odd_characters_is_refused(self):
        with pytest.raises(NotifyError, match = "企业 ID"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True, **wecom_kwargs(wecom_corp_id = "ww-182/aa")))

    def test_a_non_numeric_agent_id_is_refused(self):
        """agentid 要作为 JSON 数字提交，字符集放开就没法保证了"""
        with pytest.raises(NotifyError, match = "AgentId"):
            notify.normalize_settings(notify_values(
                wecom_enabled = True, **wecom_kwargs(wecom_agent_id = "1000005x")))

    def test_a_token_that_would_rewrite_the_request_path_is_refused(self):
        """token 会拼进 URL 路径，放行 / 与 .. 就能改掉请求目标"""
        with pytest.raises(NotifyError):
            notify.normalize_settings(notify_values(
                telegram_enabled = True,
                telegram_token = "123456789:abcdefghij/../../evil",
                telegram_chat_id = TELEGRAM_CHAT_ID))

    def test_a_chat_id_with_spaces_is_refused(self):
        with pytest.raises(NotifyError, match = "Chat ID"):
            notify.normalize_settings(notify_values(
                telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
                telegram_chat_id = "123 456"))

    def test_a_channel_name_chat_id_is_accepted(self):
        clean = notify.normalize_settings(notify_values(
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = "@my_channel"))

        assert clean["telegram_chat_id"] == "@my_channel"

    def test_a_valid_proxy_is_kept(self):
        clean = notify.normalize_settings(notify_values(proxy = "http://192.168.3.25:7890"))

        assert clean["proxy"] == "http://192.168.3.25:7890"

    def test_a_proxy_with_credentials_is_refused(self):
        with pytest.raises(NotifyError, match = "用户名密码"):
            notify.normalize_settings(notify_values(proxy = "http://user:pass@10.0.0.1:7890"))

    def test_a_switch_that_is_not_really_a_bool_is_refused(self):
        """字符串 "false" 会被 bool() 判成 True —— 这一层必须挡住"""
        with pytest.raises(NotifyError):
            notify.normalize_settings(notify_values(on_complete = "false"))

    def test_control_characters_are_refused(self):
        """凭据里的换行会撕开日志行，也可能被当成注入"""
        with pytest.raises(NotifyError, match = "控制字符"):
            notify.normalize_settings(notify_values(proxy = "http://10.0.0.1:7890\ninjected"))


class TestNotifyChannels:
    """"哪些渠道算就绪" —— 判断与实际发送共用同一个函数，不会各说各话"""

    def test_a_switch_without_credentials_is_not_a_channel(self):
        assert notify.enabled_channels(notify_values(wecom_enabled = True)) == []

    def test_credentials_without_a_switch_are_not_a_channel(self):
        assert notify.enabled_channels(notify_values(**wecom_kwargs())) == []

    def test_partial_credentials_are_not_a_channel(self):
        """四项缺一项也算没配好 —— 真发出去只会换回一句参数错误"""
        assert notify.enabled_channels(notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_secret = ""))) == []

    def test_ready_channels_are_listed(self):
        channels = notify.enabled_channels(notify_values(
            wecom_enabled = True, **wecom_kwargs(),
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = TELEGRAM_CHAT_ID,
        ))

        assert channels == ["wecom", "telegram"]


class TestNotifySending:
    """发送路径。urllib 换成替身，一条都别真发出去"""

    @pytest.fixture(autouse = True)
    def _clean(self):
        notify.HISTORY.clear()
        # access_token 的缓存是模块级的：不清掉，后一个用例就少发一次 gettoken，
        # 请求序号的断言会莫名其妙地错位
        notify._WECOM_TOKENS.clear()

        yield

        notify.HISTORY.clear()
        notify._WECOM_TOKENS.clear()

    def test_wecom_gets_a_token_then_posts_to_the_official_host(self, monkeypatch):
        opener, _ = fake_wecom(monkeypatch)

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        ok, detail = notify.send("wecom", settings, "B站下载完成", "· 西游记")

        assert ok is True
        assert detail == "已送达"
        assert len(opener.requests) == 2

        token_url = opener.requests[0].full_url

        assert token_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/gettoken")
        assert WECOM_CORP_ID in token_url
        assert WECOM_SECRET in token_url

        send = opener.requests[1]
        payload = json.loads(send.data.decode("utf-8"))

        assert send.full_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/message/send")
        assert "tok-abc" in send.full_url
        assert payload["touser"] == WECOM_TOUSER
        # agentid 必须是数字，字符串会被企业微信判成参数错误
        assert payload["agentid"] == int(WECOM_AGENT_ID)
        assert payload["msgtype"] == "text"
        assert "B站下载完成" in payload["text"]["content"]
        assert "西游记" in payload["text"]["content"]

    def test_the_access_token_is_cached_between_sends(self, monkeypatch):
        """gettoken 有频率限制，一个合集几十集不能每集都去换一次 token"""
        opener, _ = fake_wecom(monkeypatch)

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        notify.send("wecom", settings, "第一次")
        notify.send("wecom", settings, "第二次")

        assert len([r for r in opener.requests if "gettoken" in r.full_url]) == 1
        assert len(opener.requests) == 3

    def test_the_relay_base_replaces_the_official_host(self, monkeypatch):
        """
        「消息代理地址」是**换基地址**，不是 HTTP 代理 —— 请求原样发给中转，
        由它代发，出口 IP 就固定成那台机器（企业微信要求调用方 IP 可信）
        """
        opener, captured = fake_wecom(monkeypatch)

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_api_base = WECOM_API_BASE)))

        notify.send("wecom", settings, "标题")

        assert opener.requests[0].full_url.startswith(WECOM_API_BASE + "/cgi-bin/gettoken")
        assert opener.requests[1].full_url.startswith(WECOM_API_BASE + "/cgi-bin/message/send")

        # 换基地址与走 HTTP 代理是两回事：这条路径上不该挂 proxy
        assert all(getattr(h, "proxies", None) == {} for h in captured["handlers"])

    def test_a_wecom_error_code_is_reported(self, monkeypatch):
        fake_wecom(monkeypatch, send_body = b'{"errcode":60020,"errmsg":"not allow to access from your ip"}')

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        ok, detail = notify.send("wecom", settings, "标题", "正文")

        assert ok is False
        assert "60020" in detail

    def test_a_failed_token_request_reports_its_errcode(self, monkeypatch):
        """"填错了 Secret / 企业 ID"是最常见的，40013 就是它"""
        fake_build_opener(monkeypatch, b"{}", routes = {
            "gettoken": b'{"errcode":40013,"errmsg":"invalid corpid"}',
        })

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        ok, detail = notify.send("wecom", settings, "标题")

        assert ok is False
        assert "40013" in detail
        assert "access_token" in detail

    def test_telegram_posts_to_the_bot_url_with_the_chat_id(self, monkeypatch):
        opener, _ = fake_build_opener(monkeypatch, b'{"ok":true}')

        settings = notify.normalize_settings(notify_values(
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = TELEGRAM_CHAT_ID))

        ok, _ = notify.send("telegram", settings, "B站下载失败", "· 第1集")

        assert ok is True
        assert opener.requests[0].full_url == f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        payload = json.loads(opener.requests[0].data.decode("utf-8"))

        assert payload["chat_id"] == TELEGRAM_CHAT_ID
        assert "第1集" in payload["text"]

    def test_a_telegram_failure_is_reported_not_raised(self, monkeypatch):
        """推送是旁路，发不出去不能把下载流程带崩"""
        fake_build_opener(monkeypatch, b'{"ok":false,"description":"chat not found"}')

        settings = notify.normalize_settings(notify_values(
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = TELEGRAM_CHAT_ID))

        ok, detail = notify.send("telegram", settings, "标题")

        assert ok is False
        assert "chat not found" in detail

    def test_a_non_json_reply_is_reported(self, monkeypatch):
        """网关拦截页回的是 HTML，直接 json.loads 会抛一串看不懂的堆栈"""
        fake_build_opener(monkeypatch, b"<html>blocked</html>")

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        ok, detail = notify.send("wecom", settings, "标题")

        assert ok is False
        assert "JSON" in detail

    def test_the_configured_proxy_is_passed_to_the_opener(self, monkeypatch):
        _, captured = fake_build_opener(monkeypatch, b'{"ok":true}')

        settings = notify.normalize_settings(notify_values(proxy = "http://192.168.3.25:7890"))

        notify.send("telegram", settings, "标题")

        assert any(
            getattr(handler, "proxies", {}).get("https") == "http://192.168.3.25:7890"
            for handler in captured["handlers"]
        )

    def test_no_proxy_means_the_environment_proxy_is_ignored(self, monkeypatch):
        """
        显式传一个空 ProxyHandler：不传的话 urllib 会自己去读 http_proxy ——
        本机开发环境挂着系统代理，容器里可能有运维留下的全局代理，
        两者都会让"明明没填代理"的预期落空
        """
        _, captured = fake_build_opener(monkeypatch, b'{"errcode":0}')

        settings = notify.normalize_settings(notify_values(**wecom_kwargs()))

        notify.send("wecom", settings, "标题")

        handlers = captured["handlers"]

        assert handlers
        assert all(getattr(handler, "proxies", None) == {} for handler in handlers)

    def test_history_records_every_attempt(self, monkeypatch):
        fake_wecom(monkeypatch)

        settings = notify.normalize_settings(notify_values(
            wecom_enabled = True, **wecom_kwargs()))

        notify.send("wecom", settings, "标题")

        history = notify.history()

        assert len(history) == 1
        assert history[0]["channel"] == "企业微信"
        assert history[0]["ok"] is True

    def test_an_unknown_channel_is_refused(self):
        ok, detail = notify.send("sms", {}, "标题")

        assert ok is False
        assert "未知渠道" in detail


def _kill_notify_timers():
    """
    把通知模块里遗留的静默期计时器彻底清掉。

    🔴 `Timer.cancel()` 对**已经起跑**的线程无效 —— 而 `_flush_pending` 走"队列还在跑"
    那条分支时会自己 `_arm_timer()` 一次，于是上一条用例的计时器线程能一路活到本用例里，
    继续按 0.05s 的节奏打 `_flush_pending`。本用例的 `RUNNING_PROBE` 是替换过的，看着
    无害，但它与本用例自己的计时器抢同一份 `_PENDING`：只要它在 monkeypatch 撤销后的
    那一刻醒来，读到的探针就是真身（队列里没任务 → False），于是把本用例刚攒的批次提前
    发走 —— 表现成「held 用例偶发失败」（2026-10-02 全量跑挂过一次，单独跑/重跑不复现）。
    所以 cancel 之后必须 join；最后再拿一次 `_PENDING_LOCK`，保证没有 `_flush_pending`
    正卡在锁里、等我们放锁之后再支一个计时器出来。
    """
    for _ in range(50):
        timer = notify._TIMER

        if timer is None:
            break

        timer.cancel()
        timer.join(timeout = 1.0)

    with notify._PENDING_LOCK:
        if notify._TIMER is not None:
            notify._TIMER.cancel()

        notify._TIMER = None


class TestNotifyBatching:
    """完成通知的静默期：一个合集几十集，逐集推会把手机刷爆"""

    def setup_method(self):
        # _PENDING / _TIMER / HISTORY / RUNNING_PROBE 都是模块级状态，跨用例共享，必须自己清
        _kill_notify_timers()
        notify._PENDING.clear()
        notify.HISTORY.clear()
        notify.RUNNING_PROBE = None

    def teardown_method(self):
        # 本用例自己的计时器也要收干净：静默期只有 0.05s，放出去会按真身发一条真实通知
        _kill_notify_timers()
        notify._PENDING.clear()

    def prepare(self, monkeypatch, delay = 0.05, running = False):
        fake = FakeConfig()
        fake.store["notification_wecom_enabled"] = True
        fake.store.update({
            "notification_" + key: value for key, value in wecom_kwargs().items()
        })

        monkeypatch.setattr("util.common.notify.config", fake)
        monkeypatch.setattr(notify, "BATCH_DELAY", delay)
        # 「队列里还有没有任务在跑」真去查任务库的话，用例就得连 appdata 一起搬进沙箱，
        # 所以默认替换成一个常量函数；要测"还在跑"的用例把它换成返回 True
        monkeypatch.setattr(notify, "RUNNING_PROBE", lambda: running)

        sent = []
        monkeypatch.setattr(
            notify, "send_to_channels",
            lambda channels, settings, title, text = "": sent.append((title, text)) or {}
        )

        return fake, sent

    def fake_task(self, season, number, title = ""):
        from util.download.task.info import TaskInfo

        task_info = TaskInfo()
        task_info.Basic.show_title = title or f"{season} 第{number}集"
        task_info.Episode.season_title = season
        task_info.Episode.episode_number = number

        return task_info

    def test_a_single_completion_is_held_until_the_quiet_period_ends(self, monkeypatch):
        _, sent = self.prepare(monkeypatch)

        notify.notify_completed("西游记 第1集")

        assert sent == []

        time.sleep(0.2)

        assert len(sent) == 1
        assert sent[0][0] == "B站下载完成"
        assert "西游记 第1集" in sent[0][1]

    def test_a_burst_of_episodes_collapses_into_one_message(self, monkeypatch):
        _, sent = self.prepare(monkeypatch)

        for index in range(1, 6):
            notify.notify_completed(f"西游记 第{index}集")

        time.sleep(0.25)

        assert len(sent) == 1
        assert "第5集" in sent[0][1]
        # 拿不到合集名/集号的（这里是纯标题字符串）仍逐条列出，一条都没丢
        assert "· 西游记 第5集" in sent[0][1]

    def test_the_title_list_is_capped(self, monkeypatch):
        """几百集的任务不能撑出一条谁都读不完的消息"""
        _, sent = self.prepare(monkeypatch)

        for index in range(1, notify.BATCH_MAX_TITLES + 8):
            notify.notify_completed(f"第{index}集")

        time.sleep(0.25)

        assert len(sent) == 1
        assert "…等" in sent[0][1]

    def test_a_whole_season_collapses_into_one_block(self, monkeypatch):
        """
        西游记 1-25 集：正文要压成一块四行（剧名 / 年份 / 集数 / 状态），
        而不是把 25 个片名全列出来（那正是"逐集推"换了个形式的刷屏）
        """
        _, sent = self.prepare(monkeypatch)

        for index in range(1, 26):
            notify.notify_completed(self.fake_task("西游记", index, f"第{index}集 三打白骨精"))

        time.sleep(0.25)

        assert len(sent) == 1
        assert "剧名：西游记" in sent[0][1]
        assert "集数：第1-25集（共25集）" in sent[0][1]
        assert "状态：已完成" in sent[0][1]
        # 逐条片名不能同时出现：压成一块就是压成一块
        assert "三打白骨精" not in sent[0][1]

    def test_the_block_is_exactly_four_lines(self, monkeypatch):
        """一块就是四行，前后不留空行、也不拖一句总数 —— 正文形状要可预期"""
        _, sent = self.prepare(monkeypatch)

        for index in range(1, 26):
            notify.notify_completed(self.fake_task("西游记", index))

        time.sleep(0.25)

        assert sent[0][1].splitlines() == [
            "剧名：西游记",
            "集数：第1-25集（共25集）",
            "状态：已完成",
        ]

    def test_the_year_comes_from_the_identify_table(self, monkeypatch):
        """年份只能是「名称识别」表里那一格 —— B站数据里根本没有首播年份"""
        fake, sent = self.prepare(monkeypatch)

        fake.store["naming_alias_list"] = [
            {"match": "西游记", "field": "season_title", "mode": "contains",
             "title": "西游记", "season": 1, "year": "1986", "tmdb": "13923"},
        ]

        for index in range(1, 4):
            notify.notify_completed(self.fake_task("西游记", index))

        time.sleep(0.25)

        assert "年份：1986" in sent[0][1]

    def test_an_unmatched_show_has_no_year_line_at_all(self, monkeypatch):
        """
        没配识别规则时「年份」整行不出现 —— 不能留「年份：」后面空着，
        那比少一行更让人犯嘀咕；更不能拿上架时间冒充首播年份
        """
        _, sent = self.prepare(monkeypatch)

        # 表里有这一条，但它是 TMDB 链接代填留下的、没填年份
        notify.config.store["naming_alias_list"] = [{"match": "西游记", "year": ""}]

        notify.notify_completed(self.fake_task("西游记", 1))

        time.sleep(0.25)

        assert "年份" not in sent[0][1]

    def test_the_default_year_lookup_really_runs_its_own_body(self, monkeypatch, caplog):
        """
        默认的年份查询要真能走通那条真身，并且**不许吞异常**

        🔴 与 _batch_still_running 是同一个病：这段外面裹着 `except Exception`，
        单测把注入点一换就绕过了真身 —— 真身里写错个导入名（`..config` 之类），
        表现是"年份永远查不到"，而全部用例照样绿。所以这里跑真身，只把配置换成
        假的那一份，然后断言日志里没有任何 ERROR。
        """
        fake, _ = self.prepare(monkeypatch)

        fake.store["naming_alias_list"] = [{"match": "西游记", "year": "1986"}]

        with caplog.at_level(logging.ERROR):
            year = notify._alias_year("西游记", "")

        assert year == "1986"
        assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []

    def test_two_seasons_are_reported_on_separate_lines(self, monkeypatch):
        """不同合集不能混成一行 —— 否则「第1-25集」到底是谁的说不清"""
        _, sent = self.prepare(monkeypatch)

        notify.notify_completed(self.fake_task("西游记", 1))
        notify.notify_completed(self.fake_task("红楼梦", 2))

        time.sleep(0.25)

        assert len(sent) == 1
        assert "剧名：西游记" in sent[0][1]
        assert "剧名：红楼梦" in sent[0][1]
        # 两块，各自带自己的集数 —— 混成一块的话「第1集」到底是谁的说不清
        assert sent[0][1].count("剧名：") == 2
        assert "集数：第1集（共1集）" in sent[0][1]
        assert "集数：第2集（共1集）" in sent[0][1]

    def test_episode_numbers_are_compressed_into_ranges(self, monkeypatch):
        """断号要如实断开：1,2,3,7,9,10 → 1-3、7、9-10"""
        _, sent = self.prepare(monkeypatch)

        for number in (1, 2, 3, 7, 9, 10):
            notify.notify_completed(self.fake_task("西游记", number))

        time.sleep(0.25)

        assert "集数：第1-3、7、9-10集（共6集）" in sent[0][1]

    def test_an_entry_without_a_season_falls_back_to_its_title(self, monkeypatch):
        """拿不到合集名/集号的记录退化成逐条列出，不能丢"""
        _, sent = self.prepare(monkeypatch)

        notify.notify_completed(self.fake_task("西游记", 1))
        notify.notify_completed("某个没有合集信息的任务")

        time.sleep(0.25)

        assert "剧名：西游记" in sent[0][1]
        assert "· 某个没有合集信息的任务" in sent[0][1]

    def test_the_notification_waits_while_the_queue_is_still_running(self, monkeypatch):
        """
        🔴 整批下完才通知：静默期到了，队列里还有任务在跑就继续等 ——
        合并、后处理会让完成事件之间出现几十秒空档，光看静默期，
        一个 25 集的合集会被拆成好几条
        """
        _, sent = self.prepare(monkeypatch, running = True)

        notify.notify_completed(self.fake_task("西游记", 1))

        time.sleep(0.25)

        assert sent == []
        assert len(notify._PENDING) == 1
        # 计时器要重新支起来，否则这批事件就永远躺在队列里了
        assert notify._TIMER is not None

    def test_the_held_batch_is_sent_once_the_queue_drains(self, monkeypatch):
        """队列跑空后，同一个批次补发出去，且只发一条"""
        fake, sent = self.prepare(monkeypatch, running = True)

        for index in range(1, 6):
            notify.notify_completed(self.fake_task("西游记", index))

        time.sleep(0.25)

        assert sent == []

        # 队列空了（RUNNING_PROBE 是每次调用时读的，改它即可）
        notify.RUNNING_PROBE = lambda: False

        time.sleep(0.25)

        assert len(sent) == 1
        assert "集数：第1-5集（共5集）" in sent[0][1]

    def test_flush_now_does_not_wait_for_the_queue(self, monkeypatch):
        """退出前补发：进程马上要没了，再等队列跑空等于一条都发不出去"""
        _, sent = self.prepare(monkeypatch, running = True)

        notify.notify_completed(self.fake_task("西游记", 1))
        notify.flush_now()

        assert len(sent) == 1
        assert "剧名：西游记" in sent[0][1]
        assert "状态：已完成" in sent[0][1]

    def test_the_default_probe_really_can_read_the_task_queue(self, monkeypatch, caplog):
        """
        默认探针（不带 RUNNING_PROBE）要真能导入任务库与状态枚举，并且**不许吞异常**

        🔴 这条是补票的：探针里的相对导入一度写成 `..enum`（util.enum 根本不存在），
        而 except 把 ModuleNotFoundError 一起吞了，表现是"永远按没有任务在跑处理"
        —— 整批等待静默失效，通知又退回成只看静默期，界面与其余用例全看不出来
        （其余用例都把 RUNNING_PROBE 替换掉了，正好绕过这段真身）。
        所以这里跑真身，只把任务库的查询换掉，然后断言日志里没有任何 ERROR。
        """
        module = importlib.import_module("util.common.notify")

        monkeypatch.setattr(module, "RUNNING_PROBE", None)

        from util.download.task import manager as task_manager_module

        monkeypatch.setattr(task_manager_module.task_manager, "query", lambda *a, **k: [])

        with caplog.at_level(logging.ERROR, logger = "util.common.notify"):
            assert module._batch_still_running() is False

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [], \
            "默认探针在导入或查询时出错了 —— 异常被 except 吞掉，通知会静默退回成只看静默期"

    def test_nothing_is_sent_when_no_channel_is_ready(self, monkeypatch):
        fake, sent = self.prepare(monkeypatch)
        fake.store["notification_wecom_enabled"] = False

        notify.notify_completed("西游记 第1集")
        time.sleep(0.2)

        assert sent == []

    def test_turning_the_switch_off_during_the_quiet_period_is_respected(self, monkeypatch):
        """静默期里改了设置，发送前要再判一次"""
        fake, sent = self.prepare(monkeypatch)

        notify.notify_completed("西游记 第1集")
        fake.store["notification_on_complete"] = False

        time.sleep(0.2)

        assert sent == []

    def test_a_failure_is_sent_immediately(self, monkeypatch):
        """失败要马上知道，静默期会把"下到一半挂了"拖成二十分钟后才收到"""
        fake = FakeConfig()
        fake.store["notification_wecom_enabled"] = True
        fake.store.update({
            "notification_" + key: value for key, value in wecom_kwargs().items()
        })
        fake.store["notification_on_fail"] = True

        monkeypatch.setattr("util.common.notify.config", fake)

        sent = []
        monkeypatch.setattr(
            notify, "send_in_background",
            lambda channels, settings, title, text = "": sent.append((title, text))
        )

        notify.notify_failed("西游记 第3集", "分片下载失败")

        assert len(sent) == 1
        assert sent[0][0] == "B站下载失败"
        assert "分片下载失败" in sent[0][1]


class TestNotifyApi:
    """「通知」页的三个接口：读 / 保存 / 测试发送"""

    def prepare(self, monkeypatch):
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        # load_settings 读的是 notify 模块里那份 config 绑定
        monkeypatch.setattr("util.common.notify.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        notify.HISTORY.clear()
        # access_token 缓存同样是模块级的，跨用例共享会打乱请求断言
        notify._WECOM_TOKENS.clear()

        return fake

    def get_payload(self, panel):
        status, raw = request(panel, "GET", PANEL_NOTIFY_PATH, cookie = login(panel))

        assert status == 200

        return json.loads(raw)

    def post(self, panel, path, body):
        return request(panel, "POST", path, body = json.dumps(body).encode(), cookie = login(panel))

    def test_get_requires_login(self, panel):
        assert request(panel, "GET", PANEL_NOTIFY_PATH)[0] == 401

    def test_get_returns_the_default_configuration(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        d = self.get_payload(panel)

        assert d["config"]["wecom_enabled"] is False
        assert d["config"]["on_complete"] is True
        assert d["config"]["on_fail"] is False
        assert d["channels"] == []
        assert d["history"] == []

    def test_save_writes_the_credentials(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NOTIFY_SAVE_PATH, {"config": notify_values(
            wecom_enabled = True, **wecom_kwargs(),
            telegram_enabled = True, telegram_token = TELEGRAM_TOKEN,
            telegram_chat_id = TELEGRAM_CHAT_ID,
            proxy = "http://192.168.3.25:7890",
            on_fail = True,
        )})

        assert status == 200

        d = json.loads(raw)

        assert d["channels"] == ["wecom", "telegram"]
        assert fake.store["notification_wecom_secret"] == WECOM_SECRET
        assert fake.store["notification_wecom_agent_id"] == WECOM_AGENT_ID
        assert fake.store["notification_telegram_token"] == TELEGRAM_TOKEN
        assert fake.store["notification_on_fail"] is True

    def test_save_refuses_an_enabled_channel_without_credentials(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NOTIFY_SAVE_PATH, {"config": notify_values(
            telegram_enabled = True)})

        assert status == 400
        assert "Bot Token" in json.loads(raw)["error"]

    def test_save_refuses_an_incomplete_wecom_configuration(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NOTIFY_SAVE_PATH, {"config": notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_secret = ""))})

        assert status == 400
        assert "应用 Secret" in json.loads(raw)["error"]

    def test_save_keeps_the_relay_address(self, panel, monkeypatch):
        fake = self.prepare(monkeypatch)

        status, _ = self.post(panel, PANEL_NOTIFY_SAVE_PATH, {"config": notify_values(
            wecom_enabled = True, **wecom_kwargs(wecom_api_base = WECOM_API_BASE))})

        assert status == 200
        assert fake.store["notification_wecom_api_base"] == WECOM_API_BASE

    def test_save_without_a_body_is_rejected(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.post(panel, PANEL_NOTIFY_SAVE_PATH, {})[0] == 400

    def test_credentials_never_reach_the_page(self, panel, monkeypatch):
        """
        读接口只回"设了没"，四项凭据的真值一直留在服务端

        这几个字段原本是直接回填进输入框的 —— 用户随手一张整页截图（多半还是为了问
        "为什么发不出去"）就把它带出去了。导出配置那侧本来就掩码，下发这侧没道理例外
        """
        fake = self.prepare(monkeypatch)

        fake.store["notification_wecom_secret"] = "wecom-secret-live"
        fake.store["notification_wecom_callback_token"] = CALLBACK_TOKEN
        fake.store["notification_wecom_aes_key"] = CALLBACK_AES_KEY
        fake.store["notification_telegram_token"] = TELEGRAM_TOKEN

        status, raw = request(panel, "GET", PANEL_NOTIFY_PATH, cookie = login(panel))

        assert status == 200

        d = json.loads(raw)

        for key in ("wecom_secret", "wecom_callback_token", "wecom_aes_key", "telegram_token"):
            assert d["config"][key] == "", key
            assert d["secrets"][key] is True, key

        body = raw.decode("utf-8")

        for secret in ("wecom-secret-live", CALLBACK_TOKEN, CALLBACK_AES_KEY, TELEGRAM_TOKEN):
            assert secret not in body

    def test_secrets_report_false_when_nothing_is_stored(self, panel, monkeypatch):
        """
        没设过就是 false —— 前端靠这一位决定 placeholder 写"未填写"还是"已设置"

        没有它的话，两个状态在前端看起来一模一样，用户会以为自己填过
        """
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_NOTIFY_PATH, cookie = login(panel))

        assert status == 200

        secrets = json.loads(raw)["secrets"]

        assert set(secrets) == {"wecom_secret", "wecom_callback_token", "wecom_aes_key", "telegram_token"}
        assert not any(secrets.values())

    def test_the_test_endpoint_reports_a_successful_send(self, panel, monkeypatch):
        self.prepare(monkeypatch)
        fake_wecom(monkeypatch)

        status, raw = self.post(panel, PANEL_NOTIFY_TEST_PATH, {
            "channel": "wecom",
            "config": notify_values(**wecom_kwargs()),
        })

        d = json.loads(raw)

        assert status == 200
        assert d["ok"] is True
        assert d["message"] == "已送达"

        # 测试过的记录要落进历史，面板上看得见
        assert d["history"][0]["ok"] is True

    def test_the_test_endpoint_reports_a_failure_as_200_false(self, panel, monkeypatch):
        """
        发送失败是用户要看的**结果**（比如"代理没填对"），不是请求错了。
        回 4xx 会被前端的统一错误处理当成异常抛掉
        """
        self.prepare(monkeypatch)
        fake_build_opener(monkeypatch, b'{"ok":false,"description":"chat not found"}')

        status, raw = self.post(panel, PANEL_NOTIFY_TEST_PATH, {
            "channel": "telegram",
            "config": notify_values(
                telegram_token = TELEGRAM_TOKEN, telegram_chat_id = TELEGRAM_CHAT_ID),
        })

        d = json.loads(raw)

        assert status == 200
        assert d["ok"] is False
        assert "chat not found" in d["message"]

    def test_the_test_endpoint_reports_an_incomplete_form_as_a_result(self, panel, monkeypatch):
        """表单还没填完就点了测试：把校验结论当结果回，不弹 400"""
        self.prepare(monkeypatch)

        status, raw = self.post(panel, PANEL_NOTIFY_TEST_PATH, {
            "channel": "telegram", "config": notify_values()})

        d = json.loads(raw)

        assert status == 200
        assert d["ok"] is False
        assert "Telegram" in d["message"]

    def test_the_test_endpoint_rejects_an_unknown_channel(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        assert self.post(panel, PANEL_NOTIFY_TEST_PATH, {"channel": "sms"})[0] == 400

    def test_the_test_endpoint_works_before_the_settings_are_saved(self, panel, monkeypatch):
        """用户多半是"填完先测一下，通了再保存" —— 未保存的现值必须能用"""
        fake = self.prepare(monkeypatch)
        opener, _ = fake_wecom(monkeypatch)

        self.post(panel, PANEL_NOTIFY_TEST_PATH, {
            "channel": "wecom", "config": notify_values(**wecom_kwargs())})

        assert opener.requests[0].full_url.startswith(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken")
        # 配置里的键始终没被写过
        assert "notification_wecom_secret" not in fake.store


class TestTailLines:
    """
    回读文件尾部。

    这里踩过一次坑：为了探测文件大小先 seek 到了末尾，走"不必截断"分支时
    忘了归零，于是小文件（永远走这一支）稳定读到空内容 —— 而大文件正常，
    正好绕开所有手工验证
    """

    def test_small_file_is_read_whole(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text("一\n二\n三\n", encoding = "utf-8")

        assert _tail_lines(path) == ["一", "二", "三"]

    def test_limit_keeps_the_tail(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text("".join(f"行{i:03d}\n" for i in range(200)), encoding = "utf-8")

        lines = _tail_lines(path, max_bytes = 40)

        assert lines[-1] == "行199"
        assert 0 < len(lines) < 200

    def test_partial_first_line_is_dropped(self, tmp_path):
        # 从字节中间截断时，首行必定是残缺的，要连它一起丢掉。
        # 每行 7 字节（"行"占 3、三位数字占 3、换行占 1），
        # 45 字节 = 6 个整行（行194…行199）+ 行193 的尾巴 3 字节（"93\n"），
        # 所以残行必须消失，首行是行194
        path = tmp_path / "a.log"
        path.write_text("".join(f"行{i:03d}\n" for i in range(200)), encoding = "utf-8")

        lines = _tail_lines(path, max_bytes = 45)

        assert lines[0] == "行194"
        assert all(len(line) == 4 for line in lines)

    def test_multibyte_is_not_mangled(self, tmp_path):
        # 中文一条三字节，按字节截断很可能切在字符中间；解码用 replace，
        # 残缺的那个字符不该把整行拖成乱码
        path = tmp_path / "a.log"
        path.write_text("".join("中文日志条目\n" for _ in range(50)), encoding = "utf-8")

        assert set(_tail_lines(path, max_bytes = 25)) == {"中文日志条目"}

class TestPanelPage:
    """面板页面自身的结构约定"""

    def page(self, panel):
        status, raw = request(panel, "GET", "/")

        assert status == 200

        return raw.decode("utf-8")

    def test_each_section_is_a_page(self, panel):
        page = self.page(panel)

        for section in (
            'id="page-overview"', 'id="page-downloading"', 'id="page-done"',
            'id="page-fav"', 'id="page-log"', 'id="page-naming"', 'id="page-identify"',
            'id="page-sync"', 'id="page-mcp"', 'id="page-notify"',
            'id="page-settings"', 'id="page-account"',
        ):
            assert section in page

    def test_notification_cards_start_collapsed(self, panel):
        """
        通知页 6 张卡片默认全收起，展开过的记在 localStorage

        每张卡标题栏都挂了一枚状态胶囊 —— 收起来之后得看得出里面开没开、
        攒了多少条记录，否则一排光标题等于没信息
        """
        page = self.page(panel)

        badges = {
            "wecom": "ntWecomBadge",
            "callback": "ntCbBadge",
            "telegram": "ntTgBadge",
            "trigger": "ntTrigBadge",
            "cbhistory": "ntCbHistBadge",
            "history": "ntHistBadge",
        }

        for key, badge in badges.items():
            assert f'data-nt="{key}"' in page, key
            assert f'id="{badge}"' in page, badge

        # 整页合并成一张卡，里面每项一行（不再是 6 张孤立的小条）
        assert page.count('class="glass panel nt-acc"') == 1
        assert page.count('class="nt-item collapsed"') == len(badges)
        assert page.count('<i class="fchev">▾</i>') == len(badges)
        assert page.count('class="nt-t"') == len(badges)

        # 正文是靠 CSS 一起藏掉的（没包额外的 div，行内部的 id 才不受影响）
        assert ".nt-item.collapsed > *:not(.h)" in page
        assert "NT_FOLD_KEY" in page

    def test_overview_shows_free_space_under_the_download_folder(self, panel):
        """概览那张「下载目录」卡：除了挂载点，还要说清还能再存多少"""
        page = self.page(panel)

        assert 'id="ovFree"' in page
        # 原来那行说明不能被顶掉
        assert "容器内挂载 /downloads" in page


        """
        命名规则是独立页面，排在侧边栏「云端同步」上面

        桌面端它是「设置 → 文件命名」里弹出来的一整窗（可视化编辑器），网页上
        没必要占设置页的一块地方 —— 规则条数与变量表都在长，独立页面装得下
        """
        page = self.page(panel)

        assert 'id="page-naming"' in page
        assert 'data-page="naming"' in page

        # 三个接口都要接上
        assert PANEL_NAMING_PATH.lstrip("/") in page
        assert PANEL_NAMING_PREVIEW_PATH.lstrip("/") in page
        assert PANEL_NAMING_SAVE_PATH.lstrip("/") in page

        # 导航顺序：命名规则在云端同步之前
        assert page.index('data-page="naming"') < page.index('data-page="sync"')

        # 编辑区该有的控件一个都不能少
        for control in ('id="nmItems"', 'id="nmName"', 'id="nmType"', 'id="nmRule"',
                        'id="nmVars"', 'id="nmPreview"', 'id="nmErr"',
                        'id="nmAddBtn"', 'id="nmDupBtn"', 'id="nmDelBtn"',
                        'id="nmSaveBtn"', 'id="nmRestoreBtn"'):
            assert control in page, control

    def test_name_recognition_lives_below_the_naming_rules_page(self, panel):
        """
        名称识别是独立页面，紧跟在侧边栏「命名规则」下面

        两者是配套的：命名规则决定「名字长什么样」，名称识别决定「变量填什么」——
        规则里写 {year} / {tmdb_id}，识别表负责把它们填上。挨着放，用户改完规则
        顺手就能配一条识别
        """
        page = self.page(panel)

        assert 'id="page-identify"' in page
        assert 'data-page="identify"' in page

        # 读 / 预览 / 保存 / TMDB 取字段，四个接口都要接上
        assert PANEL_IDENTIFY_PATH.lstrip("/") in page
        assert PANEL_IDENTIFY_PREVIEW_PATH.lstrip("/") in page
        assert PANEL_IDENTIFY_SAVE_PATH.lstrip("/") in page
        assert PANEL_IDENTIFY_TMDB_PATH.lstrip("/") in page

        # 导航顺序：命名规则 → 名称识别 → 云端同步
        assert page.index('data-page="naming"') < page.index('data-page="identify"')
        assert page.index('data-page="identify"') < page.index('data-page="sync"')

    def test_name_recognition_page_takes_a_tmdb_link(self, panel):
        """
        「TMDB 链接」那一行要真的在页面上

        B站给不出年份与 TMDB 编号（episodes[].release_date 恒空），这两栏从前只能
        对着 TMDB 页面手抄。控件没了就等于把人打回手抄，所以输入框、按钮、提示位
        一起断言 —— 只留个输入框而按钮没绑事件，是看不出来的
        """
        page = self.page(panel)

        assert 'id="alTmdbUrl"' in page
        assert 'id="alTmdbFill"' in page
        assert 'id="alTmdbMsg"' in page

        # 按钮得真的发起请求，不能是个空壳
        assert 'post("api/panel/identify/tmdb"' in page

        # 「获取」要连匹配内容一起填 —— 匹配内容是规则的命门，只填另外四栏的话
        # 用户仍得回 TMDB 抄一遍名字，这一行的意义就丢了大半
        assert "a.match = d.title;" in page

        # 静态提示与 JS 里那个常量必须是同一句：换规则时回落的正是它，
        # 两处不一致就会出现"提示说默认取 TMDB 名称、实际却不是"
        hint = "匹配内容默认取 TMDB 名称 —— 对不上 B站那边的季标题时改一下"

        assert 'id="alTmdbMsg">' + hint + "<" in page
        assert 'var AL_TMDB_HINT = "' + hint + '";' in page

        # 旧文案「匹配内容仍需手填」不能再留着 —— 它现在与行为相反
        assert "仍需手填" not in page

        # 换规则 / 重新载入时要把上一条「已填入 xxx」收回去 —— 不收的话表单已经是
        # 西游记、提示却还在说巴啦啦小魔仙，比不提示更误导
        assert 'alTmdbSay("");' in page

        # 编辑区该有的控件一个都不能少
        for control in ('id="alItems"', 'id="alField"', 'id="alMode"', 'id="alMatch"',
                        'id="alTitle"', 'id="alSeason"', 'id="alYear"', 'id="alTmdb"',
                        'id="alEnabled"', 'id="alPreview"', 'id="alErr"',
                        'id="alAddBtn"', 'id="alDupBtn"', 'id="alDelBtn"',
                        'id="alUpBtn"', 'id="alDownBtn"', 'id="alSaveBtn"'):
            assert control in page, control

    def test_favorites_live_below_the_done_page(self, panel):
        """
        「收藏」是独立侧栏页，位置在「已完成」下面 —— 用户点名的位置

        桌面端把这个入口做成侧栏按钮 + 收藏浮层；面板里落成一整页。放在「已完成」
        下面是有道理的：它俩都是"存量"（下完的、收藏的），再往下才是一串配置页
        """
        page = self.page(panel)

        assert 'data-page="fav"' in page
        assert 'id="page-fav"' in page

        # 导航顺序：已完成 → 收藏 → 日志
        assert page.index('data-page="done"') < page.index('data-page="fav"')
        assert page.index('data-page="fav"') < page.index('data-page="log"')

        # 五个分类的 tab 与刷新按钮，一个都不能少。
        # 顺序是桌面端收藏浮层的顺序，也是用户点名的五项
        for kind in ("favorite", "subscription", "follow", "watch_later", "history"):
            assert f'data-fav="{kind}"' in page, kind

        assert page.index('data-fav="follow"') < page.index('data-fav="watch_later"')
        assert page.index('data-fav="watch_later"') < page.index('data-fav="history"')

        assert 'id="favReloadBtn"' in page
        assert 'id="favList"' in page

        # 接口要真的接上，且取的是这一页自己的路由
        assert PANEL_FAVORITES_PATH.lstrip("/") in page
        assert '"api/panel/favorites?kind="' in page

        # 第一次进页面才拉数据（要新数据有「刷新」按钮）
        assert 'if (key === "fav" && !favState.kind) favSetKind("favorite");' in page

    def test_the_two_list_less_categories_go_straight_to_parsing(self, panel):
        """
        「稍后再看」「历史记录」桌面端就没有列表可点，点一下直接丢给解析器
        （flyout.py:246,253）。面板照做：拿到服务端那句 direct_parse 就不铺卡片，
        直接解析出条目

        🔴 地址（bili23://…）**只能由服务端给**。前端再抄一份的话，两边改一边漏一边，
        症状是"点了没反应"，而页面上没有任何线索
        """
        page = self.page(panel)

        assert 'if (d.ok && d.direct_parse) { favParseDirect(d); return; }' in page

        # 用服务端给的地址去解析，而不是写死的字符串。
        # `direct: true` 让空态说成"这个列表里暂时没有内容"（账号里没存过东西
        # 不是异常），而不是"这个收藏夹没解析出内容"
        assert 'favParseInto(panel, { title: d.label || "", direct: true }, 1, d.parse_url);' in page

        # 前端不许出现这两个协议地址 —— 它们属于服务端（favorites.DIRECT_PARSE_KINDS）
        assert "bili23://" not in page

        # 空态两种说法要分开：稍后再看/历史记录本来就可能是空的（账号里没存过东西），
        # 那不是异常；收藏夹解析不出内容才该是"这个收藏夹"的说法
        assert 'meta.direct ? "这个列表里暂时没有内容" : "这个收藏夹没解析出内容"' in page

        # 未登录时给一条能走的路，而不是一句错误对象
        assert "去登录" in page

    def test_favorites_page_reuses_the_parse_and_download_chain(self, panel):
        """
        点开一个收藏夹后走的是「下载中」页那条链路，不另写一套解析 / 建任务

        这一页只负责列出收藏夹；解析与建任务自己再实现一遍，迟早出现
        「网页里能下、客户端里不行」这类分叉
        """
        page = self.page(panel)

        assert 'api("parse_url", { url: url, page: page || 1 })' in page
        assert 'api("create_download", { episode_ids: picked })' in page

        # 没登录 B站账号时要给出下一步，而不是干巴巴一句错误
        assert "need_login" in page

        # 一次只展开一个收藏夹，否则一屏里堆着好几份勾选列表，分不清在挑哪个
        assert 'querySelectorAll(".favep")' in page

    def test_a_series_offers_its_other_seasons(self, panel):
        """
        番剧解析后要能换季 —— 桌面端是解析页左下角那个下拉（SeasonComboBox），
        「能解析好几部」靠的就是它

        两处都要有：概览页解析结果那一行、收藏页展开的条目那一行。都是同一份
        约定：`seasons` 由 parse_url 透出，换一项就重新解析那一部
        """
        page = self.page(panel)

        # 概览页：解析结果头行里的下拉
        assert 'id="seasonSel"' in page
        assert 'function renderSeasonSel(d, url)' in page
        assert 'if (list.length < 2) {' in page

        # 收藏页展开：同一个下拉，用同一个字段
        assert 'function favSeasonSel(d, url)' in page
        assert 'd.seasons' in page
        assert 'd.current_season_id' in page

        # 换季 = 重新解析那一部（不是就地筛一遍当前列表）
        assert 'favParseInto(panel, meta, 1, this.value)' in page
        assert 'runParse(url, 1)' in page

    def test_a_paginated_listing_can_turn_its_pages(self, panel):
        """
        稍后再看 / 历史记录这类接口本身分页（一页 20 条），不翻页就只能看到第一页

        翻页是**重新解析**那一页，等于整页替换，所以勾选跟着重来 ——
        页码写在条目数旁边，让人知道自己在第几页
        """
        page = self.page(panel)

        assert 'data-act="prev"' in page and 'data-act="next"' in page
        assert 'd.pagination' in page

        # 分页信息由服务端从解析器那儿透出（util/mcp/tools/parse.py）
        assert 'favParseInto(panel, meta, page + 1, url)' in page
        assert 'favParseInto(panel, meta, page - 1, url)' in page

    def test_notifications_live_below_the_account_page(self, panel):
        """
        通知是独立页面，排在侧边栏最末 ——「B站账号」下面

        前九页都是"配置下载器"，只有通知页是下载器反过来找我：它会在没人盯着
        的时候主动出声，所以搁在最后、与账号授权挨着
        """
        page = self.page(panel)

        assert 'id="page-notify"' in page
        assert 'data-page="notify"' in page

        # 三个接口都要接上
        assert PANEL_NOTIFY_PATH.lstrip("/") in page
        assert PANEL_NOTIFY_SAVE_PATH.lstrip("/") in page
        assert PANEL_NOTIFY_TEST_PATH.lstrip("/") in page

        # 导航顺序：设置 → B站账号 → 通知
        assert page.index('data-page="settings"') < page.index('data-page="account"')
        assert page.index('data-page="account"') < page.index('data-page="notify"')

        # 它就是最后一项 —— 侧栏里 data-page 的收尾是 notify
        nav = page[page.index('<nav class="nav">'):page.index("</nav>")]

        assert re.findall(r'data-page="([a-z]+)"', nav)[-1] == "notify"

        for control in ('id="ntWecomOn"', 'id="ntWecomCorp"', 'id="ntWecomAgent"',
                        'id="ntWecomSecret"', 'id="ntWecomUser"', 'id="ntWecomBase"',
                        'id="ntWecomTestBtn"',
                        'id="ntTgOn"', 'id="ntTgToken"', 'id="ntTgChat"', 'id="ntProxy"',
                        'id="ntTgTestBtn"', 'id="ntOnComplete"', 'id="ntOnFail"',
                        'id="ntHistory"', 'id="ntErr"', 'id="ntSaveBtn"'):
            assert control in page, control

        # 凭据输入框默认是掩码，旁边要有显示/隐藏的开关
        assert 'id="ntWecomEye"' in page
        assert 'id="ntTgEye"' in page

    def test_the_two_credential_fields_are_rendered_masked(self, panel):
        """应用 Secret 与 bot token 等于以你的名义发消息的权限，默认不该明文躺在页面上"""
        page = self.page(panel)

        for field in ("ntWecomSecret", "ntTgToken"):
            found = re.search(r'<input[^>]*id="%s"[^>]*>' % field, page)

            assert found, field
            assert 'type="password"' in found.group(0), found.group(0)

    @staticmethod
    def _matching_close(html, start):
        """
        从 start 处的那个 <div 出发，返回配对的 </div> 的结束位置

        start 必须落在 "<div" 的 "<" 上 —— 落在属性里（比如 "class=" 的位置）
        会让计数从内层 div 开始错位，结果是一个提前返回的假终点
        """
        depth = 0

        for token in re.finditer(r"<div\b|</div>", html[start:]):
            if token.group(0) == "</div>":
                depth -= 1

                if depth == 0:
                    return start + token.end()

            else:
                depth += 1

        raise AssertionError("没有配对的 </div>")

    def _card_span(self, page, marker_index):
        """marker 所落的那张 .glass panel 卡片的 [开标签起点, 闭标签终点)"""
        attr = page.rindex('class="glass panel"', 0, marker_index)
        start = page.rindex("<div", 0, attr)

        return start, self._matching_close(page, start)

    def test_explanations_sit_inside_the_config_card(self, panel):
        """
        「备份原理」与「MCP 是什么」两段说明在配置卡片**内部**底端

        它们原先各自是卡片外面独立的一小块。实测两者间距本来就被压到 0 ——
        看着"分成两块"其实是两张卡各自的边框与阴影。移进卡片少一层壳，
        说明在视觉上也归到了它所解释的那组配置里
        """
        page = self.page(panel)

        assert 'class="howto-in"' in page
        # 旧的独立块外壳不能残留
        assert 'class="glass howto"' not in page

        for marker in ("备份原理：", "MCP 是什么："):
            marker_index = page.index(marker)
            panel_start, panel_end = self._card_span(page, marker_index)

            # 落在卡片的开闭标签之间 = 在卡片内部，而不是卡外的兄弟节点
            assert panel_start < marker_index < panel_end, marker

    def test_the_card_span_check_discriminates_the_two_layouts(self):
        """
        反向验证：上面那条断言真的分得开"卡内"与"卡外"

        一个只会说"通过"的守卫比没有守卫更糟 —— 上次就吃过一次亏（CDP 那
        条断言永远为真，等于没测）。这里把两种版式各构造一份，验证判定
        确实会给出相反结论
        """
        inside = (
            '<div class="glass panel"><div class="h">CD2 配置</div>'
            '<div style="padding:6px 20px 16px"><input></div>'
            '<div class="howto-in">备份原理：说明在卡片内部</div></div>'
        )
        outside = (
            '<div class="glass panel"><div class="h">CD2 配置</div>'
            '<div style="padding:6px 20px 16px"><input></div></div>'
            '<div class="glass howto">备份原理：说明在卡片外面</div>'
        )

        for html, expected in ((inside, True), (outside, False)):
            marker_index = html.index("备份原理：")
            start, end = self._card_span(html, marker_index)

            assert (start < marker_index < end) is expected, html

    def test_the_mcp_explanation_points_at_the_copy_above_it(self, panel):
        """说明进卡片后就在配置上方，文案里的"下面的客户端配置"必须跟着改，否则指错方向"""
        page = self.page(panel)

        assert "上面的客户端配置" in page
        assert "下面的客户端配置" not in page

    def test_done_page_has_a_clear_records_button(self, panel):
        """
        「已完成」页要有「清理记录」，并且**必须写明不删文件**

        这个按钮删的是数据库里的记录行，不是磁盘上的成品。用户在下载页上顺手点
        「清理」，很容易理解成「把这一集删了」—— 1.3 GB 的成品真被删掉是不可逆的，
        所以界面上的那句话是这条测试真正要守的东西
        """
        page = self.page(panel)

        assert 'id="doneClearBtn"' in page
        assert PANEL_DONE_CLEAR_PATH.lstrip("/") in page
        assert "文件不会被删除" in page

    def test_downloading_page_has_bulk_pause_cancel_and_resume(self, panel):
        """
        「下载中」页要有「一键暂停 / 一键取消 / 一键开始」

        三颗按钮走 pause_all_tasks / cancel_all_tasks / resume_all_tasks（页面里由
        action + "_all_tasks" 拼出来，所以断言的是那个后缀），服务端逐条复用单任务
        那条控制链路 —— 前端循环调 30 次 pause_task 的话，每 3 秒一轮的自动刷新会
        与它互相打架，列表一边暂停一边重画。

        同时锁住两件事：
        - "队列空时禁用"：那三个 disabled 赋值在刷新里，少了它们，空队列下点按钮
          只会得到一句 toast，按钮看着还是能按的
        - 取消的确认框：它是这三颗里唯一不可逆的（会删未完成的临时文件），
          确认框必须写在发请求之前，并且要说明"已下载好的文件不受影响"
        """
        page = self.page(panel)

        assert 'id="dlPauseAll"' in page
        assert 'id="dlCancelAll"' in page
        assert 'id="dlStartAll"' in page
        assert "一键暂停" in page
        assert "一键取消" in page
        assert "一键开始" in page

        assert 'bulkControl("pause"' in page
        assert 'bulkControl("cancel"' in page
        assert 'bulkControl("resume"' in page
        assert '_all_tasks' in page

        assert 'dlPauseAll").disabled' in page
        assert 'dlCancelAll").disabled' in page
        assert 'dlStartAll").disabled' in page

        assert "这个操作不可撤销" in page
        assert "已经下载好的文件不受影响" in page

    def test_done_page_pages_through_the_records(self, panel):
        """
        「已完成」是长期累积的，按页取而不是一次拉几百条

        光有翻页按钮不算数：页码要作为 offset 交给 list_tasks，服务端再下推到
        SQL —— 页面源码里得同时看得见这两样，还得有那条分页条
        """
        page = self.page(panel)

        assert 'id="donePager"' in page
        assert "DONE_PAGE_SIZE" in page
        assert 'state: "completed"' in page
        assert "offset:" in page
        assert "上一页" in page and "下一页" in page
        # 页码是前端状态，自动刷新（每 3 秒）不能把用户弹回第一页
        assert "donePage" in page

    def test_has_a_log_window_wired_to_the_logs_endpoint(self, panel):
        page = self.page(panel)

        assert 'id="page-log"' in page
        assert 'id="logBox"' in page
        # 页面里用相对路径取数据，所以比对的是去掉前导斜杠的那一份
        assert PANEL_LOGS_PATH.lstrip("/") in page

    def test_tab_icon_is_embedded(self, panel):
        """浏览器标签页图标以内嵌 data URI 提供：容器可能上不了外网，外链会白"""
        page = self.page(panel)

        assert 'rel="icon"' in page
        assert 'href="data:image/x-icon;base64,' in page

    def test_mcp_lives_on_its_own_page(self, panel):
        """
        MCP 服务器是独立页面（侧边栏「云端同步」下面那项），不再挤在设置页里
        """
        page = self.page(panel)

        assert 'id="page-mcp"' in page
        assert 'data-page="mcp"' in page
        assert 'id="mcpBox"' in page
        assert PANEL_MCP_TOKEN_PATH.lstrip("/") in page
        # 操作按钮由 renderMcp 动态画，页面源码里应有它们的委托标记
        assert "data-mcp=\"copy-token\"" in page
        assert "data-mcp=\"regen-token\"" in page
        assert "data-mcp=\"copy-client\"" in page
        # 旧版挤在设置页里的容器必须删干净，免得出现两处渲染
        assert 'id="setMcp"' not in page

    def test_settings_groups_are_foldable(self, panel):
        """分组可折叠：组头带 fold 标记，状态记在 localStorage，代理行有灰显逻辑"""
        page = self.page(panel)

        assert "setgroup fold" in page
        assert 'localStorage.getItem("setFold")' in page
        assert "syncProxyDim" in page
        assert 'data-key="proxy_mode"' in page

    def test_config_file_section_is_wired(self, panel):
        """「配置文件设置」三个按钮 + 隐藏文件选择框 + 三个端点都要在页面上"""
        page = self.page(panel)

        assert 'id="cfgExportBtn"' in page
        assert 'id="cfgImportBtn"' in page
        assert 'id="cfgResetBtn"' in page
        assert 'id="cfgFile"' in page
        assert PANEL_CONFIG_EXPORT_PATH.lstrip("/") in page
        assert PANEL_CONFIG_IMPORT_PATH.lstrip("/") in page
        assert PANEL_CONFIG_RESET_PATH.lstrip("/") in page

    def test_every_nav_button_points_at_a_real_page(self, panel):
        """
        data-page 的值必须真的对应一个页面 section 的 id 后缀（page-<key>）

        踩过一次（浮层时代）：按钮上写的是短名，开关要的却是元素 id，
        两套命名一错位就是"点了没反应"，而且异常发生在监听器里，
        页面上不留任何痕迹，只能靠人肉点一遍才发现。页面版沿用同一条纪律
        """
        page = self.page(panel)

        keys = set(re.findall(r'data-page="([a-z]+)"', page))

        assert keys, "一个导航按钮都没找到"

        for key in sorted(keys):
            assert f'id="page-{key}"' in page, f"导航指向了不存在的页面：{key}"

    def test_stays_offline_capable(self, panel):
        # 容器与 NAS 上的浏览器未必能出网，任何外部引用都会让面板在最需要
        # 它的时候白屏
        page = self.page(panel)

        assert 'src="http' not in page
        assert 'href="http' not in page

    def test_theme_follows_the_system_scheme(self, panel):
        """
        浅色与暗色共用同一份布局，靠 CSS 变量切换：:root 是暗色默认，
        系统报 light 时用媒体查询换到浅色变量 —— 不写 JS，跟随零延迟
        """
        page = self.page(panel)

        assert "color-scheme: dark" in page
        assert "@media (prefers-color-scheme: light)" in page

        # 两套变量都必须是完整的：切换不是叠加背景色，而是整套颜色互换。
        # 统计"定义"（变量名后紧跟冒号），每个 key 变量在两套主题里各定义一次
        for var in ("--bg:", "--panel:", "--text:", "--muted:", "--line:", "--accent-green:"):
            assert page.count(var) == 2, f"{var} 定义了 {page.count(var)} 次，应为 2（暗色与浅色各一）"

    def test_cloud_backup_button_is_wired(self, panel):
        page = self.page(panel)

        assert 'id="syncBtn"' in page
        assert PANEL_SYNC_PATH.lstrip("/") in page

    def test_overview_actions_use_the_ring_button_style(self, panel):
        """
        概览页右上角那排圆形按钮：**就三颗** —— 刷新 / 云端备份 / 修改面板密码。
        图标走 stroke="url(#icoGrad)" 引用共享渐变 —— 引不到时 stroke 会变成 none，
        图标整个消失且不报错，所以 defs 必须跟着按钮一起断言
        """
        page = self.page(panel)

        assert 'class="ring-btn" id="refreshBtn"' in page
        assert 'class="ring-btn" id="ovBackupBtn"' in page
        assert 'class="ring-btn" id="ovPwdBtn"' in page
        assert '<linearGradient id="icoGrad"' in page

        rings = page.count('class="ring-btn"')

        assert rings == 3, f"概览右上角有 {rings} 颗圆环按钮，应为 3"

        # 数"引用的图标"，不数那个字符串 —— 注释里提到它一次也会被算进去
        cited = re.findall(r'<svg[^>]*stroke="url\(#icoGrad\)"', page)

        assert len(cited) == rings, f"引用了渐变的图标有 {len(cited)} 个，应为 {rings}"

        # 环本身：外层吃渐变当底色，::before 铺内圆，中间留出那一圈描边。
        # 配色必须是主题那套「紫 → 青」（= --grad 的两个端点），不能再出现粉/紫/靛那版
        # —— 用户 2026-10-02 反馈"跟面板主题不搭、太跳"
        assert "linear-gradient(135deg, #7c3aed" in page
        assert ".ring-btn::before" in page

        # 环和图标（icoGrad 的 stop）必须同色，否则按钮会出现"外圈一个色、里面图标另一个色"
        assert '<stop offset="0" stop-color="#7c3aed"/>' in page
        assert '<stop offset="1" stop-color="#06b6d4"/>' in page
        # 旧那套（粉→紫→靛）不许再出现在实际样式里 —— 注释里也不写色号，就是为让这条能生效
        assert 'stop-color="#f472b6"' not in page
        assert "linear-gradient(135deg, #f472b6" not in page

        # 刷新那颗要转得起来
        assert "ring-spin" in page

    def test_brand_mark_uses_the_bilibili_logo(self, panel):
        """
        侧栏顶部品牌方块里放的是 bilibili 官方图标（assets/app.svg 那个「电视头」，
        也是桌面版程序图标），不再是白色字母 B —— 那个看着像占位符。
        断言用 path 的开头片段定位，别整条贴上来（太长，以后微调形状就红）
        """
        page = self.page(panel)

        assert '<span class="mark">' in page
        assert '<span class="mark">B</span>' not in page, "字母 B 占位符又回来了"
        assert 'class="mark"><svg viewBox="0 0 24 24"' in page
        assert 'd="M17.813 4.653h.854' in page, "没找到 bilibili 图标那条 path"
        # 白描在主题渐变底上，浅/暗两套主题都成立
        assert ".mark svg { width: 21px; height: 21px; display: block; fill: #fff; }" in page

    def test_overview_header_keeps_only_three_actions(self, panel):
        """
        概览右上角**只留动作按钮**，顺序 = 刷新 → 云端备份 → 修改密码。

        曾经加过 5 颗切页快捷（下载中/已完成/日志/设置/通知）与一条分组竖线，
        用户看过之后要求撤掉 —— 侧栏本来就够用，那个位置留给真正的动作。
        这条断言是防回潮的：nav 那 5 个 id 与 .pact-sep 一旦重新出现就红
        """
        page = self.page(panel)

        order = ["refreshBtn", "ovBackupBtn", "ovPwdBtn"]
        spots = [page.index(f'id="{b}"') for b in order]

        assert spots == sorted(spots), "右上角顺序应为 刷新 → 云端备份 → 修改密码"

        for gone in ("ovGoDownloading", "ovGoDone", "ovGoLog", "ovGoSettings", "ovGoNotify"):
            assert gone not in page, f"{gone} 应该已经撤掉"

    def test_parse_row_count_is_a_quiet_label_not_a_status_chip(self, panel):
        """
        解析结果那一行的计数（「31 个条目」）原来复用了 `.tag` —— 那是**任务行里的状态胶囊**
        （11px、圆角 999px），摆进 `.row` 这种 flex 行就被 `align-items` 的默认值 stretch
        纵向拉满：线上实测渲染成 **64 × 42px 的空药丸**，11px 的字只占中间一小条，
        旁边却是 14px 的两颗按钮 ⇒ 用户 2026-10-02 圈出来说「显示不协调」。

        现在三处一起改：
          ① 专用 `.cnt`（13px 静音字 + 数字加重，与 `.phead .count` 同一语汇）
          ② `.row` 补 `align-items: center` —— 从根上不让行里的矮元素再被拉满
          ③ 该行左右不再内缩，「开始下载」的右缘与上下卡片的右缘落在同一条竖线上
        """
        page = self.page(panel)

        # ① 计数换成语义正确的类；.tag 只该属于任务状态胶囊
        assert 'class="cnt" id="parseCount"' in page
        assert '<span class="tag" id="parseCount"' not in page, "计数又用回状态胶囊了"

        cnt = re.search(r"\.cnt \{[^}]*\}", page)

        assert cnt, "找不到 .cnt 的样式"
        assert "font-size: 13px" in cnt.group(0), ".cnt 没跟上按钮的字号（11px 那版会发小）"
        assert ".cnt b {" in page, "数字没有单独加重"

        # ② 通用行垂直居中。少这一句，行里任何比按钮矮的东西都会被拉成一条空白
        rule = re.search(r"\.row \{[^}]*\}", page)

        assert rule, "找不到 .row 的样式"
        assert "align-items: center" in rule.group(0), ".row 没有垂直居中，矮元素会被 stretch 拉满"

        # ③ 行不内缩，右缘与卡片对齐（原来是 4px 内缩，看着差半个像素）
        assert 'class="row" style="margin:10px 0 8px"' in page

        # 数字经 Number() 收一道再拼进 HTML，不把外部字符串原样塞进去
        assert '"<b>" + Number(episodes.length) + "</b> 个条目"' in page

        assert 'class="pact-sep"' not in page
        assert "pact-sep" not in page, ".pact-sep 的样式与用法都该清干净"

    def test_change_password_gate_has_one_implementation(self, panel):
        """设置页那颗方按钮与概览那颗圆按钮都要能开改密码浮层，共用 openPwdGate"""
        page = self.page(panel)

        assert "function openPwdGate(" in page
        assert '$("pwdBtn").onclick = openPwdGate;' in page
        assert '$("ovPwdBtn").onclick = openPwdGate;' in page

    def test_overview_backup_shares_the_cloud_sync_action(self, panel):
        """概览页的快捷备份不另写一份请求逻辑：两个入口都归 runCloudBackup"""
        page = self.page(panel)

        assert "function runCloudBackup(" in page
        assert "runCloudBackup(this, true)" in page    # 概览：离手近，先问一句
        assert "runCloudBackup(this, false)" in page   # 同步页：原样直发

    def test_sync_page_is_wired_to_the_config_endpoints(self, panel):
        """云端同步页：CD2 连接信息与凭据的读写接口都要在页面里接上"""
        page = self.page(panel)

        assert 'id="syncHost"' in page
        assert 'id="syncSource"' in page
        assert 'id="syncUser"' in page
        assert 'id="syncPass"' in page
        assert PANEL_SYNC_CONFIG_PATH.lstrip("/") in page
        assert PANEL_SYNC_CONFIG_SAVE_PATH.lstrip("/") in page

    def test_sync_page_explains_the_cd2_backup_path(self, panel):
        """页面上要说清备份是"让 CD2 自己重扫那条备份"，不是逐文件复制"""
        page = self.page(panel)
        sect = page.split('id="page-sync"')[1].split("</section>")[0]

        assert "BackupRestartWalkingThrough" in sect
        assert "GetToken" in sect
        # 旧的容器内 cp 逐文件实现必须被写成"已取代"，而不是留着当现行说明
        assert "不再是在容器里" in sect

    def test_download_queue_lives_on_its_own_page(self, panel):
        """概览页不再常驻下载队列（2026-10-03 用户要求撤掉那张卡）。

        队列**只在**「下载中」页渲染一份。同一天还试过在解析结果头行摆两颗
        「下载中 / 已完成」跳转按钮，用户随即要求撤掉（「取消这里下载中，已完成，按钮」）
        ⇒ 概览页现在**没有任何队列入口**。
        ⚠️ 断言写成 `not in` 是防回潮：`#active` 一旦被加回来、而 refresh 里那份
        `renderTasks($("active"), …)` 没跟着恢复，就是每 3 秒 `null.innerHTML` 崩一次，
        页面上只表现为"队列不更新"，控制台之外什么线索都没有。
        """
        page = self.page(panel)

        assert 'id="active"' not in page
        assert 'id="activeDetail"' in page
        assert 'class="metrics"' in page
        # 两颗跳转按钮已被用户撤掉，别再回加
        assert 'id="goActiveBtn"' not in page
        assert 'id="goDoneBtn"' not in page
        assert 'class="jbtn"' not in page

    def test_the_removed_queue_buttons_leave_no_dangling_bindings(self, panel):
        """按钮撤了，绑在它们身上的 onclick 与计数赋值必须一起撤。

        留一句 `$("goActiveBtn").onclick = …` 就是给 null 挂属性，页面启动即抛
        TypeError；留一句 `$("goActiveCnt").textContent = …` 则是每 3 秒的 refresh
        里炸一次 —— 两种都属"按钮看着没了、页面静默失灵"。撤按钮时最容易漏的就是这处。
        """
        page = self.page(panel)

        assert '$("goActiveBtn")' not in page
        assert '$("goDoneBtn")' not in page
        assert '$("goActiveCnt")' not in page
        assert '$("goDoneCnt")' not in page
        # 概览那份渲染必须一起撤掉，否则 null.innerHTML
        assert 'renderTasks($("active")' not in page


class TestWecomCallbackEndpoint:
    """
    企微回调端点（/api/wecom/callback）。

    它与全站其它接口最大的差别是**匿名**：企微服务器手里没有面板令牌，所以
    "谁能进"只能由 msg_signature 决定。这个类要钉住的正是这条界线 ——
    既有"该放行时真能通"，也有"签名不对时一步都别想进"。
    """

    def prepare(self, monkeypatch, **overrides):
        fake = FakeConfig()

        monkeypatch.setattr("util.web.server.config", fake)
        monkeypatch.setattr("util.common.notify.config", fake)
        monkeypatch.setattr("util.mcp.invoke.call_in_main_thread", lambda func, *a, **kw: func(*a))

        # 回调历史是模块级的，跨用例共享会让"记没记一笔"的断言失真
        notify.CALLBACK_HISTORY.clear()

        self.enable(fake, **overrides)

        return fake

    def enable(self, fake, **overrides):
        """直接写进假配置 —— 用例的焦点在端点上，不必绕一层保存接口"""
        settings = callback_kwargs(**overrides)

        fake.store["notification_wecom_callback_enabled"] = settings["wecom_callback_enabled"]
        fake.store["notification_wecom_callback_token"] = settings["wecom_callback_token"]
        fake.store["notification_wecom_aes_key"] = settings["wecom_aes_key"]
        fake.store["notification_wecom_callback_base"] = settings["wecom_callback_base"]

    @staticmethod
    def pack(aes_key: str, message_xml: str, receive_id: str = WECOM_CORP_ID) -> str:
        """按企微的报文格式造一条密文（random16 + len4 + msg + receiveid）"""
        key = wecom_callback.decode_aes_key(aes_key)
        raw = message_xml.encode("utf-8")
        payload = b"\x07" * 16 + struct.pack(">I", len(raw)) + raw + receive_id.encode("utf-8")

        return base64.b64encode(wecom_callback._cbc_encrypt(key, wecom_callback._pad(payload))).decode("ascii")

    def verify_url(self, echo: str, token = None, timestamp = "1759385000", nonce = "nonce8000") -> str:
        """拼一条企微会发出的 GET 验证请求（echostr 必须 URL 编码：base64 里有 +/= ）"""
        token = token or CALLBACK_TOKEN
        signature = wecom_callback.compute_signature(token, timestamp, nonce, echo)

        return (
            f"{WECOM_CALLBACK_PATH}?msg_signature={signature}"
            f"&timestamp={timestamp}&nonce={nonce}&echostr={quote(echo)}"
        )

    def post_message(self, panel, message_xml, token = None, body_wrap = None, with_headers = False):
        token = token or CALLBACK_TOKEN
        echo = self.pack(CALLBACK_AES_KEY, message_xml)
        timestamp, nonce = "1759385000", "nonce8000"
        signature = wecom_callback.compute_signature(token, timestamp, nonce, echo)

        body = body_wrap or (
            f"<xml><ToUserName><![CDATA[{WECOM_CORP_ID}]]></ToUserName>"
            f"<Encrypt><![CDATA[{echo}]]></Encrypt><AgentID><![CDATA[1000005]]></AgentID></xml>"
        )

        path = f"{WECOM_CALLBACK_PATH}?msg_signature={signature}&timestamp={timestamp}&nonce={nonce}"
        body = body.encode("utf-8")

        if with_headers:
            status, raw, headers = full_request(panel, "POST", path, body = body)

            return status, raw, headers

        return request(panel, "POST", path, body = body)

    # ---- 门禁 ----

    def test_reachable_without_login(self, panel, monkeypatch):
        """匿名是设计要求：企微不会带我们的令牌来敲门"""
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, "<xml><Content><![CDATA[ping]]></Content></xml>")))

        assert status == 200
        assert raw.decode("utf-8") == "<xml><Content><![CDATA[ping]]></Content></xml>"

    def test_forbidden_when_callback_disabled(self, panel, monkeypatch):
        """没开回调就被敲门：不给任何线索，只回 forbidden"""
        self.prepare(monkeypatch, wecom_callback_enabled = False)

        status, raw = request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, "<xml/>")))

        assert status == 403
        assert raw == b"forbidden"

    def test_forbidden_on_bad_signature(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        echo = self.pack(CALLBACK_AES_KEY, "<xml/>")
        path = f"{WECOM_CALLBACK_PATH}?msg_signature={'0' * 40}&timestamp=1759385000&nonce=nonce8000&echostr={quote(echo)}"

        status, raw = request(panel, "GET", path)

        assert status == 403
        # 失败原因只进日志：响应体是任何人读得到的输出，写细节等于递梯子
        assert raw == b"forbidden"

    def test_rejection_does_not_leak_the_reason(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        echo = self.pack(CALLBACK_AES_KEY, "<xml/>")
        path = f"{WECOM_CALLBACK_PATH}?msg_signature=deadbeef&timestamp=1&nonce=2&echostr={quote(echo)}"

        status, raw = request(panel, "GET", path)

        assert status == 403
        assert b"signature" not in raw and b"Token" not in raw

    def test_forbidden_when_credentials_incomplete(self, panel, monkeypatch):
        """开着开关但 EncodingAESKey 是空的：解不出任何东西，必须拒绝而不是 500"""
        self.prepare(monkeypatch, wecom_aes_key = "")

        status, raw = request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, "<xml/>")))

        assert status == 403

    # ---- 验证通过 ----

    def test_verification_returns_plain_text_verbatim(self, panel, monkeypatch):
        """明文必须原样回：多一个引号、换行或 BOM，企微都判配置失败"""
        self.prepare(monkeypatch)

        plain = "<xml><ToUserName><![CDATA[ww1]]></ToUserName><MsgType><![CDATA[text]]></MsgType></xml>"

        status, raw = request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, plain)))

        assert status == 200
        assert raw.decode("utf-8") == plain
        assert not raw.startswith(b"\xef\xbb\xbf")

    def test_verification_is_recorded(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, "<xml/>")))

        entries = notify.callback_history()

        assert len(entries) == 1
        assert entries[0]["ok"] is True
        assert entries[0]["detail"] == "URL 验证通过"

    def test_no_cache_header_on_callback_response(self, panel, monkeypatch):
        """反代与浏览器都不该缓存这个响应 —— 验证明文每次都不一样"""
        self.prepare(monkeypatch)

        _, _, headers = full_request(panel, "GET", self.verify_url(self.pack(CALLBACK_AES_KEY, "<xml/>")))

        assert "no-store" in headers.get("Cache-Control", "")

    # ---- 收消息 ----

    def test_text_message_gets_an_encrypted_reply(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        xml = (
            f"<xml><ToUserName><![CDATA[{WECOM_CORP_ID}]]></ToUserName>"
            "<FromUserName><![CDATA[Desire]]></FromUserName><CreateTime>1</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[在吗]]></Content>"
            "<MsgId>1</MsgId><AgentID>1000005</AgentID></xml>"
        )

        status, raw, headers = self.post_message(panel, xml, with_headers = True)

        assert status == 200
        assert "application/xml" in headers.get("Content-Type", "")

        fields = wecom_callback.parse_message(raw.decode("utf-8"))

        assert "Encrypt" in fields and "MsgSignature" in fields

        # 回复必须能被同一套规则解回来 —— 企微就是这么读它的
        decrypted = wecom_callback.verify_url(
            CALLBACK_TOKEN, wecom_callback.decode_aes_key(CALLBACK_AES_KEY),
            fields["TimeStamp"], fields["Nonce"], fields["MsgSignature"], fields["Encrypt"], WECOM_CORP_ID,
        )

        assert "<Content><![CDATA[已收到" in decrypted

    def test_event_message_gets_success(self, panel, monkeypatch):
        """非文本消息不做被动回复，按官方建议回 success"""
        self.prepare(monkeypatch)

        xml = (
            f"<xml><ToUserName><![CDATA[{WECOM_CORP_ID}]]></ToUserName>"
            "<FromUserName><![CDATA[Desire]]></FromUserName><CreateTime>1</CreateTime>"
            "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[click]]></Event>"
            "<EventKey><![CDATA[SYNC]]></EventKey><AgentID>1000005</AgentID></xml>"
        )

        status, raw = self.post_message(panel, xml)

        assert status == 200
        assert raw == b"success"

        assert notify.callback_history()[0]["detail"] == "事件 click:SYNC"

    def test_post_with_bad_signature_is_forbidden(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = self.post_message(panel, "<xml><MsgType><![CDATA[text]]></MsgType></xml>", token = "WrongToken")

        assert status == 403
        assert raw == b"forbidden"

    def test_post_without_encrypt_field_is_forbidden(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, _ = self.post_message(panel, "<xml/>", body_wrap = "<xml><ToUserName><![CDATA[x]]></ToUserName></xml>")

        assert status == 403

    # ---- 与通知页的衔接 ----

    def test_payload_exposes_the_callback_path_and_readiness(self, panel, monkeypatch):
        self.prepare(monkeypatch)

        status, raw = request(panel, "GET", PANEL_NOTIFY_PATH, cookie = login(panel))

        assert status == 200

        d = json.loads(raw)

        assert d["callback_path"] == WECOM_CALLBACK_PATH
        assert d["callback_ready"] is True
        assert d["callback_history"] == []

    def test_saved_credentials_are_never_echoed_back(self, panel, monkeypatch):
        """
        保存成功、状态正确，但响应体里连一个字符的真值都没有

        EncodingAESKey 拿到就能解出任何一条抓到过的回调密文、伪造一条"来自企业微信"
        的推送；回调 Token 拿到就能算出合法签名。它们没有理由回浏览器一趟
        """
        fake = self.prepare(monkeypatch, wecom_callback_enabled = False)

        status, raw = request(
            panel, "POST", PANEL_NOTIFY_SAVE_PATH,
            body = json.dumps({"config": notify_values(**callback_kwargs())}).encode("utf-8"),
            cookie = login(panel),
        )

        assert status == 200

        d = json.loads(raw)

        assert d["config"]["wecom_callback_enabled"] is True
        # 值不回，只回"设了没"
        assert d["config"]["wecom_callback_token"] == ""
        assert d["config"]["wecom_aes_key"] == ""
        assert d["secrets"]["wecom_callback_token"] is True
        assert d["secrets"]["wecom_aes_key"] is True
        assert d["callback_ready"] is True

        # 真值确实写进配置了
        assert fake.store["notification_wecom_callback_token"] == CALLBACK_TOKEN
        assert fake.store["notification_wecom_aes_key"] == CALLBACK_AES_KEY

        # 但响应里一个字都不许有
        body = raw.decode("utf-8")
        assert CALLBACK_TOKEN not in body
        assert CALLBACK_AES_KEY not in body

    def test_blank_credentials_keep_what_is_already_stored(self, panel, monkeypatch):
        """
        留空 = 不改。用户本来就看得到空框（真值不下发），要是再把"空"当成"清空"，
        他改一下开关就把回调凭据抹没了，而且完全看不出发生了什么
        """
        fake = self.prepare(monkeypatch, wecom_callback_enabled = True)

        status, raw = request(
            panel, "POST", PANEL_NOTIFY_SAVE_PATH,
            body = json.dumps({"config": notify_values(
                wecom_callback_enabled = True,
                wecom_callback_token = "",
                wecom_aes_key = "",
            )}).encode("utf-8"),
            cookie = login(panel),
        )

        assert status == 200

        d = json.loads(raw)

        assert d["secrets"]["wecom_callback_token"] is True
        assert fake.store["notification_wecom_callback_token"] == CALLBACK_TOKEN
        assert fake.store["notification_wecom_aes_key"] == CALLBACK_AES_KEY

    def test_enabling_without_credentials_is_rejected(self, panel, monkeypatch):
        """
        两边都没有凭据时开启 → 企微那边只会显示"配置失败"，这里就说明白缺什么

        要**连服务端已存的那份也清掉**：留空提交的语义是"不改"，服务端有值的时候
        不该报错（见 test_blank_credentials_keep_what_is_already_stored）
        """
        self.prepare(
            monkeypatch, wecom_callback_enabled = False,
            wecom_callback_token = "", wecom_aes_key = "",
        )

        status, raw = request(
            panel, "POST", PANEL_NOTIFY_SAVE_PATH,
            body = json.dumps({"config": notify_values(wecom_callback_enabled = True)}).encode("utf-8"),
            cookie = login(panel),
        )

        assert status == 400
        assert "回调 Token" in json.loads(raw)["error"]

    def test_bad_aes_key_length_is_rejected(self, panel, monkeypatch):
        self.prepare(monkeypatch, wecom_callback_enabled = False)

        status, raw = request(
            panel, "POST", PANEL_NOTIFY_SAVE_PATH,
            body = json.dumps({"config": notify_values(**callback_kwargs(wecom_aes_key = "tooshort"))}).encode("utf-8"),
            cookie = login(panel),
        )

        assert status == 400
        assert "43" in json.loads(raw)["error"]

    def test_callback_base_host_is_lowercased(self, panel, monkeypatch):
        """
        对外地址的主机名存下来必须是小写。

        反代（实测是 Lucky）按字符串比对域名：大写那个会落到默认站点吃 404，
        小写的才是真正的规则。用户在地址栏怎么敲的不该决定企微能不能通
        """
        self.prepare(monkeypatch, wecom_callback_enabled = False)

        status, raw = request(
            panel, "POST", PANEL_NOTIFY_SAVE_PATH,
            body = json.dumps({"config": notify_values(**callback_kwargs(
                wecom_callback_base = "https://Bili23.892639.xyz:2662/"))}).encode("utf-8"),
            cookie = login(panel),
        )

        assert status == 200
        assert json.loads(raw)["config"]["wecom_callback_base"] == "https://bili23.892639.xyz:2662"

    def test_callback_page_controls_exist(self, panel):
        """通知页要有这四个控件与那条待复制的地址，否则用户无处可填"""
        status, raw = request(panel, "GET", "/", cookie = login(panel))

        assert status == 200

        page = raw.decode("utf-8")

        for control in ("ntCbOn", "ntCbToken", "ntCbAes", "ntCbBase", "ntCbUrl", "ntCbCopy", "ntCallbackHistory"):
            assert f'id="{control}"' in page

        # 路径由服务端下发，前端不该自己拼死一份
        assert "callback_path" in page


class TestTaskListOrdering:
    """
    list_tasks 的排序与分页 —— 「下载中」列表要正序、已完成列表要能翻页

    这两个都发生在 util/mcp/tools/task.py 与 util/download/task/ 里，面板只是
    消费方。放在本文件是因为面板的下载队列与已完成列表用的就是这两个入口，
    改了这里而没改面板的取数方式，界面上的表现会与断言不符。
    """

    def task(self, created, completed = 0, title = ""):
        from util.download.task.info import TaskInfo

        task_info = TaskInfo()
        task_info.Basic.show_title = title
        task_info.Basic.created_time = created
        task_info.Basic.completed_time = completed

        return task_info

    def order(self, tasks):
        from util.mcp.tools.task import _list_sort_key

        return [t.Basic.show_title for t in sorted(tasks, key = _list_sort_key)]

    def test_unfinished_tasks_are_listed_in_creation_order(self):
        """
        正序。一次批量下载里，排在前面的那几集正是正在下的、以及离完成最近的
        （下载按创建顺序推进）；倒序会把最该看的这批压到列表最底下 —— 用户
        2026-10-02 的截图里就是这么显示的，他要的是反过来
        """
        tasks = [self.task(300, title = "第3集"), self.task(100, title = "第1集"), self.task(200, title = "第2集")]

        assert self.order(tasks) == ["第1集", "第2集", "第3集"]

    def test_completed_tasks_stay_newest_first(self):
        """已完成反过来：刚下完的排在最上面（「最近完成」就该是最近在前）"""
        tasks = [self.task(1, completed = 100, title = "先完成"), self.task(2, completed = 300, title = "后完成")]

        assert self.order(tasks) == ["后完成", "先完成"]

    def test_unfinished_still_comes_before_completed(self):
        """未完成的整体排在已完成之前，这条不能被"正序"改掉"""
        tasks = [self.task(1, completed = 500, title = "已完成"), self.task(900, title = "在下的")]

        assert self.order(tasks) == ["在下的", "已完成"]

    def test_offset_is_pushed_down_to_sql(self):
        """
        offset 要进 SQL 的 OFFSET，不是"全查回来再切"

        后者在几百条时就是几百次 JSON 反序列化 —— 分页本来就是为了避开这个
        """
        from util.download.task.db import TaskDatabase

        seen = []

        db = TaskDatabase.__new__(TaskDatabase)
        db.query = lambda sql, params = (): seen.append((sql, params)) or []

        db.query_tasks(True, 20, 40)

        sql, params = seen[0]

        assert "ORDER BY" in sql and "OFFSET" in sql
        assert params == (20, 40)

    def test_offset_only_applies_when_there_is_a_limit(self):
        """不传 limit 时 offset 无意义，SQL 里不该冒出孤零零的 OFFSET"""
        from util.download.task.db import TaskDatabase

        seen = []

        db = TaskDatabase.__new__(TaskDatabase)
        db.query = lambda sql, params = (): seen.append((sql, params)) or []

        db.query_tasks(False)

        assert "OFFSET" not in seen[0][0]

    def test_offset_is_clamped_to_zero(self):
        """负数与非法值一律归 0：OFFSET 为负在 SQLite 里等于不偏移，会静默退化成第一页"""
        from util.mcp.tools.task import _clamp_offset

        assert _clamp_offset(-5) == 0
        assert _clamp_offset(None) == 0
        assert _clamp_offset("40") == 0
        assert _clamp_offset(True) == 0
        assert _clamp_offset(40) == 40
        assert _clamp_offset(0) == 0


class TestParsedGroupsAndBatchUI:
    """
    概览解析区的三块新东西：章节分组、批量解析、解析记录

    分组是为了让「西游记（29 条）」这种结果能分出「正片 / 相关推荐 / 重温原著…」——
    摊成一片的时候，哪几条属于哪一段完全看不出来，而它们该不该下差别很大。

    断言一律落在**接线**上（函数在、选择器对、调用点参数齐），不去真解析：
    真解析要连 B 站，那是线上验收脚本的事
    """

    def page(self, panel):
        status, raw = request(panel, "GET", "/")

        assert status == 200

        return raw.decode("utf-8")

    def test_the_two_new_entry_points_sit_next_to_the_parse_button(self, panel):
        page = self.page(panel)

        assert 'id="batchBtn"' in page
        assert 'id="histBtn"' in page

        # 都在那行输入框旁边的按钮组里，且排在「解析」之后
        add_row = page[page.index('<div class="add">'):page.index('<div class="metrics">')]

        assert 'id="parseBtn"' in add_row
        assert add_row.index('id="parseBtn"') < add_row.index('id="batchBtn"')
        assert add_row.index('id="batchBtn"') < add_row.index('id="histBtn"')

    def test_both_dialogs_start_hidden(self, panel):
        """两个浮层默认都得是 hidden，否则一进面板就糊住整个界面"""
        page = self.page(panel)

        assert 'id="batchGate" class="overlay hidden"' in page
        assert 'id="histGate" class="overlay hidden"' in page

    def test_the_group_tree_is_rendered_with_collapse_and_group_checkboxes(self, panel):
        page = self.page(panel)

        for token in (".ggrp", ".ghead", ".gtog", ".gbody", ".gchk", ".gcnt"):
            assert token in page

        assert "function groupRows(" in page
        assert "function renderGrouped(" in page
        assert "function syncGroups(" in page

        # 折叠是纯 CSS：.ggrp.collapsed 把 body 收掉，JS 只翻转这个类
        assert ".ggrp.collapsed > .gbody { display: none; }" in page
        assert 'grp.classList.toggle("collapsed")' in page

    def test_the_old_flat_renderer_is_gone(self, panel):
        """留着一个没人调用的 renderEpisodes，下次改样式会改错那一份"""
        assert "renderEpisodes" not in self.page(panel)

    def test_only_the_main_chapter_is_checked_by_default(self, panel):
        """
        正片之外都是切片（相关推荐 / 精彩看点 / 高能片段）。原来每条都默认勾着 ——
        一部 157 条的番里 145 条是切片，一路点「开始下载」就全拖下来了
        """
        page = self.page(panel)

        assert 'var MAIN_CHAPTER = "正片";' in page
        assert "function collectMainIds(" in page
        assert 'String(node.title || "").trim() === MAIN_CHAPTER' in page

        # 条目行的默认勾选由那张 id 表决定，不再写死 checked
        assert '(on ? " checked" : "")' in page
        assert "!mainIds || mainIds[id] === true" in page

        # 章节名要去空白再比：B站回来的标题带尾空格时不能当没匹配上
        assert ".trim() === MAIN_CHAPTER" in page

    def test_a_tree_without_the_main_chapter_falls_back_to_checking_everything(self, panel):
        """
        🔴 认不出「正片」时（投稿视频那种平树、或者章节换了名字）必须退回全勾。
        默认值宁可多勾，也不能因为认不出来就变成「一条都不下」—— 那是更难查的故障
        """
        page = self.page(panel)

        assert "var mainIds = null;" in page
        assert "if (Object.keys(main).length) mainIds = main;" in page

        # 平铺那条退路没有章节信息，只能全勾
        assert "episodes.map(function (e, i) { return epRow(e, i, true); })" in page

    def test_item_selection_only_counts_the_leaf_rows(self, panel):
        """
        分组头的 .gchk 没有 data-i。混进选择器里会把整组当成一个条目，
        episodes[NaN] 取到 undefined，点「开始下载」就静默少下几条
        """
        page = self.page(panel)

        assert "function leafBoxes(" in page
        assert "input[type=\"checkbox\"][data-i]" in page

        # 旧写法整个换成 leafBoxes 了，不许再有裸的 querySelectorAll("input")
        assert 'querySelectorAll("input")' not in page

    def test_checking_a_group_selects_the_whole_group(self, panel):
        page = self.page(panel)

        assert 'e.target.classList.contains("gchk")' in page
        assert "syncGroups();" in page

        # 勾选状态只有一份真源（条目行），父行的勾选框与计数都由它推出来
        assert "head.indeterminate =" in page

    def test_a_single_parse_asks_for_the_full_list(self, panel):
        """默认只给 100 条，而分组是按完整列表画的 —— 少掉的那截会整段消失"""
        page = self.page(panel)

        assert 'api("parse_url", { url: url, page: page || 1, limit: 500 })' in page

        # 截断时要说清总数，别让人以为这部番只有这么多集
        assert '"（共 " + Number(d.total) + " 项）"' in page

    def test_a_season_switch_still_reparses(self, panel):
        """分组不能把「同系列多季」那颗下拉挤掉，两者在同一个头行里"""
        page = self.page(panel)

        assert "function renderSeasonSel(" in page
        assert 'id="seasonSel"' in page
        assert "function showParsed(d, url)" in page

    def test_batch_parse_sends_urls_and_the_auto_add_flag(self, panel):
        page = self.page(panel)

        # 🔴 auto_add 是**有条件**提交的：读不到当前设置就不带这个字段。
        # 无条件提交 checked=false 会在每次批量解析时把用户开着的开关关掉
        assert "var body = { urls: lines, limit: 500 };" in page
        assert 'if (batchAutoKnown) body.auto_add = $("batchAuto").checked;' in page
        assert 'api("parse_batch", body)' in page

        # 条数实时报：逐条串行跑，贴错一条等到整批跑完才发现太亏
        assert "已填 " + '" + batchLines().length + " 条' in page

        # 弹窗里必须有那两样：文本框与自动入队开关
        assert 'id="batchText"' in page
        assert 'id="batchAuto"' in page

    def test_the_auto_add_box_is_prefilled_from_the_users_setting(self, panel):
        """
        🔴 复选框初值必须来自用户当前设置，不能从 DOM 的默认"未勾选"起步 —— 批量工具
        写的是全局配置，永远提交 false 等于每点一次批量解析就把开关关一次

        回填发生在**打开弹窗**那一段，不是提交时：提交时才去读，用户看到的勾选状态
        与真正提交的值就不是同一个东西了
        """
        page = self.page(panel)

        assert "var batchAutoKnown = false;" in page
        assert 'f.key === "auto_add_to_download_list"' in page
        # 读失败时后端给 null，不能拿它当"关着"
        assert 'typeof field.value === "boolean"' in page

        open_at = page.index('$("batchBtn").onclick')
        submit_at = page.index('if (batchAutoKnown) body.auto_add')
        batch_at = page.index('api("parse_batch", body)')

        assert open_at < submit_at < batch_at

    def test_the_batch_button_reports_errors_inside_the_dialog(self, panel):
        """错误得留在弹窗里（$("batchErr")），关掉弹窗再报等于没报"""
        page = self.page(panel)

        assert 'id="batchErr"' in page
        assert '$("batchErr").textContent = e.message;' in page

    def test_history_dialog_can_reparse_delete_and_clear(self, panel):
        page = self.page(panel)

        assert 'api("list_parse_history", {})' in page
        assert 'api("delete_parse_history", { history_id: record.history_id })' in page
        assert 'api("clear_parse_history", {})' in page

        # 点一条直接重新解析：历史记录的价值就在这里
        assert "runParse(record.url, 1).catch(parseFailed);" in page

    def test_clearing_the_history_is_confirmed_before_it_is_sent(self, panel):
        """
        🔴 清空不可逆，确认框必须排在调用**之前**。
        反过来的话，用户点「取消」时请求早就发出去了

        与桌面端不同（那边点了就清），但这里隔着浏览器，误触的代价一样
        """
        page = self.page(panel)

        confirm_at = page.index('window.confirm("清空全部解析记录')
        call_at = page.index('api("clear_parse_history"')

        assert confirm_at < call_at

    def test_the_history_dialog_says_only_the_newest_hundred_are_kept(self, panel):
        page = self.page(panel)

        assert "只保留最近 100 条" in page

    def test_history_rows_are_bound_from_the_record_not_from_the_dom(self, panel):
        """
        历史 id 是拼进 HTML 的，从 dataset 反查还得再比对一次；
        直接用列表里那份对象更稳
        """
        page = self.page(panel)

        assert "list.filter(function (r) { return r.history_id === row.dataset.id; })[0]" in page
        assert "if (!record) return;" in page
