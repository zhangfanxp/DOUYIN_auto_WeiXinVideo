from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from channels_publisher.automation import ChannelsBrowser


logger = logging.getLogger("channels_publisher")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VIDEO_ROOT = Path(os.getenv("CHANNELS_VIDEO_DIR", str(PROJECT_ROOT / "Videos"))).expanduser().resolve()
PROFILE_DIR = Path(
    os.getenv("CHANNELS_BROWSER_PROFILE", str(PROJECT_ROOT / "publisher" / "var" / "browser-profile"))
).expanduser().resolve()

VideoAnnotation = Literal[
    "无需标注",
    "含AI生成内容",
    "内容为虚构剧情,仅供娱乐",
    "个人观点,仅供参考",
    "内容包含营销广告",
    "内容为自行拍摄",
    "内容为转载",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_bind(host: str) -> None:
    if not _loopback(host) and not os.getenv("CHANNELS_API_KEY"):
        raise RuntimeError("Set CHANNELS_API_KEY before binding the publisher API beyond localhost")


def resolve_video(name: str) -> Path:
    requested = Path(name)
    if requested.is_absolute():
        raise ValueError("video must be a file name relative to Videos/")
    video_root = Path(VIDEO_ROOT).resolve()
    path = (video_root / requested).resolve()
    if not path.is_relative_to(video_root):
        raise ValueError("video must stay inside Videos/")
    if path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
        raise ValueError("video must be an MP4, M4V, or MOV file")
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found in Videos/: {name}")
    if path.stat().st_size == 0:
        raise ValueError("video file is empty")
    return path


class PublishRequest(BaseModel):
    video: str = Field(min_length=1, max_length=255, description="File name or relative path within Videos/")
    description: str = Field(min_length=1, max_length=1000)
    short_title: str | None = Field(
        default=None,
        max_length=15,
        description="Optional title of up to 15 letters/numbers and spaces; punctuation and symbols are not allowed.",
    )
    video_annotation: VideoAnnotation = "无需标注"
    topics: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["prepare", "draft", "publish"] = "draft"
    confirm_publish: bool = False
    is_original: bool = False
    # Kept for existing API callers; prefer video_annotation for new requests.
    contains_ai_content: bool = False

    @field_validator("short_title", mode="before")
    @classmethod
    def validate_short_title(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        value = unicodedata.normalize("NFC", value).strip()
        if any(
            not (
                unicodedata.category(char)[0] in {"L", "N", "M"}
                or unicodedata.category(char) == "Zs"
            )
            for char in value
        ):
            raise ValueError("Short title may contain only letters, numbers, and spaces")
        return value or None

    @field_validator("topics")
    @classmethod
    def clean_topics(cls, topics: list[str]) -> list[str]:
        cleaned = [topic.strip().lstrip("#") for topic in topics]
        if any(not topic or len(topic) > 30 for topic in cleaned):
            raise ValueError("Each topic must contain 1 to 30 characters")
        return cleaned

    @model_validator(mode="after")
    def require_publish_confirmation(self):
        if self.mode == "publish" and not self.confirm_publish:
            raise ValueError("mode='publish' requires confirm_publish=true")
        if self.contains_ai_content:
            if self.video_annotation == "无需标注":
                self.video_annotation = "含AI生成内容"
            elif self.video_annotation != "含AI生成内容":
                raise ValueError(
                    "contains_ai_content conflicts with video_annotation; use video_annotation only"
                )
        return self


class PublishJob:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.job_id = uuid.uuid4().hex
        self.payload = payload
        self.status: Literal["queued", "running", "succeeded", "failed"] = "queued"
        self.step = "等待发布队列处理"
        self.steps = [self.step]
        self.created_at = _now()
        self.updated_at = self.created_at
        self.error: str | None = None
        self.result: dict[str, Any] | None = None

    def response(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "step": self.step,
            "steps": list(self.steps),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "video": Path(self.payload["video_path"]).name,
            "mode": self.payload["mode"],
            "result": self.result,
            "error": self.error,
        }


class Publisher:
    def __init__(self, browser: ChannelsBrowser) -> None:
        self.browser = browser
        self.jobs: dict[str, PublishJob] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.worker_task = asyncio.create_task(self._worker(), name="channels-publish-worker")
        # Restore the persistent browser profile when the API starts so the
        # first publish job cannot fail just because /session/login was omitted.
        await self.browser.start_login()
        logger.info("视频号浏览器会话已开始自动恢复")

    async def stop(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
        await self.browser.close()

    def enqueue(self, payload: dict[str, Any]) -> PublishJob:
        job = PublishJob(payload)
        try:
            self.queue.put_nowait(job.job_id)
        except asyncio.QueueFull:
            raise
        self.jobs[job.job_id] = job
        return job

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = self.jobs[job_id]
                job.status = "running"
                job.updated_at = _now()

                def report_step(step: str) -> None:
                    job.step = step
                    if not job.steps or job.steps[-1] != step:
                        job.steps.append(step)
                    job.updated_at = _now()
                    logger.info("[发布任务 %s] %s", job.job_id[:8], step)

                logger.info(
                    "[发布任务 %s] 开始处理视频：%s",
                    job.job_id[:8],
                    Path(job.payload["video_path"]).name,
                )
                job.result = await self.browser.publish(job.payload, on_step=report_step)
                job.status = "succeeded"
                job.updated_at = _now()
                if job.result.get("platform_state") == "submitted":
                    job.step = "视频号已确认接收提交"
                    logger.info("[发布任务 %s] 平台已确认接收发布提交", job.job_id[:8])
                elif job.result.get("platform_state") == "submit_clicked_unverified":
                    job.step = "已点击发表，但提交结果未确认"
                    logger.warning("[发布任务 %s] 已点击发表，但未获得平台确认；浏览器页面已保留", job.job_id[:8])
                else:
                    job.step = f"自动化操作完成：{job.result.get('platform_state')}"
                    logger.info("[发布任务 %s] 自动化操作完成：%s", job.job_id[:8], job.result.get("platform_state"))
                if not job.steps or job.steps[-1] != job.step:
                    job.steps.append(job.step)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job = self.jobs.get(job_id)
                if job:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"[:500]
                    job.step = f"失败：{job.error}"
                    job.steps.append(job.step)
                    job.updated_at = _now()
                    logger.exception("[发布任务 %s] 失败：%s", job.job_id[:8], job.error)
            finally:
                self.queue.task_done()


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv("CHANNELS_API_KEY", "")
    if expected and (not x_api_key or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("CHANNELS_API_HOST", "127.0.0.1")
    _check_bind(host)
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    browser = ChannelsBrowser(PROFILE_DIR)
    publisher = Publisher(browser)
    await publisher.start()
    app.state.publisher = publisher
    try:
        yield
    finally:
        await publisher.stop()


app = FastAPI(
    title="WeChat Channels Publisher API",
    version="0.1.0",
    description="本机通过已授权的视频号助手浏览器会话上传、保存草稿或提交发布。",
    lifespan=lifespan,
)


def _publisher(request: Request) -> Publisher:
    publisher = getattr(request.app.state, "publisher", None)
    if publisher is None:
        raise HTTPException(status_code=503, detail="Publisher is not ready")
    return publisher


@app.get("/healthz", tags=["service"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/session", dependencies=[Depends(require_api_key)], tags=["session"])
async def session_status(request: Request) -> dict[str, Any]:
    browser = _publisher(request).browser
    await browser.is_authenticated()
    return browser.session_info()


@app.post(
    "/api/v1/session/login",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
    tags=["session"],
)
async def start_login(request: Request) -> dict[str, Any]:
    browser = _publisher(request).browser
    await browser.start_login()
    return browser.session_info()


@app.post(
    "/api/v1/publish",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
    tags=["publishing"],
)
async def create_publish_job(payload: PublishRequest, request: Request) -> dict[str, Any]:
    try:
        video_path = resolve_video(payload.video)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    publisher = _publisher(request)
    if publisher.queue.full():
        raise HTTPException(status_code=429, detail="The publishing queue is full")
    job = publisher.enqueue({**payload.model_dump(), "video_path": str(video_path)})
    return job.response()


@app.get(
    "/api/v1/publish/{job_id}",
    dependencies=[Depends(require_api_key)],
    tags=["publishing"],
)
async def get_publish_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _publisher(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return job.response()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.getenv("CHANNELS_API_HOST", "127.0.0.1")
    _check_bind(host)
    try:
        port = int(os.getenv("CHANNELS_API_PORT", "8002"))
    except ValueError as exc:
        raise RuntimeError("CHANNELS_API_PORT must be an integer") from exc
    uvicorn.run("channels_publisher.api:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
