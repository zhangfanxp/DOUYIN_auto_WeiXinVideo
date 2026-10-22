from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright


TAOBAO_HOME_URL = "https://myseller.taobao.com/"
logger = logging.getLogger("taobao_publisher.automation")


def _fresh_video_form_url(current_url: str) -> str:
    """Keep Taobao's current editor route but remove a restored draft id."""
    outer = urlsplit(current_url)
    outer_params = parse_qsl(outer.query, keep_blank_values=True)
    for index, (key, embedded_url) in enumerate(outer_params):
        if key != "pub_url":
            continue
        embedded = urlsplit(embedded_url)
        embedded_params = parse_qsl(embedded.query, keep_blank_values=True)
        fresh_params = [
            (name, value)
            for name, value in embedded_params
            if name.casefold().replace("_", "") != "contentid"
        ]
        if len(fresh_params) == len(embedded_params):
            break
        fresh_embedded_url = urlunsplit(
            embedded._replace(query=urlencode(fresh_params), fragment="")
        )
        outer_params[index] = (key, fresh_embedded_url)
        scene_index = next(
            (i for i, (name, _) in enumerate(outer_params) if name == "pub_scene"),
            None,
        )
        if scene_index is None:
            outer_params.append(("pub_scene", "gg"))
        elif not outer_params[scene_index][1]:
            outer_params[scene_index] = ("pub_scene", "gg")
        return urlunsplit(outer._replace(query=urlencode(outer_params)))
    raise RuntimeError("淘宝恢复了已有视频草稿，但无法从发布页地址中移除草稿编号")


def is_video_upload_request(method: str, url: str) -> bool:
    """Return whether a request looks like media transfer rather than telemetry."""
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return False
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host == "log.aliyuncs.com" or host.endswith(".log.aliyuncs.com"):
        return False
    if "/logstores/" in path and path.rstrip("/").endswith("/track"):
        return False
    target = f"{host}{path}?{parsed.query}"
    return bool(
        re.search(
            r"upload|media|video|vod|chunk|file|(?:^|[.-])oss(?:[.-]|$)|\.(?:mp4|mov)(?:$|[/?#])",
            target,
            re.I,
        )
    )


class TaobaoBrowser:
    """Own one visible, persistent browser profile for the authorized seller account."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.home_page: Page | None = None
        self.login_task: asyncio.Task[None] | None = None
        self.prepared_pages: list[Page] = []
        self.prepared_forms: dict[str, tuple[Page, Any]] = {}
        self.product_attached_page_ids: set[int] = set()
        self.last_submit_button_info: dict[str, Any] | None = None
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
            if self.home_page is not None and not self.home_page.is_closed():
                try:
                    await self.home_page.bring_to_front()
                except Exception:
                    pass
            return
        if self.context and self.home_page and await self._looks_logged_in(self.home_page):
            self.status = "authenticated"
            return
        self.error = None
        self.status = "opening_browser"
        self.login_task = asyncio.create_task(self._open_login_browser())

    async def _open_login_browser(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            if self.context is None:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel=os.getenv("TAOBAO_BROWSER_CHANNEL", "chrome"),
                    headless=False,
                    no_viewport=True,
                    args=["--start-maximized"],
                )
            self.home_page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.status = "waiting_for_login"
            await self.home_page.goto(TAOBAO_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                await self.home_page.bring_to_front()
            except Exception:
                pass

            deadline = asyncio.get_running_loop().time() + 600
            while asyncio.get_running_loop().time() < deadline:
                if await self._looks_logged_in(self.home_page):
                    self.status = "authenticated"
                    self.error = None
                    return
                await asyncio.sleep(1)
            self.status = "login_timeout"
            self.error = "Login was not detected within 10 minutes; retry the login request."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            logger.exception("无法打开淘宝商家后台")

    async def _looks_logged_in(self, page: Page) -> bool:
        try:
            current_url = page.url.lower()
            if any(marker in current_url for marker in ("login", "passport", "alicert")):
                return False
            body = re.sub(r"\s+", "", await page.locator("body").inner_text(timeout=3_000))
            if any(marker in body for marker in ("扫码登录", "请登录", "密码登录", "验证码登录")):
                return False
            return "店铺" in body and any(
                marker in body
                for marker in ("消息", "商品", "内容中心", "营销", "首页", "订单", "商家工作台")
            )
        except Exception:
            return False

    async def is_authenticated(self) -> bool:
        if self.context is None:
            return False
        if self.home_page is None or self.home_page.is_closed():
            self.home_page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            if not self.home_page.url or self.home_page.url == "about:blank":
                await self.home_page.goto(TAOBAO_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        if await self._looks_logged_in(self.home_page):
            self.status = "authenticated"
            return True
        if self.status == "authenticated":
            self.status = "login_required"
        return False

    async def _wait_for_login(self, on_step: Callable[[str], None] | None) -> None:
        await self.start_login()
        if on_step:
            on_step("淘宝商家后台登录会话未就绪；请在弹出的浏览器中完成登录")
        deadline = asyncio.get_running_loop().time() + 600
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_authenticated():
                return
            if self.login_task and self.login_task.done() and self.login_task.exception():
                break
            await asyncio.sleep(1)
        raise RuntimeError(f"淘宝登录会话未就绪：{self.error or '请完成登录后重试'}")

    async def publish(
        self,
        payload: dict[str, Any],
        on_step: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if not await self.is_authenticated():
            await self._wait_for_login(on_step)
        assert self.context is not None

        video_path = Path(payload["video_path"])
        mode = payload["mode"]
        self.last_submit_button_info = None
        prepared_form = self._get_prepared_form(video_path) if mode == "publish" else None
        reusing_prepared_form = prepared_form is not None
        if prepared_form is None:
            page = await self.context.new_page()
            scope = page
        else:
            page, scope = prepared_form
        keep_page_open = False

        def report(step: str) -> None:
            if on_step:
                on_step(step)

        try:
            if reusing_prepared_form:
                report("正在复用已准备好的淘宝发布表单")
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                if payload.get("title", "").strip():
                    await self._fill_field(
                        scope,
                        ("视频标题", "标题", "加个标题"),
                        payload["title"],
                        field_kind="title",
                    )
                if payload.get("description", "").strip():
                    await self._fill_field(
                        scope,
                        ("视频描述", "描述", "详情", "简介"),
                        payload["description"],
                        field_kind="description",
                    )
                await self._select_creator_statement(scope)
            else:
                report("正在打开淘宝商家后台内容中心")
                await page.goto(TAOBAO_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                if not await self._wait_until_authenticated(page, report, timeout=120):
                    self.status = "login_required"
                    keep_page_open = True
                    self._retain_page(page)
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    raise RuntimeError(
                        "淘宝商家后台登录态未确认。请在淘宝发布服务打开的独立浏览器窗口登录后，再重试。"
                    )

                await self._open_content_center(page, report)
                await self._click_text(page, "发视频", exact=True)
                report("已进入视频发布流程，正在定位上传表单")
                page, scope = await self._wait_for_upload_form(self.context, page)
                if parse_qs(urlsplit(scope.url).query).get("contentId"):
                    report("淘宝恢复了已有视频草稿；正在打开新的空白发布表单")
                    fresh_form_url = _fresh_video_form_url(page.url)
                    logger.info("发现恢复草稿的 contentId，正在移除编号并打开空白表单")
                    await page.goto(
                        fresh_form_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    page, scope = await self._wait_for_upload_form(self.context, page)
                self.status = "authenticated"

                report(f"正在上传视频：{video_path.name}")
                upload_activity: dict[str, Any] = {"started": False, "pending": set(), "failed": []}

                def track_upload_request(request) -> None:
                    if is_video_upload_request(request.method, request.url):
                        upload_activity["started"] = True
                        upload_activity["pending"].add(request)

                def finish_upload_request(request) -> None:
                    upload_activity["pending"].discard(request)

                def fail_upload_request(request) -> None:
                    if request in upload_activity["pending"]:
                        upload_activity["failed"].append(request.url[:240])
                    finish_upload_request(request)

                def inspect_upload_response(response) -> None:
                    if response.request in upload_activity["pending"] and response.status >= 400:
                        upload_activity["failed"].append(
                            f"HTTP {response.status}: {response.url[:200]}"
                        )

                page.on("request", track_upload_request)
                page.on("response", inspect_upload_response)
                page.on("requestfinished", finish_upload_request)
                page.on("requestfailed", fail_upload_request)
                await self._upload_video(page, scope, video_path)
                await self._wait_for_upload_complete(page, scope, upload_activity, video_path.name)

                report("正在填写可选内容并选择创作者声明")
                if payload.get("title", "").strip():
                    await self._fill_field(
                        scope,
                        ("视频标题", "标题", "加个标题"),
                        payload["title"],
                        field_kind="title",
                    )
                if payload.get("description", "").strip():
                    await self._fill_field(
                        scope,
                        ("视频描述", "描述", "详情", "简介"),
                        payload["description"],
                        field_kind="description",
                    )
                await self._select_creator_statement(scope)

            if id(page) not in self.product_attached_page_ids:
                await self._add_first_product(page, scope, report)
                self.product_attached_page_ids.add(id(page))

            if mode == "prepare":
                keep_page_open = True
                self._retain_page(page)
                self.prepared_forms[str(video_path.resolve())] = (page, scope)
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                report("视频已上传；发布页已保留，等待人工复核，尚未提交")
                return {
                    "mode": mode,
                    "video": video_path.name,
                    "platform_state": "form_prepared",
                    "note": "视频已上传，创作者声明已选择“个人观点，仅供参考”，并已添加商品列表中的第一个商品；发布表单留在浏览器中，没有点击发布。",
                }

            if not payload.get("confirm_publish"):
                raise RuntimeError("发布模式缺少显式确认，未提交淘宝视频")
            button = await self._find_submit_button(scope)
            self.last_submit_button_info = await button.evaluate(
                """button => {
                  const rect = button.getBoundingClientRect();
                  const x = rect.left + rect.width / 2;
                  const y = rect.top + rect.height / 2;
                  const hit = button.ownerDocument.elementFromPoint(x, y);
                  const describe = node => node ? {
                    tag: node.tagName,
                    className: typeof node.className === 'string' ? node.className : '',
                    text: (node.innerText || '').trim().slice(0, 120),
                    containsButton: node === button || button.contains(node)
                  } : null;
                  return {
                    text: (button.innerText || '').trim(),
                    disabled: !!button.disabled,
                    ariaDisabled: button.getAttribute('aria-disabled'),
                    className: typeof button.className === 'string' ? button.className : '',
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    centerHit: describe(hit),
                    outerHTML: button.outerHTML.slice(0, 1600),
                    footerHTML: button.closest('.fixed-btn-container')?.outerHTML.slice(0, 2400) || null
                  };
                }"""
            )
            hit = self.last_submit_button_info.get("centerHit") or {}
            hit_status = "按钮中心可直接命中" if hit.get("containsButton") else "按钮中心疑似被覆盖"
            report(f"已定位淘宝“发布内容”按钮，{hit_status}；准备点击")
            logger.info("发布按钮诊断：%s", self.last_submit_button_info)
            await button.click(timeout=10_000)
            self.prepared_forms.pop(str(video_path.resolve()), None)
            report("已点击“发布内容”，正在检查淘宝二次确认")
            if await self._confirm_submit_dialog_if_present(scope, page):
                report("已确认淘宝弹窗，正在等待发布结果")
            else:
                report("未出现二次确认弹窗，正在等待淘宝发布结果")
            state, note = await self._wait_for_submit_result(scope, report)
            if state == "submit_clicked_unverified":
                keep_page_open = True
                self._retain_page(page)
                await self._save_page_diagnostics(page, scope)
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                report("页面未明确确认提交；发布页已保留，请检查结果后再决定是否重试")
            else:
                report("淘宝页面已确认接收发布提交；最终审核状态请到商家后台查看")
            return {
                "mode": mode,
                "video": video_path.name,
                "platform_state": state,
                "note": note,
            }
        except Exception as exc:
            report(f"执行失败：{type(exc).__name__}: {exc}")
            if not page.is_closed():
                keep_page_open = True
                self._retain_page(page)
                try:
                    await self._save_page_diagnostics(page, scope)
                    await page.bring_to_front()
                    report("错误页面诊断已保存，浏览器页面已保留供检查")
                except Exception as diagnostic_error:
                    logger.warning("保存淘宝页面诊断失败：%s", diagnostic_error)
            logger.exception("淘宝页面自动化失败")
            raise
        finally:
            if not keep_page_open:
                self.product_attached_page_ids.discard(id(page))
                if not page.is_closed():
                    await page.close()

    async def _open_content_center(self, page: Page, report: Callable[[str], None]) -> None:
        report("正在点击左侧“店铺”菜单")
        await self._click_text(page, "店铺", exact=True)
        await page.wait_for_timeout(400)
        report("正在点击“内容中心”菜单")
        await self._click_text(page, "内容中心", exact=True)
        await page.wait_for_timeout(500)

    async def _wait_until_authenticated(
        self,
        page: Page,
        report: Callable[[str], None],
        timeout: float,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        reported_login = False
        while asyncio.get_running_loop().time() < deadline:
            if await self._looks_logged_in(page):
                self.status = "authenticated"
                self.error = None
                return True
            if not reported_login:
                report("淘宝页面尚未确认登录；请在淘宝发布服务的独立浏览器窗口完成登录")
                reported_login = True
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            await page.wait_for_timeout(750)
        return False

    async def _click_text(self, page: Page, label: str, exact: bool) -> None:
        candidates = page.get_by_text(label, exact=exact)
        for index in range(await candidates.count()):
            item = candidates.nth(index)
            try:
                if await item.is_visible() and await item.is_enabled():
                    await item.click(timeout=5_000)
                    return
            except Exception:
                continue
        raise RuntimeError(f"淘宝页面未找到可点击的“{label}”入口")

    async def _wait_for_upload_form(self, context: BrowserContext, fallback_page: Page):
        """Find the actual video editor, including when Taobao embeds it in an iframe.

        The content-center shell also has a global search input and a hidden file input,
        so those generic controls are not enough to identify the publishing form.
        """
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            for page in reversed(context.pages):
                if page.is_closed():
                    continue
                for frame in page.frames:
                    try:
                        root = frame.locator("#publishContentContainer")
                        heading = frame.locator(".container-title")
                        if not await root.count() or not await root.first.is_visible():
                            continue
                        if not await heading.count():
                            continue
                        heading_text = re.sub(r"\s+", "", await heading.first.inner_text())
                        if heading_text == "发布视频":
                            return page, frame
                    except Exception:
                        # Taobao frequently replaces the embedded editor while it loads.
                        continue
            await asyncio.sleep(0.5)
        diagnostic_page = next(
            (candidate for candidate in reversed(context.pages) if not candidate.is_closed()),
            fallback_page,
        )
        await self._save_page_diagnostics(diagnostic_page, diagnostic_page)
        raise TimeoutError("淘宝视频发布表单没有加载；已检查当前页面和嵌套页面，诊断已保存")

    async def _upload_video(self, page: Page, scope, video_path: Path) -> None:
        file_input = None
        inputs = scope.locator('input[type="file"]')
        for index in range(await inputs.count()):
            candidate = inputs.nth(index)
            accept = (await candidate.get_attribute("accept") or "").lower()
            name = (await candidate.get_attribute("name") or "").lower()
            if "video" in accept or "video" in name or not accept:
                file_input = candidate
                break
        if file_input is None and await inputs.count() == 1:
            file_input = inputs.first
        if file_input is not None:
            await file_input.set_input_files(str(video_path), timeout=60_000)
            return

        for label in ("上传视频", "选择视频", "上传"):
            try:
                async with page.expect_file_chooser(timeout=2_000) as chooser_info:
                    candidates = scope.get_by_text(label, exact=False)
                    clicked = False
                    for index in range(await candidates.count()):
                        candidate = candidates.nth(index)
                        if await candidate.is_visible() and await candidate.is_enabled():
                            await candidate.click(timeout=5_000)
                            clicked = True
                            break
                    if not clicked:
                        continue
                await (await chooser_info.value).set_files(str(video_path))
                return
            except Exception:
                continue
        await self._save_page_diagnostics(page, scope)
        raise RuntimeError("淘宝视频上传控件没有找到；页面诊断已保存")

    async def _wait_for_upload_complete(
        self,
        page: Page,
        scope,
        upload_activity: dict[str, Any],
        file_name: str,
    ) -> None:
        timeout = max(60, int(os.getenv("TAOBAO_UPLOAD_TIMEOUT_SECONDS", "900")))
        deadline = asyncio.get_running_loop().time() + timeout
        stable_ready_samples = 0
        while asyncio.get_running_loop().time() < deadline:
            body = re.sub(r"\s+", "", await scope.locator("body").inner_text())
            failure = next((text for text in ("上传失败", "格式不支持", "文件过大") if text in body), None)
            if failure or upload_activity["failed"]:
                raise RuntimeError(f"淘宝视频上传失败：{failure or upload_activity['failed'][0]}")
            pending = any(text in body for text in ("上传中", "正在上传", "处理中"))
            fields = scope.locator("input:not([type=file]),textarea,[contenteditable=true]")
            ready = False
            for index in range(await fields.count()):
                try:
                    if await fields.nth(index).is_visible() and await fields.nth(index).is_enabled():
                        ready = True
                        break
                except Exception:
                    continue
            progress_count = await scope.locator('[role="progressbar"]:visible, [class*="progress"]:visible').count()
            visible_evidence = (
                file_name in body
                or await scope.locator("video:visible, [class*='video-preview']:visible, [class*='upload-success']:visible").count() > 0
            )
            if ready and not pending and progress_count == 0 and not upload_activity["pending"]:
                if upload_activity["started"] or visible_evidence:
                    stable_ready_samples += 1
                else:
                    # Require a stable form and visible selected-file evidence when the upload
                    # transport does not expose a recognizable request to Playwright.
                    stable_ready_samples = 0
            else:
                stable_ready_samples = 0
            if stable_ready_samples >= 3:
                return
            await asyncio.sleep(1)
        raise TimeoutError(f"淘宝视频上传或转码超过 {timeout} 秒仍未完成")

    async def _fill_field(self, scope, keywords: tuple[str, ...], value: str, field_kind: str) -> None:
        if not value.strip():
            return
        preferred_selectors = (
            (
                'input[placeholder="加个标题让内容更吸引人"]',
                ".short-title-form input",
                'input[placeholder*="加个标题"]',
            )
            if field_kind == "title"
            else (".rich-text-container-wrap [contenteditable='true']",)
        )
        for selector in preferred_selectors:
            preferred = scope.locator(selector)
            for index in range(await preferred.count()):
                field = preferred.nth(index)
                try:
                    if await field.is_visible() and await field.is_enabled():
                        await field.fill(value, timeout=10_000)
                        actual = (
                            await field.input_value()
                            if await field.evaluate("node => 'value' in node")
                            else await field.inner_text()
                        )
                        if actual.strip() != value.strip():
                            raise RuntimeError(f"淘宝表单“{keywords[0]}”内容回读不一致")
                        return
                except RuntimeError:
                    raise
                except Exception:
                    continue
        controls = scope.locator('input:not([type="file"]):not([type="hidden"]),textarea,[contenteditable="true"]')
        best: tuple[int, Any] | None = None
        for index in range(await controls.count()):
            field = controls.nth(index)
            try:
                if not await field.is_visible() or not await field.is_enabled():
                    continue
                info = await field.evaluate(
                    """node => {
                      const parent = node.closest('label') || node.parentElement;
                      return [node.getAttribute('placeholder'), node.getAttribute('aria-label'),
                        node.getAttribute('name'), node.id, parent && parent.innerText]
                        .filter(Boolean).join(' ').slice(0, 500);
                    }"""
                )
                normalized = re.sub(r"\s+", "", str(info))
                score = max((len(keyword) for keyword in keywords if keyword in normalized), default=0)
                if field_kind == "title" and any(word in normalized for word in ("搜索", "店铺名称", "商品名称")):
                    score = 0
                if field_kind == "description" and any(word in normalized for word in ("标题", "搜索")):
                    score = 0
                if field_kind == "description" and field.evaluate("node => node.isContentEditable"):
                    score = max(score, 1)
                if score and (best is None or score > best[0]):
                    best = (score, field)
            except Exception:
                continue
        if best is None and field_kind == "description":
            for index in range(await controls.count()):
                field = controls.nth(index)
                try:
                    if await field.is_visible() and await field.is_enabled() and await field.evaluate(
                        "node => node.tagName === 'TEXTAREA' || node.isContentEditable"
                    ):
                        best = (1, field)
                        break
                except Exception:
                    continue
        if best is None:
            raise RuntimeError(f"淘宝表单未找到“{keywords[0]}”输入框；页面诊断已保存")
        await best[1].fill(value, timeout=10_000)
        actual = await best[1].input_value() if await best[1].evaluate("node => 'value' in node") else await best[1].inner_text()
        if actual.strip() != value.strip():
            raise RuntimeError(f"淘宝表单“{keywords[0]}”内容回读不一致")

    async def _find_submit_button(self, scope):
        labels = ("发布内容", "发布视频", "确认发布", "立即发布", "发布")
        # Taobao keeps the red submit control in a fixed footer at the bottom of
        # the embedded editor. Bring that footer into view before resolving it.
        try:
            await scope.evaluate(
                """() => {
                  const nodes = [...document.querySelectorAll(
                    '#publishContentContainer, #publishContentContainer *'
                  )];
                  for (const node of nodes) {
                    const style = getComputedStyle(node);
                    if (node.scrollHeight > node.clientHeight + 2 &&
                        ['auto', 'scroll'].includes(style.overflowY)) {
                      node.scrollTop = node.scrollHeight;
                    }
                  }
                  const root = document.scrollingElement;
                  if (root) root.scrollTop = root.scrollHeight;
                }"""
            )
        except Exception:
            pass
        footer = scope.locator(".fixed-btn-container")
        try:
            if await footer.count():
                await footer.last.scroll_into_view_if_needed(timeout=5_000)
            else:
                await scope.locator("body").evaluate(
                    "body => { const root = body.ownerDocument.scrollingElement; "
                    "if (root) root.scrollTop = root.scrollHeight; }"
                )
        except Exception:
            pass

        # Prefer the real button DOM; Taobao's button can be missing from the
        # accessibility tree even though it is present and enabled in the form.
        buttons = scope.locator(".fixed-btn-container button, button")
        for index in range(await buttons.count()):
            candidate = buttons.nth(index)
            try:
                text = re.sub(r"\s+", "", await candidate.inner_text())
                if text not in labels:
                    continue
                await candidate.scroll_into_view_if_needed(timeout=5_000)
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except Exception:
                continue

        for label in labels:
            candidates = scope.get_by_role("button", name=label, exact=True)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    await candidate.scroll_into_view_if_needed(timeout=5_000)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        for label in labels:
            candidates = scope.get_by_text(label, exact=True)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    await candidate.scroll_into_view_if_needed(timeout=5_000)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        raise RuntimeError("淘宝视频表单中没有找到可用的发布按钮")

    async def _select_creator_statement(self, scope) -> None:
        label = "个人观点，仅供参考"
        candidates = (
            scope.get_by_label(label, exact=True),
            scope.get_by_role("radio", name=label, exact=True),
        )
        for controls in candidates:
            for index in range(await controls.count()):
                control = controls.nth(index)
                try:
                    if not await control.is_visible() or not await control.is_enabled():
                        continue
                    try:
                        if await control.is_checked():
                            return
                    except Exception:
                        if await control.get_attribute("aria-checked") == "true":
                            return
                    try:
                        await control.check(timeout=5_000)
                    except Exception:
                        await control.click(timeout=5_000)
                    if await self._creator_statement_is_selected(scope, label):
                        return
                except Exception:
                    continue

        labels = scope.get_by_text(label, exact=True)
        for index in range(await labels.count()):
            candidate = labels.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click(timeout=5_000)
                    if await self._creator_statement_is_selected(scope, label):
                        return
            except Exception:
                continue
        raise RuntimeError("淘宝创作者声明未能选中“个人观点，仅供参考”；页面诊断已保存")

    async def _creator_statement_is_selected(self, scope, label: str) -> bool:
        for controls in (
            scope.get_by_label(label, exact=True),
            scope.get_by_role("radio", name=label, exact=True),
        ):
            for index in range(await controls.count()):
                control = controls.nth(index)
                try:
                    if await control.is_checked():
                        return True
                except Exception:
                    if await control.get_attribute("aria-checked") == "true":
                        return True
        return False

    async def _add_first_product(self, page: Page, scope, report: Callable[[str], None]) -> None:
        """Add the first visible product from Taobao's product picker before publishing."""
        report("正在打开“添加商品”并选择店铺商品列表中的第一个商品")
        add_labels = scope.get_by_text("添加商品", exact=True)
        add_target = None
        for index in range(await add_labels.count() - 1, -1, -1):
            candidate = add_labels.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    add_target = candidate
                    break
            except Exception:
                continue
        if add_target is None:
            await self._save_page_diagnostics(page, scope)
            raise RuntimeError("淘宝发布表单中没有找到“添加商品”入口；页面诊断已保存")
        await add_target.click(timeout=8_000)

        picker = None
        picker_selectors = (
            '[role="dialog"]:visible',
            ".next-dialog:visible",
            ".next-overlay-wrapper:visible",
            ".next-drawer:visible",
            '[class*="modal"]:visible',
            '[class*="drawer"]:visible',
        )
        picker_markers = (
            "选择商品",
            "选择宝贝",
            "关联商品",
            "搜索商品",
            "搜索宝贝",
            "商品列表",
            "商品名称",
            "宝贝列表",
            "宝贝名称",
            "已选商品",
        )
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline and picker is None:
            roots = [scope]
            if page is not scope:
                roots.append(page)
            try:
                roots.extend(frame for frame in page.frames if frame not in roots)
            except Exception:
                pass
            for root in roots:
                for selector in picker_selectors:
                    candidates = root.locator(selector)
                    try:
                        for index in range(await candidates.count()):
                            candidate = candidates.nth(index)
                            if not await candidate.is_visible():
                                continue
                            text = re.sub(r"\s+", "", await candidate.inner_text(timeout=1_000))
                            if selector.startswith('[role="dialog"]') or any(
                                marker in text for marker in picker_markers
                            ):
                                picker = candidate
                                break
                    except Exception:
                        continue
                    if picker is not None:
                        break
                if picker is not None:
                    break
            if picker is None:
                await asyncio.sleep(0.25)

        if picker is None:
            await self._save_page_diagnostics(page, scope)
            raise RuntimeError("点击“添加商品”后未找到淘宝商品选择面板；页面诊断已保存")

        selected_label = await self._select_first_product_in_picker(picker, scope)
        if selected_label is None:
            await self._save_page_diagnostics(page, scope)
            raise RuntimeError("淘宝商品选择面板中没有找到可选择的商品；页面诊断已保存")

        report(f"已选择第一个商品：{selected_label[:80]}；正在确认添加")
        confirm_labels = ("确定添加", "添加商品", "确认选择", "确定", "确认", "完成", "选好了")
        confirm_button = None
        for label in confirm_labels:
            candidates = picker.get_by_role("button", name=label, exact=True)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if await candidate.is_visible() and await candidate.is_enabled():
                        confirm_button = candidate
                        break
                except Exception:
                    continue
            if confirm_button is not None:
                break
        if confirm_button is None:
            buttons = picker.locator("button, [role=button]")
            for index in range(await buttons.count()):
                candidate = buttons.nth(index)
                try:
                    label = re.sub(r"\s+", "", await candidate.inner_text())
                    if label in confirm_labels and await candidate.is_visible() and await candidate.is_enabled():
                        confirm_button = candidate
                        break
                except Exception:
                    continue

        if confirm_button is not None:
            await confirm_button.click(timeout=8_000)
            await page.wait_for_timeout(400)
        else:
            # Some versions add the item as soon as the row is selected and close
            # the picker without a separate confirmation button.
            try:
                if await picker.is_visible():
                    await self._save_page_diagnostics(page, scope)
                    raise RuntimeError("已选中商品，但淘宝面板没有可用的确认按钮；页面诊断已保存")
            except RuntimeError:
                raise
            except Exception:
                pass

    async def _select_first_product_in_picker(self, picker, fallback_scope=None) -> str | None:
        row_selectors = (
            "tbody tr",
            '[role="row"]',
            '[class*="product-item"]',
            '[class*="goods-item"]',
            '[class*="productCard"]',
            '[class*="goodsCard"]',
            "li",
        )
        selection_selectors = (
            'input[type="checkbox"]',
            'input[type="radio"]',
            '[role="checkbox"]',
            '[role="radio"]',
            ".next-checkbox-wrapper",
            ".next-radio-wrapper",
        )

        async def is_selected(control) -> bool:
            try:
                if await control.is_checked():
                    return True
            except Exception:
                pass
            try:
                if await control.get_attribute("aria-checked") == "true":
                    return True
            except Exception:
                pass
            try:
                class_name = await control.get_attribute("class") or ""
                return bool(re.search(r"(?:^|[-_ ])(?:checked|selected)(?:$|[-_ ])", class_name))
            except Exception:
                return False

        async def activate(control, context=None) -> bool:
            """Handle native inputs and Taobao's visually styled checkbox wrappers."""
            if await is_selected(control):
                return True

            try:
                native = await control.evaluate(
                    "node => node.tagName === 'INPUT' && ['checkbox', 'radio'].includes(node.type)"
                )
            except Exception:
                native = False

            if native:
                try:
                    await control.check(timeout=2_000, force=True)
                except Exception:
                    try:
                        # HTMLElement.click() dispatches the browser's normal
                        # click/change events for React-controlled checkboxes.
                        await control.evaluate("node => node.click()")
                    except Exception:
                        pass
                await asyncio.sleep(0.1)
                if await is_selected(control):
                    return True
            else:
                try:
                    await control.click(timeout=2_000, force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.1)
                if await is_selected(control):
                    return True

            # Some versions hide the native input and handle clicks on a label
            # or a nearby styled wrapper instead.
            wrapper_candidates = []
            try:
                label = control.locator("xpath=ancestor::label[1]")
                if await label.count():
                    wrapper_candidates.append(label.first)
            except Exception:
                pass
            for level in range(1, 5):
                try:
                    xpath = "/".join([".."] * level)
                    ancestor = control.locator(f"xpath={xpath}")
                    if await ancestor.count():
                        ancestor = ancestor.first
                        role = await ancestor.get_attribute("role")
                        class_name = await ancestor.get_attribute("class") or ""
                        if role == "checkbox" or "checkbox" in class_name or "radio" in class_name:
                            wrapper_candidates.append(ancestor)
                except Exception:
                    continue

            for candidate in wrapper_candidates:
                try:
                    if not await candidate.is_visible():
                        continue
                    await candidate.click(timeout=2_000, force=True)
                except Exception:
                    continue
                await asyncio.sleep(0.1)
                if await is_selected(control):
                    return True

            # Last resort: click the product card containing the control.
            if context is not None:
                try:
                    await context.click(timeout=2_000, force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.1)
                if await is_selected(control):
                    return True
            return False

        for selector in row_selectors:
            rows = picker.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                try:
                    if not await row.is_visible():
                        continue
                    row_text = re.sub(r"\s+", " ", await row.inner_text()).strip()
                    normalized = re.sub(r"\s+", "", row_text)
                    if len(normalized) < 3 or any(
                        marker in normalized for marker in ("全选", "暂无商品", "没有商品")
                    ) or (
                        "商品名称" in normalized
                        and any(marker in normalized for marker in ("价格", "库存", "操作", "销量"))
                    ):
                        continue
                    for control_selector in selection_selectors:
                        controls = row.locator(control_selector)
                        for control_index in range(await controls.count()):
                            control = controls.nth(control_index)
                            try:
                                if not await control.is_visible() and control_selector.startswith("input["):
                                    try:
                                        if await control.is_checked():
                                            return row_text
                                    except Exception:
                                        pass
                                checked = False
                                checked = await is_selected(control)
                                if checked or await activate(control, row):
                                    return row_text
                            except Exception:
                                continue
                    select_buttons = row.get_by_role("button", name="选择", exact=True)
                    if await select_buttons.count():
                        await select_buttons.first.click(timeout=4_000)
                        return row_text
                    if selector in {
                        'tbody tr',
                        '[class*="product-item"]',
                        '[class*="goods-item"]',
                        '[class*="productCard"]',
                        '[class*="goodsCard"]',
                    }:
                        # Card based pickers sometimes expose selection on the
                        # card itself instead of rendering a checkbox or radio.
                        await row.click(timeout=4_000)
                        return row_text
                except Exception:
                    continue

        # Taobao's current picker renders each product as a free-form card
        # (not a table/list row). Locate the first visible price, walk up to
        # its product card, and select the checkbox inside that card.
        roots = [picker]
        if fallback_scope is not None and fallback_scope is not picker:
            roots.append(fallback_scope)
        for root in roots:
            try:
                prices = root.get_by_text(re.compile(r"¥\s*[\d,.]+"))
                for price_index in range(await prices.count()):
                    price = prices.nth(price_index)
                    if not await price.is_visible():
                        continue
                    for level in range(1, 9):
                        try:
                            xpath = "/".join([".."] * level)
                            card = price.locator(f"xpath={xpath}")
                            card_text = re.sub(r"\s+", " ", await card.inner_text()).strip()
                            if not card_text or not re.search(r"¥\s*[\d,.]+", card_text):
                                continue
                            controls = card.locator(
                                'input[type="checkbox"], [role="checkbox"], .next-checkbox-wrapper'
                            )
                            for control_index in range(await controls.count()):
                                control = controls.nth(control_index)
                                if await activate(control, card):
                                    return card_text[:300]
                        except Exception:
                            continue
            except Exception:
                continue

        # Final fallback: picker versions with a single product checkbox but
        # no useful row/card markup. Restrict this to the picker or its editor
        # iframe; never search the top-level merchant page's unrelated controls.
        for root in roots:
            controls = root.locator('input[type="checkbox"], [role="checkbox"]')
            for index in range(await controls.count()):
                control = controls.nth(index)
                try:
                    native = await control.evaluate(
                        "node => node.tagName === 'INPUT' && node.type === 'checkbox'"
                    )
                    if not await control.is_visible() and not native:
                        continue
                    context_text = re.sub(
                        r"\s+",
                        "",
                        await control.evaluate(
                            "node => (node.closest('label') || node.parentElement)?.innerText || ''"
                        ),
                    )
                    if any(marker in context_text for marker in ("全选", "筛选", "搜索", "仅看")):
                        continue
                    if not context_text and await controls.count() > 1:
                        continue
                    if await activate(control):
                        # Empty wrapper labels are common in this picker. The
                        # product is still unambiguous when there is one checkbox.
                        return context_text or "第一个商品"
                except Exception:
                    continue
        return None

    async def _wait_for_submit_result(
        self, scope, report: Callable[[str], None], timeout: float = 45
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + timeout
        next_progress = asyncio.get_running_loop().time() + 10
        success_markers = ("发布成功", "提交成功", "视频已发布", "已提交审核")
        error_markers = ("发布失败", "提交失败", "标题不能为空", "内容不能为空", "上传失败", "请完善必填")
        page = scope if isinstance(scope, Page) else scope.page
        scope_detached = False
        while asyncio.get_running_loop().time() < deadline:
            bodies = []
            sources = [scope, page]
            # The Taobao editor can remove or replace its cross-origin iframe
            # immediately after accepting the submit click. Inspect any new
            # frames as well as the parent page for a toast or result message.
            try:
                sources.extend(frame for frame in page.frames if frame not in sources)
            except Exception:
                pass
            for source in sources:
                try:
                    bodies.append(await source.locator("body").inner_text(timeout=2_000))
                except Exception as exc:
                    if "frame was detached" in str(exc).lower() and not scope_detached:
                        scope_detached = True
                        logger.info("淘宝提交后发布 iframe 已刷新或关闭；继续检查页面结果")
                    continue
            body = re.sub(r"\s+", "", "\n".join(bodies))
            error = next((marker for marker in error_markers if marker in body), None)
            if error:
                raise RuntimeError(f"淘宝页面提示提交失败：{error}")
            success = next((marker for marker in success_markers if marker in body), None)
            if success:
                return "submitted", f"淘宝页面显示“{success}”；审核状态请在商家后台查看。"
            now = asyncio.get_running_loop().time()
            if now >= next_progress:
                report("淘宝发布按钮已点击，页面仍未显示结果；继续等待确认")
                next_progress = now + 10
            await asyncio.sleep(1)
        if scope_detached:
            return (
                "submit_clicked_unverified",
                "淘宝在点击发布后刷新或关闭了发布页面，但外层页面没有显示明确结果；请到商家后台核实后再决定是否重试。",
            )
        return "submit_clicked_unverified", "已点击发布，但淘宝页面未在等待时间内显示确认结果。"

    async def _confirm_submit_dialog_if_present(self, scope, page: Page | None = None) -> bool:
        deadline = asyncio.get_running_loop().time() + 3
        roots = [scope]
        if page is not None and page is not scope:
            roots.append(page)
            try:
                roots.extend(frame for frame in page.frames if frame not in roots)
            except Exception:
                pass
        detached_frame_seen = False
        while asyncio.get_running_loop().time() < deadline:
            if page is not None:
                try:
                    roots.extend(frame for frame in page.frames if frame not in roots)
                except Exception:
                    pass
            for root in roots:
                try:
                    dialogs = root.locator(
                        '[role="dialog"]:visible, .ant-modal:visible, [class*="modal"]:visible'
                    )
                    if await dialogs.count() == 0:
                        continue
                except Exception as exc:
                    if "frame was detached" in str(exc).lower():
                        detached_frame_seen = True
                    continue
                dialog = dialogs.last
                for label in ("发布内容", "确认发布", "确定", "确认", "提交"):
                    buttons = dialog.get_by_role("button", name=label, exact=True)
                    try:
                        button_count = await buttons.count()
                    except Exception as exc:
                        if "frame was detached" in str(exc).lower():
                            detached_frame_seen = True
                        continue
                    for index in range(button_count):
                        button = buttons.nth(index)
                        try:
                            if await button.is_visible() and await button.is_enabled():
                                await button.click(timeout=5_000)
                                return True
                        except Exception:
                            continue
            await asyncio.sleep(0.2)
        if detached_frame_seen:
            logger.info("淘宝提交后发布 iframe 已脱离；未发现需要二次确认的弹窗")
        return False

    async def _save_page_diagnostics(self, page: Page, scope) -> None:
        diagnostics_dir = self.profile_dir.parent / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        try:
            frames = []
            for frame in page.frames[:20]:
                try:
                    frame_body = (await frame.locator("body").inner_text(timeout=2_000))[:3000]
                    frames.append(
                        {
                            "url": frame.url,
                            "body_text": frame_body,
                            "has_publish_root": bool(
                                await frame.locator("#publishContentContainer").count()
                            ),
                        }
                    )
                except Exception as frame_error:
                    frames.append({"url": frame.url, "error": str(frame_error)[:300]})
            scope_error = None
            try:
                scope_body_text = (await scope.locator("body").inner_text(timeout=2_000))[:6000]
            except Exception as exc:
                scope_body_text = ""
                scope_error = str(exc)[:500]
            try:
                inputs = await scope.locator(
                    "input,textarea,[contenteditable=true],[role=radio]"
                ).evaluate_all(
                    "nodes => nodes.slice(0, 60).map(node => ({tag:node.tagName,type:node.type,"
                    "name:node.name,placeholder:node.placeholder,aria:node.getAttribute('aria-label'),"
                    "className:typeof node.className==='string'?node.className:'',"
                    "checked:!!node.checked,ariaChecked:node.getAttribute('aria-checked'),"
                    "parentText:(node.closest('label')?.innerText||node.parentElement?.innerText||'').slice(0,120),"
                    "ancestors:(()=>{let e=node,out=[];for(let i=0;e&&i<4;i++,e=e.parentElement)"
                    "out.push({tag:e.tagName,className:typeof e.className==='string'?e.className:'',"
                    "text:(e.innerText||'').slice(0,180)});return out})(),"
                    "text:(node.innerText||'').slice(0,120)}))"
                )
            except Exception as exc:
                inputs = []
                scope_error = scope_error or str(exc)[:500]
            try:
                buttons = await scope.locator("button").evaluate_all(
                    "nodes => nodes.slice(0, 60).map(node => ({text:(node.innerText||'').trim(),"
                    "disabled:!!node.disabled,aria:node.getAttribute('aria-label')}))"
                )
            except Exception as exc:
                buttons = []
                scope_error = scope_error or str(exc)[:500]
            data = {
                "url": page.url,
                "title": await page.title(),
                "body_text": (await page.locator("body").inner_text())[:6000],
                "scope_url": getattr(scope, "url", page.url),
                "scope_body_text": scope_body_text,
                "scope_error": scope_error,
                "frames": frames,
                "inputs": inputs,
                "buttons": buttons,
                "last_submit_button": self.last_submit_button_info,
            }
            (diagnostics_dir / "latest-taobao-page.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await page.screenshot(path=str(diagnostics_dir / "latest-taobao-page.png"), full_page=True)
        except Exception as exc:
            logger.warning("淘宝页面诊断无法完整保存：%s", exc)

    def _retain_page(self, page: Page) -> None:
        self.prepared_pages = [candidate for candidate in self.prepared_pages if not candidate.is_closed()]
        if page not in self.prepared_pages:
            self.prepared_pages.append(page)

    def _get_prepared_form(self, video_path: Path) -> tuple[Page, Any] | None:
        key = str(video_path.resolve())
        prepared = self.prepared_forms.get(key)
        if prepared is not None and prepared[0].is_closed():
            self.prepared_forms.pop(key, None)
            return None
        return prepared

    async def close(self) -> None:
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
