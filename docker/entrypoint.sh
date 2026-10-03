#!/bin/sh
# Bili23 Downloader 容器入口。
#
# 顺序上做了三件事，缺一不可：
#   1. 自检 Qt 能否在 offscreen 平台上初始化 —— 缺库时在这里给出可读的原因，
#      而不是让主程序抛一长串 Qt 栈
#   2. 引导配置文件 —— MCP 默认关闭，容器里又没有界面能打开它
#   3. 以 PID 1 的子进程身份启动主程序，由 tini 负责信号转发与僵尸回收
#
# 主程序是图形程序，容器里没有显示服务，靠 offscreen 平台插件运行：
# 窗口照常创建、事件循环照常转，只是不输出到屏幕。下载与 MCP 链路不依赖显示。

set -e

DOWNLOAD_PATH="${BILI23_DOWNLOAD_PATH:-/downloads}"
CONFIG_HOME="${XDG_DATA_HOME:-/config}"

echo "=============================================================="
echo " Bili23 Downloader 容器"
echo "--------------------------------------------------------------"
echo " 配置目录 : ${CONFIG_HOME}"
echo " 下载目录 : ${DOWNLOAD_PATH}"
echo " Qt 平台  : ${QT_QPA_PLATFORM:-未设置}"
echo " MCP 绑定 : ${BILI23_MCP_HOST:-127.0.0.1}:${BILI23_MCP_PORT:-23330}"
echo " Web 面板 : ${BILI23_WEB_HOST:-127.0.0.1}:${BILI23_WEB_PORT:-23331}（账号 ${BILI23_WEB_USERNAME:-admin}，初始密码 password）"
echo "=============================================================="

# 挂载点由用户提供，容器这边只保证目录存在
mkdir -p "${DOWNLOAD_PATH}" "${CONFIG_HOME}"

# 挂载卷的属主可能是宿主机上的别的用户，这里提前探一次可写性。
# 不中断启动：只读挂载下程序仍能跑（下载会失败，但 MCP 上的查询类调用依然可用），
# 与其直接退出，不如把问题说清楚
if ! touch "${DOWNLOAD_PATH}/.bili23_write_test" 2>/dev/null; then
    echo "[bili23] 警告：下载目录不可写（${DOWNLOAD_PATH}）—— 下载会失败，请检查挂载与属主" >&2

else
    rm -f "${DOWNLOAD_PATH}/.bili23_write_test"
fi

# ---------------------------------------------------------------------------
# Qt 平台插件自检
#
# offscreen 插件仍需 libGL / libfontconfig / libxkbcommon 等系统库。
# 缺库时 QApplication 构造会失败，主程序给出的是一堆 Qt 内部报错，
# 在这里探一次能直接指出是镜像依赖不全还是别的问题
# ---------------------------------------------------------------------------
if [ "${BILI23_SKIP_SELFCHECK:-0}" != "1" ]; then
    if ! python - <<'PY'
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

app = QApplication([])

print(f"[bili23] Qt 平台插件自检通过：{app.platformName()}")
PY
    then
        echo "[bili23] 错误：Qt 平台插件初始化失败，无法启动" >&2

        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 配置引导
# ---------------------------------------------------------------------------
python /app/docker/seed_config.py

echo "[bili23] 启动主程序……"

cd /app

# exec 交出 PID 1，让 tini 能收到并转发停止信号；
# 不 exec 的话信号只会打到 shell，主程序收不到，停止时要等超时被 SIGKILL
exec python /app/src/main.py
