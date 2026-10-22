# 淘宝商家后台视频发布 API

`publish_taobao/` 是第二版的独立淘宝发布服务。视频号发布代码和端口保持原样；淘宝使用独立的 API、浏览器登录资料和 JSON 发布状态。

## 可行性与边界

该服务通过可见浏览器操作商家后台：打开 `myseller.taobao.com`，进入「店铺 → 内容中心 → 发视频」，上传视频。淘宝版 WebUI 默认从抖音原始文案提取标题：只取首个 `#` 前的部分，去掉“第 X 集/期/话”等集数标记及标点、表情等符号，并将结果（最多 29 字）填入淘宝标题；描述/详情可留空。提交前默认选择创作者声明“个人观点，仅供参考”，并关联商品选择面板中的第一个商品。WebUI 的“下载并发布到淘宝”会在下载后自动上传并提交；也可用“分步操作”先准备包含商品关联的表单，再人工确认发布。它会使用单独的 `var/browser-profile/` 保存淘宝登录态。

淘宝开放平台提供短视频内容 API，但官方文档列出的发布接口还要求应用授权、上传后的 `file_id`、视频模板服务 ID 和子订单号，不能直接替代截图里的商家后台发布流程。当前实现因此使用网页自动化，页面改版后需要维护字段定位器。

服务支持两种模式：

- `prepare`（默认）：上传视频，可选填写标题和描述/详情，选择商品列表中的第一个商品，保留表单供人工检查，不点击发布。
- `publish`：只有请求同时传入 `confirm_publish: true` 才会点击发布按钮。提交结果未确认时会保留页面，避免误报成功。

提交成功表示淘宝页面确认接收，不代表审核通过或前台公开。请在商家后台核实最终状态。这个工作区环境无法读取登录后的淘宝动态表单，因此还需要用账号实际打开一次「发视频」表单来校准页面选择器与字段限制。

## 安装与运行

```bash
cd publish_taobao
uv sync
uv run taobao-publisher-api
```

服务监听 `http://127.0.0.1:8003`，启动后会打开淘宝商家后台。首次使用请在窗口中登录。API 文档为 `http://127.0.0.1:8003/docs`，状态查询为 `http://127.0.0.1:8003/api/v1/session`。

淘宝登录资料使用本服务自己的浏览器目录，不会继承日常 Chrome 或视频号发布器的登录态。在淘宝版界面点击“检查淘宝登录状态”查看状态；如需登录，点击“打开 / 恢复淘宝登录窗口”，并在该独立窗口完成淘宝商家账号登录后再重试。

需要手动打开或恢复登录会话时：

```bash
curl -X POST http://127.0.0.1:8003/api/v1/session/login
```

## 启动淘宝版界面

复用现有 WebUI 环境和抖音下载 API，在项目根目录运行：

```bash
cd webui
uv run python ../app_taobao.py
```

淘宝版界面默认监听 `http://127.0.0.1:7861`，发布 API 默认是 `8003`。两者可以分别使用这些环境变量覆盖：

- `TAOBAO_API_HOST`、`TAOBAO_API_PORT`、`TAOBAO_API_KEY`
- `TAOBAO_API_BASE_URL`、`TAOBAO_API_TIMEOUT_SECONDS`
- `TAOBAO_VIDEO_DIR`（默认项目根目录的 `Videos/`）
- `TAOBAO_BROWSER_PROFILE`（默认 `publish_taobao/var/browser-profile/`）
- `TAOBAO_BROWSER_CHANNEL`（默认 `chrome`；可用 `chromium`）
- `TAOBAO_WEBUI_PORT`（默认 `7861`）
- `TAOBAO_UPLOAD_TIMEOUT_SECONDS`（默认 `900`）

如果未安装 Chrome，可在 `publish_taobao/` 中运行 `uv run playwright install chromium`，并设置 `TAOBAO_BROWSER_CHANNEL=chromium`。

## API 示例

先准备表单：

```bash
curl -X POST http://127.0.0.1:8003/api/v1/publish \
  -H 'Content-Type: application/json' \
  -d '{"video":"some-video.mp4","title":"视频标题","description":"视频描述和详情","mode":"prepare"}'
```

确实要提交发布时，明确设置发布模式和确认字段：

```bash
curl -X POST http://127.0.0.1:8003/api/v1/publish \
  -H 'Content-Type: application/json' \
  -d '{"video":"some-video.mp4","title":"视频标题","description":"视频描述和详情","mode":"publish","confirm_publish":true}'
```

视频文件必须位于 `Videos/` 中。淘宝开放平台短视频指南对“订阅视频”列出的要求是 MP4、300MB 以内；本版按该类目限制上传。淘宝版界面将标题限制为 29 字。
