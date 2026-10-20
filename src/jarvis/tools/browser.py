"""Playwright-driven Chromium for doing things on web pages (reading, clicking, forms).

Elements are referenced by refs ("e12") that browser_read stamps onto the DOM as
data-jarvis-ref attributes. Refs are invalidated by the next browser_read or navigation.

Risk: GET forms (searches) are Tier 1; anything that submits a POST form, touches a form
with a password field, or is labelled like "Buy"/"Send"/"Delete" is Tier 2.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from jarvis.config import BrowserSettings
from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.security.permissions import SecurityPolicy
from jarvis.security.risk import destructive_keyword
from jarvis.security.validation import ValidationError
from jarvis.tools.base import BaseTool

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
MAX_ELEMENTS = 80
# Prefer the article/main region so extracts aren't spent on navigation menus.
_MAIN_TEXT_JS = """
() => {
  const el = document.querySelector('main, article, [role=main], #mw-content-text, #content')
    || document.body;
  return el ? el.innerText : '';
}
"""
MAX_TEXT_CHARS = 4000

_COLLECT_JS = """
(maxEls) => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary,' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem],' +
    '[role=option], [role=switch], [role=textbox], [role=combobox], [role=searchbox],' +
    '[contenteditable=""], [contenteditable=true]';
  document.querySelectorAll('[data-jarvis-ref]').forEach(e => e.removeAttribute('data-jarvis-ref'));
  const items = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2) continue;
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const ref = 'e' + (++i);
    el.setAttribute('data-jarvis-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let name = el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.getAttribute('alt') ||
      (el.querySelector('img') && el.querySelector('img').alt) || el.value || '';
    const item = {ref, tag, name: name.replace(/\\s+/g, ' ').trim().slice(0, 80)};
    const role = el.getAttribute('role'); if (role) item.role = role;
    if (type) item.type = type;
    if (tag === 'a' && el.href) item.href = el.href.slice(0, 150);
    if (el.form) item.form = (el.form.getAttribute('method') || 'get').toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag) && type !== 'password' && el.value)
      item.value = String(el.value).slice(0, 60);
    item._inview = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    items.push(item);
  }
  items.sort((a, b) => (b._inview - a._inview));
  return items.slice(0, maxEls).map(it => {
    if (!it._inview) it.offscreen = true;
    delete it._inview; return it;
  });
}
"""

_DESCRIBE_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || (tag === 'button' ? 'submit' : '')).toLowerCase();
  const form = el.form || el.closest('form');
  const name = (el.getAttribute('aria-label') || el.innerText || el.value ||
    el.getAttribute('placeholder') || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
  return {
    tag, type, name: name.slice(0, 80),
    in_form: !!form,
    form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
    form_has_password: !!(form && form.querySelector('input[type=password]')),
    is_submit: !!form && ((tag === 'button' && type === 'submit') ||
                          (tag === 'input' && ['submit', 'image'].includes(type))),
    is_password: type === 'password',
  };
}
"""


class BrowserError(Exception):
    pass


class BrowserSession:
    """Lazily started persistent Chromium context (own profile, separate from the user's)."""

    def __init__(self, settings: BrowserSettings, policy: SecurityPolicy):
        self.settings = settings
        self.policy = policy
        self._pw = None
        self._context = None
        self._page = None
        self._headless = None  # separate invisible browser for background page reads
        self._lock = asyncio.Lock()

    async def _ensure_playwright(self) -> None:
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

    async def _start(self) -> None:
        await self._ensure_playwright()
        profile = Path(self.settings.profile_dir).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        self._context = await self._pw.chromium.launch_persistent_context(
            str(profile),
            headless=self.settings.headless,
            no_viewport=not self.settings.headless,
            args=["--start-maximized"],
        )
        self._context.set_default_timeout(15000)
        self._context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        await self._context.route("**/*", self._guard)
        self._context.on("page", self._on_new_page)
        self._context.on("close", lambda _ctx: self._reset())

    def _reset(self) -> None:
        self._context = None
        self._page = None

    async def _guard(self, route) -> None:
        """Block top-level navigations the URL policy forbids (also after redirects/clicks)."""
        request = route.request
        if request.is_navigation_request() and request.frame.parent_frame is None:
            reason = self.policy.url_rejection_reason(request.url)
            if reason and not request.url.startswith(("about:", "data:", "blob:")):
                log.warning("Blocked navigation to %s: %s", request.url, reason)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def _on_new_page(self, page) -> None:
        self._page = page  # follow new tabs/popups

    async def page(self):
        async with self._lock:
            if self._context is None:
                await self._start()
            if self._page is None or self._page.is_closed():
                open_pages = [p for p in self._context.pages if not p.is_closed()]
                self._page = open_pages[-1] if open_pages else await self._context.new_page()
            return self._page

    async def element(self, ref: str):
        page = await self.page()
        loc = page.locator(f'[data-jarvis-ref="{ref}"]')
        if await loc.count() == 0:
            raise BrowserError(f"Element {ref} not found (refs expire after navigation or a new "
                               "browser_read). Call browser_read again.")
        return loc.first

    async def snapshot(self, query: str | None = None, include_text: bool = True) -> dict:
        page = await self.page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001 - still read whatever is there
            pass
        elements = await page.evaluate(_COLLECT_JS, MAX_ELEMENTS * 3)
        if query:
            q = query.lower()
            elements = [e for e in elements
                        if q in e.get("name", "").lower() or q in e.get("href", "").lower()]
        data: dict[str, Any] = {
            "url": page.url, "title": await page.title(), "elements": elements[:MAX_ELEMENTS],
        }
        if include_text:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            data["text"] = text[:MAX_TEXT_CHARS]
            data["text_truncated"] = len(text) > MAX_TEXT_CHARS
        return data

    async def fetch_text(self, url: str) -> dict:
        """Load a URL in an invisible browser and return its title/text (fetch fallback)."""
        async with self._lock:
            if self._headless is None:
                await self._ensure_playwright()
                browser = await self._pw.chromium.launch(headless=True)
                self._headless = await browser.new_context(user_agent=_USER_AGENT)
                await self._headless.route("**/*", self._guard)
        tab = await self._headless.new_page()
        try:
            resp = await tab.goto(url, wait_until="domcontentloaded")
            text = await tab.evaluate(_MAIN_TEXT_JS)
            return {"url": tab.url, "status": resp.status if resp else None,
                    "title": await tab.title(), "text": text}
        finally:
            await tab.close()

    async def close_matching(self, target: str) -> list[str]:
        """Close Jarvis-browser tabs whose title or URL contains `target`. Returns titles."""
        if self._context is None:
            return []
        q = target.lower()
        closed = []
        for page in list(self._context.pages):
            if page.is_closed():
                continue
            title = await page.title()
            if q in title.lower() or q in page.url.lower():
                closed.append(title or page.url)
                await page.close()
        if self._page is not None and self._page.is_closed():
            self._page = None
        return closed

    async def close(self) -> None:
        for ctx in (self._context, self._headless):
            try:
                if ctx is not None:
                    await ctx.close()
                    if ctx.browser is not None:
                        await ctx.browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._headless = None
        if self._pw is not None:
            await self._pw.stop()
        self._pw = None
        self._reset()


def _fail(tool_call_id: str, error: str) -> ToolResult:
    return ToolResult(tool_call_id=tool_call_id, success=False, error=error)


async def _settle(page, timeout_ms: int = 8000) -> None:
    """Give a possible navigation time to start, then wait for it to load."""
    await asyncio.sleep(0.4)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - not every action navigates
        pass


async def _assess_element(session: BrowserSession, ref: str, verb: str,
                          submits: bool = False) -> tuple[RiskLevel, str]:
    try:
        el = await session.element(ref)
    except BrowserError as e:
        raise ValidationError(str(e)) from e
    info = await el.evaluate(_DESCRIBE_JS)
    label = f"{verb} {info['tag']} '{info['name']}'"
    if info["form_has_password"] and (submits or info["is_submit"]):
        return RiskLevel.HIGH, f"{label}: submits a login form"
    if (submits or info["is_submit"]) and info["form_method"] == "post":
        return RiskLevel.HIGH, f"{label}: submits a form (POST)"
    keyword = destructive_keyword(info["name"])
    if keyword:
        return RiskLevel.HIGH, f"{label}: looks high-impact ('{keyword}')"
    return RiskLevel.LOW, label


class BrowserOpenTool(BaseTool):
    name = "browser_open"
    description = (
        "Navigate Jarvis's automated browser to a URL so it can read or interact with the page."
    )
    risk_level = RiskLevel.LOW
    url_arguments = ("url",)
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL."}},
        "required": ["url"],
    }

    def __init__(self, session: BrowserSession):
        self.session = session

    async def execute(self, tool_call_id: str, url: str) -> ToolResult:
        try:
            page = await self.session.page()
            resp = await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001 - playwright raises its own error types
            return _fail(tool_call_id, f"Navigation failed: {e}".splitlines()[0])
        status = resp.status if resp else None
        return ToolResult(
            tool_call_id=tool_call_id,
            success=status is None or status < 400,
            error=None if status is None or status < 400 else f"HTTP {status}",
            data={"url": page.url, "title": await page.title(), "status": status},
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success:
            return False
        page = await self.session.page()
        return page.url == execution_result.data["url"] and page.url != "about:blank"


class BrowserReadTool(BaseTool):
    name = "browser_read"
    description = (
        "Read the current page in Jarvis's browser: URL, title, visible text and interactive "
        "elements with refs (e.g. e5) for browser_click / browser_type."
    )
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Only elements matching this text."},
            "include_text": {"type": "boolean", "description": "Include page text (default true)."},
        },
    }

    def __init__(self, session: BrowserSession):
        self.session = session

    async def execute(
        self, tool_call_id: str, query: str | None = None, include_text: bool = True
    ) -> ToolResult:
        try:
            data = await self.session.snapshot(query, include_text)
        except Exception as e:  # noqa: BLE001
            return _fail(tool_call_id, f"Could not read page: {e}".splitlines()[0])
        return ToolResult(tool_call_id=tool_call_id, success=True, data=data)

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class BrowserClickTool(BaseTool):
    name = "browser_click"
    description = "Click an element (link, button, checkbox…) in Jarvis's browser by its ref."
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "description": "Element ref from browser_read."}},
        "required": ["ref"],
    }

    def __init__(self, session: BrowserSession):
        self.session = session

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        return await _assess_element(self.session, arguments["ref"], "click")

    async def execute(self, tool_call_id: str, ref: str) -> ToolResult:
        try:
            page = await self.session.page()
            before = page.url
            el = await self.session.element(ref)
            await el.click()
            await _settle(page)
            page = await self.session.page()  # may have switched to a new tab
        except BrowserError as e:
            return _fail(tool_call_id, str(e))
        except Exception as e:  # noqa: BLE001
            return _fail(tool_call_id, f"Click failed: {e}".splitlines()[0])
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"ref": ref, "url_before": before, "url_after": page.url,
                                "title": await page.title()})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class BrowserTypeTool(BaseTool):
    name = "browser_type"
    description = (
        "Type text into an input field in Jarvis's browser by ref, optionally pressing Enter "
        "(e.g. to run a search)."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Input element ref from browser_read."},
            "text": {"type": "string"},
            "press_enter": {"type": "boolean", "description": "Press Enter afterwards."},
        },
        "required": ["ref", "text"],
    }

    def __init__(self, session: BrowserSession):
        self.session = session

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        return await _assess_element(self.session, arguments["ref"], "type into",
                                     submits=bool(arguments.get("press_enter")))

    async def execute(
        self, tool_call_id: str, ref: str, text: str, press_enter: bool = False
    ) -> ToolResult:
        try:
            el = await self.session.element(ref)
            info = await el.evaluate(_DESCRIBE_JS)
            if info["is_password"]:
                return _fail(tool_call_id, "Jarvis does not type passwords. Ask the user to "
                                           "log in themselves.")
            await el.fill(text)
            page = await self.session.page()
            if press_enter:
                await el.press("Enter")
                await _settle(page)
                page = await self.session.page()
                return ToolResult(tool_call_id=tool_call_id, success=True,
                                  data={"ref": ref, "submitted": True, "url": page.url})
            value = await el.input_value() if info["tag"] in ("input", "textarea", "select") \
                else text
        except BrowserError as e:
            return _fail(tool_call_id, str(e))
        except Exception as e:  # noqa: BLE001
            return _fail(tool_call_id, f"Typing failed: {e}".splitlines()[0])
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"ref": ref, "expected": text, "value": value})

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success:
            return False
        data = execution_result.data
        return data.get("submitted") or data.get("value") == data.get("expected")


class SubmitWebFormTool(BaseTool):
    name = "submit_web_form"
    description = "Submit the form containing the given element (always requires approval)."
    risk_level = RiskLevel.HIGH
    parameters = {
        "type": "object",
        "properties": {"ref": {"type": "string", "description": "A field or button in the form."}},
        "required": ["ref"],
    }

    def __init__(self, session: BrowserSession):
        self.session = session

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        _, context = await _assess_element(self.session, arguments["ref"], "submit form via",
                                           submits=True)
        return RiskLevel.HIGH, context

    async def execute(self, tool_call_id: str, ref: str) -> ToolResult:
        try:
            page = await self.session.page()
            before = page.url
            el = await self.session.element(ref)
            in_form = await el.evaluate("(el) => !!(el.form || el.closest('form'))")
            if not in_form:
                return _fail(tool_call_id, "Element is not inside a form.")
            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                    await el.evaluate("(el) => (el.form || el.closest('form')).requestSubmit()")
            except Exception:  # noqa: BLE001 - JS-handled forms may not navigate
                pass
            page = await self.session.page()
            still_there = await page.locator(f'[data-jarvis-ref="{ref}"]').count() > 0
        except BrowserError as e:
            return _fail(tool_call_id, str(e))
        except Exception as e:  # noqa: BLE001
            return _fail(tool_call_id, f"Submit failed: {e}".splitlines()[0])
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"url_before": before, "url_after": page.url,
                                "form_still_present": still_there})

    async def verify(self, execution_result: ToolResult) -> bool:
        # A submit should navigate or replace the form; if neither happened, report it.
        data = execution_result.data or {}
        return execution_result.success and (
            data.get("url_after") != data.get("url_before") or not data.get("form_still_present")
        )


def build_browser_tools(session: BrowserSession) -> list[BaseTool]:
    return [
        BrowserOpenTool(session),
        BrowserReadTool(session),
        BrowserClickTool(session),
        BrowserTypeTool(session),
        SubmitWebFormTool(session),
    ]
