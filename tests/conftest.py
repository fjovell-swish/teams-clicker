import logging
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import Playwright
from playwright.sync_api import Route
from playwright.sync_api import sync_playwright

import teams_bot_clicker


@pytest.fixture(scope="session")
def playwright() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--browser",
        action="append",
        choices=teams_bot_clicker.SUPPORTED_BROWSERS,
        help="Browser to run the tests in (repeatable). Default: all supported browsers",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run every test that uses a browser once per browser (chromium, firefox)."""
    if "browser_name" in metafunc.fixturenames:
        names = metafunc.config.getoption("browser") or teams_bot_clicker.SUPPORTED_BROWSERS
        metafunc.parametrize("browser_name", names, scope="session")


@pytest.fixture(scope="session")
def browser(playwright: Playwright, browser_name: str) -> Iterator[Browser]:
    browser = playwright[browser_name].launch()
    yield browser
    browser.close()


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context()
    context.tracing.start(screenshots=True, snapshots=True)
    context.tracing.start_chunk()
    yield context
    context.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    return context.new_page()


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink waits and timeouts so tests run in seconds, not minutes."""
    monkeypatch.setattr(teams_bot_clicker, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(teams_bot_clicker, "CHAT_LOAD_PAUSE_MS", 50)
    monkeypatch.setattr(teams_bot_clicker, "GO_TO_CHAT_TIMEOUT_MS", 500)
    monkeypatch.setattr(teams_bot_clicker, "DESKTOP_PROMPT_TIMEOUT_MS", 300)
    monkeypatch.setattr(teams_bot_clicker, "HEADLESS_LOAD_TIMEOUT_MS", 1000)
    monkeypatch.setattr(teams_bot_clicker, "LIST_SCROLL_PAUSE_MS", 50)
    monkeypatch.setattr(teams_bot_clicker, "MAX_LIST_SCROLLS", 10)


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    """The script logs progress at INFO; let tests assert on it."""
    caplog.set_level(logging.INFO, logger="teams_bot_clicker")


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def serve(context: BrowserContext) -> Callable[[str], None]:
    """Answer every request in the context with the given HTML (fake Teams, fake login...)."""

    def _serve(html: str) -> None:
        def handler(route: Route) -> None:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        context.route("**/*", handler)

    return _serve


@pytest.fixture
def bot_conversation_html() -> str:
    """A fake bot chat.

    One old card already answered, then a new card. Clicking any button on a card disables it
    and, half a second later, the bot posts the next card. The fourth card has no buttons.
    There's also a toolbar button that must never be clicked.
    """
    return """
    <div role="tree"><div role="treeitem"><span>Albert</span></div></div>
    <div class="toolbar"><button onclick="clicked('toolbar')">Configuració</button></div>
    <div id="list">
      <div data-mid="1700000000000"><div class="ac-adaptiveCard">
        <button onclick="clicked('old')">Comença el teu proper curs</button>
      </div></div>
    </div>
    <script>
      window.clicks = [];
      let cardNumber = 0;
      function clicked(what) { window.clicks.push(what); }
      function postCard() {
        cardNumber++;
        const id = String(1764000000000 + cardNumber);
        const card = document.createElement('div');
        card.setAttribute('data-mid', id);
        card.innerHTML = '<div class="ac-adaptiveCard"><p>Card ' + cardNumber + '</p>' +
          (cardNumber < 4 ? '<button>Sí</button><button>No</button>' : '<p>Curs acabat</p>') +
          '</div>';
        card.querySelectorAll('button').forEach(button => button.onclick = () => {
          clicked(button.textContent + ' ' + cardNumber);
          button.disabled = true;
          setTimeout(postCard, 500);
        });
        document.getElementById('list').appendChild(card);
      }
      postCard();
    </script>
    """


@pytest.fixture
def clicks(page: Page) -> Callable[[], list[str]]:
    def _clicks() -> list[str]:
        result: list[str] = page.evaluate("window.clicks")
        return result

    return _clicks
