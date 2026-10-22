from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import gradio as gr
import httpx

import webui.app as download_app


logger = logging.getLogger("douyin.taobao_webui")
TAOBAO_API_BASE_URL = os.getenv("TAOBAO_API_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
TAOBAO_API_KEY = os.getenv("TAOBAO_API_KEY", "")
PUBLISH_TIMEOUT_SECONDS = int(os.getenv("TAOBAO_API_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL_SECONDS = float(os.getenv("WEBUI_POLL_INTERVAL_SECONDS", "2"))
MAX_TAOBAO_TITLE_LENGTH = 29
DOUYIN_EPISODE_MARKER = re.compile(
    r"第\s*[0-9零〇一二两兩三四五六七八九十百千万億亿几]+\s*(?:集|期|话|回|章|节|季)"
)


def _clean_douyin_title(value: str | None) -> str:
    """Extract a plain Taobao title from the Douyin caption."""
    title = unicodedata.normalize("NFKC", value or "").split("#", 1)[0]
    title = DOUYIN_EPISODE_MARKER.sub(" ", title)
    plain = "".join(
        char
        for char in title
        if unicodedata.category(char)[0] in {"L", "N", "M"}
        or unicodedata.category(char) == "Zs"
    )
    return " ".join(plain.split())[:MAX_TAOBAO_TITLE_LENGTH]


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if TAOBAO_API_KEY:
        headers["X-API-Key"] = TAOBAO_API_KEY
    return headers


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        raise RuntimeError(f"淘宝发布服务返回 HTTP {response.status_code}: {response.text[:1000]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("淘宝发布服务返回的内容不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("淘宝发布服务返回了意外的数据格式")
    return data


def check_taobao_session() -> str:
    try:
        with httpx.Client(timeout=15) as client:
            data = _response_json(
                client.get(f"{TAOBAO_API_BASE_URL}/api/v1/session", headers=_headers())
            )
        status = data.get("status", "unknown")
        labels = {
            "authenticated": "已检测到淘宝登录态。",
            "waiting_for_login": "淘宝浏览器已打开，等待登录。请登录发布服务单独打开的浏览器窗口。",
            "opening_browser": "正在打开淘宝登录窗口。",
            "login_required": "淘宝登录态已过期或未确认。请在发布服务浏览器窗口登录。",
            "login_timeout": "登录等待超时，请重新打开登录窗口。",
            "error": "淘宝浏览器启动失败。",
        }
        message = labels.get(status, f"淘宝会话状态：{status}")
        if data.get("error"):
            message += f"\n错误：{data['error']}"
        message += f"\n登录窗口：{'已打开' if data.get('browser_open') else '未打开'}"
        return message
    except Exception as exc:
        return f"无法连接淘宝发布 API：{exc}"


def open_taobao_login() -> str:
    try:
        with httpx.Client(timeout=15) as client:
            data = _response_json(
                client.post(f"{TAOBAO_API_BASE_URL}/api/v1/session/login", headers=_headers())
            )
        return (
            f"淘宝登录窗口状态：{data.get('status', 'unknown')}。"
            "请在淘宝发布服务单独打开的浏览器窗口中完成登录，再点“检查淘宝登录状态”。"
        )
    except Exception as exc:
        return f"无法打开淘宝登录窗口：{exc}"


def download_taobao_video(url: str, progress=gr.Progress()) -> tuple[Any, ...]:
    """Reuse the original downloader while recording separate Taobao metadata."""
    result = list(download_app.download_video(url, progress))
    state = result[9] if len(result) > 9 else None
    if not isinstance(state, dict) or not state.get("metadata_path"):
        return tuple(result)

    metadata_path = Path(state["metadata_path"])
    metadata = download_app._load_metadata(metadata_path)
    metadata.setdefault("taobao_video_published", False)
    metadata.setdefault("taobao_video_publish_status", "not_submitted")
    douyin_title = str(
        metadata.get("original_title") or metadata.get("original_description") or ""
    )
    cleaned_title = _clean_douyin_title(douyin_title)
    metadata["taobao_video_suggested_title"] = cleaned_title
    download_app._write_metadata(metadata_path, metadata)
    state = {**state, "metadata": metadata}
    # Keep the Douyin caption intact in original_* metadata, but prefill Taobao
    # with a cleaned title that excludes hashtags and episode markers.
    result[2] = cleaned_title
    result[4] = ""
    result[5] = cleaned_title
    result[9] = state
    return tuple(result)


def _current_video(state: dict[str, Any] | None) -> tuple[Path, Path, dict[str, Any]]:
    return download_app._current_video(state)


def submit_taobao_video(
    mode: Literal["prepare", "publish"],
    state: dict[str, Any] | None,
    title: str,
    description: str,
    progress=gr.Progress(),
) -> tuple[str, dict[str, Any]]:
    try:
        video_path, metadata_path, metadata = _current_video(state)
    except gr.Error:
        logger.exception("淘宝发布前校验失败：当前视频状态无效")
        raise

    title = (title or metadata.get("taobao_video_suggested_title") or "").strip()
    if len(title.strip()) > MAX_TAOBAO_TITLE_LENGTH:
        raise gr.Error(f"淘宝视频标题不能超过 {MAX_TAOBAO_TITLE_LENGTH} 个字符。")
    if mode == "publish" and metadata.get("taobao_video_published"):
        raise gr.Error("此视频的 JSON 已标记为淘宝发布成功，为避免重复发布已停止提交。")
    if mode == "publish" and metadata.get("taobao_video_publish_status") in {
        "submitted",
        "submit_clicked_unverified",
    }:
        raise gr.Error("此视频已有一次淘宝发布提交记录。请先到商家后台核对，再决定是否重试。")

    payload = {
        "video": video_path.name,
        "title": title.strip(),
        "description": description.strip(),
        "mode": mode,
        "confirm_publish": mode == "publish",
    }
    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=30)
    try:
        with httpx.Client(timeout=timeout) as client:
            health = _response_json(
                client.get(f"{TAOBAO_API_BASE_URL}/healthz", headers=_headers())
            )
            if health.get("status") != "ok":
                raise RuntimeError("淘宝发布 API 健康检查未通过")
            progress(0.02, desc="正在提交淘宝视频任务")
            job = _response_json(
                client.post(
                    f"{TAOBAO_API_BASE_URL}/api/v1/publish",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json=payload,
                    timeout=30,
                )
            )
            job_id = job.get("job_id")
            if not job_id:
                raise RuntimeError("淘宝发布服务没有返回 job_id")

            deadline = time.monotonic() + PUBLISH_TIMEOUT_SECONDS
            logged_steps = 0
            while job.get("status") not in {"succeeded", "failed"}:
                steps = job.get("steps")
                if isinstance(steps, list):
                    for step in steps[logged_steps:]:
                        logger.info("[淘宝任务 %s] %s", job_id[:8], step)
                    logged_steps = len(steps)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"淘宝发布任务超过 {PUBLISH_TIMEOUT_SECONDS} 秒仍未完成")
                progress(0.05, desc=job.get("step") or "正在处理淘宝视频")
                time.sleep(POLL_INTERVAL_SECONDS)
                job = _response_json(
                    client.get(
                        f"{TAOBAO_API_BASE_URL}/api/v1/publish/{job_id}",
                        headers=_headers(),
                        timeout=30,
                    )
                )

        if job.get("status") != "succeeded":
            raise RuntimeError(job.get("error") or "未知错误")
        result = job.get("result") or {}
        platform_state = result.get("platform_state")
        if mode == "prepare":
            if platform_state != "form_prepared":
                raise RuntimeError(f"淘宝服务返回了未预期的表单状态：{platform_state!r}")
            return "✅ 视频已上传，创作者声明已选择“个人观点，仅供参考”，并已添加商品列表中的第一个商品；表单已保留，尚未提交。", state or {}
        if platform_state not in {"submitted", "submit_clicked_unverified"}:
            raise RuntimeError(f"淘宝服务返回了未预期的发布状态：{platform_state!r}")

        metadata = download_app._load_metadata(metadata_path)
        metadata["taobao_video_published"] = False
        metadata["taobao_video_publish_status"] = platform_state
        metadata["taobao_video_submit_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata["taobao_video_submit_job_id"] = job_id
        metadata["taobao_video_submit_title"] = title.strip()
        metadata["taobao_video_submit_description"] = description.strip()
        download_app._write_metadata(metadata_path, metadata)
        state = {**(state or {}), "metadata": metadata}

        note = result.get("note") or "淘宝发布操作已提交。"
        if platform_state == "submitted":
            return f"✅ 淘宝页面已确认接收提交。{note}\n审核结果请在淘宝商家后台查看。", state
        return f"淘宝页面未确认提交结果，请检查保留的浏览器页后再决定是否重试。\n{note}", state
    except gr.Error:
        raise
    except Exception as exc:
        logger.exception("淘宝视频发布流程报错：video=%s error=%s", video_path.name, exc)
        raise gr.Error(f"淘宝发布失败：{exc}") from exc


def mark_taobao_video_published(
    state: dict[str, Any] | None,
    confirmed: bool,
) -> tuple[str, dict[str, Any], bool]:
    if not confirmed:
        raise gr.Error("请先确认你已在淘宝商家后台看到该视频发布成功。")
    _, metadata_path, metadata = _current_video(state)
    metadata["taobao_video_published"] = True
    metadata["taobao_video_publish_status"] = "published"
    metadata["taobao_video_published_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    download_app._write_metadata(metadata_path, metadata)
    state = {**(state or {}), "metadata": metadata}
    return f"已更新 {metadata_path.name}：taobao_video_published = true", state, False


def _publish_after_download(
    download_status: str,
    state: dict[str, Any] | None,
    title: str,
    description: str,
    progress=gr.Progress(),
) -> tuple[str, Any]:
    status_text = (download_status or "").strip()
    if not status_text.startswith(("下载完成：", "已从 Videos 恢复")):
        return "下载未成功，已取消淘宝发布。请先检查下载状态。", gr.skip()
    return submit_taobao_video("publish", state, title, description, progress=progress)


def build_app() -> gr.Blocks:
    with gr.Blocks(title="抖音下载与淘宝视频发布") as demo:
        prepare_mode = gr.State("prepare")
        publish_mode = gr.State("publish")
        gr.Markdown(
            "# 抖音下载与淘宝视频发布\n"
            "淘宝标题会从抖音文案提取并清洗后自动填写，也可在分步操作中修改；描述/详情可留空。视频需为 300MB 以内的 MP4。"
            "创作者声明默认选择“个人观点，仅供参考”；发布前会自动关联店铺商品列表中的第一个商品。"
            "单步按钮会自动提交发布；如需检查表单或填写标题、描述，请先下载，再用分步操作。"
        )
        with gr.Row():
            session_check_button = gr.Button("检查淘宝登录状态")
            login_button = gr.Button("打开 / 恢复淘宝登录窗口")
        session_status = gr.Textbox(label="淘宝登录状态", interactive=False, lines=2)
        with gr.Row():
            douyin_url = gr.Textbox(
                label="抖音视频链接",
                placeholder="https://www.douyin.com/video/7668965098207755689",
                scale=5,
            )
            download_button = gr.Button("下载视频与资料", variant="primary", scale=1)

        download_and_publish_button = gr.Button("下载并发布到淘宝", variant="primary")
        download_status = gr.Textbox(label="下载状态", interactive=False, lines=3)
        current_state = gr.State(value=None)

        with gr.Row():
            preview = gr.Video(label="下载的视频", interactive=False, height=360, scale=2)
            with gr.Column(scale=3):
                original_title = gr.Textbox(label="整理后的抖音标题", interactive=False)
                original_publish_date = gr.Textbox(label="原视频发布日期", interactive=False)
                title = gr.Textbox(
                    label="淘宝视频标题（已自动填写，可修改，最多 29 字）",
                    max_length=MAX_TAOBAO_TITLE_LENGTH,
                )
                description = gr.Textbox(label="视频描述 / 详情（可选）", lines=5, max_lines=12)
                # The shared downloader returns these two legacy values; they are not Taobao fields.
                annotation = gr.Dropdown(choices=download_app.VIDEO_ANNOTATIONS, value="无需标注", visible=False)
                topics = gr.Textbox(visible=False)

        downloaded_files = gr.File(label="Videos 中的视频与 JSON", file_count="multiple")
        gr.Markdown(
            "点击“下载并发布到淘宝”后，程序会自动下载视频、上传到淘宝、选择创作者声明、关联商品列表中的第一个商品并提交发布。"
            "标题和描述/详情可留空。提交结果未确认时，请先到淘宝商家后台核实，再决定是否重试。"
            "如需先检查淘宝表单再提交，可使用下方的分步操作。审核或公开状态以淘宝商家后台为准。"
        )
        with gr.Accordion("分步操作（可选）", open=False):
            with gr.Row():
                prepare_button = gr.Button("准备淘宝发布表单（不提交）", variant="secondary")
                publish_button = gr.Button("确认并发布到淘宝", variant="stop")
        publish_status = gr.Textbox(label="淘宝发布状态", interactive=False, lines=4)

        with gr.Row():
            confirmed_published = gr.Checkbox(
                label="我已在淘宝商家后台确认该视频发布成功",
                value=False,
            )
            mark_published_button = gr.Button("确认并更新 JSON 淘宝发布标识")
        tracking_status = gr.Textbox(label="JSON 淘宝发布标识", interactive=False)

        download_outputs = [
            download_status,
            preview,
            original_title,
            original_publish_date,
            description,
            title,
            annotation,
            topics,
            downloaded_files,
            current_state,
        ]
        session_check_button.click(fn=check_taobao_session, outputs=[session_status])
        login_button.click(fn=open_taobao_login, outputs=[session_status])
        download_button.click(
            fn=download_taobao_video,
            inputs=[douyin_url],
            outputs=download_outputs,
            concurrency_limit=2,
            show_progress="full",
        )
        download_and_publish_button.click(
            fn=download_taobao_video,
            inputs=[douyin_url],
            outputs=download_outputs,
            concurrency_limit=2,
            show_progress="full",
        ).then(
            fn=_publish_after_download,
            inputs=[download_status, current_state, title, description],
            outputs=[publish_status, current_state],
            concurrency_limit=1,
            concurrency_id="taobao_publish",
            show_progress="full",
        )
        prepare_button.click(
            fn=submit_taobao_video,
            inputs=[prepare_mode, current_state, title, description],
            outputs=[publish_status, current_state],
            concurrency_limit=1,
            concurrency_id="taobao_publish",
            show_progress="full",
        )
        publish_button.click(
            fn=submit_taobao_video,
            inputs=[publish_mode, current_state, title, description],
            outputs=[publish_status, current_state],
            concurrency_limit=1,
            concurrency_id="taobao_publish",
            show_progress="full",
        )
        mark_published_button.click(
            fn=mark_taobao_video_published,
            inputs=[current_state, confirmed_published],
            outputs=[tracking_status, current_state, confirmed_published],
            concurrency_limit=1,
        )
    return demo


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_app()
    app.queue(default_concurrency_limit=2)
    app.launch(
        server_name=os.getenv("WEBUI_HOST", "127.0.0.1"),
        server_port=int(os.getenv("TAOBAO_WEBUI_PORT", "7861")),
        allowed_paths=[str(download_app.VIDEOS_DIR)],
        theme=gr.themes.Soft(),
        inbrowser=True,
        share=False,
    )
