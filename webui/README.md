# 抖音下载与视频号发布 Web UI

Gradio 主程序，串接已有的 `douyindl-api` 下载服务和 `channels-publisher-api` 视频号发布服务。

## 启动

先分别启动下载 API 和视频号发布 API，然后：

```bash
cd webui
uv sync
uv run python app.py
```

浏览器默认打开 `http://127.0.0.1:7860`。UI 默认只监听本机，不会创建 Gradio 公网分享链接。
为显示下载结果，Gradio 只允许读取 `Videos/` 目录中的文件；保持默认的 `127.0.0.1` 监听地址可避免把这些文件提供给局域网中的其他设备。

如果 API 地址或 API Key 不同，可在启动前设置：

```bash
export DOUYIN_API_BASE_URL='http://127.0.0.1:8000'
export DOUYIN_API_KEY=''
export CHANNELS_API_BASE_URL='http://127.0.0.1:8002'
export CHANNELS_API_KEY=''
export VIDEOS_DIR='../Videos'
uv run python app.py
```

如果服务端设置了 `CHANNELS_VIDEO_DIR`，请让 `VIDEOS_DIR` 指向同一个目录；发布 API 只接受该目录中的文件名。

## 使用流程

1. 输入一个抖音视频链接，点击“下载视频与资料”。程序轮询下载 API，把 MP4 和同名 JSON 保存到 `Videos/`。如果相同视频已在本地，页面会从 `Videos/` 恢复，不会重新下载。
2. 原视频描述会预填到视频号描述框；原视频标题会先移除标点和特殊符号，再截取前 15 个字符作为短标题。短标题最多 15 个字符，只能包含文字、数字和空格。可以修改短标题、描述、标注和话题。
3. 点击“自动发布到视频号（会点击‘发表’）”后，程序调用发布 API，上传视频、填写表单并提交。
4. 提交完成只会在 JSON 中记为 `submitted` / `submit_clicked_unverified`。在视频号助手确认状态为“已发布”后，勾选确认框并点击“确认并更新 JSON 发布标识”，才会把 `wechat_channels_published` 更新为 `true`。

若此前已经提交但还没有核实状态，程序会阻止再次提交，避免误发重复视频。请先在视频号助手确认结果。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOUYIN_API_BASE_URL` | `http://127.0.0.1:8000` | 下载 API 地址 |
| `DOUYIN_API_KEY` | 空 | 下载 API Key（服务启用 Key 时必填） |
| `CHANNELS_API_BASE_URL` | `http://127.0.0.1:8002` | 视频号发布 API 地址 |
| `CHANNELS_API_KEY` | 空 | 视频号 API Key（服务启用 Key 时必填） |
| `VIDEOS_DIR` | 项目根目录下 `Videos/` | 视频和 JSON 的本地保存目录 |
| `DOUYIN_API_TIMEOUT_SECONDS` | `900` | 下载任务最长等待时间 |
| `CHANNELS_API_TIMEOUT_SECONDS` | `1800` | 发布任务最长等待时间 |
| `WEBUI_HOST` | `127.0.0.1` | Gradio 监听地址 |
| `WEBUI_PORT` | `7860` | Gradio 端口 |

发布过程需要已经登录的视频号助手浏览器会话。首次启动或重启发布 API 后，如 `/api/v1/session` 显示 `not_started`，先启动持久浏览器会话并完成登录：

```bash
curl -X POST http://127.0.0.1:8002/api/v1/session/login
curl http://127.0.0.1:8002/api/v1/session
```

确认返回状态为 `authenticated` 后再发布。Gradio 页面不会保存登录 Cookie 或 API Key 到视频 JSON。
