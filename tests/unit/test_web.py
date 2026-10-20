import base64

import httpx
import pytest

from jarvis.security.permissions import SecurityPolicy
from jarvis.tools.web import (
    BingResultParser,
    FetchWebpageTool,
    OpenWebsiteTool,
    ReadableTextParser,
    WebSearchTool,
    decode_bing_url,
)


def _bing_href(url: str) -> str:
    enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{enc}&ntb=1"


BING_HTML = f"""
<html><body><ol id="b_results">
<li class="b_algo"><div class="b_tpcn"><a href="https://x">site</a></div>
  <h2><a href="{_bing_href('https://example.org/a?b=1')}">Example <strong>Title</strong></a></h2>
  <div class="b_caption"><p class="b_lineclamp2">First &amp; best snippet.</p></div></li>
<li class="b_ad"><h2><a href="https://ads.example">Ad</a></h2></li>
<li class="b_algo"><h2><a href="https://plain.example/page">Second</a></h2>
  <div class="b_caption"><p>Second snippet</p></div></li>
</ol></body></html>
"""


def test_decode_bing_url():
    assert decode_bing_url(_bing_href("https://a.b/c?d=e")) == "https://a.b/c?d=e"
    assert decode_bing_url("https://direct.example/") == "https://direct.example/"


def test_bing_parser():
    p = BingResultParser()
    p.feed(BING_HTML)
    assert p.results == [
        {"title": "Example Title", "url": "https://example.org/a?b=1",
         "snippet": "First & best snippet."},
        {"title": "Second", "url": "https://plain.example/page", "snippet": "Second snippet"},
    ]


def test_readable_text_skips_scripts_and_nav():
    p = ReadableTextParser()
    p.feed("<html><head><title>T &amp; Co</title><script>var x=1</script></head><body>"
           "<nav>Menu</nav><h1>Heading</h1><p>Body   text</p><footer>foot</footer></body></html>")
    assert p.title == "T & Co"
    assert p.text() == "Heading\nBody text"


def test_url_policy():
    policy = SecurityPolicy([], blocked_domains=["bad.example"])
    assert policy.is_url_allowed("https://good.example/x")
    for url, why in [
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("https://bad.example/", "blocked"),
        ("https://sub.bad.example/", "blocked"),
        ("http://localhost:11434/api/tags", "private"),
        ("http://127.0.0.1/", "private"),
        ("http://192.168.1.1/admin", "private"),
        ("http://[::1]/", "private"),
        ("https://user:pw@good.example/", "credentials"),
    ]:
        reason = policy.url_rejection_reason(url)
        assert reason and why in reason.lower(), (url, reason)
    assert SecurityPolicy([], allow_private_network=True).is_url_allowed("http://localhost:8000/")


async def test_web_search_filters_policy_blocked_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "python"
        return httpx.Response(200, text=BING_HTML)

    policy = SecurityPolicy([], blocked_domains=["plain.example"])
    tool = WebSearchTool(policy, transport=httpx.MockTransport(handler))
    result = await tool.execute("c", query="python")
    assert result.success
    assert [r["url"] for r in result.data["results"]] == ["https://example.org/a?b=1"]


async def test_fetch_webpage_and_browser_fallback_on_403():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ok.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<title>OK</title><p>Hello</p>")
        return httpx.Response(403, text="go away")

    class FakeBrowser:
        async def fetch_text(self, url):
            return {"url": url, "status": 200, "title": "Via browser", "text": "rendered"}

    policy = SecurityPolicy([])
    tool = FetchWebpageTool(policy, transport=httpx.MockTransport(handler))
    ok = await tool.execute("c", url="https://ok.example/")
    assert ok.success and ok.data["title"] == "OK" and ok.data["text"] == "Hello"

    denied = await tool.execute("c", url="https://wall.example/")
    assert not denied.success and "403" in denied.error

    tool.browser = FakeBrowser()
    fallback = await tool.execute("c", url="https://wall.example/")
    assert fallback.success and fallback.data["title"] == "Via browser"


async def test_fetch_rejects_redirect_into_private_network():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="secret")

    tool = FetchWebpageTool(SecurityPolicy([]), transport=httpx.MockTransport(handler))
    result = await tool.execute("c", url="https://evil.example/")
    assert not result.success and "private" in result.error.lower()


@pytest.mark.parametrize("url,expected", [("youtube.com", "https://youtube.com"),
                                          ("https://a.example/x", "https://a.example/x")])
async def test_open_website(url, expected):
    calls = []

    async def launcher(argv):
        calls.append(argv)
        return 0, ""

    result = await OpenWebsiteTool(SecurityPolicy([]), launcher).execute("c", url=url)
    assert result.success and calls == [["gio", "open", expected]]


BRAVE_HTML = """
<div id="results">
<div class="snippet svelte-x" data-pos="0" data-type="web"><div class="result-body">
  <a href="https://en.wikipedia.org/wiki/Thing" class="l1"><div class="site-name">Wikipedia</div>
    <div class="title search-snippet-title" title="Thing - Wikipedia">Thing - Wikipedia</div></a>
  <div class="generic-snippet"><div class="content t-primary">
    <span class="t-secondary">3 weeks ago -</span> The <strong>Thing</strong> is great.</div></div>
</div></div>
<div class="snippet" data-type="video"><a href="https://video.example/">Video</a></div>
<div class="snippet" data-pos="1" data-type="web"><a href="https://b.example/">
  <div class="title">B title</div></a><div class="content">B snippet</div></div>
</div>
"""


def test_brave_parser():
    from jarvis.tools.web import BraveResultParser

    p = BraveResultParser()
    p.feed(BRAVE_HTML)
    assert p.results == [
        {"title": "Thing - Wikipedia", "url": "https://en.wikipedia.org/wiki/Thing",
         "snippet": "3 weeks ago - The Thing is great."},
        {"title": "B title", "url": "https://b.example/", "snippet": "B snippet"},
    ]


async def test_web_search_uses_brave_and_adds_page_extracts():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "search.brave.com":
            return httpx.Response(200, text=BRAVE_HTML)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=f"<title>{request.url.host}</title><p>Body of page</p>")

    transport = httpx.MockTransport(handler)
    policy = SecurityPolicy([])
    tool = WebSearchTool(policy, transport=transport,
                         fetcher=FetchWebpageTool(policy, transport=transport))
    result = await tool.execute("c", query="thing", max_results=1)
    assert result.data["engine"] == "brave"
    assert len(result.data["results"]) == 2  # at least MIN_RESULTS requested
    assert [p["text"] for p in result.data["page_extracts"]] == ["Body of page"] * 2
