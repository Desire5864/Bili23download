"""
内置的 Web 面板

在本地环回地址上提供一个网页，用于查看下载队列与进度、解析链接、建下载任务。

与 MCP 一样刻意不在导入期做任何事：面板默认关闭，相关实现（http.server、
解析链路、下载链路）只在真正启用时才被拉进来。
"""

import logging

logger = logging.getLogger(__name__)

def start_web_panel() -> bool:
    """
    按当前配置启动 Web 面板，未启用时静默返回
    """
    from ..common.config import config

    if not config.get(config.web_panel_enabled):
        return False

    from .server import web_panel_manager

    return web_panel_manager.start()

def stop_web_panel(timeout: float = 2.0):
    """
    停止 Web 面板

    退出流程中调用。服务器从未启动过时不应把模块导入进来，
    因此先看是否已经加载
    """
    import sys

    module = sys.modules.get(f"{__package__}.server")

    if module is None:
        return

    try:
        module.web_panel_manager.stop(timeout)

    except Exception:
        logger.exception("停止 Web 面板失败")

def restart_web_panel() -> bool:
    """
    应用配置变更（端口、开关、令牌）后重启面板
    """
    from ..common.config import config
    from .server import web_panel_manager

    web_panel_manager.stop()

    if not config.get(config.web_panel_enabled):
        return False

    return web_panel_manager.start()
