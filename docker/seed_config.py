#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
容器启动时的配置文件引导。

解决的问题是一个鸡生蛋：MCP 服务默认关闭，而容器里没有任何界面可以把它打开 ——
不开 MCP 就无法远程驱动这个程序，于是容器起来也没用。因此必须在主程序启动前
先把配置写好。

配置路径不写死，而是用 QStandardPaths 现算。原因是程序读的是
util/common/config.py 里的

    appdata_path = QStandardPaths.writableLocation(AppDataLocation)
    config_path  = Path(appdata_path) / app_dirname / "config.json"

而 AppDataLocation 会拼接 Qt 的 applicationName。容器里主程序同样是
`python src/main.py` 这种跑法，本脚本与它由同一个解释器启动、argv 形态也一致，
因此这里算出来的路径与主程序将要读的路径必然相同 —— 比按 XDG 规范手工推断可靠。

可用的环境变量：

    BILI23_DOWNLOAD_PATH   下载目录，写进 Download.download_path
    BILI23_MCP_PORT        MCP 端口，写进 MCP.mcp_port
    BILI23_MCP_TOKEN       MCP 访问令牌；留空则沿用已有值，没有则随机生成
    BILI23_MCP_ENABLED     是否打开 MCP，默认 1
    BILI23_WEB_PORT        Web 面板端口，写进 Web Panel.web_panel_port
    BILI23_WEB_USERNAME    Web 面板登录用户名，默认 admin
    BILI23_WEB_TOKEN       Web 面板访问令牌；留空则沿用已有值，没有则随机生成
    BILI23_WEB_ENABLED     是否打开 Web 面板，默认 0（容器部署时由 compose 置 1）
    BILI23_SEED_FORCE      置 1 时即使配置文件已存在也重新写入部署相关字段

Web 面板的**密码**不在这里写：它由主程序首次启动时初始化成默认密码的哈希并在
面板里修改。这里再实现一份哈希等于把格式约定抄成两处，迟早对不上。
"""

import json
import os
import secrets
import sys
from pathlib import Path

# 主程序用的配置版本。低于它的配置会被 patch_config 判定为旧版，
# 进而把命名规则整张表重置回内置默认值 —— 那会把用户自建的规则清掉
CONFIG_VERSION = 2200

DEFAULT_DOWNLOAD_PATH = "/downloads"
DEFAULT_MCP_PORT = 23330
DEFAULT_WEB_PORT = 23331
DEFAULT_WEB_USERNAME = "admin"


def read_port(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()

    if not raw:
        return fallback

    try:
        return int(raw)

    except ValueError:
        print(f"[bili23] {name} 不是整数，回退到 {fallback}", flush=True)

        return fallback


def read_token(name: str, section: str, key: str, data: dict) -> str:
    """
    取令牌：环境变量 > 配置里的旧值 > 新生成

    沿用旧值是为了让"改端口"这类操作不至于把已经配好的客户端全部失效
    """
    token = os.environ.get(name, "").strip()

    if token:
        return token

    return data.get(section, {}).get(key, "") or secrets.token_urlsafe(32)


def resolve_config_path() -> Path:
    """按主程序的方式算出配置文件路径"""
    from PySide6.QtCore import QStandardPaths

    appdata_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)

    # 与 util/common/config.py 同一套推导：容器里 BILI23_APP_DIRNAME 为空，
    # 配置直接落 /config/config.json
    app_dirname = os.environ.get("BILI23_APP_DIRNAME", "Bili23 Downloader")

    return Path(appdata_path) / app_dirname / "config.json"


def truthy(value: str, default: bool = True) -> bool:
    if value is None or value.strip() == "":
        return default

    return value.strip().lower() not in ("0", "false", "no", "off")


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except (OSError, json.JSONDecodeError) as exc:
        # 配置损坏时不能直接放弃 —— 那会让程序自己再生成一份默认配置，
        # 而默认配置里 MCP 是关的，等于把容器废掉。改名留档后重写
        backup = path.with_suffix(f".json.corrupt-{os.getpid()}")

        try:
            path.replace(backup)
            print(f"[bili23] 配置无法解析（{exc}），已改名留档：{backup}", flush=True)

        except OSError:
            print(f"[bili23] 配置无法解析（{exc}），且无法改名留档，将直接覆盖", flush=True)

        return {}


def build_config(existing: dict) -> dict:
    """在已有配置的基础上写入部署相关字段，其余键原样保留"""
    data = {group: dict(section) for group, section in existing.items() if isinstance(section, dict)}

    download_path = os.environ.get("BILI23_DOWNLOAD_PATH", "").strip() or DEFAULT_DOWNLOAD_PATH

    mcp_port = read_port("BILI23_MCP_PORT", DEFAULT_MCP_PORT)
    mcp_token = read_token("BILI23_MCP_TOKEN", "MCP", "mcp_token", data)

    web_port = read_port("BILI23_WEB_PORT", DEFAULT_WEB_PORT)
    web_token = read_token("BILI23_WEB_TOKEN", "Web Panel", "web_panel_token", data)
    web_username = os.environ.get("BILI23_WEB_USERNAME", "").strip() or DEFAULT_WEB_USERNAME

    mcp_enabled = truthy(os.environ.get("BILI23_MCP_ENABLED"), default=True)

    # Web 面板默认不开：它面向人，本机安装时用户在设置界面里开更合适。
    # 容器部署时由 compose 显式置 1 —— 网页正是容器形态下最顺手的控制入口
    web_enabled = truthy(os.environ.get("BILI23_WEB_ENABLED"), default=False)

    # Application.accepted_terms 是首次启动时那个服务条款弹窗的开关。
    # 容器里没人能点它，而弹窗不关会一直挡在启动路径上
    application = data.setdefault("Application", {})
    application["accepted_terms"] = True
    application["config_version"] = CONFIG_VERSION

    download = data.setdefault("Download", {})
    download["download_path"] = download_path

    # 没有界面就没有"手动选择 FFmpeg 路径"的机会，且镜像里 apt 装的那份
    # 一定存在于 PATH 上，直接锁定为 system 来源，省掉一次注定失败的查找
    advanced = data.setdefault("Advanced", {})
    advanced["ffmpeg_source"] = "system"

    mcp = data.setdefault("MCP", {})
    mcp["mcp_enabled"] = mcp_enabled
    mcp["mcp_port"] = mcp_port
    mcp["mcp_token"] = mcp_token

    web = data.setdefault("Web Panel", {})
    web["web_panel_enabled"] = web_enabled
    web["web_panel_port"] = web_port
    web["web_panel_username"] = web_username
    # web_panel_password 刻意不写：留空时主程序会用默认密码初始化一份哈希，
    # 用户设置过的哈希也就不会被这里覆盖掉
    web["web_panel_token"] = web_token

    return data


def main() -> int:
    config_path = resolve_config_path()

    print(f"[bili23] 配置文件路径：{config_path}", flush=True)

    existed = config_path.exists()

    if existed and not truthy(os.environ.get("BILI23_SEED_FORCE"), default=False):
        print("[bili23] 配置文件已存在，仅同步部署相关字段（下载目录、MCP/Web 的开关、端口、令牌）", flush=True)

    existing = load_existing(config_path)
    data = build_config(existing)

    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 先写临时文件再原子改名：容器可能在写入途中被 kill，
    # 半截文件会让程序读到一个损坏的配置
    tmp_path = config_path.with_suffix(".json.tmp")

    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4, sort_keys=True)

    tmp_path.replace(config_path)

    print(f"[bili23] 下载目录：{data['Download']['download_path']}", flush=True)

    mcp = data["MCP"]

    print(f"[bili23] MCP：{'已启用' if mcp['mcp_enabled'] else '已关闭'}，端口 {mcp['mcp_port']}", flush=True)

    if mcp["mcp_enabled"]:
        print(f"[bili23] MCP 访问令牌：{mcp['mcp_token']}", flush=True)

    web = data["Web Panel"]

    print(f"[bili23] Web 面板：{'已启用' if web['web_panel_enabled'] else '已关闭'}，端口 {web['web_panel_port']}", flush=True)

    if web["web_panel_enabled"]:
        print(f"[bili23] Web 面板登录账号：{web['web_panel_username']}，初始密码 password（登录后请尽快修改）", flush=True)
        print(f"[bili23] Web 面板访问令牌：{web['web_panel_token']}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
