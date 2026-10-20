"""Real headless Chromium against a local test site. Skipped if Playwright's browser is missing."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from jarvis.config import BrowserSettings
from jarvis.models.schemas import RiskLevel
from jarvis.security.permissions import SecurityPolicy
from jarvis.tools.browser import (
    BrowserClickTool,
    BrowserOpenTool,
    BrowserReadTool,
    BrowserSession,
    BrowserTypeTool,
    SubmitWebFormTool,
)

pytestmark = pytest.mark.integration

PAGES = {
    "/": """<html><head><title>Home</title></head><body>
        <h1>Test shop</h1>
        <form action="/search" method="get"><input name="q" aria-label="Search"></form>
        <a href="/about">About us</a>
        <form action="/login" method="post">
          <input name="user" aria-label="User"><input type="password" name="pw" aria-label="Pw">
          <button>Log in</button></form>
        <form action="/order" method="post"><input name="qty" aria-label="Qty" value="1">
          <button>Place order</button></form>
        <a href="https://blocked.example/">Blocked link</a>
        </body></html>""",
    "/about": "<html><head><title>About</title></head><body><p>We sell things.</p></body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: str):
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/search":
            q = parse_qs(url.query).get("q", [""])[0]
            return self._send(f"<html><head><title>Results</title></head><body>"
                              f"<p>Results for {q}</p></body></html>")
        self._send(PAGES.get(url.path, "<p>404</p>"))

    def do_POST(self):  # noqa: N802
        self._send("<html><head><title>Done</title></head><body>Posted</body></html>")


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
async def session(tmp_path):
    policy = SecurityPolicy([], blocked_domains=["blocked.example"], allow_private_network=True)
    s = BrowserSession(BrowserSettings(headless=True, profile_dir=str(tmp_path / "profile")),
                       policy)
    try:
        await s.page()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Chromium unavailable: {e}")
    yield s
    await s.close()


def ref_named(snapshot, name):
    return next(e["ref"] for e in snapshot["elements"] if e["name"] == name)


async def test_browse_search_and_click(session, site):
    opened = await BrowserOpenTool(session).execute("c", url=site + "/")
    assert opened.success and opened.data["title"] == "Home"
    assert await BrowserOpenTool(session).verify(opened)

    snap = (await BrowserReadTool(session).execute("c")).data
    assert "Test shop" in snap["text"]
    search = ref_named(snap, "Search")

    typer = BrowserTypeTool(session)
    risk, _ = await typer.assess({"ref": search, "text": "lamps", "press_enter": True})
    assert risk == RiskLevel.LOW  # GET search form
    result = await typer.execute("c", ref=search, text="lamps", press_enter=True)
    assert result.success and "q=lamps" in result.data["url"]
    snap = (await BrowserReadTool(session).execute("c")).data
    assert "Results for lamps" in snap["text"]

    await BrowserOpenTool(session).execute("c", url=site + "/")
    snap = (await BrowserReadTool(session).execute("c")).data
    click = await BrowserClickTool(session).execute("c", ref=ref_named(snap, "About us"))
    assert click.success and click.data["url_after"].endswith("/about")


async def test_risky_forms_escalate(session, site):
    await BrowserOpenTool(session).execute("c", url=site + "/")
    snap = (await BrowserReadTool(session).execute("c")).data
    click = BrowserClickTool(session)

    risk, ctx = await click.assess({"ref": ref_named(snap, "Log in")})
    assert risk == RiskLevel.HIGH and "login" in ctx
    risk, ctx = await click.assess({"ref": ref_named(snap, "Place order")})
    assert risk == RiskLevel.HIGH
    risk, _ = await click.assess({"ref": ref_named(snap, "About us")})
    assert risk == RiskLevel.LOW

    typed = await BrowserTypeTool(session).execute("c", ref=ref_named(snap, "Pw"), text="x")
    assert not typed.success and "password" in typed.error

    qty = ref_named(snap, "Qty")
    submit = SubmitWebFormTool(session)
    assert (await submit.assess({"ref": qty}))[0] == RiskLevel.HIGH
    result = await submit.execute("c", ref=qty)
    assert result.success and result.data["url_after"].endswith("/order")
    assert await submit.verify(result)


async def test_stale_ref_and_blocked_navigation(session, site):
    await BrowserOpenTool(session).execute("c", url=site + "/")
    snap = (await BrowserReadTool(session).execute("c")).data
    blocked_ref = ref_named(snap, "Blocked link")
    await BrowserOpenTool(session).execute("c", url=site + "/about")
    stale = await BrowserClickTool(session).execute("c", ref=blocked_ref)
    assert not stale.success and "browser_read again" in stale.error

    await BrowserOpenTool(session).execute("c", url=site + "/")
    snap = (await BrowserReadTool(session).execute("c")).data
    await BrowserClickTool(session).execute("c", ref=ref_named(snap, "Blocked link"))
    page = await session.page()
    assert "blocked.example" not in page.url  # the route guard aborted the navigation
