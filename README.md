# 抖音下载与微信视频号发布

本项目由下载 API、视频号发布 API 和 Gradio WebUI 组成。可以分别启动各服务进行调试，也可以运行根目录的 `auto.py` 一次启动全部服务。

| 服务 | 目录 | 默认地址 | 用途 |
|---|---|---|---|
| 抖音下载 API | `downloader/` | `http://127.0.0.1:8000` | 下载视频和原视频元数据 JSON |
| 视频号发布 API | `publisher/` | `http://127.0.0.1:8002` | 使用已授权的浏览器会话填写发布表单并提交 |
| Gradio WebUI | `webui/` | `http://127.0.0.1:7860` | 提供下载、编辑资料、发布和状态管理界面 |

## 环境要求

- macOS 图形桌面环境。视频号发布 API 使用可见浏览器操作视频号助手。
- 已安装 [uv](https://docs.astral.sh/uv/)；可用 `uv --version` 检查。
- Python 3.12。下载器依赖要求 Python `>=3.12,<3.14`；下面的安装步骤会为下载器固定到 3.12。
- 已安装 Google Chrome。若没有 Chrome，可使用 Playwright Chromium，见下文“浏览器准备”。

进入项目根目录：

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
```

## 首次安装与更新依赖

三个服务各自管理 Python 环境和锁文件。推荐保留这种隔离方式，在首次部署或某个服务的依赖发生变化后，分别执行 `uv sync --locked`：

```bash
# 下载 API：使用 Python 3.12
uv python install 3.12
cd downloader
uv python pin 3.12
uv sync --locked

# 视频号发布 API
cd ../publisher
uv sync --locked

# Gradio WebUI
cd ../webui
uv sync --locked

# 回到项目根目录
cd ..
```

`--locked` 会按各目录现有的 `uv.lock` 安装依赖；如果修改了该服务的 `pyproject.toml`，先在服务目录运行 `uv lock` 更新锁文件，再运行 `uv sync --locked`。

根目录的 `pyproject.toml`、`uv.lock` 和 `requirements.txt` 汇总了项目依赖。需要额外建立一个包含全部服务依赖的单一环境时，可以运行：

```bash
uv sync --group services
```

这个聚合环境是可选的；下面的服务启动方式仍会使用每个子目录自己的环境和锁文件。

## 浏览器准备与视频号登录

发布 API 默认使用本机安装的 Google Chrome，并在启动时打开视频号助手。首次使用时，在该浏览器窗口扫码登录。浏览器登录资料保存在 `publisher/var/browser-profile/`，后续启动会尝试恢复登录状态。发布期间请保持发布 API 和浏览器运行。

如果没有安装 Google Chrome，先安装 Playwright Chromium：

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/publisher
uv run playwright install chromium
```

然后在启动发布 API 或 `auto.py` 前设置浏览器通道：

```bash
export CHANNELS_BROWSER_CHANNEL=chromium
```

可通过 API 查看发布会话状态：

```bash
curl http://127.0.0.1:8002/api/v1/session
```

状态为 `authenticated` 表示登录有效。默认本机调用不需要 API Key；若启用了 `CHANNELS_API_KEY`，查询时添加请求头：

```bash
curl -H 'X-API-Key: 替换为你的密钥' http://127.0.0.1:8002/api/v1/session
```

## 分布启动和调试

依赖安装完成后，打开三个终端窗口，分别启动服务。每个命令都从对应服务目录运行。

### 终端一：下载 API

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/downloader
NO_PROXY='*' no_proxy='*' uv run douyindl-api
```

下载器访问抖音时需要绕过系统代理。启动后检查服务：

```bash
curl http://127.0.0.1:8000/healthz
```

正常时会返回 `{"status":"ok"}`。API 文档地址为 <http://127.0.0.1:8000/docs>。

### 终端二：视频号发布 API

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/publisher
uv run channels-publisher-api
```

启动后会恢复或打开用于视频号助手的 Chrome 浏览器，并在终端输出发布步骤和错误日志。检查 API 是否就绪：

```bash
curl http://127.0.0.1:8002/healthz
curl http://127.0.0.1:8002/api/v1/session
```

API 文档地址为 <http://127.0.0.1:8002/docs>。首次启动或登录过期时，在打开的浏览器中扫码登录。也可以手动请求打开登录会话：

```bash
curl -X POST http://127.0.0.1:8002/api/v1/session/login
```

### 终端三：WebUI

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/webui
uv run python app.py
```

WebUI 默认会打开浏览器并监听 <http://127.0.0.1:7860>。页面中可以下载单个抖音视频、编辑描述和短标题、选择视频标注，并通过“自动发布到视频号”或“下载并发布”执行发布流程。

分布启动时要保持三个服务都在运行。按各终端的 `Ctrl+C` 可以分别停止对应服务。

## 一键启动全部服务

日常使用时可以只在项目根目录启动 `auto.py`：

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
python3 auto.py
```

`auto.py` 会调用 `uv`，按顺序启动下载 API、视频号发布 API 和 WebUI；每个服务通过健康检查后才启动下一个。首次启动时，`uv run` 会在对应服务目录准备其虚拟环境和依赖。所有服务日志会显示在当前终端，WebUI 默认地址是 <http://127.0.0.1:7860>。

视频号发布 API 启动时会打开授权浏览器。首次使用请扫码登录，发布时保持浏览器和 `auto.py` 运行。按 `Ctrl+C` 会停止本次由 `auto.py` 启动的服务；已经在运行且健康检查通过的服务会被复用，`auto.py` 不会关闭这些外部启动的服务。

默认端口为下载 API `8000`、视频号发布 API `8002`、WebUI `7860`。启动前可以设置端口：

```bash
export DOUYIN_API_PORT=8000
export CHANNELS_API_PORT=8002
export WEBUI_PORT=7860
python3 auto.py
```

如果同时设置了 `DOUYIN_API_BASE_URL` 或 `CHANNELS_API_BASE_URL`，请确保它们指向对应 API 的实际地址；未设置时，`auto.py` 会根据端口生成本机连接地址。端口被其他非本项目服务占用时，启动器会报错退出，可修改端口后重试。

## 常用环境变量

默认只监听本机，不需要配置 API Key。若将 API 绑定到非本机地址，必须设置相应 Key；WebUI 使用同名 Key 变量调用 API。

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `DOUYIN_API_HOST` | `127.0.0.1` | 下载 API 监听地址 |
| `DOUYIN_API_PORT` | `8000` | 下载 API 端口 |
| `DOUYIN_API_KEY` | 空 | 下载 API 访问密钥；非本机监听时必填 |
| `DOUYIN_API_OUTPUT_DIR` | `downloader/var/downloads` | 下载 API 暂存目录 |
| `CHANNELS_API_HOST` | `127.0.0.1` | 视频号发布 API 监听地址 |
| `CHANNELS_API_PORT` | `8002` | 视频号发布 API 端口 |
| `CHANNELS_API_KEY` | 空 | 发布 API 访问密钥；非本机监听时必填 |
| `CHANNELS_VIDEO_DIR` | 项目根目录的 `Videos/` | 发布 API 可访问的视频目录 |
| `CHANNELS_BROWSER_PROFILE` | `publisher/var/browser-profile/` | 持久化浏览器登录资料目录 |
| `CHANNELS_BROWSER_CHANNEL` | `chrome` | 浏览器通道；使用 Playwright Chromium 时设为 `chromium` |
| `DOUYIN_API_BASE_URL` | `http://127.0.0.1:8000` | WebUI 调用的下载 API 地址 |
| `CHANNELS_API_BASE_URL` | `http://127.0.0.1:8002` | WebUI 调用的视频号发布 API 地址 |
| `VIDEOS_DIR` | 项目根目录的 `Videos/` | WebUI 保存和读取视频、JSON 的目录 |
| `WEBUI_HOST` | `127.0.0.1` | Gradio 监听地址 |
| `WEBUI_PORT` | `7860` | Gradio 端口 |

使用 API Key 时，为对应服务和 WebUI 设置相同密钥。例如：

```bash
export DOUYIN_API_KEY='生成一个长随机密钥'
export CHANNELS_API_KEY='生成另一个长随机密钥'
python3 auto.py
```

`Videos/` 是 WebUI 保存下载结果、发布 API 读取视频的共享目录。若修改 `VIDEOS_DIR`，也要将 `CHANNELS_VIDEO_DIR` 设置为同一路径，否则发布 API 找不到视频文件。

## 文件和发布状态

- 下载 API 的任务文件暂存在 `downloader/var/downloads/`。
- WebUI 将视频和同名 JSON 保存到项目根目录的 `Videos/`。
- JSON 保存原视频标题、描述、发布日期，以及 `wechat_channels_published` 等视频号发布状态字段。
- 发布提交成功不等于视频已公开。请在视频号助手确认已发布后，再通过 WebUI 的确认操作更新 JSON 标识。
- `Videos/`、浏览器授权资料和运行日志属于本机数据，不应提交到代码仓库。

## 常见问题

### 端口已被占用

如果占用端口的是本项目已启动且健康的服务，`auto.py` 会复用它。如果是其他程序，调整对应的 `*_PORT` 环境变量，或先停止占用端口的程序。默认 WebUI 端口是 `7860`。

### 浏览器没有启动或需要重新登录

确认本机 Chrome 已安装，或按“浏览器准备与视频号登录”安装 Playwright Chromium。若登录过期，在发布 API 打开的浏览器中重新扫码；登录资料保存在 `publisher/var/browser-profile/`。

### WebUI 找不到 API 或视频

检查三个服务是否仍在运行，并分别请求下载和发布 API 的 `/healthz`。确认 `DOUYIN_API_BASE_URL`、`CHANNELS_API_BASE_URL` 与 API 端口一致，且 WebUI 的 `VIDEOS_DIR` 和发布 API 的 `CHANNELS_VIDEO_DIR` 指向同一目录。

更多服务接口和参数说明见：[下载 API 文档](downloader/README.md)、[视频号发布 API 文档](publisher/README.md)、[WebUI 文档](webui/README.md)。
