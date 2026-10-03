# Bili23 Downloader 容器版

把 Bili23 Downloader 跑在 Docker 里，无显示器、无窗口，由 MCP 客户端或内置的
Web 面板远程驱动。适合放在 NAS 上长期挂着下 B 站内容。

---

## 一、目录结构

这是一个自包含的 compose 项目，在部署目录里长这样：

```
bili23/
├── docker-compose.yml      编排（唯一入口，可调项全部内联在本文件里）
├── docker/README.docker.md 本文件
├── data/                   持久化：config.json、日志、任务库（挂到容器 /config）
└── app/                    构建上下文：源码与 Dockerfile
```

下载的文件不在部署目录里 —— compose 挂载的是 `/volume1/Storage/哔哩哔哩`。

`sources` 放在 `app/` 之下、编排与配置放在顶层，是为了让 `data/` 与下载目录
不会被 `docker-compose down -v` 之类的操作牵连，升级时也只需换掉 `app/`。

---

## 二、快速开始

```bash
cd bili23
docker-compose up -d
```

访问令牌已直接写在 compose 的 environment 里，无需从日志里取。

> **两个容易踩的点。**
>
> 一是群晖上 compose 是独立的 `docker-compose` 二进制，**没有 `docker compose`
> 这个子命令**，写成那样会报 `'compose' is not a docker command`。
>
> 二是 `up -d` **不要加 `--build`**。镜像已经建好了，`--build` 等于让 Docker 把
> Dockerfile 从头跑一遍 —— "本地有镜像" 与 "构建被跳过" 是两码事：只要 Dockerfile
> 里任何一条指令的输入变了（哪怕只是加了个 `ENV`），它之后的层就全部作废重来，
> 一次几分钟到几小时。日常启动、改端口、改路径都不需要它。

启动后：

| 用途 | 地址 |
|---|---|
| **Web 面板**（浏览器） | `http://<NAS地址>:23331/`，账号 `admin` / 密码 `password` |
| **MCP 端点**（AI 客户端） | `http://<NAS地址>:23330/mcp`，请求头带 `Authorization: Bearer <MCP 令牌>` |

两个端口都写在 compose 的 `ports` 里，容器内固定 23330 / 23331。面板的登录
用户名可在 environment 里用 `BILI23_WEB_USERNAME` 改，密码则在面板里改
（见第三节）。

---

## 三、Web 面板

浏览器打开 `http://<NAS地址>:23331/` 就能用，**地址里不需要带令牌**。

面板里有**两道门**，各管各的，别混：

| 门 | 管什么 | 凭据 |
|---|---|---|
| 面板登录 | 谁能打开这个网页 | 账号密码，默认 `admin` / `password` |
| B站授权登录 | 用哪个 B 站账号下载 | 手机哔哩哔哩客户端扫码 |

### 面板登录

默认账号 `admin`、密码 `password`，**第一次登录后请立刻改掉**（右上角「改密码」，
至少 6 位）。密码以 PBKDF2-HMAC-SHA256 加盐哈希落盘，明文不写进文件；改完之后
其他设备上开着的面板会一并失效，当前这个自动续上。

会话存在内存里，容器重启就需要重新登录 —— 会话令牌落盘等于把"永久免密登录"
写进一份要备份、也可能被别人读到的配置文件。

### B站授权登录（扫码）

点右上角「B站账号」→「获取二维码」，用哔哩哔哩手机客户端扫一下并在手机上确认。

**为什么不是账号密码登录**：B 站的密码登录接口要求先过极验验证码 —— 那需要在
浏览器里渲染 canvas、采集人机行为，容器里的程序给不出来。扫码只用「取二维码」
与「轮询状态」两个接口，扫码这个动作在你自己的手机上完成，两边都不必模拟浏览器。

二维码由服务端直接生成 SVG 交给页面，不引外部图片也不依赖额外编码库。登录成功
后 Cookie 写进容器配置，重启容器不用重扫。

### 面板能做的事

- 看下载队列、进度、速度、剩余大小（每 3 秒自动刷新）
- 看已完成任务
- 粘贴链接 → 解析 → 勾选条目 → 建下载任务
- 扫码授权 / 退出 B站登录
- 改面板密码

> 面板与 MCP 是两个独立服务，各有开关。只想要网页就把 `BILI23_MCP_ENABLED` 设成 0，
> 只想要 AI 客户端就把 `BILI23_WEB_ENABLED` 设成 0。

> 脚本与自动化不方便先登录换 Cookie，可以继续用 API 令牌（`X-Panel-Token` 头或
> `?token=` 查询参数），它和会话是"任一有效即可"，见第四节。

---

## 四、配置项（docker-compose.yml）

可调项全部内联在 compose 里，没有 `.env`。常用的几处：

| 位置 | 说明 |
|---|---|
| `ports` 两行 | MCP / Web 面板映射到宿主机的端口。缺省绑定所有网卡（局域网可达）；只允许本机与 SSH 隧道访问时改成 `127.0.0.1:23330:23330` |
| `BILI23_WEB_USERNAME` | 面板登录用户名。密码在面板里改，不在这里设 |
| `BILI23_MCP_TOKEN` / `BILI23_WEB_TOKEN` | 免登录调接口用的令牌，启动时同步进容器配置 |
| volumes 的 `/volume1/Storage/哔哩哔哩` | 下载文件在宿主机上的落点，换成媒体库路径即可 |
| `BILI23_MCP_ENABLED` / `BILI23_WEB_ENABLED` | 两个服务各自的开关 |
| `BILI23_APP_DIRNAME` | 配置目录层开关：**保持空串**。容器内配置直接落 `/config`，没有 `Bili23 Downloader` 子目录 |

> ⚠️ 改了 compose 里的端口、路径或令牌，要 `docker-compose up -d` 重建容器才生效。

---

## 五、接入 MCP 客户端

不要用 `/mcp` 之外的路径，也不要在浏览器里直接打开它 —— 那是 JSON-RPC 端点。
浏览器里要看东西请走 Web 面板（23331）。

MCP 客户端（Claude Desktop、Cherry Studio 等）填：

```json
{
  "mcpServers": {
    "bili23": {
      "type": "http",
      "url": "http://192.168.3.36:23330/mcp",
      "headers": { "Authorization": "Bearer <MCP 令牌>" }
    }
  }
}
```

**从本机经 SSH 隧道访问**（不想把端口暴露给局域网时）：

```bash
ssh -L 23330:127.0.0.1:23330 -L 23331:127.0.0.1:23331 <user>@<NAS 地址>
```

配合 compose 里的 `ports` 绑定地址改成 `127.0.0.1`，这样两个端口都只有本机能碰。

可用的工具：`parse_url`、`get_episodes`、`create_download`、`list_tasks`、
`get_task_status`、`get_login_status`。

---

## 六、登录 B 站账号（重要）

**不登录只能拿到 480P / 360P。**

推荐直接在 Web 面板里扫码（见第三节），全程不用碰文件：

1. 打开 `http://<NAS地址>:23331/`，用 `admin` / `password` 登录面板
2. 右上角「B站账号」→「获取二维码」
3. 手机哔哩哔哩客户端扫码，并在手机上确认

登录成功后 Cookie 会写进容器配置，`docker-compose restart` 不会丢。

**备选：搬宿主机上已登录的配置。** 适合面板进不去、或者想连带把命名规则与
任务历史一起迁过来的场合：

```bash
# 停容器 —— 程序运行期间配置整份在内存里，退出时会写回磁盘，运行中改会被覆盖
docker-compose stop

# 把 Windows 上的配置复制过来（源路径：%APPDATA%\Bili23 Downloader\config.json）
cp "/path/to/config.json" "./data/config.json"

docker-compose start       # 用 start 而不是 restart：start 不重跑启动脚本
```

容器内的配置路径固定是 `/config/config.json`（没有 `Bili23 Downloader` 子目录层，
由 `BILI23_APP_DIRNAME` 控制）。

**只覆盖部署相关字段。** 启动脚本每次只写下载目录与 MCP / Web 面板的开关、
端口、用户名、令牌，Cookie、命名规则、其它偏好一概保留 —— 包括面板密码的哈希，
不会被启动脚本冲掉。所以搬过来的配置里那些自定义命名规则（含
`{video_dynamic_range}` / `{audio_codec}` 这些）原样生效。

---

## 七、数据与备份

持久化内容都在部署根的 `data/` 里（下载的文件在挂载的媒体目录，另算）：

```
data/
├── config.json     配置（含两个访问令牌，备份时注意）
├── task.db         下载任务库
└── logs/           运行日志与崩溃记录
```

备份就是打包 `data/`。恢复就是解回去再 `docker-compose up -d`。

日志在 `data/logs/app.log`，也可 `docker-compose logs bili23`。

---

## 八、这个镜像与安装版的差异

除了跑在容器里，镜像里的程序比官方安装版多了两处改动：

**1. 音视频参数改成实读**

命名规则里的动态范围、视频编码、分辨率、音频编码、声道这几项，官方版取的是
B 站下发的档位名（或干脆写死字面量），本版由下载完成后、改名交付之前对成品
`ffmpeg -i` 实读得到：

| 变量 | 实读取值 |
|---|---|
| `{video_dynamic_range}` | `SDR` / `HDR10` / `HLG` / `DV` |
| `{video_resolution}` | `1920x1080` |
| `{video_codec}` | `AVC` / `HEVC` / `AV1` |
| `{audio_codec}` | `AAC` / `FLAC` / `EAC3` |
| `{audio_channels}` | `2.0` / `5.1` |

读不到时保留原文件名，绝不因为探测失败而改变落盘路径。

> 动态范围靠 `color_transfer` 判定。ffmpeg 不带 ffprobe 时读不到 side data，
> 所以 HDR10 与 HDR10+ 一律报 `HDR10`。

> `{video_quality}`（`1080P` / `智能修复` 这类）与 `{audio_quality}`（`192K`）
> **仍然是 B 站下发的档位名**，没有实读 —— 它们是「选了哪一档」的记录，
> 不是媒体本身的属性。想看真实像素尺寸请用 `{video_resolution}`。

**2. 内置的 Web 面板**

官方版只有一个 Qt 界面；本版多一个浏览器可开的网页面板：带账号密码登录、
能看队列与进度、能解析建任务、还能扫码授权 B 站账号。见第三节。

上面两处都不碰 Qt 界面，offscreen 下与桌面版行为一致。

---

## 九、实测记录

部署环境：群晖 NAS `192.168.3.36`，部署根 `/volume1/docker/bili23`，
镜像 `ghcr.io/desire5864/bili23download:latest`（2026-10-03 之前是本地构建的
`bili23-downloader:2.20.0-probed`，见「十二、镜像发布与升级」）。

**1. 启动（不构建）**

镜像已经在本地（或已从 ghcr.io 拉下来），日常 `docker-compose up -d` 秒级起来 ——
它只创建容器，不碰 Dockerfile。容器起来后双端口都通：

```
NAME    IMAGE                                        STATUS        PORTS
bili23  ghcr.io/desire5864/bili23download:latest     Up (healthy)  0.0.0.0:23330-23331->23330-23331/tcp
```

**2. Web 面板**

| 检查项 | 结果 |
|---|---|
| `GET /` | 200，20937 字节，含标题、登录表单与扫码入口 |
| `GET /health` | 200 `ok` |
| `POST /api/call` 无凭据 / 错令牌 / 伪造会话 | 401 / 401 / 401 |
| 登录 `admin` + 正确密码 | 200，下发会话 Cookie |
| 登录 错误密码 / 不存在的用户 | 401，且**两者返回同一句话**（不泄露用户是否存在） |
| 用会话调 `list_tasks` | 200，`via_token=False` —— 走的确实是会话，不是令牌兜底 |
| 改密码后用旧会话 | 401（改密立即吊销所有会话） |
| 改密码 → 新密码登录 → 再改回 | 200 / 200 / 200，闭环通 |
| 取二维码 `qr/start` | 200，424×424 SVG，单条 `<path>` |
| 轮询 `qr/poll`（未扫码） | `code=86101 等待扫码` |
| `POST /api/call` 正确令牌 → `parse_url` | 200，解析出 1396 集 |

**3. 实读改造（下载一集真实校验）**

下载 `BV1GJ411x7h7`（26 MB，未登录所以只拿到 480P），落盘文件名：

```
【官方 MV】Never Gonna Give You Up - Rick Astley.480P.bilibili.WEB-DL.SDR.AVC.AAC 2.0.mp4
```

容器内对同一个文件跑 `ffmpeg -i` 反查：

```
Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 852x480 ...
Stream #0:1: Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 201 kb/s
```

四项与文件名逐项对上：`AVC` / `SDR` / `AAC 2.0` 全部来自实读，
`.SDR.` 是 `<.{video_dynamic_range}>` 渲染出来的，可选段语法在真实链路上生效。

**4. 动态范围三路判定（容器内合成素材）**

用容器自带 ffmpeg 造三段只差传输特性的素材，再调镜像里的 `probe_streams()`：

| 素材 `-color_trc` | 判定结果 |
|---|---|
| `smpte2084` | `HDR10` |
| `arib-std-b67` | `HLG` |
| `bt709` | `SDR` |

> ⚠️ 这里踩过一个坑，已被回归测试盯住：ffmpeg 打印的色彩括号里除了色彩信息
> 还可能跟扫描方式 —— `yuv444p(tv, bt2020nc/bt2020/smpte2084, progressive)`。
> 早先直接对整段做 `split("/")[-1]`，取到的是 `smpte2084, progressive`，
> 匹配不上任何曲线，**HDR 会被静默判成 SDR**。现在先按逗号截断再取传输特性。
> 真实片源常见的是 `(tv, bt709, progressive)`，本来就该是 SDR，所以这个 bug
> 只在 HDR 素材上才暴露得出来。

**5. 可选段降级**

`video_dynamic_range` 为空（探测失败、FFmpeg 缺失）时，整段连同前导点一起消失：

```
… 轻音少女 第二季.S02E18.1080P.bilibili.WEB-DL.HEVC.AAC 2.0     ← 不留 `..`
… 轻音少女 第二季.S02E18.1080P.bilibili.WEB-DL.HDR10.HEVC.AAC 2.0
```

**6. 回归测试**

源码侧 **782 项全过**（新增探测视频流、Web 面板账号与会话、扫码 SVG 等 51 项）。

**7. 构建耗时（走代理 vs 镜像源）**

同一台群晖、同一批包的对照实测：

| 拉取路径 | 实测速度 | 整次构建 |
|---|---|---|
| **经 HTTP 代理 → 官方源** | apt 18~21 MB/s，pypi 42 MB/s | **1 分 53 秒** |
| 直连阿里云镜像 | apt 390 KB/s，pip 1.1 MB/s | 磨了半小时仍未过 apt |
| 直连官方源 | 10 KB/s | 估约 5 小时 |

> 结论：**有代理就用代理拉官方源**，比任何国内镜像都快一个数量级。
> 镜像源（阿里云）是"没有代理"时的退路，不是首选。
> 详见第十一节与「常见问题」里那份拉取路径对照表。

---

## 十、常见问题

**容器起来了但端口连不上**

compose 里 `ports` 的绑定地址是不是 `127.0.0.1`？那样只有 NAS 本机能连。另外确认
`ports` 映射的宿主机端口没被别的服务占用。

**Web 面板提示"用户名或密码不正确"**

默认是 `admin` / `password`。若是自己改过又忘了，停容器后把配置里的密码字段
清空再启动 —— 程序检测到没有密码会重新初始化为默认密码的哈希：

```bash
docker-compose stop
# 编辑 data/config.json，
# 把 "web_panel_password" 的值改成一个空串（原本是 pbkdf2_sha256$... 一长串）
docker-compose start
```

**B站扫码后没反应 / 一直显示"等待扫码"**

先确认容器能出网：`docker-compose exec bili23 python -c "import httpx;print(httpx.get('https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header').status_code)"`
应当打印 200。另外二维码有有效期（约三分钟），过期了就点「获取二维码」换一张。

**日志里 `MCP 服务器启动失败，端口 23330`**

宿主机上那个端口被占了。改 compose 里 `ports` 的宿主机端口再 `docker-compose up -d`。

**下载目录不可写**

容器默认以 root 运行，正常不会遇到。若在 compose 里改了 `user` 为别的 uid:gid，
记得把 `data/` 与下载目录的属主也改过来：

```bash
sudo chown -R 1026:100 ./data '/volume1/Storage/哔哩哔哩'
```

**想换下载目录**

改 compose 里 volumes 的 `/volume1/Storage/哔哩哔哩` 一行（容器内固定挂
`/downloads`），然后 `docker-compose up -d`。启动脚本会自动把新路径同步进
程序配置，不必手工编辑 config.json。

**构建镜像时 apt / pip 卡住，进度条半天不动**

先看是不是在走官方源。群晖直连 `deb.debian.org` 只有 **10 KB/s**，而 ffmpeg
那一坨依赖是 **189 MB / 204 个包** —— 照这个速度要五个小时，看起来就是卡死。

两条路，**有代理就走代理，快得多**：

| 拉取路径 | 实测速度 | 189 MB 要多久 |
|---|---|---|
| **经 HTTP 代理 → 官方源** | **18 ~ 21 MB/s**（pypi 42 MB/s） | **约 10 秒** |
| 直连阿里云镜像 | 390 KB/s ~ 1.1 MB/s | 3 ~ 8 分钟 |
| 直连官方源 | 10 KB/s | 约 5 小时 |

本仓库的构建脚本默认走代理（`BILI23_BUILD_PROXY`，默认
`http://192.168.3.25:7890`），并且**走代理时会把软件源置空、直接拉官方源**：

```bash
python _nas_deploy.py build              # 默认：经代理拉官方源
python _nas_deploy.py build --no-proxy   # 没有代理时：退回阿里云镜像
```

手工 `docker-compose build` 时，代理要写成构建参数（Docker 构建跑在自己的网络
命名空间里，`127.0.0.1` 指的是构建容器自身，必须写宿主机能访问到的地址）：

```yaml
build:
  args:
    APT_MIRROR: ""
    PIP_INDEX_URL: https://pypi.org/simple
    http_proxy: http://192.168.3.25:7890
    https_proxy: http://192.168.3.25:7890
    no_proxy: localhost,127.0.0.1,::1
```

这三个代理值只活在构建过程中，不会进入最终镜像的环境变量。

---

## 十一、构建参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `APT_MIRROR` | `mirrors.aliyun.com` | Debian 软件源主机名。换别家写域名；**传空串则保持官方源**，配合下面的代理用 |
| `PIP_INDEX_URL` | `https://mirrors.aliyun.com/pypi/simple` | pip 源。走代理时传 `https://pypi.org/simple` |
| `http_proxy` / `https_proxy` | 空 | 构建期代理。**有代理就填上，比任何镜像站都快一个数量级**（实测 18~42 MB/s）。只作用于构建过程，不进最终镜像 |
| `no_proxy` | `localhost,127.0.0.1,::1` | 不走代理的地址。构建期只访问 apt 与 pypi，列环回就够 |

> **`docker-compose.yml` 里的 `build:` 段默认是注释掉的，这是有意为之。**
> 声明了 build 段，在群晖 Container Manager 上点「构建」就会把整个 Dockerfile
> 从头跑一遍（装系统包、装 Python 包），而镜像其实已经躺在本地了。注释掉之后，
> 「启动」才只是启动。需要重建镜像时把那段放开，再 `docker-compose build`。
>
> 顺带说清 apt 源为什么默认换成阿里云：实测从群晖直连 `deb.debian.org` 只有
> **10 KB/s**，而那一坨 ffmpeg 依赖是 **189 MB**（204 个包），照这个速度要跑
> 五个小时 —— 构建看上去就是"卡在 `Get:1 ...` 不动"。换阿里云后 390 KB/s，
> pip 侧更是 1.1 MB/s。

镜像基于 `python:3.13-slim`，另装 Qt 运行库、中文字体与 ffmpeg。
程序以 Qt 的 `offscreen` 平台插件运行：窗口照常创建、事件循环照常转，
下载、解析、MCP 与 Web 面板链路都不依赖显示输出。

### 镜像里带了什么

容器起来之后不需要再联网装任何东西：

| 类别 | 内容 |
|---|---|
| 系统 | Debian bookworm slim、`ffmpeg`、Qt 运行库（libGL / libEGL / libxkbcommon / libdbus …）、`fonts-noto-cjk` 中文字体、`tini` |
| Python | `requirements.txt` 全量（PySide6、httpx、qrcode 等），另加 `verhub-sdk` |
| 程序 | 本版源码（实读改造 + Web 面板）与启动引导脚本 |
| 网页 | 面板的样式、脚本、二维码全部内联或由服务端生成，**不引任何外部 CDN** |

最后一条不是洁癖：容器与 NAS 上的浏览器未必能出外网，一个 CDN 引用就能让面板
在最需要它的时候白屏。二维码同理 —— 它是服务端算出来的一串 SVG，不是外部图片。
程序启动时只出网做该做的事（解析、下载、扫码轮询），不做任何"补依赖"的动作。

---

## 十二、镜像发布与升级（ghcr.io）

2026-10-03 之前，镜像只在 NAS 本地构建（`_nas_deploy.py build`），每次升级都要把
源码推上去、在 NAS 上重跑一遍 apt + pip，二十来分钟。现在改成**代码在 GitHub、
镜像由 Actions 构建并发布到 ghcr.io**，升级退化成一条 `pull`。

### 发布地址与标签

```
ghcr.io/desire5864/bili23download:v1.0.1    # 精确版本，内容不可变，回滚用这个
ghcr.io/desire5864/bili23download:latest    # 跟随最新一个 v* 标签
ghcr.io/desire5864/bili23download:sha-abc1234   # 每次构建都有，能定位到具体提交
```

🔴 **镜像名必须全小写**。容器仓库的命名规范不允许大写字母，而 GitHub 账号是
`Desire5864` —— 直接拿账号名拼会得到 `invalid reference format`。

### 怎么发一版

```bash
# 1. 改源码，提交
git push

# 2. 打标签推上去 —— 这一下就触发了构建
git tag v1.0.2 && git push origin v1.0.2
```

`.github/workflows/docker-publish.yml` 会拉源码、用仓库根目录的 `Dockerfile`
构建、推 `v1.0.2` + `latest` + `sha-<短哈希>` 三个标签。用仓库内置的
`GITHUB_TOKEN` 登录，**不需要在仓库里另存任何密钥**。

也可以在 Actions 页面手动触发 `Publish container image`，从任意分支出一个
`latest`（不带版本号），用来验证还没打标签的改动。

### 怎么升级 NAS

```bash
cd /volume1/docker/bili23
/usr/local/bin/docker-compose pull      # 群晖的 compose 是独立二进制，要写绝对路径
/usr/local/bin/docker-compose up -d
```

`config.json`、日志、任务库都在挂出来的 `data/` 里，`pull` 不会碰它们。

要钉在某个版本上，把 compose 里的 `:latest` 换成 `:v1.0.1`；
这样 `pull` 不会把它悄悄换成更新的版本。

### 本地构建这条路还留着

改一行源码就去发个版本太重，所以 `_nas_deploy.py build` 原样保留，**并且打的是
同一个镜像名**（`ghcr.io/desire5864/bili23download:latest`）—— 于是 compose 会优先
用本地这份、不去拉远端：

```bash
python _nas_deploy.py push      # 推源码
python _nas_deploy.py build     # 在 NAS 上本地构建，约 20 分钟（有缓存时更快）
python work_up.py               # 用本地镜像起容器
```

两条路互不冲突：本地构建是开发回环，ghcr 是发布通道。

### 为什么 GHCR 而不是 Docker Hub

- **一套凭据**：源码、Actions、镜像都在 GitHub，Actions 用内置令牌推镜像，
  仓库里不用存任何 secret；换成 Docker Hub 就得再维护一份账号与 Access Token。
- **免登录拉取**：包设为公开后，NAS 上 `docker-compose pull` 直接可用，
  不用在 NAS 上存一份只读令牌。
- **和代码同生命周期**：包挂在仓库下，谁有仓库权限谁就能发版。
