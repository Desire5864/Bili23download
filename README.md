<div align="center">

# Bili23 Downloader · 容器版

上游 [Bili23-Downloader](https://github.com/ScottSloan/Bili23-Downloader) v2.20.0 的衍生版：
**音视频参数实读** + **内置 Web 面板** + **一键部署的容器镜像**

[![Publish container image](https://github.com/Desire5864/Bili23download/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Desire5864/Bili23download/actions/workflows/docker-publish.yml)
[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)

</div>

---

## 这是一个衍生版

本仓库不是 [ScottSloan/Bili23-Downloader](https://github.com/ScottSloan/Bili23-Downloader) 的官方仓库。
它在 v2.20.0 的基础上做了几处改动，并把整条「改代码 → 构建镜像 → 部署到 NAS」
的链路固化下来，主要服务于「在一台常开的 NAS 上挂着下 B 站番剧、下载结果要能直接
被 Plex / Jellyfin 刮削」这个用法。

上游的安装版（Windows / macOS / Linux 桌面程序）依然在
[官方网站](https://bili23.scott-sloan.cn/) 发布；桌面界面在本仓库里基本原样保留。

## 快速开始（容器）

```bash
# docker-compose.example.yml 是脱敏版模板，复制一份再改
curl -O https://raw.githubusercontent.com/Desire5864/Bili23download/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
# 改掉里面的两个令牌与两处挂载路径，然后：
docker compose up -d
```

镜像已经发布在 GitHub 容器仓库，**公开、免登录**：

```bash
docker pull ghcr.io/desire5864/bili23download:latest
```

起来之后：

| 入口 | 地址 | 凭据 |
|---|---|---|
| Web 面板 | `http://<宿主地址>:23331/` | `admin` / 初始密码 `password`，登录后改 |
| MCP 服务 | `http://<宿主地址>:23330/mcp` | 请求头 `Authorization: Bearer <MCP 令牌>` |

完整的部署说明、配置项逐条解释、以及踩过的坑都在
**[`docker/README.docker.md`](docker/README.docker.md)**。

## Web 面板

官方版只有一个 Qt 界面；本版多一个浏览器可开的网页面板 —— NAS 上没有显示器，
浏览器才是顺手的入口。左侧竖导航共 12 页：

```
概览 · 下载中 · 已完成 · 账号收藏 · 日志 · 命名规则
名称识别 · 云端同步 · MCP 服务器 · 设置 · B站账号 · 通知
```

其中几项是上游没有的：

- **解析结果按「类别 → 章节 → 条目」分组**，可折叠，组头带整组勾选与 `已选 / 总数`
  计数。默认**只勾「正片」**，预告、花絮、精彩看点这些切片默认不勾 ——
  一部 157 条的番剧里 145 条是切片，默认全勾着会一路把它们全拖下来。
- **批量解析**：一次粘多条 av / BV 链接，逐条解析后并进同一个列表。
- **解析记录**：解析过的链接存一份，能翻、能重新解析、能删。
- **账号收藏**：把 B 站账号的收藏夹、追番、关注列表直接列出来点着下。
- **通知**：下载完成 / 失败推送到企业微信自建应用或 Telegram，并支持企业微信回调。
- **云端同步**：直连 CloudDrive2 的 gRPC 触发备份（容器里不需要挂 `docker.sock`）。

## 这个分支改了什么

对照上游 v2.20.0 的完整清单：

### 1. 音视频参数改成实读

命名规则里的动态范围、视频编码、分辨率、音频编码、声道，官方版取的是 B 站下发的
档位名（或写死字面量），本版由下载完成后、改名交付之前对成品实读得到
（新增 `src/util/ffmpeg/probe.py`）：

| 变量 | 实读取值 |
|---|---|
| `{video_dynamic_range}` | `SDR` / `HDR10` / `HLG` / `DV` |
| `{video_resolution}` | `1920x1080` |
| `{video_codec}` | `AVC` / `HEVC` / `AV1` |
| `{audio_codec}` | `AAC` / `FLAC` / `EAC3` |
| `{audio_channels}` | `2.0` / `5.1` |

读不到时保留原文件名，绝不因为探测失败而改变落盘路径。

> `{video_quality}`（`1080P` / `智能修复`）与 `{audio_quality}`（`192K`）**仍然是
> B 站下发的档位名**，没有实读 —— 它们是「选了哪一档」的记录，不是媒体本身的属性。

### 2. 新增模块

| 路径 | 作用 |
|---|---|
| `src/util/web/` | Web 面板：HTTP 服务、鉴权、扫码登录、收藏夹、命名与识别规则、页面本体 |
| `src/util/clouddrive/` | 直连 CloudDrive2 的 gRPC（走 gRPC-Web），触发云端备份 |
| `src/util/common/notify.py` | 通知：企业微信自建应用 / Telegram |
| `src/util/common/wecom_callback.py` | 企业微信回调接收与 AES 解密（纯标准库实现） |
| `src/util/common/naming_alias.py` | 名称识别表：把 B 站剧名映射到 TMDB 剧名 / 年份 / 编号 |
| `src/util/common/net.py` | 出网工具：代理选择、超时、重定向处理 |
| `src/util/mcp/tools/history.py` | MCP 侧的解析记录读写 |
| `docker/` | 容器入口脚本与构建期配置引导（`seed_config.py`） |

### 3. 任务控制与队列

暂停 / 继续 / 取消（含批量）、下载队列正序（未完成按创建时间升序，离完成最近的排最前）、
已完成列表分页、容器重建后僵尸态任务归一化。

### 4. 容器化

`Dockerfile` + `docker-compose.example.yml` + `docker/entrypoint.sh`。程序是 PySide6
图形程序，容器里靠 Qt 的 `offscreen` 平台插件运行 —— 窗口照常创建、事件循环照常转，
只是不输出到屏幕；下载、解析、MCP 与 Web 面板链路都不依赖显示。

### 5. 测试

测试从上游的 36 个文件 / 412 个用例扩到 **48 个文件 / 1075 个用例**（运行时
1364 条，含参数化）。

## 从源码运行 / 开发

```bash
pip install -r requirements.txt
python src/main.py
```

需要 Python 3.11+ 与 PySide6。

跑测试：

```bash
pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest
```

> ⚠️ 测试集里有大量 GUI 用例：它们在 offscreen 平台上创建真实窗口并断言控件尺寸，
> 而尺寸取决于系统字体度量。换一个 locale 或换一套字体就可能有用例转红 ——
> 权威的跑法是在容器里跑（环境与生产一致）。仓库的 CI 因此**只跑 ruff**，
> 不搬上游那份跨平台测试矩阵，理由写在 `.github/workflows/quality.yml` 末尾。

## 镜像发布

镜像由 GitHub Actions 构建并发布到 GHCR，**打一个 `v*` 标签就自动出一版**：

```bash
git tag v1.0.2 && git push origin v1.0.2
```

产出三个标签：

```
ghcr.io/desire5864/bili23download:v1.0.2          # 精确版本，内容不可变，回滚用这个
ghcr.io/desire5864/bili23download:latest          # 跟随最新一个 v* 标签
ghcr.io/desire5864/bili23download:sha-<短哈希>     # 每次构建都有，能定位到具体提交
```

**升级部署**：

```bash
docker compose pull && docker compose up -d
```

> 🔴 镜像名必须**全小写** —— 容器仓库的命名规范不允许大写字母，而 GitHub 账号是
> `Desire5864`。直接拿账号名拼会得到 `invalid reference format`。

也可以在本地构建（改一行源码就发一版太重）：见 `docker/README.docker.md` 第十二节。

## 许可与致谢

本项目与上游一致，以 **GPL-3.0** 授权，见 [LICENSE](LICENSE)。

一切归功于上游作者 [ScottSloan](https://github.com/ScottSloan) 与
[Bili23-Downloader](https://github.com/ScottSloan/Bili23-Downloader) 的贡献者。
本仓库只做增量改动，遇到与本版改动无关的问题请优先查阅
[上游文档](https://bili23.scott-sloan.cn/doc/intro.html)。
