from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import gradio as gr
import httpx


logger = logging.getLogger("douyin.webui")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIDEOS_DIR = Path(os.getenv("VIDEOS_DIR", str(PROJECT_ROOT / "Videos"))).expanduser().resolve()
DOUYIN_API_BASE_URL = os.getenv("DOUYIN_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CHANNELS_API_BASE_URL = os.getenv("CHANNELS_API_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
DOUYIN_API_KEY = os.getenv("DOUYIN_API_KEY", "")
CHANNELS_API_KEY = os.getenv("CHANNELS_API_KEY", "")
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOUYIN_API_TIMEOUT_SECONDS", "900"))
PUBLISH_TIMEOUT_SECONDS = int(os.getenv("CHANNELS_API_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL_SECONDS = float(os.getenv("WEBUI_POLL_INTERVAL_SECONDS", "2"))
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov"}
VIDEO_ANNOTATIONS = [
    "无需标注",
    "含AI生成内容",
    "内容为虚构剧情,仅供娱乐",
    "个人观点,仅供参考",
    "内容包含营销广告",
    "内容为自行拍摄",
    "内容为转载",
]
TRACKING_FIELDS = (
    "wechat_channels_published",
    "wechat_channels_publish_status",
    "wechat_channels_published_at",
    "wechat_channels_submit_at",
    "wechat_channels_submit_job_id",
)
MAX_SHORT_TITLE_LENGTH = 15


def _is_plain_title_character(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] in {"L", "N", "M"} or category == "Zs"


def _sanitize_short_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    plain = "".join(char for char in normalized if _is_plain_title_character(char))
    return " ".join(plain.split())[:MAX_SHORT_TITLE_LENGTH]


def _validated_short_title(value: str | None) -> str | None:
    title = unicodedata.normalize("NFC", (value or "").strip())
    if len(title) > MAX_SHORT_TITLE_LENGTH:
        raise ValueError("视频号短标题必须少于 16 个字符（最多 15 个）。")
    if any(not _is_plain_title_character(char) for char in title):
        raise ValueError("视频号短标题只能包含文字、数字和空格，不能包含标点、特殊符号或表情。")
    return title or None


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        detail = response.text[:1000]
        raise RuntimeError(f"服务返回 HTTP {response.status_code}: {detail}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("服务返回的内容不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("服务返回了意外的数据格式")
    return data


def _api_file_name(item: dict[str, Any]) -> str:
    raw_name = str(item.get("name") or "")
    name = Path(raw_name).name
    if not name or name != raw_name or name in {".", ".."}:
        raise RuntimeError(f"下载服务返回了不安全的文件名: {raw_name!r}")
    return name


def _download_to_path(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    item: dict[str, Any],
    path: Path,
) -> None:
    download_url = str(item.get("download_url") or "")
    if not download_url.startswith("/"):
        raise RuntimeError("下载服务返回了无效的文件地址")
    url = urljoin(f"{base_url}/", download_url)
    with client.stream("GET", url, headers=_headers(api_key)) as response:
        if response.is_error:
            detail = response.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"下载文件失败（HTTP {response.status_code}）: {detail}")
        with path.open("xb") as output:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"下载服务返回了空文件: {item.get('name')}")


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取视频元数据文件 {path.name}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError(f"元数据文件不是 JSON 对象: {path.name}")
    return metadata


def _find_cached_download(url: str) -> tuple[Path, Path, dict[str, Any]] | None:
    match = re.search(r"/video/(\d+)", url)
    if not match or not VIDEOS_DIR.is_dir():
        return None

    video_id = match.group(1)
    metadata_matches = list(VIDEOS_DIR.glob(f"*{video_id}.json"))
    if len(metadata_matches) != 1:
        return None

    metadata_path = metadata_matches[0].resolve()
    if not metadata_path.is_relative_to(VIDEOS_DIR):
        return None
    video_path = next(
        (
            metadata_path.with_suffix(suffix)
            for suffix in VIDEO_EXTENSIONS
            if metadata_path.with_suffix(suffix).is_file()
        ),
        None,
    )
    if video_path is None:
        return None

    metadata = _load_metadata(metadata_path)
    if not isinstance(metadata.get("wechat_channels_published"), bool):
        return None
    return video_path.resolve(), metadata_path, metadata


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _save_downloaded_pair(
    client: httpx.Client,
    video_item: dict[str, Any],
    metadata_item: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any], bool]:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    video_name = _api_file_name(video_item)
    metadata_name = _api_file_name(metadata_item)
    if Path(video_name).suffix.lower() not in VIDEO_EXTENSIONS:
        raise RuntimeError(f"下载服务返回的文件不是视频: {video_name}")
    if Path(metadata_name).suffix.lower() != ".json":
        raise RuntimeError(f"下载服务没有返回 JSON 元数据文件: {metadata_name}")
    if Path(video_name).stem != Path(metadata_name).stem:
        raise RuntimeError("下载服务返回的视频与 JSON 文件名不匹配")

    video_path = (VIDEOS_DIR / video_name).resolve()
    metadata_path = (VIDEOS_DIR / metadata_name).resolve()
    if not video_path.is_relative_to(VIDEOS_DIR) or not metadata_path.is_relative_to(VIDEOS_DIR):
        raise RuntimeError("下载服务返回的文件路径超出 Videos 目录")

    video_exists = video_path.exists()
    metadata_exists = metadata_path.exists()
    if metadata_exists and not video_exists:
        raise FileExistsError(
            f"Videos 中存在没有对应视频的元数据文件: {metadata_path.name}；请先检查并处理该文件"
        )

    token = uuid.uuid4().hex
    video_temp = video_path.with_name(f".{video_path.name}.{token}.part")
    metadata_temp = metadata_path.with_name(f".{metadata_path.name}.{token}.part")
    try:
        if not video_exists:
            _download_to_path(
                client, DOUYIN_API_BASE_URL, DOUYIN_API_KEY, video_item, video_temp
            )
        _download_to_path(
            client, DOUYIN_API_BASE_URL, DOUYIN_API_KEY, metadata_item, metadata_temp
        )
        downloaded_metadata = _load_metadata(metadata_temp)
        if not downloaded_metadata.get("original_title") and not downloaded_metadata.get(
            "original_description"
        ):
            raise RuntimeError("JSON 元数据缺少原视频标题和描述")
        if not isinstance(downloaded_metadata.get("wechat_channels_published"), bool):
            raise RuntimeError("JSON 元数据缺少有效的 wechat_channels_published 布尔标识")

        # Preserve a previous publish state if the same video is downloaded again.
        if metadata_exists:
            old_metadata = _load_metadata(metadata_path)
            for key in TRACKING_FIELDS:
                if key in old_metadata:
                    downloaded_metadata[key] = old_metadata[key]
        else:
            downloaded_metadata.setdefault("wechat_channels_published", False)

        if not video_exists:
            video_temp.replace(video_path)
        try:
            _write_metadata(metadata_path, downloaded_metadata)
        except Exception:
            if not video_exists:
                video_path.unlink(missing_ok=True)
            raise
    finally:
        video_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)

    return video_path, metadata_path, downloaded_metadata, video_exists


def download_video(url: str, progress=gr.Progress()) -> tuple[Any, ...]:
    url = (url or "").strip()
    if not url:
        raise gr.Error("请先输入抖音视频链接。")

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cached = _find_cached_download(url)
    except Exception as exc:
        return (f"读取 Videos 中的已有文件失败：{exc}", *(gr.skip() for _ in range(9)))
    if cached is not None:
        video_path, metadata_path, metadata = cached
        title = str(metadata.get("original_title") or "")
        description = str(metadata.get("original_description") or "")
        publish_date = str(metadata.get("original_publish_date") or "发布日期未知")
        flag = "已发布" if metadata.get("wechat_channels_published") else "尚未发布"
        message = (
            "已从 Videos 恢复该视频的页面状态，无需重新下载。\n"
            f"文件：{video_path.name}\n"
            f"元数据：{metadata_path.name}\n"
            f"视频号状态：{flag}"
        )
        progress(1, desc="已恢复本地视频")
        state = {
            "video_path": str(video_path),
            "metadata_path": str(metadata_path),
            "metadata": metadata,
            "files": [str(video_path), str(metadata_path)],
            "download_job_id": None,
        }
        return (
            message,
            str(video_path),
            title,
            publish_date,
            description,
            _sanitize_short_title(title),
            "无需标注",
            "",
            [str(video_path), str(metadata_path)],
            state,
        )

    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=30)
    try:
        with httpx.Client(timeout=timeout) as client:
            health = _response_json(
                client.get(f"{DOUYIN_API_BASE_URL}/healthz", headers=_headers(DOUYIN_API_KEY))
            )
            if health.get("status") != "ok":
                raise RuntimeError("douyindl-api 健康检查未通过")

            progress(0.02, desc="正在提交下载任务")
            job = _response_json(
                client.post(
                    f"{DOUYIN_API_BASE_URL}/api/v1/downloads",
                    headers={**_headers(DOUYIN_API_KEY), "Content-Type": "application/json"},
                    json={"url": url, "max_items": 1},
                    timeout=30,
                )
            )
            job_id = job.get("job_id")
            if not job_id:
                raise RuntimeError("下载服务没有返回 job_id")

            deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
            while job.get("status") not in {"succeeded", "failed"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"下载任务超过 {DOWNLOAD_TIMEOUT_SECONDS} 秒仍未完成（job_id={job_id}）"
                    )
                progress(0.05, desc=f"下载任务 {job.get('status', 'queued')}：{job_id[:8]}")
                time.sleep(POLL_INTERVAL_SECONDS)
                job = _response_json(
                    client.get(
                        f"{DOUYIN_API_BASE_URL}/api/v1/downloads/{job_id}",
                        headers=_headers(DOUYIN_API_KEY),
                        timeout=30,
                    )
                )

            if job.get("status") != "succeeded":
                raise RuntimeError(f"douyindl-api 下载失败：{job.get('error') or '未知错误'}")

            files = job.get("files") or []
            video_item = next(
                (
                    item
                    for item in files
                    if Path(str(item.get("name", ""))).suffix.lower() in VIDEO_EXTENSIONS
                ),
                None,
            )
            if video_item is None:
                raise RuntimeError("下载任务完成，但 API 没有返回视频文件")
            matching_json = next(
                (
                    item
                    for item in files
                    if Path(str(item.get("name", ""))).suffix.lower() == ".json"
                    and Path(str(item.get("name", ""))).stem
                    == Path(str(video_item.get("name", ""))).stem
                ),
                None,
            )
            if matching_json is None:
                raise RuntimeError(
                    "下载任务完成，但没有返回与视频同名的 JSON。请重启更新后的 douyindl-api 后重试。"
                )

            progress(0.8, desc="正在将视频和元数据保存到 Videos")
            video_path, metadata_path, metadata, reused = _save_downloaded_pair(
                client, video_item, matching_json
            )

        title = str(metadata.get("original_title") or "")
        description = str(metadata.get("original_description") or "")
        publish_date = str(metadata.get("original_publish_date") or "发布日期未知")
        short_title = _sanitize_short_title(title)
        flag = "已发布" if metadata.get("wechat_channels_published") else "尚未发布"
        message = (
            f"下载完成：{'复用已有视频' if reused else '已保存视频'}和元数据。\n"
            f"文件：{video_path.name}\n"
            f"元数据：{metadata_path.name}\n"
            f"视频号状态：{flag}"
        )
        progress(1, desc="下载完成")
        state = {
            "video_path": str(video_path),
            "metadata_path": str(metadata_path),
            "metadata": metadata,
            "files": [str(video_path), str(metadata_path)],
            "download_job_id": job_id,
        }
        return (
            message,
            str(video_path),
            title,
            publish_date,
            description,
            short_title,
            "无需标注",
            "",
            [str(video_path), str(metadata_path)],
            state,
        )
    except gr.Error as exc:
        return (f"下载失败：{exc}", *(gr.skip() for _ in range(9)))
    except Exception as exc:
        return (f"下载失败：{exc}", *(gr.skip() for _ in range(9)))


def _current_video(state: dict[str, Any] | None) -> tuple[Path, Path, dict[str, Any]]:
    if not state or not state.get("video_path"):
        raise gr.Error("请先下载一个视频。")
    video_path = Path(state["video_path"]).expanduser().resolve()
    metadata_path = Path(state.get("metadata_path") or video_path.with_suffix(".json")).resolve()
    if not video_path.is_relative_to(VIDEOS_DIR) or not metadata_path.is_relative_to(VIDEOS_DIR):
        raise gr.Error("当前视频或元数据文件不在 Videos 目录中。")
    if not video_path.is_file():
        raise gr.Error(f"视频文件不存在：{video_path.name}")
    if not metadata_path.is_file():
        raise gr.Error(f"对应的元数据 JSON 不存在：{metadata_path.name}")
    return video_path, metadata_path, _load_metadata(metadata_path)


def _topics_from_text(raw_topics: str) -> list[str]:
    return [
        topic.strip().lstrip("#")
        for topic in re.split(r"[,，\n]", raw_topics or "")
        if topic.strip().lstrip("#")
    ]


def publish_video(
    state: dict[str, Any] | None,
    description: str,
    short_title: str,
    annotation: str,
    topics: str,
    is_original: bool,
    progress=gr.Progress(),
) -> tuple[str, dict[str, Any]]:
    try:
        video_path, metadata_path, metadata = _current_video(state)
    except gr.Error:
        logger.exception("发布前校验失败：当前视频状态无效")
        raise
    logger.info("视频号发布请求：video=%s", video_path.name)
    if metadata.get("wechat_channels_published"):
        logger.warning("已阻止重复发布：JSON 标记该视频已发布，video=%s", video_path.name)
        raise gr.Error("此视频的 JSON 已标记为发布成功。为避免重复投稿，已阻止再次发布。")
    if metadata.get("wechat_channels_publish_status") in {
        "submitted",
        "submit_clicked_unverified",
    }:
        logger.warning("已阻止重复提交：已有未核实的发布记录，video=%s", video_path.name)
        raise gr.Error(
            "此视频已有一次发布提交记录。请先到视频号助手核对投稿状态，确认未发布后再处理。"
        )
    if not (description or "").strip():
        logger.error("发布参数校验失败：视频描述为空，video=%s", video_path.name)
        raise gr.Error("视频描述不能为空。")
    try:
        clean_short_title = _validated_short_title(short_title)
    except ValueError as exc:
        logger.error("发布参数校验失败：短标题不符合限制，video=%s error=%s", video_path.name, exc)
        raise gr.Error(str(exc)) from exc
    if annotation not in VIDEO_ANNOTATIONS:
        logger.error("发布参数校验失败：视频标注无效，video=%s annotation=%r", video_path.name, annotation)
        raise gr.Error("请选择有效的视频标注。")
    logger.info(
        "发布参数已校验：标题=%r，标注=%s，话题数=%d",
        clean_short_title,
        annotation,
        len(_topics_from_text(topics)),
    )

    payload = {
        "video": video_path.name,
        "description": description.strip(),
        "short_title": clean_short_title,
        "video_annotation": annotation,
        "topics": _topics_from_text(topics),
        "is_original": bool(is_original),
        "mode": "publish",
        "confirm_publish": True,
    }
    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=30)
    try:
        with httpx.Client(timeout=timeout) as client:
            logger.info("正在检查视频号发布 API：%s", CHANNELS_API_BASE_URL)
            health = _response_json(
                client.get(
                    f"{CHANNELS_API_BASE_URL}/healthz",
                    headers=_headers(CHANNELS_API_KEY),
                )
            )
            if health.get("status") != "ok":
                raise RuntimeError("视频号发布 API 健康检查未通过")
            progress(0.02, desc="正在提交视频号发布任务")
            logger.info("发布 API 可用，正在创建浏览器自动化任务")
            job = _response_json(
                client.post(
                    f"{CHANNELS_API_BASE_URL}/api/v1/publish",
                    headers={**_headers(CHANNELS_API_KEY), "Content-Type": "application/json"},
                    json=payload,
                    timeout=30,
                )
            )
            job_id = job.get("job_id")
            if not job_id:
                raise RuntimeError("视频号发布服务没有返回 job_id")
            logger.info("发布任务已创建：job_id=%s", job_id)

            deadline = time.monotonic() + PUBLISH_TIMEOUT_SECONDS
            logged_steps = 0
            while job.get("status") not in {"succeeded", "failed"}:
                steps = job.get("steps")
                if isinstance(steps, list):
                    for step in steps[logged_steps:]:
                        logger.info("[发布任务 %s] %s", job_id[:8], step)
                    logged_steps = len(steps)
                else:
                    step = job.get("step")
                    if step:
                        logger.info("[发布任务 %s] %s", job_id[:8], step)
                current_step = job.get("step") or "正在处理"
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"发布任务超过 {PUBLISH_TIMEOUT_SECONDS} 秒仍未完成（job_id={job_id}）"
                    )
                progress(0.05, desc=f"{current_step}（任务 {job.get('status', 'queued')}）")
                time.sleep(POLL_INTERVAL_SECONDS)
                job = _response_json(
                    client.get(
                        f"{CHANNELS_API_BASE_URL}/api/v1/publish/{job_id}",
                        headers=_headers(CHANNELS_API_KEY),
                        timeout=30,
                    )
                )

            steps = job.get("steps")
            if isinstance(steps, list):
                for step in steps[logged_steps:]:
                    logger.info("[发布任务 %s] %s", job_id[:8], step)

        if job.get("status") != "succeeded":
            logger.error("发布任务失败：job_id=%s error=%s", job_id, job.get("error"))
            raise RuntimeError(f"视频号发布失败：{job.get('error') or '未知错误'}")
        result = job.get("result") or {}
        platform_state = result.get("platform_state")
        if platform_state not in {"submitted", "submit_clicked_unverified"}:
            raise RuntimeError(f"发布服务返回了未预期的平台状态：{platform_state!r}")

        metadata = _load_metadata(metadata_path)
        metadata["wechat_channels_published"] = False
        metadata["wechat_channels_publish_status"] = platform_state
        metadata["wechat_channels_submit_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata["wechat_channels_submit_job_id"] = job_id
        metadata["wechat_channels_submit_description"] = description.strip()
        metadata["wechat_channels_submit_short_title"] = clean_short_title
        metadata["wechat_channels_submit_annotation"] = annotation
        _write_metadata(metadata_path, metadata)
        state = {**(state or {}), "metadata": metadata}

        note = result.get("note") or "发布操作已提交。"
        if platform_state == "submitted":
            note += " 已进入视频号助手内容管理页；审核或公开状态仍需在平台核实。"
            logger.info("发布提交已被视频号确认：job_id=%s video=%s", job_id, video_path.name)
        else:
            note += " 页面未能核实提交结果；请先检查视频号助手，避免重复投稿。"
            logger.warning("发表按钮已点击但平台未确认：job_id=%s video=%s", job_id, video_path.name)
        progress(1, desc="发布操作结束")
        if platform_state == "submitted":
            return f"✅ 发布提交成功（视频号已确认接收）\n{note}", state
        return f"发布结果未确认：浏览器页面已保留，请检查视频号助手后再决定下一步。\n{note}", state
    except gr.Error:
        raise
    except Exception as exc:
        logger.exception("视频号发布流程报错：video=%s error=%s", video_path.name, exc)
        raise gr.Error(f"发布失败：{exc}") from exc


def publish_after_download(
    download_status: str,
    state: dict[str, Any] | None,
    description: str,
    short_title: str,
    annotation: str,
    topics: str,
    is_original: bool,
    progress=gr.Progress(),
) -> tuple[str, Any]:
    """Continue the combined action only when this download succeeded."""
    status_text = (download_status or "").strip()
    if not status_text.startswith(("下载完成：", "已从 Videos 恢复")):
        logger.warning("下载未成功，已跳过自动发布：%s", status_text or "没有下载结果")
        return "下载未成功，已取消自动发布。请检查上方下载状态。", gr.skip()

    logger.info("下载完成，开始执行自动发布")
    return publish_video(
        state,
        description,
        short_title,
        annotation,
        topics,
        is_original,
        progress=progress,
    )


def mark_video_as_published(
    state: dict[str, Any] | None,
    confirmed: bool,
) -> tuple[str, dict[str, Any], bool]:
    if not confirmed:
        raise gr.Error("请先确认你已在视频号助手看到该视频状态为“已发布”。")
    video_path, metadata_path, metadata = _current_video(state)
    metadata["wechat_channels_published"] = True
    metadata["wechat_channels_publish_status"] = "published"
    metadata["wechat_channels_published_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    _write_metadata(metadata_path, metadata)
    state = {**(state or {}), "metadata": metadata}
    return f"已更新 {metadata_path.name}：wechat_channels_published = true", state, False


def build_app() -> gr.Blocks:
    with gr.Blocks(
        title="抖音下载与视频号发布",
        css="""
        #download-and-publish,
        #download-and-publish > button,
        #download-and-publish button {
            background: #f28c28 !important;
            border-color: #f28c28 !important;
            color: #ffffff !important;
        }
        #download-and-publish:hover,
        #download-and-publish > button:hover,
        #download-and-publish button:hover {
            background: #df7817 !important;
            border-color: #df7817 !important;
        }
        """,
    ) as demo:
        gr.Markdown(
            "# 抖音下载与视频号发布\n"
            "先从抖音下载视频和元数据，再检查/编辑发布内容并提交到视频号。"
        )
        with gr.Row():
            douyin_url = gr.Textbox(
                label="抖音视频链接",
                placeholder="https://www.douyin.com/video/7668965098207755689",
                scale=5,
            )
            download_button = gr.Button("下载视频与资料", variant="primary", scale=1)

        download_and_publish_button = gr.Button(
            "下载并发布",
            variant="primary",
            elem_id="download-and-publish",
        )

        download_status = gr.Textbox(label="下载状态", interactive=False, lines=3)
        current_state = gr.State(value=None)

        with gr.Row():
            preview = gr.Video(label="下载的视频", interactive=False, height=360, scale=2)
            with gr.Column(scale=3):
                original_title = gr.Textbox(label="原视频标题", interactive=False)
                original_publish_date = gr.Textbox(label="原视频发布日期", interactive=False)
                short_title = gr.Textbox(
                    label="视频号短标题（最多 15 字）",
                    info="限15个字符，仅文字、数字和空格，不含标点、特殊符号或表情。",
                    max_length=MAX_SHORT_TITLE_LENGTH,
                )
                description = gr.Textbox(label="视频描述", lines=5, max_lines=12)
                annotation = gr.Dropdown(
                    label="视频标注",
                    choices=VIDEO_ANNOTATIONS,
                    value="无需标注",
                    allow_custom_value=False,
                )
                topics = gr.Textbox(
                    label="话题（可选，多个话题用逗号分隔）",
                    placeholder="话题一, 话题二",
                )
                is_original = gr.Checkbox(
                    label="同时声明原创（需确认拥有原创权益）",
                    value=False,
                )

        downloaded_files = gr.File(label="Videos 中的视频与 JSON", file_count="multiple")
        gr.Markdown(
            "**自动发布会真实提交到视频号。** 请先核对描述、短标题和视频标注；"
            "提交后平台可能仍在审核，程序不会把“已提交”误标为“已公开发布”。"
            "遇到报错或平台未确认时，浏览器会保留在当前页面供检查。"
        )
        publish_button = gr.Button("自动发布到视频号（会点击“发表”）", variant="stop")
        publish_status = gr.Textbox(label="发布状态", interactive=False, lines=4)

        with gr.Row():
            confirmed_published = gr.Checkbox(
                label="我已在视频号助手确认该视频状态为“已发布”",
                value=False,
            )
            mark_published_button = gr.Button("确认并更新 JSON 发布标识")
        tracking_status = gr.Textbox(label="JSON 发布标识", interactive=False)

        download_button.click(
            fn=download_video,
            inputs=[douyin_url],
            outputs=[
                download_status,
                preview,
                original_title,
                original_publish_date,
                description,
                short_title,
                annotation,
                topics,
                downloaded_files,
                current_state,
            ],
            concurrency_limit=2,
            show_progress="full",
        )
        download_and_publish_button.click(
            fn=download_video,
            inputs=[douyin_url],
            outputs=[
                download_status,
                preview,
                original_title,
                original_publish_date,
                description,
                short_title,
                annotation,
                topics,
                downloaded_files,
                current_state,
            ],
            concurrency_limit=2,
            show_progress="full",
        ).then(
            fn=publish_after_download,
            inputs=[
                download_status,
                current_state,
                description,
                short_title,
                annotation,
                topics,
                is_original,
            ],
            outputs=[publish_status, current_state],
            concurrency_limit=1,
            concurrency_id="publish",
            show_progress="full",
        )
        publish_button.click(
            fn=publish_video,
            inputs=[current_state, description, short_title, annotation, topics, is_original],
            outputs=[publish_status, current_state],
            concurrency_limit=1,
            concurrency_id="publish",
            show_progress="full",
        )
        mark_published_button.click(
            fn=mark_video_as_published,
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
        server_port=int(os.getenv("WEBUI_PORT", "7860")),
        allowed_paths=[str(VIDEOS_DIR)],
        theme=gr.themes.Soft(),
        inbrowser=True,
        share=False,
    )
