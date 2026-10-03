"""
B 站账号的授权登录（扫码）。

为什么不用账号密码：B 站的密码登录接口要求先过极验验证码 —— 那需要在浏览器里
渲染 canvas、采集人机行为，容器里的程序给不出来。扫码登录只用到"取二维码"与
"轮询状态"两个接口，扫码这个动作在用户自己的手机 App 上完成，两边都不必模拟浏览器。

二维码直接以 SVG 文本交给页面：面板不引外部资源（NAS 与容器里的浏览器未必能出外网），
也不为了出一张图把 Pillow 这类编码库拖进来 —— 一段字符串足够浏览器画出来。

本模块刻意不碰 Qt：它跑在 HTTP 线程上，唯一的例外是最后把登录结果写回配置那一步，
那一步与 MCP 工具走同一条约束，回到 GUI 线程执行。
"""

from ..common._json import loads

import logging

logger = logging.getLogger(__name__)

QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 与桌面端登录对话框保持一致：source 决定这枚二维码出现在 App 的哪个入口下
GENERATE_PARAMS = {
    "source": "main-fe-header",
    "go_url": "https://www.bilibili.com/",
    "web_location": "333.1007",
}

# 单模块边长与静区宽度。53x53 的矩阵配 2 格静区，页面上缩到 240px 仍然好扫
QRCODE_BOX = 8
QRCODE_BORDER = 2

# 登录相关的 Cookie 字段，与 util/auth/cookie_login.py 保持同一组
LOGIN_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")

# B 站扫码状态。0 以外的几个值就是要让用户看到的三种提示
SCAN_STATUS_TEXT = {
    86101: "等待扫码",
    86090: "已扫码，请在手机上确认",
    86038: "二维码已过期",
}

def render_svg(data: str, box: int = QRCODE_BOX, border: int = QRCODE_BORDER) -> str:
    """
    把二维码画成 SVG

    逐行合并连续的黑格再写进一条 path：53x53 的矩阵逐个模块一个 <rect> 是近三千
    个节点，合并成游程后通常不到一千条子路径，浏览器解析起来快得多
    """
    import qrcode

    code = qrcode.QRCode(border = border, error_correction = qrcode.constants.ERROR_CORRECT_M)
    code.add_data(data)
    code.make(fit = True)

    matrix = code.get_matrix()
    side = len(matrix) * box

    commands = []

    for y, row in enumerate(matrix):
        x = 0

        while x < len(row):
            if not row[x]:
                x += 1

                continue

            start = x

            while x < len(row) and row[x]:
                x += 1

            width = (x - start) * box

            commands.append(f"M{start * box} {y * box}h{width}v{box}h-{width}z")

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}"'
        f' viewBox="0 0 {side} {side}" shape-rendering="crispEdges">'
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
        f'<path fill="#000000" d="{"".join(commands)}"/></svg>'
    )

def parse_login_url(url: str) -> dict:
    """
    从登录成功后的回调地址里取出 Cookie

    形如 https://passport.biligame.com/crossDomain?DedeUserID=...&SESSDATA=...&bili_jct=...
    它是给浏览器跨域跳转用的，这里只当作 Set-Cookie 之外的一份兜底
    """
    if not url:
        return {}

    from urllib.parse import parse_qsl, urlsplit

    cookies = {}

    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values = False):
        if key in LOGIN_COOKIE_KEYS and value:
            cookies[key] = value

    return cookies

def start() -> dict:
    """
    取一枚新的二维码，返回 {"qrcode_key": ..., "svg": ...}

    失败一律抛异常：调用方是 HTTP 接口，把状态码与文案交给它统一处理
    """
    from ..network.request import SyncNetWorkRequest

    response = SyncNetWorkRequest(QRCODE_GENERATE_URL, params = dict(GENERATE_PARAMS)).run()

    if response.get("code") != 0:
        raise RuntimeError(response.get("message") or "获取二维码失败")

    data = response.get("data") or {}

    key = data.get("qrcode_key") or ""
    url = data.get("url") or ""

    if not key or not url:
        raise RuntimeError("二维码响应缺少必要字段")

    return {"qrcode_key": key, "svg": render_svg(url)}

def poll(qrcode_key: str) -> dict:
    """
    查询扫码状态

    登录成功时 Cookie 由响应头 Set-Cookie 带回来，而请求走的是全局 httpx client，
    它会自己收进 cookiejar —— 这里额外要做的只有把那份会话写进配置。
    响应的 data.url 里也带了一份 Cookie，作为兜底先补上。
    """
    from ..network.request import SyncNetWorkRequest, ResponseType, set_client_cookies

    response = SyncNetWorkRequest(
        QRCODE_POLL_URL,
        params = {"qrcode_key": qrcode_key},
        response_type = ResponseType.RESPONSE,
        raise_for_status = False,
    ).run()

    payload = loads(response.text)

    # 顶层 code 是接口状态，data.code 才是扫码状态，两者不要混
    if payload.get("code") != 0:
        return {"code": -1, "message": payload.get("message") or "查询扫码状态失败"}

    data = payload.get("data") or {}
    code = data.get("code")

    if code != 0:
        return {"code": code, "message": SCAN_STATUS_TEXT.get(code, data.get("message") or "")}

    fallback = parse_login_url(data.get("url") or "")

    if fallback:
        set_client_cookies(fallback)

    commit_login()

    return {"code": 0, "message": "登录成功"}

def commit_login():
    """
    把登录结果写回配置与运行时状态

    回到 GUI 线程执行：config.set 会落盘、runtime.auth 是 GUI 线程的状态，
    与 MCP 工具是同一条约束
    """
    from ..mcp.invoke import call_in_main_thread

    def apply():
        from ..auth.base import AuthBase

        # 复用既有的登录收尾：四个 Cookie 与 is_login 一起写好，
        # 并且顺带清掉"会话已过期"的标记
        AuthBase().update_cookies()

        try:
            # 用户名与 UID 由 nav 接口补上。它是异步的，慢一点不影响登录本身，
            # 面板下一次轮询就会看到名字
            from ..auth.user import user_manager

            user_manager.get_user_info()

        except Exception:
            logger.exception("刷新用户信息失败")

        return True

    call_in_main_thread(apply, timeout = 15.0)

def logout():
    """
    退出 B 站登录

    以本地清理为准：服务端注销那一步是尽力而为 —— 网络不通时若还等它的回调
    才清配置，用户看到的就是"点了退出但状态没变"
    """
    from ..mcp.invoke import call_in_main_thread

    def run():
        from ..common.config import config
        from ..common.runtime import runtime
        from ..network.request import delete_client_cookies

        logged_in = bool(config.get(config.is_login))

        for key in ("bili_jct", "DedeUserID", "DedeUserID__ckMd5", "SESSDATA"):
            config.set(getattr(config, key), "")

        config.set(config.is_login, False)

        runtime.auth.is_expired = False
        runtime.auth.uname = ""
        runtime.auth.uid = ""
        runtime.auth.avatar_pixmap = None

        # 配置清空还不够：cookiejar 里那四个还是登录态，下一次解析会以
        # 已登录身份发出去，直到进程重启
        delete_client_cookies(LOGIN_COOKIE_KEYS)

        if logged_in:
            try:
                from ..auth.user import user_manager

                user_manager.logout()

            except Exception:
                # 服务端注销失败不影响本地已退出这个事实，不必让面板报错
                logger.exception("请求服务端注销失败")

        return True

    call_in_main_thread(run, timeout = 15.0)
