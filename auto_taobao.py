#!/usr/bin/env python3
"""Start the Douyin downloader API, Taobao publisher API, and Taobao WebUI."""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


PROJECT_ROOT = Path(__file__).resolve().parent
STARTUP_TIMEOUT_SECONDS = 120
HEALTH_CHECK_INTERVAL_SECONDS = 1
SHUTDOWN_TIMEOUT_SECONDS = 10
STOP_REQUESTED = Event()
HTTP = build_opener(ProxyHandler({}))


@dataclass
class Service:
    name: str
    cwd: Path
    command: list[str]
    health_url: str
    env: dict[str, str]
    process: subprocess.Popen[bytes] | None = None
    reused: bool = False


def _local_probe_host(bind_host: str) -> str:
    if bind_host in {"", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return bind_host.strip("[]")


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _health_url(host: str, port: int, path: str = "/healthz") -> str:
    return f"http://{_url_host(_local_probe_host(host))}:{port}{path}"


def _probe(url: str, timeout: float = 1.5) -> tuple[str, str]:
    """Return (healthy|offline|http_error|unavailable, detail)."""
    request = Request(url, headers={"User-Agent": "douyin-taobao-launcher/1.0"})
    try:
        with HTTP.open(request, timeout=timeout) as response:
            if response.status == 200:
                return "healthy", "HTTP 200"
            return "http_error", f"HTTP {response.status}"
    except HTTPError as exc:
        return "http_error", f"HTTP {exc.code}"
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
            return "offline", "连接被拒绝"
        return "unavailable", str(reason)
    except TimeoutError as exc:
        return "unavailable", str(exc) or "连接超时"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return "offline", "连接被拒绝"
        return "unavailable", str(exc)


def _video_dir_from_env(env: dict[str, str]) -> str:
    configured = [env.get("VIDEOS_DIR"), env.get("TAOBAO_VIDEO_DIR")]
    paths = []
    for value in configured:
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        paths.append(path.resolve())
    if len(paths) == 2 and paths[0] != paths[1]:
        raise ValueError("VIDEOS_DIR 和 TAOBAO_VIDEO_DIR 必须指向同一个目录")
    return str(paths[0] if paths else (PROJECT_ROOT / "Videos").resolve())


def _build_services(uv: str) -> list[Service]:
    env = os.environ.copy()

    downloader_host = env.get("DOUYIN_API_HOST", "127.0.0.1")
    downloader_port = int(env.get("DOUYIN_API_PORT", "8000"))
    downloader_env = env.copy()
    # Douyin requests can fail when inherited shell proxy variables are used.
    downloader_env["NO_PROXY"] = "*"
    downloader_env["no_proxy"] = "*"

    taobao_host = env.get("TAOBAO_API_HOST", "127.0.0.1")
    taobao_port = int(env.get("TAOBAO_API_PORT", "8003"))
    taobao_env = env.copy()

    webui_host = env.get("WEBUI_HOST", "127.0.0.1")
    webui_port = int(env.get("TAOBAO_WEBUI_PORT", "7861"))
    webui_env = env.copy()

    video_dir = _video_dir_from_env(env)
    taobao_env["TAOBAO_VIDEO_DIR"] = video_dir
    webui_env["VIDEOS_DIR"] = video_dir
    webui_env.setdefault(
        "DOUYIN_API_BASE_URL",
        f"http://{_url_host(_local_probe_host(downloader_host))}:{downloader_port}",
    )
    webui_env.setdefault(
        "TAOBAO_API_BASE_URL",
        f"http://{_url_host(_local_probe_host(taobao_host))}:{taobao_port}",
    )

    return [
        Service(
            name="抖音下载 API",
            cwd=PROJECT_ROOT / "downloader",
            command=[uv, "run", "douyindl-api"],
            health_url=_health_url(downloader_host, downloader_port),
            env=downloader_env,
        ),
        Service(
            name="淘宝发布 API",
            cwd=PROJECT_ROOT / "publish_taobao",
            command=[uv, "run", "taobao-publisher-api"],
            health_url=_health_url(taobao_host, taobao_port),
            env=taobao_env,
        ),
        Service(
            name="淘宝版 WebUI",
            cwd=PROJECT_ROOT / "webui",
            command=[uv, "run", "python", "../app_taobao.py"],
            health_url=_health_url(webui_host, webui_port, "/"),
            env=webui_env,
        ),
    ]


def _preflight(services: list[Service]) -> None:
    """Reuse healthy services and reject ports occupied by unrelated processes."""
    for service in services:
        state, detail = _probe(service.health_url)
        if state == "healthy":
            service.reused = True
            print(f"[复用] {service.name} 已运行并通过检查：{service.health_url}", flush=True)
        elif state == "offline":
            print(f"[待启动] {service.name}：{service.health_url}", flush=True)
        else:
            raise RuntimeError(
                f"{service.name} 的端口无法用于启动（{service.health_url}，{detail}）。"
                "请检查端口占用，或调整对应端口环境变量。"
            )


def _start(service: Service) -> None:
    print(
        f"[启动] {service.name}：在 {service.cwd} 运行 {shlex.join(service.command)}",
        flush=True,
    )
    service.process = subprocess.Popen(
        service.command,
        cwd=service.cwd,
        env=service.env,
        start_new_session=(os.name == "posix"),
    )


def _wait_until_ready(service: Service, timeout: int = STARTUP_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "尚未响应"
    while time.monotonic() < deadline:
        if STOP_REQUESTED.is_set():
            raise InterruptedError("收到停止信号")
        assert service.process is not None
        exit_code = service.process.poll()
        if exit_code is not None:
            raise RuntimeError(f"{service.name} 启动后退出，退出码：{exit_code}")

        state, detail = _probe(service.health_url)
        if state == "healthy":
            print(f"[就绪] {service.name}：{service.health_url}", flush=True)
            return
        if state == "http_error":
            raise RuntimeError(f"{service.name} 健康检查返回 {detail}：{service.health_url}")
        last_detail = detail
        time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

    raise TimeoutError(
        f"等待 {service.name} 就绪超时（{timeout} 秒，最近状态：{last_detail}）。"
        "请查看上方服务日志。"
    )


def _terminate(service: Service) -> None:
    process = service.process
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return


def _stop_started_services(services: list[Service]) -> None:
    started = [service for service in services if service.process and service.process.poll() is None]
    if not started:
        return
    print("\n[停止] 正在关闭由 auto_taobao.py 启动的服务……", flush=True)
    for service in reversed(started):
        _terminate(service)

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    for service in reversed(started):
        assert service.process is not None
        remaining = max(0, deadline - time.monotonic())
        try:
            service.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(service.process.pid, signal.SIGKILL)
                else:
                    service.process.kill()
            except ProcessLookupError:
                pass
            service.process.wait()
        print(f"[已停止] {service.name}", flush=True)


def _wait_for_services(services: list[Service]) -> None:
    print("\n淘宝版服务已就绪。按 Ctrl+C 停止 auto_taobao.py 启动的服务。", flush=True)
    for service in services:
        if "WebUI" in service.name:
            print(f"淘宝版 WebUI 地址：{service.health_url}", flush=True)

    while not STOP_REQUESTED.wait(0.5):
        for service in services:
            if service.process is not None:
                exit_code = service.process.poll()
                if exit_code is not None:
                    raise RuntimeError(f"{service.name} 意外退出，退出码：{exit_code}")


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("找不到 uv，请先安装 uv 并确保它已加入 PATH。", file=sys.stderr)
        return 1

    try:
        services = _build_services(uv)
    except ValueError as exc:
        print(f"配置无效：{exc}", file=sys.stderr)
        return 2

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: STOP_REQUESTED.set())

    try:
        _preflight(services)
        for service in services:
            if service.reused:
                continue
            _start(service)
            _wait_until_ready(service)
        _wait_for_services(services)
        return 0
    except KeyboardInterrupt:
        STOP_REQUESTED.set()
        return 0
    except InterruptedError:
        return 0
    except Exception as exc:
        print(f"\n[错误] {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        _stop_started_services(services)


if __name__ == "__main__":
    raise SystemExit(main())
