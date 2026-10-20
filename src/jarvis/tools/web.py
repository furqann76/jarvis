"""Web search (Bing HTML results, no API key), page reading, and opening sites for the user."""

import asyncio
import base64
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.security.permissions import SecurityPolicy
from jarvis.tools.applications import Launcher, spawn_detached
from jarvis.tools.base import BaseTool

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
# Some sites (e.g. Wikipedia) reject browser-like UAs from non-browsers; retry honestly.
BOT_HEADERS = {"User-Agent": "JarvisDesktop/0.1 (local personal assistant; httpx)"}
MAX_PAGE_CHARS = 6000
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", unescape(text)).strip()


def decode_bing_url(href: str) -> str:
    """Bing wraps results as /ck/a?...&u=a1<base64url>; unwrap to the real URL."""
    parts = urlsplit(href)
    if parts.netloc.endswith("bing.com") and parts.path.startswith("/ck/"):
        u = parse_qs(parts.query).get("u", [""])[0]
        if u.startswith("a1"):
            payload = u[2:]
            try:
                return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                pass
    return href


class BingResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._depth = 0  # depth inside the current li.b_algo
        self._in_title = False
        self._in_snippet = False
        self._cur: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._cur = {"title": "", "url": "", "snippet": ""}
            self._depth = 1
            return
        if self._cur is None:
            return
        if tag in ("li", "div", "p", "h2", "a", "span", "ul", "cite", "strong", "b"):
            self._depth += tag in ("li", "div", "ul")
        if tag == "h2":
            self._in_title = True
        elif tag == "a" and self._in_title and not self._cur["url"]:
            self._cur["url"] = decode_bing_url(a.get("href") or "")
        elif tag == "p" and not self._cur["snippet"]:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        if tag == "h2":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag in ("li", "div", "ul"):
            self._depth -= 1
            if self._depth <= 0:
                if self._cur["url"].startswith("http") and self._cur["title"]:
                    self.results.append({k: _clean(v) for k, v in self._cur.items()})
                self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is None:
            return
        if self._in_title:
            self._cur["title"] += data
        elif self._in_snippet:
            self._cur["snippet"] += data


class ReadableTextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "tr", "br", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (_WS.sub(" ", line).strip() for line in "".join(self._chunks).splitlines())
        return "\n".join(line for line in lines if len(line) > 1)


class BraveResultParser(HTMLParser):
    """Organic results from search.brave.com (div.snippet[data-type=web])."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._cur: dict[str, str] | None = None
        self._depth = 0
        self._field: str | None = None
        self._field_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "snippet" in classes and a.get("data-type") == "web":
            self._finish()
            self._cur = {"title": "", "url": "", "snippet": ""}
            self._depth = 1
            return
        if self._cur is None:
            return
        if tag == "div":
            self._depth += 1
        if tag == "a" and not self._cur["url"] and (a.get("href") or "").startswith("http"):
            self._cur["url"] = a["href"]
        if tag == "div" and "title" in classes and not self._cur["title"]:
            self._field, self._field_depth = "title", self._depth
        elif tag == "div" and "content" in classes and not self._cur["snippet"]:
            self._field, self._field_depth = "snippet", self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None or tag != "div":
            return
        if self._field and self._depth == self._field_depth:
            self._field = None
        self._depth -= 1
        if self._depth <= 0:
            self._finish()

    def handle_data(self, data: str) -> None:
        if self._cur is not None and self._field:
            self._cur[self._field] += data

    def _finish(self) -> None:
        if self._cur and self._cur["url"] and self._cur["title"]:
            self.results.append({k: _clean(v) for k, v in self._cur.items()})
        self._cur = None
        self._field = None


SEARCH_ENGINES = (
    # (name, url, query param name, parser). Brave first: plain-HTTP Bing results are often
    # degraded/irrelevant for non-browser clients.
    ("brave", "https://search.brave.com/search", "q", BraveResultParser),
    ("bing", "https://www.bing.com/search", "q", BingResultParser),
)
MIN_RESULTS = 3
EXTRACT_PAGES = 2
EXTRACT_CHARS = 1800


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the web. Returns the top results (title, URL, snippet) plus the readable text "
        "of the top pages, so most questions can be answered directly from it. Use "
        "fetch_webpage to read another result in full."
    )
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        policy: SecurityPolicy,
        transport: httpx.AsyncBaseTransport | None = None,
        fetcher: "FetchWebpageTool | None" = None,
    ):
        self.policy = policy
        self.transport = transport
        self.fetcher = fetcher  # reads the top pages; None disables page extracts

    async def _search(self, client: httpx.AsyncClient, query: str) -> tuple[str, list, str]:
        errors = []
        for name, url, param, parser_cls in SEARCH_ENGINES:
            try:
                resp = await client.get(url, params={param: query})
            except httpx.HTTPError as e:
                errors.append(f"{name}: {type(e).__name__}")
                continue
            parser = parser_cls()
            parser.feed(resp.text)
            results = [r for r in parser.results if self.policy.is_url_allowed(r["url"])]
            if results:
                return name, results, ""
            errors.append(f"{name}: no results (HTTP {resp.status_code})")
        return "", [], "; ".join(errors)

    async def execute(self, tool_call_id: str, query: str, max_results: int = 6) -> ToolResult:
        max_results = max(MIN_RESULTS, max_results)  # one snippet is too little evidence
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True,
                                     transport=self.transport) as client:
            engine, results, error = await self._search(client, query)
        if not results:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Search failed: {error}")
        results = results[:max_results]
        data: dict[str, Any] = {"query": query, "engine": engine, "results": results}
        if self.fetcher is not None:
            pages = await asyncio.gather(*(
                asyncio.wait_for(self.fetcher.execute("extract", url=r["url"]), 15)
                for r in results[:EXTRACT_PAGES]
            ), return_exceptions=True)
            data["page_extracts"] = [
                {"url": p.data["url"], "title": p.data["title"],
                 "text": p.data["text"][:EXTRACT_CHARS]}
                for p in pages if isinstance(p, ToolResult) and p.success and p.data
            ]
        return ToolResult(tool_call_id=tool_call_id, success=True, data=data)

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class FetchWebpageTool(BaseTool):
    name = "fetch_webpage"
    description = "Download a web page and return its readable text (for reading articles, docs)."
    risk_level = RiskLevel.SAFE
    url_arguments = ("url",)
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL."}},
        "required": ["url"],
    }

    def __init__(
        self,
        policy: SecurityPolicy,
        transport: httpx.AsyncBaseTransport | None = None,
        browser: Any = None,  # BrowserSession: fallback for sites that refuse plain HTTP
    ):
        self.policy = policy
        self.transport = transport
        self.browser = browser

    async def _via_browser(self, tool_call_id: str, url: str, why: str) -> ToolResult:
        try:
            page = await self.browser.fetch_text(url)
        except Exception as e:  # noqa: BLE001
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"{why}; browser fallback failed: {e}".splitlines()[0])
        text = page["text"]
        ok = page["status"] is None or page["status"] < 400
        return ToolResult(
            tool_call_id=tool_call_id, success=ok,
            error=None if ok else f"HTTP {page['status']}",
            data={"url": page["url"], "status": page["status"], "title": page["title"],
                  "text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS},
        )

    async def execute(self, tool_call_id: str, url: str) -> ToolResult:
        reason = self.policy.url_rejection_reason(url)
        if reason:
            return ToolResult(tool_call_id=tool_call_id, success=False, error=reason)
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True,
                                         transport=self.transport) as client:
                resp = await client.get(url)
                if resp.status_code == 403:
                    resp = await client.get(url, headers=BOT_HEADERS)
        except httpx.HTTPError as e:
            why = f"Fetch failed: {type(e).__name__} {e}".strip()
            if self.browser is not None:
                return await self._via_browser(tool_call_id, url, why)
            return ToolResult(tool_call_id=tool_call_id, success=False, error=why)
        if resp.status_code in (403, 429) and self.browser is not None:
            return await self._via_browser(tool_call_id, url, f"HTTP {resp.status_code}")
        final_reason = self.policy.url_rejection_reason(str(resp.url))
        if final_reason:  # redirect into a blocked/private location
            return ToolResult(tool_call_id=tool_call_id, success=False, error=final_reason)
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Not a text page (content-type: {ctype or 'unknown'}).")
        if "html" in ctype:
            parser = ReadableTextParser()
            parser.feed(resp.text)
            title, text = _clean(parser.title), parser.text()
        else:
            title, text = "", resp.text
        return ToolResult(
            tool_call_id=tool_call_id,
            success=resp.status_code < 400,
            error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
            data={"url": str(resp.url), "status": resp.status_code, "title": title,
                  "text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS},
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class OpenWebsiteTool(BaseTool):
    name = "open_website"
    description = (
        "Open a URL in the user's default web browser for them to view (e.g. YouTube, a "
        "search results page, a news article). For automated page interaction use the "
        "browser_* tools instead."
    )
    risk_level = RiskLevel.LOW
    url_arguments = ("url",)
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL to open."}},
        "required": ["url"],
    }

    def __init__(self, policy: SecurityPolicy, launcher: Launcher = spawn_detached):
        self.policy = policy
        self.launcher = launcher

    async def execute(self, tool_call_id: str, url: str) -> ToolResult:
        if "://" not in url:
            url = "https://" + url
        reason = self.policy.url_rejection_reason(url)
        if reason:
            return ToolResult(tool_call_id=tool_call_id, success=False, error=reason)
        code, stderr = await self.launcher(["gio", "open", url])
        if code != 0:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Could not open browser: {stderr or code}")
        await asyncio.sleep(0)
        return ToolResult(tool_call_id=tool_call_id, success=True, data={"url": url})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


def build_web_tools(policy: SecurityPolicy, browser: Any = None) -> list[BaseTool]:
    fetcher = FetchWebpageTool(policy, browser=browser)
    return [WebSearchTool(policy, fetcher=fetcher), fetcher, OpenWebsiteTool(policy)]
