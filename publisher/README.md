# 视频号发布 API

本地 Python/FastAPI 服务通过可见的 Chromium 浏览器操作已授权的视频号助手账号。普通视频发布没有可依赖的公开内容发布 API；网页结构改动时，Playwright 选择器可能需要维护。

## 安装并启动

```bash
cd publisher
uv sync
uv run channels-publisher-api
```

默认地址为 `http://127.0.0.1:8002`。API 启动时会自动打开视频号助手，并尝试恢复保存的浏览器登录状态。首次使用或登录过期时，在打开的浏览器窗口扫码登录。登录数据保存在 `publisher/var/browser-profile/`，不会导出 Cookie。

默认使用已安装的 Google Chrome；如果没有 Chrome，可安装 Playwright Chromium 并设置 `CHANNELS_BROWSER_CHANNEL=chromium`：

```bash
uv run playwright install chromium
```

如需手动重新打开登录会话：

```bash
curl -X POST http://127.0.0.1:8002/api/v1/session/login
```

查看登录状态：

```bash
curl http://127.0.0.1:8002/api/v1/session
```

`prepared_forms_open` 表示当前保留了几个待人工复核的发表表单。

## 提交视频

视频必须是 `Videos/` 目录内的 `.mp4`、`.m4v` 或 `.mov` 文件。

默认保存草稿：

```bash
curl -X POST http://127.0.0.1:8002/api/v1/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "video": "some-video.mp4",
    "description": "视频描述",
    "short_title": "短标题",
    "video_annotation": "无需标注",
    "topics": ["话题一", "话题二"],
    "mode": "draft"
  }'
```

`description`、`short_title` 和 `video_annotation` 都由调用方在请求中填写。短标题可省略；填写时最多 15 个字符，限文字、数字和空格，不允许标点或特殊符号。标注必须从以下选项中选择一个，默认是 `无需标注`：

- `无需标注`
- `含AI生成内容`
- `内容为虚构剧情,仅供娱乐`
- `个人观点,仅供参考`
- `内容包含营销广告`
- `内容为自行拍摄`
- `内容为转载`

服务会在发布表单中填写描述和短标题，并选择对应的视频标注。`is_original` 仍用于视频号单独的“声明原创”流程；它与“内容为自行拍摄”标注是不同设置。

`mode` 支持：

- `prepare`：把视频上传到发布表单并填好内容，保持浏览器页面打开供人工复核，不提交草稿或发布。
- `draft`：保存到视频号草稿箱（默认）。
- `publish`：提交发表。必须同时传 `"confirm_publish": true`；API 只会报告网页提交结果，不把“审核中”误报为“已公开发布”。

发布任务会在终端日志和任务响应的 `steps` 字段中记录当前步骤。提交前会检查视频文件是否已载入、页面上传/处理进度、相关上传请求是否结束，并要求“发表”按钮连续可用一段时间；上传失败或等待超时不会点击“发表”。遇到页面错误或提交结果未能确认时，浏览器页面会保留供检查；只有页面跳转到内容管理页或出现成功提示，任务才会标记为平台已确认接收。

可选字段 `is_original` 和 `contains_ai_content` 默认均为 `false`。只有内容事实符合时才设为 `true`。

查询任务：

```bash
curl http://127.0.0.1:8002/api/v1/publish/JOB_ID
```

## 网络绑定

默认只监听本机。若设置 `CHANNELS_API_HOST` 为非 loopback 地址，必须同时设置 `CHANNELS_API_KEY`；受保护的接口通过 `X-API-Key` 校验。不要把保存登录态的个人号浏览器目录放进共享服务器。

## 当前边界

- 使用官方网页 UI，不调用或伪造微信内部接口。
- 需要本机有图形桌面，以便首次扫码登录。
- 账号需有视频号助手发布权限；会话过期时重新调用登录接口扫码。
- 页面改版、验证码或平台人工确认会导致任务失败，需人工处理后重试。
- 成功状态表示发布/草稿操作已提交到网页；最终审核状态以视频号助手内容管理页为准。

## 开发验证

```bash
uv run python -m unittest discover -s tests -v
```

本机已用 `Videos/` 中的 MP4 通过 `mode=prepare` 验证上传表单流程。接口返回 `form_prepared` 后，浏览器会保留在待复核表单中；该验证没有保存草稿或提交发表。

上传控件定位参考 [PostFlow 的视频号上传器](https://github.com/jefftko/PostFlow/tree/main/uploader/tencent_uploader)，使用主页面选择器和多个上传区候选控件兼容页面结构变化。视频号助手页面可能调整，正式批量操作前仍应先用单条视频复核。
