"""FastAPI service for queued Douyin downloads.

The service uses DouyinDownloader's async API directly and forces the tested
headless-browser resolver. Jobs and their status are kept in process memory;
downloaded media is written under ``var/downloads`` by default.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import mimetypes
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from douyindl.downloader import Config, DouyinDownloader, setup_logger
from douyindl.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".jpg", ".jpeg", ".png", ".webp"}
DOWNLOADABLE_EXTENSIONS = MEDIA_EXTENSIONS | {".json"}
URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；）]+", re.IGNORECASE)
DIRECT_RESOURCE_PATTERN = re.compile(
    r"^(?:/(?:video|note|collection)/\d+|/share/mix/detail/\d+)(?:/|$)",
    re.IGNORECASE,
)
RESOURCE_QUERY_PATTERN = re.compile(r"(?:modal_id|collection_id|mix_id)=\d+", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return parsed


def _host_is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _assert_safe_bind(host: str) -> None:
    if not _host_is_loopback(host) and not os.getenv("DOUYIN_API_KEY"):
        raise RuntimeError(
            "Set DOUYIN_API_KEY before binding the API to a network interface."
        )


def _validate_douyin_url(raw: str) -> str:
    """Accept one HTTPS URL hosted on Douyin, optionally pasted with share text."""
    matches = URL_PATTERN.findall(raw.strip())
    if len(matches) != 1:
        raise ValueError("Provide exactly one Douyin URL")

    url = matches[0].rstrip(".,;!?，。；）)]}>")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("The URL is malformed") from exc

    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("Only HTTPS Douyin URLs are accepted")
    if parts.username or parts.password or (port is not None and port != 443):
        raise ValueError("Credentials and non-standard URL ports are not accepted")
    return url


async def _resolve_short_or_share_url(url: str) -> str:
    """Resolve share pages, and verify redirects stay on a Douyin host."""
    parts = urlsplit(url)
    if DIRECT_RESOURCE_PATTERN.search(parts.path) or RESOURCE_QUERY_PATTERN.search(parts.query):
        return url

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=10,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DouyinDownloaderAPI/1.0)"},
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not resolve the Douyin share link: {exc}") from exc

    final_url = str(response.url)
    final = urlsplit(final_url)
    try:
        final_port = final.port
    except ValueError as exc:
        raise ValueError("The share link redirected to a malformed URL") from exc
    if (
        final.scheme != "https"
        or (final.hostname or "").lower().rstrip(".") not in ALLOWED_HOSTS
        or final.username
        or final.password
        or (final_port is not None and final_port != 443)
    ):
        raise ValueError("The share link redirected outside Douyin")
    return final_url


@dataclass(slots=True)
class DownloadJob:
    job_id: str
    url: str
    max_items: int
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    result: dict[str, Any] | None = None
    error: str | None = None
    files: list[Path] = field(default_factory=list)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096, description="抖音作品或分享链接")
    max_items: int = Field(default=1, ge=1, le=20, description="合集最多下载数量")


class DownloadFile(BaseModel):
    id: int
    name: str
    size_bytes: int
    download_url: str


class DownloadJobResponse(BaseModel):
    job_id: str
    url: str
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    files: list[DownloadFile] = Field(default_factory=list)
    error: str | None = None


class JobManager:
    def __init__(
        self,
        *,
        output_root: Path,
        base_config: Config,
        concurrency: int,
        queue_size: int,
        timeout_seconds: int,
    ) -> None:
        self.output_root = output_root
        self.base_config = base_config
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.jobs: dict[str, DownloadJob] = {}
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.workers = [
            asyncio.create_task(self._worker(), name=f"douyin-download-worker-{i + 1}")
            for i in range(self.concurrency)
        ]

    async def stop(self) -> None:
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    def enqueue(self, url: str, max_items: int) -> DownloadJob:
        job = DownloadJob(job_id=uuid.uuid4().hex, url=url, max_items=max_items)
        self.jobs[job.job_id] = job
        try:
            self.queue.put_nowait(job.job_id)
        except asyncio.QueueFull:
            self.jobs.pop(job.job_id, None)
            raise
        return job

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = self.jobs.get(job_id)
                if job is None:
                    continue
                job.status = "running"
                job.updated_at = _now()
                job_dir = self.output_root / job.job_id

                try:
                    resolved_url = await _resolve_short_or_share_url(job.url)
                    config = copy.copy(self.base_config)
                    config.resolve_mode = "browser"
                    config.enable_progress = False
                    # The API always writes a video-metadata sidecar next to each
                    # downloaded video. Do not create optional cover/audio/text files.
                    config.save_metadata = True
                    config.save_cover = False
                    config.save_desc = False
                    config.save_music = False
                    config.save_json = True
                    config.output_dir = str(job_dir)
                    downloader = DouyinDownloader(
                        output_dir=str(job_dir),
                        max_counts=job.max_items,
                        config=config,
                    )
                    result = await asyncio.wait_for(
                        downloader.run(resolved_url), timeout=self.timeout_seconds
                    )
                    job.files = sorted(
                        (
                            path
                            for path in job_dir.rglob("*")
                            if path.is_file() and path.suffix.lower() in DOWNLOADABLE_EXTENSIONS
                        ),
                        key=lambda path: (path.suffix.lower() == ".json", path.name.lower()),
                    )
                    has_media = any(path.suffix.lower() in MEDIA_EXTENSIONS for path in job.files)
                    if not has_media:
                        raise RuntimeError("The download finished without producing a media file")
                    success_count = int(result.get("success", 0))
                    if success_count < 1:
                        raise RuntimeError(
                            "The downloader produced media files but reported no successful downloads "
                            f"(success={success_count}); see downloader/app.log for the underlying error"
                        )

                    # Avoid returning the server's local filesystem path in the API response.
                    job.result = {
                        key: result.get(key)
                        for key in ("kind", "resource_id", "mix_name", "total", "success", "skipped")
                    }
                    job.status = "succeeded"
                    job.updated_at = _now()
                except asyncio.CancelledError:
                    job.status = "failed"
                    job.error = "The service stopped before the job completed"
                    job.updated_at = _now()
                    raise
                except Exception as exc:
                    logger.exception("Download job failed: job_id=%s", job_id)
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"[:500]
                    job.updated_at = _now()
            finally:
                self.queue.task_done()


def _job_response(job: DownloadJob) -> DownloadJobResponse:
    files = [
        DownloadFile(
            id=index,
            name=path.name,
            size_bytes=path.stat().st_size,
            download_url=f"/api/v1/downloads/{job.job_id}/files/{index}",
        )
        for index, path in enumerate(job.files, start=1)
        if path.exists()
    ]
    return DownloadJobResponse(
        job_id=job.job_id,
        url=job.url,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        result=job.result,
        files=files,
        error=job.error,
    )


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.getenv("DOUYIN_API_KEY", "")
    if expected and (not x_api_key or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("DOUYIN_API_HOST", "127.0.0.1")
    _assert_safe_bind(host)

    output_root = Path(
        os.getenv("DOUYIN_API_OUTPUT_DIR", str(PROJECT_ROOT / "var" / "downloads"))
    ).expanduser().resolve()
    config = Config(PROJECT_ROOT / "config" / "config.yaml")
    setup_logger(level=config.log_level, log_file=config.log_file)
    config.resolve_mode = "browser"
    config.enable_progress = False
    config.save_metadata = True
    config.save_cover = False
    config.save_desc = False
    config.save_music = False
    config.save_json = True

    manager = JobManager(
        output_root=output_root,
        base_config=config,
        concurrency=_env_int("DOUYIN_API_CONCURRENCY", 1),
        queue_size=_env_int("DOUYIN_API_QUEUE_SIZE", 20),
        timeout_seconds=_env_int("DOUYIN_API_TIMEOUT_SECONDS", 900),
    )
    await manager.start()
    app.state.job_manager = manager
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(
    title="Douyin Downloader API",
    version="1.0.0",
    description="将抖音视频下载任务加入队列，并通过 API 查询进度和获取文件。",
    lifespan=lifespan,
)


def _manager(request: Request) -> JobManager:
    manager = getattr(request.app.state, "job_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Download service is not ready")
    return manager


@app.get("/healthz", tags=["service"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/downloads",
    response_model=DownloadJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
    tags=["downloads"],
)
async def submit_download(payload: DownloadRequest, request: Request) -> DownloadJobResponse:
    try:
        url = _validate_douyin_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    manager = _manager(request)
    try:
        job = manager.enqueue(url=url, max_items=payload.max_items)
    except asyncio.QueueFull as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The download queue is full; retry later",
            headers={"Retry-After": "30"},
        ) from exc
    return _job_response(job)


@app.get(
    "/api/v1/downloads/{job_id}",
    response_model=DownloadJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["downloads"],
)
async def get_download_status(job_id: str, request: Request) -> DownloadJobResponse:
    job = _manager(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job not found")
    return _job_response(job)


@app.get(
    "/api/v1/downloads/{job_id}/files/{file_id}",
    dependencies=[Depends(require_api_key)],
    name="download_file",
    tags=["downloads"],
)
async def download_file(job_id: str, file_id: int, request: Request) -> FileResponse:
    job = _manager(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job not found")
    if job.status != "succeeded":
        raise HTTPException(status_code=409, detail="The download is not ready")
    if file_id < 1 or file_id > len(job.files):
        raise HTTPException(status_code=404, detail="Downloaded file not found")

    path = job.files[file_id - 1].resolve()
    job_dir = (_manager(request).output_root / job.job_id).resolve()
    if not path.is_relative_to(job_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="Downloaded file not found")

    return FileResponse(
        path,
        filename=path.name,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def main() -> None:
    import uvicorn

    host = os.getenv("DOUYIN_API_HOST", "127.0.0.1")
    _assert_safe_bind(host)
    port = _env_int("DOUYIN_API_PORT", 8000)
    uvicorn.run("douyindl.api:app", host=host, port=port, workers=1)
