#!/usr/bin/env python3
"""Smoke test the local Douyin download API and save video + JSON into Videos/."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DOUYIN_URL = "https://www.douyin.com/video/7683559195606512356"
API_BASE = os.environ.get("DOUYIN_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ.get("DOUYIN_API_KEY")
VIDEOS_DIR = Path(__file__).resolve().parent / "Videos"
POLL_INTERVAL_SECONDS = 2
JOB_TIMEOUT_SECONDS = 900


def request(url: str, *, method: str = "GET", body: dict | None = None):
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    try:
        return urlopen(req, timeout=30)
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401 and not API_KEY:
            raise RuntimeError(
                "API returned 401. If the service uses DOUYIN_API_KEY, set the same "
                "value in this shell before running the script."
            ) from exc
        raise RuntimeError(f"API request failed ({exc.code}): {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach API at {API_BASE}: {exc.reason}") from exc


def get_json(url: str) -> dict:
    with request(url) as response:
        return json.loads(response.read().decode("utf-8"))


def download_api_file(item: dict, destination: Path) -> None:
    file_url = urljoin(f"{API_BASE}/", item["download_url"])
    with request(file_url) as response, destination.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def safe_filename(name: str) -> str:
    # Use only the server-supplied basename; never allow a path from the API response.
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "douyin_7683559195606512356.mp4"
    if not Path(name).suffix:
        name += ".mp4"
    return name


def main() -> int:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    health = get_json(f"{API_BASE}/healthz")
    if health.get("status") != "ok":
        raise RuntimeError(f"API health check failed: {health}")
    print(f"API ready: {API_BASE}")

    with request(
        f"{API_BASE}/api/v1/downloads",
        method="POST",
        body={"url": DOUYIN_URL, "max_items": 1},
    ) as response:
        job = json.loads(response.read().decode("utf-8"))

    job_id = job["job_id"]
    print(f"Submitted download job: {job_id}")
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

    while job.get("status") not in {"succeeded", "failed"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Job {job_id} did not finish within {JOB_TIMEOUT_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)
        job = get_json(f"{API_BASE}/api/v1/downloads/{job_id}")
        print(f"Job status: {job.get('status')}")

    if job["status"] == "failed":
        raise RuntimeError(f"Download job failed: {job.get('error', 'unknown error')}")

    files = job.get("files") or []
    video = next((item for item in files if Path(item.get("name", "")).suffix.lower() == ".mp4"), None)
    if video is None:
        raise RuntimeError(f"Job succeeded but returned no MP4 file: {files}")
    video_stem = Path(video["name"]).stem
    metadata = next(
        (
            item
            for item in files
            if Path(item.get("name", "")).suffix.lower() == ".json"
            and Path(item["name"]).stem == video_stem
        ),
        None,
    )
    if metadata is None:
        raise RuntimeError(f"Job succeeded but returned no matching JSON sidecar: {files}")

    destination = VIDEOS_DIR / safe_filename(video["name"])
    metadata_destination = VIDEOS_DIR / safe_filename(metadata["name"])
    video_temp = destination.with_name(destination.name + ".part")
    metadata_temp = metadata_destination.with_name(metadata_destination.name + ".part")
    video_already_exists = destination.exists()
    for path in (metadata_destination, video_temp, metadata_temp):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")

    try:
        if not video_already_exists:
            download_api_file(video, video_temp)
        download_api_file(metadata, metadata_temp)

        size = destination.stat().st_size if video_already_exists else video_temp.stat().st_size
        if size == 0:
            raise RuntimeError("API returned an empty video file")

        metadata_data = json.loads(metadata_temp.read_text(encoding="utf-8"))
        required_fields = {
            "original_title",
            "original_description",
            "original_publish_date",
            "wechat_channels_published",
        }
        if not isinstance(metadata_data, dict):
            raise RuntimeError("The metadata JSON must contain a JSON object")
        missing_fields = required_fields - metadata_data.keys()
        if missing_fields or not isinstance(metadata_data.get("wechat_channels_published"), bool):
            raise RuntimeError(
                "The metadata JSON is missing expected fields or has an invalid publish flag: "
                f"missing={sorted(missing_fields)}"
            )

        if not video_already_exists:
            video_temp.replace(destination)
        try:
            metadata_temp.replace(metadata_destination)
        except Exception:
            if not video_already_exists:
                destination.unlink(missing_ok=True)
            raise
    finally:
        video_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)

    action = "Reused existing video" if video_already_exists else "Downloaded video"
    print(f"{action} ({size:,} bytes): {destination}")
    print(f"Saved metadata JSON to: {metadata_destination}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
