from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Locator, Page, Playwright, async_playwright


CHANNELS_LIST_URL = "https://channels.weixin.qq.com/platform/post/list"
CHANNELS_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
logger = logging.getLogger("channels_publisher.automation")


class ChannelsBrowser:
    """Owns one persistent, visible Chromium profile for the authorized account."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.home_page: Page | None = None
        self.login_task: asyncio.Task[None] | None = None
        self.prepared_pages: list[Page] = []
        self.status = "not_started"
        self.error: str | None = None

    def session_info(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "browser_open": self.context is not None,
            "prepared_forms_open": sum(not page.is_closed() for page in self.prepared_pages),
            "error": self.error,
            "profile_dir": str(self.profile_dir),
        }

    async def start_login(self) -> None:
        if self.login_task and not self.login_task.done():
            return
        if self.context and self.home_page and await self._looks_logged_in(self.home_page):
            self.status = "authenticated"
            return
        self.error = None
        self.status = "opening_browser"
        self.login_task = asyncio.create_task(self._open_login_browser())

    async def _open_login_browser(self) -> None:
        self.status = "opening_browser"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            if self.context is None:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel=os.getenv("CHANNELS_BROWSER_CHANNEL", "chrome"),
                    headless=False,
                    no_viewport=True,
                    args=["--start-maximized"],
                )
            self.home_page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.status = "waiting_for_login"
            await self.home_page.goto(
                CHANNELS_LIST_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            try:
                await self.home_page.bring_to_front()
            except Exception:
                pass

            deadline = asyncio.get_running_loop().time() + 600
            while asyncio.get_running_loop().time() < deadline:
                if await self._looks_logged_in(self.home_page):
                    self.status = "authenticated"
                    return
                await asyncio.sleep(1)
            self.status = "login_timeout"
            self.error = "Login was not detected within 10 minutes; retry the login request."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"[:500]

    async def _looks_logged_in(self, page: Page) -> bool:
        try:
            current_url = page.url.lower()
            if "login" in current_url or "passport" in current_url:
                return False
            if "/platform/" not in current_url:
                return False
            body = re.sub(r"\s+", "", "".join(await page.locator("body").all_inner_texts()))
            if any(marker in body for marker in ("扫码登录", "请登录", "微信登录")):
                return False
            return any(label in body for label in ("内容管理", "发表内容", "数据中心", "创作者中心"))
        except Exception:
            return False

    async def is_authenticated(self) -> bool:
        if self.context is None:
            return False
        if self.home_page is None or self.home_page.is_closed():
            self.home_page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            if not self.home_page.url or self.home_page.url == "about:blank":
                await self.home_page.goto(
                    CHANNELS_LIST_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
        if await self._looks_logged_in(self.home_page):
            self.status = "authenticated"
            return True
        if self.status == "authenticated":
            self.status = "login_required"
        return False

    async def publish(
        self,
        payload: dict[str, Any],
        on_step: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if not await self.is_authenticated():
            if on_step is not None:
                on_step("视频号浏览器会话未就绪，正在恢复登录状态；如出现扫码请先完成登录")
            await self.start_login()
            if self.login_task:
                await self.login_task
            if not await self.is_authenticated():
                detail = self.error or "请在视频号助手浏览器窗口完成登录后重试"
                raise RuntimeError(f"视频号登录会话未就绪：{detail}")

        page = await self.context.new_page()
        keep_page_open = False

        def report(step: str) -> None:
            if on_step is not None:
                on_step(step)

        try:
            report("正在打开视频号发表页面")
            await page.goto(
                CHANNELS_CREATE_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            report("发表页面已打开，正在检查登录状态")
            if "login" in page.url.lower() or "passport" in page.url.lower():
                self.status = "login_required"
                raise RuntimeError("The saved Video Channels login has expired; scan the QR code again")
            body = re.sub(r"\s+", "", "".join(await page.locator("body").all_inner_texts()))
            if any(marker in body for marker in ("扫码登录", "请登录", "微信登录")):
                self.status = "login_required"
                raise RuntimeError("The creator portal asks for login; scan the QR code in the service browser")

            form_scope = await self._find_form_scope(page)
            self.status = "authenticated"
            report("发布表单已加载，正在上传视频")
            upload_activity: dict[str, Any] = {
                "active": {},
                "pending_by_endpoint": {},
                "completed": [],
                "failed": [],
            }
            upload_request_sequence = 0
            upload_path_pattern = re.compile(
                r"upload|media|video|clip|file|transcod|material|chunk", re.I
            )

            def is_upload_request(request) -> bool:
                parts = urlsplit(request.url)
                return (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and bool(upload_path_pattern.search(parts.path))
                )

            def upload_endpoint_key(request) -> tuple[str, str, str]:
                parts = urlsplit(request.url)
                return request.method, parts.netloc, parts.path

            def track_upload_request(request) -> None:
                nonlocal upload_request_sequence
                if not is_upload_request(request):
                    return
                parts = urlsplit(request.url)
                upload_request_sequence += 1
                sequence = upload_request_sequence
                item = {
                    "method": request.method,
                    "path": parts.path,
                    "status": None,
                }
                upload_activity["active"][sequence] = item
                upload_activity["pending_by_endpoint"].setdefault(
                    upload_endpoint_key(request), []
                ).append(sequence)

            def track_upload_response(response) -> None:
                endpoint = upload_endpoint_key(response.request)
                for sequence in upload_activity["pending_by_endpoint"].get(endpoint, []):
                    activity = upload_activity["active"].get(sequence)
                    if activity is not None and activity["status"] is None:
                        activity["status"] = response.status
                        break

            def pop_upload_request(request) -> dict[str, Any] | None:
                endpoint = upload_endpoint_key(request)
                sequences = upload_activity["pending_by_endpoint"].get(endpoint, [])
                if not sequences:
                    return None
                sequence = sequences.pop(0)
                if not sequences:
                    upload_activity["pending_by_endpoint"].pop(endpoint, None)
                return upload_activity["active"].pop(sequence, None)

            def finish_upload_request(request) -> None:
                activity = pop_upload_request(request)
                if activity is not None:
                    upload_activity["completed"].append(activity)
                    del upload_activity["completed"][:-30]

            def fail_upload_request(request) -> None:
                activity = pop_upload_request(request)
                if activity is not None:
                    activity["error"] = str(request.failure or "request failed")[:240]
                    upload_activity["failed"].append(activity)

            page.on("request", track_upload_request)
            page.on("response", track_upload_response)
            page.on("requestfinished", finish_upload_request)
            page.on("requestfailed", fail_upload_request)
            upload_started_at = asyncio.get_running_loop().time()
            await self._upload_video(page, form_scope, Path(payload["video_path"]))
            upload_activity["file_attached"] = True
            report("视频文件已交给页面，正在填写描述和话题")
            await self._fill_description_and_tags(page, form_scope, payload)
            report("正在填写短标题")
            await self._fill_short_title(page, form_scope, payload.get("short_title"))
            report(f"正在设置视频标注：{payload.get('video_annotation', '无需标注')}")
            await self._select_video_annotation(form_scope, payload.get("video_annotation", "无需标注"))

            if payload.get("is_original"):
                report("正在设置原创声明")
                await self._declare_original(page, form_scope)

            report("正在等待视频上传和处理完成")
            try:
                await self._wait_for_upload(
                    page,
                    form_scope,
                    on_step=report,
                    upload_activity=upload_activity,
                    upload_started_at=upload_started_at,
                )
            finally:
                page.remove_listener("request", track_upload_request)
                page.remove_listener("response", track_upload_response)
                page.remove_listener("requestfinished", finish_upload_request)
                page.remove_listener("requestfailed", fail_upload_request)
            mode = payload["mode"]
            if mode == "prepare":
                keep_page_open = True
                self._retain_page(page)
                await page.bring_to_front()
                report("表单已准备完成，浏览器保持打开；没有提交")
                return {
                    "mode": mode,
                    "video": Path(payload["video_path"]).name,
                    "platform_state": "form_prepared",
                    "manual_review_required": True,
                    "note": "The publishing form remains open for review; no draft or post was submitted.",
                }

            button_text = "保存草稿" if mode == "draft" else "发表"
            exact_button_name = re.compile(rf"^\s*{re.escape(button_text)}\s*$")
            button_candidates = form_scope.locator("div.form-btns button").filter(
                has_text=exact_button_name
            )
            if await button_candidates.count() == 0:
                button_candidates = form_scope.get_by_role("button", name=button_text, exact=True)
            button = None
            button_deadline = asyncio.get_running_loop().time() + 10
            while asyncio.get_running_loop().time() < button_deadline and button is None:
                for index in range(await button_candidates.count()):
                    candidate = button_candidates.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        button = candidate
                        break
                if button is None:
                    await asyncio.sleep(0.2)
            if button is None:
                raise TimeoutError(f"视频号“{button_text}”按钮未显示或不可用")
            if not await button.is_enabled():
                raise RuntimeError(f"视频号“{button_text}”按钮当前不可用")

            click_audit = await button.evaluate(
                """node => {
                  node.__channelsPublisherClickCount = 0;
                  node.addEventListener('click', () => node.__channelsPublisherClickCount++, true);
                  const rect = node.getBoundingClientRect();
                  return {text: (node.innerText || '').trim(), disabled: node.disabled,
                    ariaDisabled: node.getAttribute('aria-disabled'),
                    className: String(node.className || ''),
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                      width: Math.round(rect.width), height: Math.round(rect.height)}};
                }"""
            )
            browser_dialog_messages: list[str] = []
            platform_responses: list[dict[str, Any]] = []
            platform_requests: list[dict[str, Any]] = []
            response_detail_tasks: set[asyncio.Task[None]] = set()

            def is_platform_request(request) -> bool:
                parts = urlsplit(request.url)
                return (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and (parts.hostname or "").endswith("weixin.qq.com")
                )

            def capture_platform_request(request) -> None:
                if not is_platform_request(request):
                    return
                parts = urlsplit(request.url)
                platform_requests.append({
                    "method": request.method,
                    "path": parts.path,
                })
                del platform_requests[:-30]

            async def capture_platform_response_detail(response, item: dict[str, Any]) -> None:
                # Keep only safe business-result fields; never persist request payloads,
                # cookies, or the full response body.
                if "post_clip_video" not in item["path"]:
                    return
                try:
                    body = await response.json()
                except Exception:
                    return

                safe_keys = {
                    "errcode", "err_code", "ret", "retcode", "code", "success",
                    "errmsg", "err_msg", "message", "msg", "state", "status",
                }
                summary: dict[str, Any] = {}
                pending = [body]
                for _ in range(3):
                    if not pending:
                        break
                    current = pending.pop(0)
                    if isinstance(current, dict):
                        for key, value in current.items():
                            if key.lower() in safe_keys and isinstance(value, (str, int, float, bool, type(None))):
                                summary[key] = str(value)[:240]
                            elif isinstance(value, dict):
                                pending.append(value)
                    elif isinstance(current, list):
                        pending.extend(value for value in current[:5] if isinstance(value, dict))
                if summary:
                    item["business_result"] = summary

            def capture_platform_response(response) -> None:
                request = response.request
                if not is_platform_request(request):
                    return
                parts = urlsplit(response.url)
                item = {
                    "method": request.method,
                    "path": parts.path,
                    "status": response.status,
                }
                platform_responses.append(item)
                del platform_responses[:-30]
                task = asyncio.create_task(capture_platform_response_detail(response, item))
                response_detail_tasks.add(task)
                task.add_done_callback(response_detail_tasks.discard)

            async def handle_browser_dialog(dialog) -> None:
                message = dialog.message.strip()
                browser_dialog_messages.append(message)
                report(f"视频号弹窗：{message or dialog.type}；已自动确认")
                await dialog.accept()

            page.on("dialog", handle_browser_dialog)
            page.on("request", capture_platform_request)
            page.on("response", capture_platform_response)
            form_frame_url = getattr(form_scope, "url", None)
            if form_frame_url:
                report(f"发布表单定位在视频号微前端 frame：{form_frame_url}")
            else:
                report("发布表单使用 Wujie 页面作用域；已启用投稿接口监测")
            report(
                f"已定位视频号“{button_text}”按钮：enabled={not click_audit['disabled']}，"
                f"位置=({click_audit['rect']['x']},{click_audit['rect']['y']})"
            )
            report(f"表单检查完成，正在点击“{button_text}”")
            await button.click(timeout=10_000)
            try:
                click_count = await button.evaluate("node => node.__channelsPublisherClickCount")
            except Exception:
                click_count = None
            if click_count is None:
                report("点击调用已返回，按钮元素已更新或页面已切换，正在等待视频号返回结果")
            else:
                report(f"点击调用已返回，按钮收到 {click_count} 次 DOM click 事件，正在等待视频号返回结果")
            platform_state, note = await self._wait_for_submit_result(
                page,
                report,
                dialog_messages=browser_dialog_messages,
                platform_responses=platform_responses,
                platform_requests=platform_requests,
            )
            if response_detail_tasks:
                await asyncio.gather(*response_detail_tasks, return_exceptions=True)
            if platform_state == "submit_clicked_unverified":
                try:
                    await self._save_page_diagnostics(
                        page, form_scope, platform_responses, platform_requests
                    )
                    report("未确认页面的截图和 DOM 已保存到 publisher/var/diagnostics")
                except Exception as diagnostic_error:
                    logger.warning("保存未确认页面诊断失败：%s", diagnostic_error)
                keep_page_open = True
                self._retain_page(page)
                await page.bring_to_front()
                report("没有检测到平台确认；浏览器页面已保留，请检查页面提示")
            elif platform_state == "submitted":
                report("视频号已确认接收提交；审核或公开状态仍需在平台查看")

            return {
                "mode": mode,
                "video": Path(payload["video_path"]).name,
                "platform_state": platform_state,
                "management_page_reached": any(
                    re.search(r"/(?:platform|micro/content)/post/list(?:/|$|\?)", frame.url.lower())
                    for frame in page.frames
                ),
                "note": note,
            }
        except Exception as exc:
            report(f"执行失败：{type(exc).__name__}: {exc}")
            if not page.is_closed():
                keep_page_open = True
                self._retain_page(page)
                try:
                    await self._save_page_diagnostics(page, page)
                    report("错误页面的截图和 DOM 已保存到 publisher/var/diagnostics")
                except Exception as diagnostic_error:
                    logger.warning("保存错误页面诊断失败：%s", diagnostic_error)
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            logger.exception("视频号页面自动化失败：%s", exc)
            raise
        finally:
            if not keep_page_open:
                await page.close()

    def _retain_page(self, page: Page) -> None:
        self.prepared_pages = [p for p in self.prepared_pages if not p.is_closed()]
        if page not in self.prepared_pages:
            self.prepared_pages.append(page)

    async def _wait_for_submit_result(
        self,
        page: Page,
        report: Callable[[str], None],
        dialog_messages: list[str] | None = None,
        platform_responses: list[dict[str, Any]] | None = None,
        platform_requests: list[dict[str, Any]] | None = None,
        timeout: float = 30,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + timeout
        confirmation_clicked = False
        success_markers = (
            "发表成功",
            "发布成功",
            "提交成功",
            "作品已提交",
            "视频已提交",
            "内容已提交",
            "已提交审核",
        )
        error_markers = (
            "发表失败",
            "发布失败",
            "提交失败",
            "上传失败",
            "内容不能为空",
            "标题不能为空",
            "视频不能为空",
            "请完善必填",
            "内容不符合",
            "包含违规",
            "校验失败",
        )

        feedback_script = """bodies => {
                  const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  return bodies.flatMap(body => [...body.querySelectorAll('*')])
                    .filter(el => visible(el) &&
                      (el.getAttribute('role') === 'alert' ||
                       /toast|message|alert|notice|error|warning|dialog|modal/i.test(String(el.className || ''))))
                    .map(el => (el.innerText || '').trim())
                    .filter(text => text && text.length < 800)
                    .slice(-30);
                }"""

        while asyncio.get_running_loop().time() < deadline:
            feedback: list[str] = []
            for frame in page.frames:
                try:
                    feedback.extend(await frame.locator("body").evaluate_all(feedback_script))
                except Exception:
                    # A frame can navigate while the platform handles the submit.
                    continue
            feedback_text = re.sub(r"\s+", "", " ".join(feedback))
            feedback_text += re.sub(r"\s+", "", " ".join(dialog_messages or []))
            error = next((marker for marker in error_markers if marker in feedback_text), None)
            if error:
                raise RuntimeError(f"视频号页面提示提交失败（{error}）：{feedback_text[-500:]}")
            success = next((marker for marker in success_markers if marker in feedback_text), None)
            if success:
                return "submitted", f"视频号页面提示“{success}”；最终审核状态请在平台查看。"
            management_url = next(
                (
                    frame.url
                    for frame in page.frames
                    if re.search(r"/(?:platform|micro/content)/post/list(?:/|$|\?)", frame.url.lower())
                ),
                None,
            )
            if management_url:
                return "submitted", "视频号页面已跳转到内容管理页，提交已被平台接收；最终审核状态请在平台查看。"

            if not confirmation_clicked:
                for frame in page.frames:
                    direct_publish = frame.get_by_role(
                        "button", name=re.compile(r"^\s*直接发表\s*$")
                    )
                    for index in range(await direct_publish.count()):
                        candidate = direct_publish.nth(index)
                        if await candidate.is_visible() and await candidate.is_enabled():
                            report("检测到“直接发表”确认按钮，正在完成二次确认")
                            await candidate.click(timeout=5_000)
                            confirmation_clicked = True
                            break
                    if confirmation_clicked:
                        break

                    dialogs = frame.locator(
                        "[role='dialog']:visible, .weui-desktop-dialog:visible, "
                        ".weui-desktop-dialog__wrp:visible, .ant-modal:visible"
                    )
                    for index in range(await dialogs.count()):
                        dialog = dialogs.nth(index)
                        dialog_text = re.sub(r"\s+", "", await dialog.inner_text())
                        if not any(
                            word in dialog_text
                            for word in (
                                "确认发表", "确认发布", "是否发表", "是否发布", "确定发表",
                                "直接发表", "直接发布",
                            )
                        ):
                            continue
                        confirm = dialog.get_by_role(
                            "button", name=re.compile("直接发表|直接发布|确认|确定|发表|发布")
                        )
                        if await confirm.count() > 0 and await confirm.last.is_visible():
                            report("检测到视频号二次确认框，正在确认提交")
                            await confirm.last.click(timeout=5_000)
                            confirmation_clicked = True
                            break
                    if confirmation_clicked:
                        break

            await asyncio.sleep(0.5)

        submit_requests = [
            item for item in (platform_requests or [])
            if item["path"].rstrip("/").endswith("/post_clip_video")
        ]
        if submit_requests:
            report("检测到视频号投稿接口请求：" + "；".join(
                f"{item['method']} {item['path']}" for item in submit_requests[-4:]
            ))
        else:
            report("未检测到视频号投稿接口 /post_clip_video 请求；按钮点击没有触发投稿提交")

        submission_responses = [
            item for item in (platform_responses or [])
            if "post_clip_video" in item["path"]
        ]
        if submission_responses:
            recent_responses = "；".join(
                f"{item['method']} {item['path']} → HTTP {item['status']}"
                + (f"，平台结果={item['business_result']}" if item.get("business_result") else "")
                for item in submission_responses[-8:]
            )
            report(f"视频号投稿接口响应：{recent_responses}")
        elif platform_responses:
            recent_responses = "；".join(
                f"{item['method']} {item['path']} → HTTP {item['status']}"
                for item in platform_responses[-5:]
            )
            report(f"点击后只有普通后台请求响应，没有投稿结果：{recent_responses}")
        else:
            report("点击后未观察到视频号非 GET 请求响应")
        return (
            "submit_clicked_unverified",
            "已点击提交，但 30 秒内没有收到视频号成功提示或内容管理页跳转。请检查保留的浏览器页面，再确认是否发布。",
        )

    async def _find_form_scope(self, page: Page):
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            # The editable form lives in Wujie's micro/content frame. The host
            # page can expose mirrored controls with the same labels; clicking
            # those records a DOM event but does not submit the real form.
            for frame in page.frames:
                if "micro/content/post/create" not in frame.url:
                    continue
                if await self._scope_has_publish_form(frame):
                    return frame

            # Fall back to the Wujie host only if Tencent changes the frame URL.
            embedded_bodies = page.locator("wujie-app body")
            for index in range(await embedded_bodies.count()):
                embedded_scope = embedded_bodies.nth(index)
                if await self._scope_has_publish_form(embedded_scope):
                    return embedded_scope

            if await self._scope_has_publish_form(page):
                return page
            await asyncio.sleep(0.5)
        raise TimeoutError("The Video Channels publishing form did not load")

    async def _scope_has_publish_form(self, scope) -> bool:
        for selector in ("div.input-editor", "div.form-btns button", 'input[type="file"]'):
            if await scope.locator(selector).count() > 0:
                return True
        return False

    async def _upload_video(self, page: Page, scope, video_path: Path) -> None:
        file_input = None
        for selector in ('input[type="file"][accept*="video"]', 'input[type="file"]'):
            candidates = scope.locator(selector)
            if await candidates.count() > 0:
                file_input = candidates.first
                break

        if file_input is None:
            upload_text = scope.get_by_text(
                re.compile(r"上传时长|20GB|MP4/H\.264"), exact=False
            ).first
            selectors = [
                "div.upload-area",
                "div.post-create-upload",
                'div[class*="upload"]',
                'button:has-text("上传视频")',
                'div:has-text("上传视频")',
            ]
            targets = [upload_text] if await upload_text.count() > 0 else []
            targets.extend(scope.locator(selector).first for selector in selectors)
            for target in targets:
                try:
                    if await target.count() == 0 or not await target.is_visible():
                        continue
                    try:
                        async with page.expect_file_chooser(timeout=3_000) as chooser_info:
                            await target.click(timeout=5_000)
                        await (await chooser_info.value).set_files(str(video_path))
                        return
                    except Exception:
                        # Some portal builds create a hidden input without opening a chooser.
                        pass
                    candidates = scope.locator('input[type="file"]')
                    if await candidates.count() > 0:
                        file_input = candidates.first
                        break
                except Exception:
                    continue

        if file_input is None:
            candidates = scope.locator('input[type="file"]')
            if await candidates.count() > 0:
                file_input = candidates.first
        if file_input is None:
            await self._save_page_diagnostics(page, scope)
            raise RuntimeError(
                "Video upload control did not appear; local page diagnostics were saved under publisher/var/diagnostics"
            )
        try:
            await file_input.wait_for(state="attached", timeout=10_000)
            await file_input.set_input_files(str(video_path), timeout=60_000)
        except Exception:
            await self._save_page_diagnostics(page, scope)
            raise

    async def _save_page_diagnostics(
        self,
        page: Page,
        scope,
        platform_responses: list[dict[str, Any]] | None = None,
        platform_requests: list[dict[str, Any]] | None = None,
    ) -> None:
        diagnostics_dir = self.profile_dir.parent / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        frame_box = None
        if hasattr(scope, "frame_element"):
            try:
                frame_box = await (await scope.frame_element()).bounding_box()
            except Exception:
                pass
        data: dict[str, Any] = {
            "url": page.url,
            "title": await page.title(),
            "viewport_size": page.viewport_size,
            "frame_box": frame_box,
            "scope_url": getattr(scope, "url", None),
            "upload_text_locator_count": await scope.get_by_text(
                re.compile(r"上传时长|20GB|MP4/H\.264"), exact=False
            ).count(),
            "scope_file_input_count": await scope.locator('input[type="file"]').count(),
            "upload_state": await self._upload_state_snapshot(scope),
            "submit_buttons": await scope.locator("div.form-btns button").evaluate_all(
                """nodes => nodes.map(node => {
                  const rect = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return {text: (node.innerText || '').trim(),
                    disabled: !!node.disabled,
                    ariaDisabled: node.getAttribute('aria-disabled'),
                    className: String(node.className || ''),
                    visible: !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none',
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
                    html: node.outerHTML.slice(0, 1400)};
                })"""
            ),
            "platform_responses_after_submit_click": platform_responses or [],
            "platform_requests_after_submit_click": platform_requests or [],
            "annotation_components": await scope.locator(
                ".form-item, [class*='mark-tag'], [class*='annotation'], [role='combobox'], [aria-haspopup]"
            ).evaluate_all(
                """nodes => nodes.slice(0, 80).map(node => {
                  const rect = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return {tag: node.tagName, cls: String(node.className || '').slice(0, 180),
                    role: node.getAttribute('role'), text: (node.innerText || node.textContent || '').trim().slice(0, 300),
                    visible: !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none',
                    html: node.outerHTML.slice(0, 1400)};
                })"""
            ),
            "frames": [],
        }
        for frame in page.frames:
            try:
                data["frames"].append({
                    "url": frame.url,
                    "body_text": "\n".join(await frame.locator("body").all_inner_texts())[:4000],
                    "file_inputs": await frame.locator('input[type="file"]').count(),
                    "upload_candidates": await frame.locator("body").first.evaluate(
                        """body => [...body.querySelectorAll('*')]
                          .filter(el => /上传|20GB|MP4|H\\.264/.test((el.innerText || el.textContent || '').trim()))
                          .slice(0, 20)
                          .map(el => ({tag: el.tagName, cls: String(el.className || '').slice(0, 160),
                            text: (el.innerText || el.textContent || '').trim().slice(0, 220),
                            html: el.outerHTML.slice(0, 800),
                            rect: (() => { const r = el.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })()}))"""
                    ),
                    "dom_summary": await frame.locator("body").first.evaluate(
                        """body => ({htmlLength: body.innerHTML.length,
                          topChildren: [...body.children].slice(0, 15).map(el => ({tag: el.tagName,
                            cls: String(el.className || '').slice(0, 120), html: el.outerHTML.slice(0, 300)})),
                          canvases: body.querySelectorAll('canvas').length,
                          openShadowRoots: [...body.querySelectorAll('*')].filter(el => el.shadowRoot).length,
                          customElements: [...body.querySelectorAll('*')].filter(el => el.tagName.includes('-')).slice(0, 30).map(el => el.tagName)})"""
                    ),
                    "buttons": await frame.locator("button").evaluate_all(
                        """nodes => nodes.slice(0, 100).map(node => {
                          const rect = node.getBoundingClientRect();
                          const style = getComputedStyle(node);
                          return {text: (node.innerText || '').trim(), title: node.title,
                            aria: node.getAttribute('aria-label'),
                            disabled: !!node.disabled,
                            visible: !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none',
                            className: String(node.className || '').slice(0, 160),
                            rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
                        })"""
                    ),
                })
            except Exception as exc:
                data["frames"].append({"url": frame.url, "inspection_error": str(exc)[:300]})
        path = diagnostics_dir / "latest-upload-page.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            await page.screenshot(path=str(diagnostics_dir / "latest-upload-page.png"), full_page=True)
        except Exception:
            pass

    async def _fill_description_and_tags(self, page: Page, scope, payload: dict[str, Any]) -> None:
        editor = scope.locator("div.input-editor").first
        if await editor.count() == 0:
            editor = scope.locator('[contenteditable="true"]').first
        await editor.wait_for(state="visible", timeout=20_000)
        await editor.fill(payload["description"])
        for topic in payload.get("topics", []):
            await editor.click()
            await page.keyboard.press("End")
            await page.keyboard.type(f" #{topic}")
            await page.keyboard.press("Space")
            await asyncio.sleep(0.3)

    async def _fill_short_title(self, page: Page, scope, short_title: str | None) -> None:
        if not short_title:
            return
        candidates = [
            scope.locator('input[placeholder*="短标题"]'),
            scope.locator('input[aria-label*="短标题"]'),
            scope.locator('xpath=//*[normalize-space(text())="短标题"]/following::input[@type="text"][1]'),
            page.locator('input[placeholder*="短标题"]'),
        ]
        for candidate in candidates:
            try:
                if await candidate.count() > 0:
                    await candidate.first.fill(short_title)
                    return
            except Exception:
                continue
        raise RuntimeError("Could not find the short-title field in the publishing form")

    async def _declare_original(self, page: Page, scope) -> None:
        item = scope.locator(".form-item").filter(has_text="声明原创").first
        wrapper = item.locator("label.ant-checkbox-wrapper").first
        if await wrapper.count() == 0:
            raise RuntimeError("Could not find the original-content declaration checkbox")
        if "checked" not in (await wrapper.get_attribute("class") or ""):
            await wrapper.click(force=True)
        dialog = page.locator('.weui-desktop-dialog:has-text("原创权益")').first
        if await dialog.count() > 0 and await dialog.is_visible():
            agreement = dialog.locator("label.ant-checkbox-wrapper").first
            if await agreement.count() > 0:
                await agreement.click(force=True)
            confirm = dialog.locator('button:has-text("声明原创")').first
            await confirm.wait_for(state="visible", timeout=10_000)
            await confirm.click()

    async def _select_video_annotation(self, scope, annotation: str) -> None:
        all_options = scope.locator(".mark-tag-option, .option-main")
        matching_options = all_options.filter(has_text=annotation)

        # The portal keeps its option list in the DOM while the dropdown is
        # collapsed. If the requested option is already selected, there is
        # nothing to open or click.
        for index in range(await matching_options.count()):
            candidate = matching_options.nth(index)
            if await self._is_video_annotation_selected(candidate):
                return

        async def visible_option():
            candidate = scope.locator(
                ".mark-tag-option:visible, .option-main:visible"
            ).filter(has_text=annotation).first
            if await candidate.count() > 0 and await candidate.is_visible():
                return candidate
            return None

        option = await visible_option()
        if option is None:
            # Click the annotation control itself. Clicking the static label
            # often does not open the custom dropdown on newer portal builds.
            field = scope.locator(".form-item").filter(has_text="视频标注").last
            if await field.count() == 0:
                label = scope.get_by_text("视频标注", exact=True).last
                if await label.count() > 0:
                    field = label.locator("xpath=..")
            trigger_candidates = [
                field.locator(
                    "[role='combobox'], [aria-haspopup], button, input, "
                    "[class*='select'], [class*='mark-tag']"
                ),
                scope.get_by_role("combobox", name=re.compile("视频标注")),
            ]
            for triggers in trigger_candidates:
                for index in range(await triggers.count()):
                    trigger = triggers.nth(index)
                    try:
                        if not await trigger.is_visible():
                            continue
                        await trigger.click(timeout=2_000)
                    except Exception:
                        continue
                    await asyncio.sleep(0.2)
                    option = await visible_option()
                    if option is not None:
                        break
                if option is not None:
                    break

            if option is None and await field.count() > 0:
                try:
                    await field.click(timeout=2_000)
                    await asyncio.sleep(0.3)
                    option = await visible_option()
                except Exception:
                    pass

        if option is None:
            # Keep a text-based fallback for portal builds that render options
            # without the known option classes.
            text_option = scope.get_by_text(annotation, exact=True).last
            if await text_option.count() > 0 and await text_option.is_visible():
                option = text_option

        if option is None:
            if isinstance(scope, Page):
                await self._save_page_diagnostics(scope, scope)
            raise RuntimeError(f"Could not open or find visible video annotation option: {annotation}")

        if not await self._is_video_annotation_selected(option):
            await option.click(timeout=5_000)
        if not await self._is_video_annotation_selected(option):
            raise RuntimeError(f"Video annotation did not become selected: {annotation}")

    async def _is_video_annotation_selected(self, option) -> bool:
        try:
            return await option.evaluate(
                """node => {
                  let current = node;
                  for (let depth = 0; current && depth < 4; depth++, current = current.parentElement) {
                    const classes = typeof current.className === 'string' ? current.className : '';
                    if (/(^|\\s)(checked|active|selected|is-selected)(\\s|$)/i.test(classes)) return true;
                    if (current.getAttribute('aria-checked') === 'true' ||
                        current.getAttribute('aria-selected') === 'true' ||
                        current.getAttribute('data-selected') === 'true') return true;
                  }
                  return false;
                }"""
            )
        except Exception:
            return False

    async def _wait_for_upload(
        self,
        page: Page,
        scope,
        on_step: Callable[[str], None] | None = None,
        upload_activity: dict[str, Any] | None = None,
        upload_started_at: float | None = None,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 600
        started = upload_started_at or asyncio.get_running_loop().time()
        next_report = started + 10
        ready_since: float | None = None
        minimum_wait_seconds = 8
        stable_ready_seconds = 5
        snapshot_failures = 0
        last_snapshot_error: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await self._upload_state_snapshot(scope)
                snapshot_failures = 0
                last_snapshot_error = None
            except Exception as exc:
                state = None
                snapshot_failures += 1
                last_snapshot_error = f"{type(exc).__name__}: {exc}"
                if snapshot_failures == 1:
                    logger.warning(
                        "读取视频上传状态失败（连续第 %s 次）：%s",
                        snapshot_failures,
                        last_snapshot_error,
                    )
                if snapshot_failures >= 5:
                    raise RuntimeError(
                        "连续 5 次无法读取视频上传状态：" + last_snapshot_error
                    ) from exc

            now = asyncio.get_running_loop().time()
            if state:
                if state["error_texts"]:
                    message = "；".join(state["error_texts"])
                    if on_step is not None:
                        on_step(f"视频号页面报告上传失败：{message}")
                    raise RuntimeError(f"视频号视频上传失败：{message}")

            activity = upload_activity or {"active": {}, "failed": [], "completed": []}
            failed_requests = activity.get("failed", [])
            if failed_requests:
                failure = failed_requests[-1]
                raise RuntimeError(
                    "视频上传请求失败："
                    f"{failure.get('method')} {failure.get('path')}，{failure.get('error')}"
                )
            failed_responses = [
                request for request in activity.get("completed", [])
                if request.get("status") is not None and request["status"] >= 400
            ]
            if failed_responses:
                failure = failed_responses[-1]
                raise RuntimeError(
                    "视频上传接口返回错误："
                    f"{failure.get('method')} {failure.get('path')}，HTTP {failure['status']}"
                )

            if state:
                active_requests = len(activity.get("active", {}))
                ready = (
                    (state["has_video_file"] or activity.get("file_attached", False))
                    and state["publish_button_enabled"]
                    and not state["upload_busy"]
                    and active_requests == 0
                )
                if ready:
                    if ready_since is None:
                        ready_since = now
                else:
                    ready_since = None

                elapsed = now - started
                stable_for = now - ready_since if ready_since is not None else 0
                if (
                    ready_since is not None
                    and elapsed >= minimum_wait_seconds
                    and stable_for >= stable_ready_seconds
                ):
                    completed_requests = len(activity.get("completed", []))
                    if state["success_texts"]:
                        source = "页面已显示“" + "、".join(state["success_texts"][:2]) + "”"
                    elif completed_requests:
                        source = f"平台相关上传/处理请求 {completed_requests} 个已结束"
                    else:
                        source = "页面未显示上传进度或处理中状态"
                    if on_step is not None:
                        on_step(
                            "已确认视频上传与处理完成："
                            f"{source}，发表按钮已连续可用 {stable_ready_seconds} 秒"
                        )
                    return

            if on_step is not None and now >= next_report:
                if state:
                    upload_state = (
                        "页面仍显示上传/处理进度"
                        if state["upload_busy"]
                        else "尚未同时满足文件就绪、按钮可用且请求结束"
                    )
                    progress = "、".join(state["progress_values"]) or "未显示百分比"
                    progress_source = state.get("upload_progress_source") or "未匹配上传控件"
                else:
                    upload_state = "正在读取视频上传状态"
                    progress = "未知"
                    progress_source = "不可用"
                active_requests = len((upload_activity or {}).get("active", {}))
                active_paths = list(dict.fromkeys(
                    item.get("path", "")
                    for item in (upload_activity or {}).get("active", {}).values()
                    if item.get("path")
                ))
                on_step(
                    f"视频仍在上传或处理中，已等待 {int(now - started)} 秒；"
                    f"{upload_state}，页面进度={progress}，进度来源={progress_source}，"
                    f"进行中的上传请求={active_requests}"
                    + (f"（{', '.join(active_paths[:3])}）" if active_paths else "")
                )
                next_report = now + 10
            await asyncio.sleep(1)
        raise TimeoutError("Video upload did not finish within 10 minutes")

    async def _upload_state_snapshot(self, scope) -> dict[str, Any]:
        script = (
            """body => {
              if (!body || typeof body.querySelectorAll !== 'function') {
                throw new Error('上传状态检测范围不是有效的 DOM 元素');
              }
              const visible = el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const bodyText = body.innerText || '';

              // Read progress only from the video upload control. Generic
              // progress/loading classes elsewhere on the form include cover
              // generation and animations, and their CSS widths are not upload percentages.
              const uploadControlCandidates = [...body.querySelectorAll(
                'button, [role="button"], a, [class*="upload"], [class*="Upload"], [class*="cancel"], [class*="Cancel"]'
              )].filter(visible);
              const cancelUpload = uploadControlCandidates.find(el =>
                (el.innerText || el.textContent || '').trim() === '取消上传');
              const cancelUploadVisible = /取消上传/.test(bodyText);
              let uploadRoot = null;
              if (cancelUpload) {
                let current = cancelUpload;
                for (let depth = 0; current && depth < 8; depth++, current = current.parentElement) {
                  const text = (current.innerText || '').trim();
                  if (text.includes('取消上传') && text.length <= 600) {
                    uploadRoot = current;
                    if (/\\d{1,3}(?:\\.\\d+)?\\s*%/.test(text)) break;
                  }
                }
              }
              if (!uploadRoot) {
                uploadRoot = [...body.querySelectorAll('[class*="upload"], [class*="Upload"]')]
                  .filter(el => visible(el) && (el.innerText || '').trim().length <= 300 &&
                    /\\d{1,3}(?:\\.\\d+)?\\s*%/.test(el.innerText || ''))
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || null;
              }
              const uploadRootText = uploadRoot ? (uploadRoot.innerText || '').trim() : '';
              const uploadPercentMatch = uploadRootText.match(/(\\d{1,3}(?:\\.\\d+)?)\\s*%/);
              const progressNodes = uploadRoot ? [
                ...(uploadRoot.matches('progress, [role="progressbar"]') ? [uploadRoot] : []),
                ...uploadRoot.querySelectorAll('progress, [role="progressbar"]')
              ].filter(visible).slice(0, 10) : [];
              const progress = progressNodes.map(el => {
                const cls = typeof el.className === 'string' ? el.className : '';
                const nowValue = el.getAttribute('aria-valuenow') ?? el.value;
                const maxValue = el.getAttribute('aria-valuemax') ?? el.max ?? 100;
                const now = Number(nowValue);
                const max = Number(maxValue);
                let percentage = Number.isFinite(now) && nowValue !== null && nowValue !== undefined
                  ? (max ? now / max * 100 : now) : null;
                if (percentage === null) {
                  const percentText = (el.innerText || '').match(/(\\d{1,3})\\s*%/);
                  if (percentText) percentage = Number(percentText[1]);
                }
                return {className: cls.slice(0, 160), text: (el.innerText || '').trim().slice(0, 80), percentage};
              });
              const busyPattern = /上传中|正在上传|视频处理中|正在处理|转码中|校验中|uploading|processing/i;
              const busyTexts = [...new Set(bodyText.split(/\\n+/).map(s => s.trim())
                .filter(s => s && s.length < 120 && busyPattern.test(s)))].slice(0, 10);
              const errorPattern = /上传失败|视频处理失败|转码失败|文件格式不支持|视频格式不支持|文件过大/i;
              const errorTexts = [...body.querySelectorAll('[role="alert"], [class*="error"], [class*="toast"], [class*="message"], [class*="toptips"]')]
                .filter(visible).map(el => (el.innerText || '').trim())
                .filter(s => s && s.length < 300 && errorPattern.test(s)).slice(-5);
              const progressBusy = cancelUploadVisible || progress.some(p => {
                const cls = p.className.toLowerCase();
                if (/loading|indeterminate|uploading/.test(cls)) return true;
                if (p.percentage !== null) return p.percentage < 100;
                return /active|uploading/.test(cls);
              });
              const fileInputs = [...body.querySelectorAll('input[type="file"]')];
              const fileSelected = fileInputs.some(input => [...(input.files || [])]
                .some(file => /\\.(mp4|m4v|mov)$/i.test(file.name)));
              const videoPreviewReady = [...body.querySelectorAll('video')].some(video =>
                visible(video) && video.readyState >= 1 && video.videoWidth > 0);
              const videoRemoveAction = [...body.querySelectorAll('button, [role="button"]')].some(button =>
                visible(button) && (button.innerText || '').trim() === '删除');
              const hasVideoFile = fileSelected || videoPreviewReady || videoRemoveAction;
              const videoButtons = [...body.querySelectorAll('button')].filter(button =>
                (button.innerText || '').trim() === '发表' && visible(button));
              const enabledButton = videoButtons.some(button => {
                const cls = typeof button.className === 'string' ? button.className : '';
                return !button.disabled && button.getAttribute('aria-disabled') !== 'true' && !/disabled/i.test(cls);
              });
              const successPattern = /上传成功|上传完成|视频处理完成|处理完成/i;
              const successTexts = [...new Set(bodyText.split(/\\n+/).map(s => s.trim())
                .filter(s => s && s.length < 120 && successPattern.test(s)))].slice(0, 10);
              return {
                has_video_file: hasVideoFile,
                file_selected: fileSelected,
                video_preview_ready: videoPreviewReady,
                video_remove_action: videoRemoveAction,
                publish_button_enabled: enabledButton,
                upload_busy: progressBusy || busyTexts.length > 0,
                progress_values: [...new Set([
                  ...(uploadPercentMatch ? [`${uploadPercentMatch[1]}%`] : []),
                  ...progress.map(p => p.percentage === null ? p.text : `${Math.round(p.percentage)}%`)
                ].filter(Boolean))],
                progress_indicators: progress,
                upload_cancel_visible: cancelUploadVisible,
                upload_progress_source: cancelUploadVisible
                  ? '页面显示取消上传'
                  : uploadRoot
                    ? `${uploadRoot.tagName.toLowerCase()}.${String(uploadRoot.className || '').slice(0, 100)}`
                    : '',
                busy_texts: busyTexts,
                success_texts: successTexts,
                error_texts: errorTexts
              };
            }"""
        )
        # A Frame/Page can safely locate its document body. A Locator may
        # already be `wujie-app body`; searching for another body below it
        # never matches, which used to make every snapshot fail silently.
        if isinstance(scope, Locator):
            return await scope.evaluate(script)
        return await scope.locator("body").first.evaluate(script)

    async def close(self) -> None:
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
