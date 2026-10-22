# 抖音视频下载与发布

当前版本：**v2.01**

本项目提供抖音视频下载、微信视频号发布和淘宝商家后台视频发布。微信视频号与淘宝是两套独立发布流程，共用抖音下载 API 和项目根目录下的 `Videos/`。

| 功能 | 启动程序 | 默认地址 |
|---|---|---|
| 微信视频号版 | `auto.py` | WebUI：<http://127.0.0.1:7860> |
| 淘宝版 | `auto_taobao.py` | WebUI：<http://127.0.0.1:7861> |

两个启动程序都会启动或复用抖音下载 API（`8000`）。微信视频号使用发布 API `8002`；淘宝使用独立发布 API `8003`。两版可同时运行。

## macOS 开发调试版部署

开发调试版按服务分别启动，日志会直接显示在各个终端，便于定位问题。

### 1. 安装工具和依赖

在 macOS「终端」中安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，关闭并重新打开终端后检查：

```bash
uv --version
```

进入项目目录。以下命令假设项目位于 `~/Documents/ChatGPT/DOUYIN_Auto_Down`；如果目录不同，请替换路径。

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
uv python install 3.12
```

每个服务目录都有自己的 `pyproject.toml` 和 `uv.lock`。逐个同步依赖会创建各自的 `.venv` 环境：

```bash
cd downloader
uv sync --locked --python 3.12
cd ../publisher
uv sync --locked --python 3.12
cd ../webui
uv sync --locked --python 3.12
cd ../publish_taobao
uv sync --locked --python 3.12
cd ..
```

### 2. 手动启动服务

按需要启动对应发布版本。每条命令都在单独的终端窗口执行，并保持终端运行。

**终端一：抖音下载 API（两版共用，启动一次即可）**

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/downloader
NO_PROXY='*' no_proxy='*' uv run douyindl-api
```

**微信视频号调试：**

```bash
# 终端二：微信视频号发布 API
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/publisher
uv run channels-publisher-api
```

```bash
# 终端三：微信视频号 WebUI
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/webui
uv run python app.py
```

**淘宝调试：**

```bash
# 终端二：淘宝发布 API
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/publish_taobao
uv run taobao-publisher-api
```

```bash
# 终端三：淘宝版 WebUI
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down/webui
uv run python ../app_taobao.py
```

如需同时调试两版，下载 API 仍只启动一次，另外分别启动两套发布 API 和 WebUI。停止单个服务时，在对应终端按 `Control+C`。

## macOS 生产部署

生产部署用 `auto.py` 或 `auto_taobao.py` 一键启动对应服务组。首次部署先创建 `uv` 环境并安装依赖；之后更新代码或依赖时再重新同步。

### 1. 安装 uv、Python 和项目依赖

如果还没有安装 `uv`，按 [uv 官方 macOS 安装说明](https://docs.astral.sh/uv/getting-started/installation/)安装，然后执行：

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
uv python install 3.12
```

安装四个服务环境。`uv sync` 会在每个服务目录创建 `.venv` 并按锁文件安装依赖；启动器本身只使用 Python 标准库，不需要单独安装依赖。

```bash
cd downloader
uv sync --locked --python 3.12
cd ../publisher
uv sync --locked --python 3.12
cd ../webui
uv sync --locked --python 3.12
cd ../publish_taobao
uv sync --locked --python 3.12
cd ..
```

### 2. 启动生产服务

**微信视频号版：**

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
python3 auto.py
```

**淘宝版：**

```bash
cd ~/Documents/ChatGPT/DOUYIN_Auto_Down
python3 auto_taobao.py
```

启动器会依次检查并启动下载 API、对应的发布 API 和 WebUI。启动成功后，终端会显示 WebUI 地址。按 `Control+C` 会关闭本次启动器启动的服务；已经健康运行的服务会被复用，不会被该启动器关闭。

首次启动发布 API 时会打开 Chrome 浏览器。请在微信视频号或淘宝各自的浏览器窗口登录；登录资料会保存在对应发布目录的 `var/browser-profile/`，后续启动会尝试恢复。保持启动器终端和相关浏览器运行。

## 浏览器和常用地址

默认需要安装 Google Chrome。服务会打开可见浏览器进行登录和页面操作。若要使用 Playwright 自带的 Chromium，请分别在 `publisher/` 或 `publish_taobao/` 目录运行：

```bash
uv run playwright install chromium
```

启动对应服务前设置浏览器通道：

```bash
export CHANNELS_BROWSER_CHANNEL=chromium
export TAOBAO_BROWSER_CHANNEL=chromium
```

默认地址：

| 服务 | 地址 |
|---|---|
| 抖音下载 API | <http://127.0.0.1:8000> |
| 微信视频号发布 API | <http://127.0.0.1:8002> |
| 淘宝发布 API | <http://127.0.0.1:8003> |
| 微信视频号 WebUI | <http://127.0.0.1:7860> |
| 淘宝 WebUI | <http://127.0.0.1:7861> |

如端口已被其他程序占用，可在启动前设置对应变量：`DOUYIN_API_PORT`、`CHANNELS_API_PORT`、`WEBUI_PORT`、`TAOBAO_API_PORT`、`TAOBAO_WEBUI_PORT`。启动器会根据这些端口检查服务状态，并生成 WebUI 调用 API 的本机地址。

视频和 JSON 默认保存在项目根目录的 `Videos/`。如果需要更改目录，请同时设置 `VIDEOS_DIR` 和 `TAOBAO_VIDEO_DIR` 为同一个路径；`auto_taobao.py` 会将它们统一传给淘宝发布 API 和 WebUI。

## 通过 Cloudflare Tunnel 绑定域名访问

可以让其他电脑通过 HTTPS 域名访问 Mac 上运行的 WebUI。推荐使用 Cloudflare Tunnel 的「公开主机名」路由：`cloudflared` 只安装、运行在项目所在的 Mac 上，Windows 电脑无需安装 `cloudflared` 或 Cloudflare One Client，使用浏览器打开域名即可。前提是域名已接入 Cloudflare DNS；完整 DNS 接入通常是最简单的方式。[Cloudflare Tunnel 官方配置说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)

### 1. 在 Mac 上运行 Tunnel

如果 Mac 尚未安装 `cloudflared`，可用 Homebrew 安装：

```bash
brew install cloudflared
```

在 Cloudflare 控制台进入 **Networking → Tunnels**，创建或选择一个 Tunnel，按页面给出的 macOS 命令安装并运行连接器。已有 Tunnel 的话，继续使用即可；不要把 Tunnel Token 写入项目文件或提交到 GitHub。[macOS 安装方式](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)

创建远程管理的 Tunnel 后，Cloudflare 页面会提供带有专属 Token 的安装命令；macOS 命令通常类似 `sudo cloudflared service install <TUNNEL_TOKEN>`。请直接从自己的 Cloudflare 控制台复制，不要复制他人的 Token。

### 2. 将域名路由到项目 WebUI

在 Tunnel 的 **Routes → Add route → Published application** 中添加需要的主机名。两个 WebUI 可以共用同一个 Tunnel，使用不同子域名：

| Cloudflare 公开主机名示例 | Service URL |
|---|---|
| `video.example.com` | `http://127.0.0.1:7860` |
| `taobao.example.com` | `http://127.0.0.1:7861` |

将 `example.com` 替换成自己的域名。只需要淘宝版时，只添加 `taobao` 这一条路由。项目 WebUI 默认监听本机 `127.0.0.1`；Tunnel 与 WebUI 都运行在这台 Mac 上，因此无需把 WebUI 改为监听所有网卡，也无需在路由器上开放端口。

**只公开 WebUI 的 7860/7861 端口，不要为下载或发布 API 的 8000/8002/8003 端口创建公开路由。** WebUI 在 Mac 本机通过这些 API 端口工作。

### 3. 限制访问人员

发布界面可以操作已登录的微信视频号或淘宝账号。建议在 Cloudflare Zero Trust 的 **Access → Applications** 中为每个公开主机名建立 Self-hosted 应用和 Allow 策略，只放行指定邮箱。配置后，Windows 用户打开域名时先通过 Cloudflare Access 登录，再进入 WebUI；没有 Access 策略保护的公开主机名可能被其他互联网用户访问。[Cloudflare Access 自托管应用](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)

### 4. 从 Windows 打开

在 Mac 上启动对应服务，并保持 Mac、WebUI、发布 API 和 Tunnel 在线；Mac 休眠或关机时，远程访问会中断。淘宝登录、视频上传和发布动作仍由 Mac 上的淘宝自动化浏览器执行，首次登录也要在 Mac 的浏览器窗口完成。Windows 电脑用 Chrome、Edge 等浏览器打开：

```text
https://video.example.com
https://taobao.example.com
```

若只配置了其中一个主机名，就只打开相应地址。公开主机名方式下，Windows 端不需要安装额外软件。只有选择私有网络路由、不给应用配置公开主机名时，远程 Windows 设备才需要通过 Cloudflare One Client 接入 Cloudflare 网络。[公开应用与私有网络方式的区别](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/choose-application-type/)

如果 WebUI 页面能打开但任务进度不刷新，可在 Cloudflare 域名的 Network 设置中确认 WebSockets 已启用；Cloudflare Tunnel 支持 WebSocket 连接。[WebSocket 支持说明](https://developers.cloudflare.com/network/websockets/)

## 淘宝版功能

淘宝版默认从抖音文案中提取首个 `#` 前的标题，去除集数标记、标点和特殊符号后最多保留 29 字。发布前会选择创作者声明“个人观点，仅供参考”，并关联淘宝商品选择面板中的第一个商品。描述/详情可留空；“准备淘宝发布表单”会保留页面供检查但不会提交。

详细接口和配置说明：

- [抖音下载 API](downloader/README.md)
- [微信视频号发布 API](publisher/README.md)
- [WebUI](webui/README.md)
- [淘宝发布 API](publish_taobao/README.md)

浏览器登录资料、下载缓存和 `Videos/` 中的视频及元数据 JSON 属于本机数据，请勿提交到代码仓库。
