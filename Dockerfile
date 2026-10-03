# Bili23 Downloader 容器镜像（含音视频参数实读改造与内置 Web 面板）
#
# 说明几个不那么显然的设计取舍：
#
# 1. 基础镜像用 python:3.13-slim-bookworm 而非 alpine。
#    PySide6 官方只提供 manylinux 轮子，链接的是 glibc；alpine 上装它要么
#    编译 Qt（不现实），要么走 gcompat 垫片（Qt 的线程与 locale 处理会在
#    垫片上出各种难查的问题）。bookworm 的 glibc 2.36 满足 PySide6 6.10 的要求。
#
# 2. 程序本体是 PySide6 图形程序，容器里没有显示服务，因此跑在 offscreen
#    平台插件上 —— 窗口照常创建、事件循环照常转，只是没有任何输出到屏幕。
#    这不是"阉割版"：下载链路、解析链路、MCP 服务与 Web 面板都不碰显示。
#
# 3. MCP 与 Web 面板默认只绑 127.0.0.1，而容器内的环回地址从宿主机连不上，
#    端口映射会形同虚设，因此镜像里把两者的监听地址设为 0.0.0.0。
#    访问仍需携带各自的令牌，暴露范围由 compose 里的端口映射决定。
#
# 4. Web 面板在本机安装时默认关闭，容器形态下由 compose 的
#    BILI23_WEB_ENABLED 打开 —— 浏览器是这里最顺手的控制入口。

FROM python:3.13-slim-bookworm

# 软件源。默认走国内镜像 —— 群晖上直连 deb.debian.org 只有 10 KB/s，
# 而 ffmpeg 那一坨依赖是 189 MB（204 个包），照这个速度要跑五个小时，
# 构建看上去就是"卡住不动"。换阿里云后 390 KB/s，pip 侧 1.1 MB/s。
# 要换别家写域名即可（mirrors.tuna.tsinghua.edu.cn）
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

# 构建期代理（可选，留空即不用）。
#
# 有可用代理时，**走代理直连官方源才是最快的**：实测经代理拉 Debian 官方源
# 18~21 MB/s、拉 pypi 官方源 42 MB/s，比上面那两个镜像站高一个数量级 ——
# 之前卡了十几分钟的 56.5 MB 字体包，走代理 2.7 秒就下来了。
# 这种情形下把上面两个源传空即可（`_nas_deploy.py build` 默认就是这么做的）。
#
# Docker 自带一组预定义代理 ARG，不声明也会自动注入 RUN 的环境变量；
# 这里显式写出来是为了让它可见，也不依赖 builder 的实现差异。
# 注意这几个值只活在构建过程中，不会进入最终镜像的环境变量
ARG http_proxy=
ARG https_proxy=
ARG no_proxy=localhost,127.0.0.1,::1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    \
    # 配置目录。程序用 QStandardPaths.AppDataLocation 定位配置，
    # 该值受 XDG_DATA_HOME 影响，固定到这里后即可整目录挂载出去
    XDG_DATA_HOME=/config \
    \
    # 去掉配置根下的 "Bili23 Downloader" 子目录层：config.json、logs、
    # task.db 等直接落在 /config，与 compose 的挂载点一一对齐。
    # 桌面版不设此变量，维持 %APPDATA%\Bili23 Downloader 的既有布局
    BILI23_APP_DIRNAME= \
    \
    # 无显示服务，走 offscreen 平台插件。main.py 只在环境变量未设置时
    # 才自作主张选 xcb，因此这里显式设置会被尊重
    QT_QPA_PLATFORM=offscreen \
    \
    # 两个服务都监听所有网卡，否则容器外无法访问
    BILI23_MCP_HOST=0.0.0.0 \
    BILI23_MCP_PORT=23330 \
    BILI23_WEB_HOST=0.0.0.0 \
    BILI23_WEB_PORT=23331 \
    BILI23_DOWNLOAD_PATH=/downloads \
    \
    # 程序靠它定位自带的 bundle/ 目录（ffmpeg 的查找路径之一）
    PYSTAND_HOME=/app

# ---------------------------------------------------------------------------
# 系统依赖
#
# 分三块：
#   ffmpeg  —— 合并音视频、重封装、以及本次改造新增的音轨实读（探测成品文件）
#   Qt 运行库 —— PySide6 的轮子里有 Qt 本体，但它链接的这些系统库不含在内；
#                缺 libGL 时 Qt 平台插件初始化会直接失败。这里装的都是
#                offscreen 插件链路真正用到的，没有为 xcb 平台额外铺依赖
#   字体     —— 界面文本与弹幕/字幕渲染都依赖系统中文字体，缺了会整片方块
#   tini     —— 作为 PID 1 回收僵尸进程并转发信号，见 ENTRYPOINT
# ---------------------------------------------------------------------------
# 换 Debian 软件源。镜像站上 debian 与 debian-security 两个路径都存在，
# 所以换掉 host 一条 sed 就够。两种源文件格式都覆盖：slim 系列历来用
# /etc/apt/sources.list，较新的 Debian 已改用 deb822 的 debian.sources。
# 留空则保持官方源不动
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f"; fi; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      tini \
      libgl1 \
      libegl1 \
      libglib2.0-0 \
      libfontconfig1 \
      libfreetype6 \
      libdbus-1-3 \
      libxkbcommon0 \
      libx11-6 \
      libxext6 \
      libxrender1 \
      libxi6 \
      libsm6 \
      libice6 \
      libgomp1 \
      fonts-noto-cjk; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：源码改动不会让这层缓存失效，重建时省掉几分钟
COPY requirements.txt ./

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 上游的 requirements.txt 里没有它，但打包版（PyStand 那套运行时）里带着它 ——
# 程序启动时会调它去查更新。缺了不影响任何下载功能，只是在日志里留下两行
# 「检查更新失败：No module named 'verhub_sdk'」，容易被当成部署出了问题。
# 单独一层：requirements 那层不动，改这里不会触发整份依赖重装
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" "verhub-sdk==0.2.10"

COPY . ./

# 程序的 FFmpeg 查找顺序是「配置指定的来源 → 另一来源 → 失败」，
# 内置来源读的是 $PYSTAND_HOME/bundle/ffmpeg。指向系统那份之后，
# 两种来源都能落到同一个二进制上，无论配置里选的是哪个都不会走到
# "没有可用的 FFmpeg" 那条分支。
# mkdir 不能省：Windows 版 ffmpeg.exe 已被 .dockerignore 挡在上下文之外，
# bundle 目录有可能是空的、甚至不存在
RUN mkdir -p /app/bundle \
 && ln -sf /usr/bin/ffmpeg /app/bundle/ffmpeg \
 && chmod +x /app/docker/entrypoint.sh

# MCP 端口与 Web 面板端口。实际暴露给宿主机与否由 compose 的 ports 决定
EXPOSE 23330 23331

# 就绪判据是两个端口有一个可连：用户可以只开 MCP，也可以只开 Web 面板。
# 容器内监听 0.0.0.0，用环回地址探测同样有效
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import socket, sys; alive = any(socket.socket().connect_ex(('127.0.0.1', p)) == 0 for p in (23331, 23330)); sys.exit(0 if alive else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["/app/docker/entrypoint.sh"]
