from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from taobao_publisher.automation import TaobaoBrowser


logger = logging.getLogger("taobao_publisher")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VIDEO_ROOT = Path(os.getenv("TAOBAO_VIDEO_DIR", str(PROJECT_ROOT / "Videos"))).expanduser().resolve()
MAX_VIDEO_BYTES = 300_000_000
PROFILE_DIR = Path(
    os.getenv(
        "TAOBAO_BROWSER_PROFILE",
        str(PROJECT_ROOT / "publish_taobao" / "var" / "browser-profile"),
    )
).expanduser().resolve()


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
    if not _loopback(host) and not os.getenv("TAOBAO_API_KEY"):
        raise RuntimeError("Set TAOBAO_API_KEY before binding the Taobao publisher API beyond localhost")


def resolve_video(name: str) -> Path:
    requested = Path(name)
    if requested.is_absolute():
        raise ValueError("video must be a file name relative to Videos/")
    video_root = Path(VIDEO_ROOT).resolve()
    path = (video_root / requested).resolve()
    if not path.is_relative_to(video_root):
        raise ValueError("video must stay inside Videos/")
    if path.suffix.lower() != ".mp4":
        raise ValueError("Taobao seller subscription videos must be MP4 files")
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found in Videos/: {name}")
    if path.stat().st_size == 0:
        raise ValueError("video file is empty")
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError("Taobao seller subscription videos must not exceed 300 MB")
    return path


class PublishRequest(BaseModel):
    video: str = Field(min_length=1, max_length=255, description="File name or relative path within Videos/")
    title: str = Field(default="", max_length=30)
    description: str = Field(default="", max_length=2000)
    topics: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["prepare", "publish"] = "prepare"
    confirm_publish: bool = False

    @field_validator("title", "description")
    @classmethod
    def trim_optional_text(cls, value: str) -> str:
        return value.strip()

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
        return self


class PublishJob:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.job_id = uuid.uuid4().hex
        self.payload = payload
        self.status: Literal["queued", "running", "succeeded", "failed"] = "queued"
        self.step = "等待淘宝发布队列处理"
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
    def __init__(self, browser: TaobaoBrowser) -> None:
        self.browser = browser
        self.jobs: dict[str, PublishJob] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.worker_task = asyncio.create_task(self._worker(), name="taobao-publish-worker")
        await self.browser.start_login()

    async def stop(self) -> None:
        if self.worker_task is not None:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
        await self.browser.close()

    def enqueue(self, payload: dict[str, Any]) -> PublishJob:
        job = PublishJob(payload)
        self.jobs[job.job_id] = job
        self.queue.put_nowait(job.job_id)
        return job

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs[job_id]
            job.status = "running"
            job.updated_at = _now()

            def report_step(step: str) -> None:
                job.step = step
                job.steps.append(step)
                job.updated_at = _now()
                logger.info("[job %s] %s", job.job_id[:8], step)

            try:
                job.result = await self.browser.publish(job.payload, on_step=report_step)
                job.status = "succeeded"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"
                job.step = "发布失败"
                job.steps.append(job.step)
                logger.exception("淘宝视频发布失败：job_id=%s", job_id)
            finally:
                job.updated_at = _now()
                self.queue.task_done()


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv("TAOBAO_API_KEY", "")
    if expected and (not x_api_key or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("TAOBAO_API_HOST", "127.0.0.1")
    _check_bind(host)
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    browser = TaobaoBrowser(PROFILE_DIR)
    publisher = Publisher(browser)
    await publisher.start()
    app.state.publisher = publisher
    try:
        yield
    finally:
        await publisher.stop()


app = FastAPI(
    title="Taobao Seller Video Publisher API",
    version="0.1.0",
    description="通过已登录的淘宝商家后台浏览器上传视频、填写信息并提交。",
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
    host = os.getenv("TAOBAO_API_HOST", "127.0.0.1")
    _check_bind(host)
    try:
        port = int(os.getenv("TAOBAO_API_PORT", "8003"))
    except ValueError as exc:
        raise RuntimeError("TAOBAO_API_PORT must be an integer") from exc
    uvicorn.run("taobao_publisher.api:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
