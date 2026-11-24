"""Drive Microsoft Teams (web) with Playwright and click buttons on a chatbot's cards.

First run: a browser window opens; sign in to Teams by hand (SSO/MFA). The session is
kept in a persistent profile directory, so later runs start already logged in.

Default behaviour: open the chat, click a button on the newest card, wait for the bot's
next card, click again... and stop once no new button appears for --idle-timeout seconds.

Examples:
    # Click through every card the bot sends, any button
    python teams_bot_clicker.py --chat-url "https://teams.cloud.microsoft/l/chat/..."

    # Only click buttons with these labels
    python teams_bot_clicker.py --chat-url "https://teams.cloud.microsoft/l/chat/..." \
        --button "Comença el teu proper curs" --button "M'ha encantat"

    # Find the chat by name instead of by link; use Firefox
    python teams_bot_clicker.py --browser firefox --chat Albert

    # Never stop: keep clicking buttons on every new card that arrives
    python teams_bot_clicker.py --chat-url "https://teams.cloud.microsoft/l/chat/..." --watch

On failure, a Playwright trace of the last steps is saved under ./traces; open it with
    playwright show-trace traces/<file>.zip
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

log = logging.getLogger("teams_bot_clicker")

TEAMS_URL = "https://teams.cloud.microsoft/"
LOGIN_HOST = "login.microsoftonline.com"
DEFAULT_PROFILE_DIR = Path.home() / ".teams-playwright-profile"
DEFAULT_STATE_FILE = Path.home() / ".teams-bot-clicker-state.json"
TRACES_DIR = Path("traces")

LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # time allowed to sign in by hand on first run
HEADLESS_LOAD_TIMEOUT_MS = 60 * 1000  # headless can't sign in, so fail sooner
ACTION_TIMEOUT_MS = 30 * 1000
GO_TO_CHAT_TIMEOUT_MS = 10 * 1000
CHAT_LOAD_PAUSE_MS = 3 * 1000  # let the message list render after opening the chat
POLL_SECONDS = 2
DEFAULT_IDLE_SECONDS = 30.0  # how long to wait for the bot's next card before stopping
DEFAULT_MAX_CLICKS = 50
# Adaptive cards (what bots send) are rendered with these class names.
CARD_BUTTON_SELECTOR = ".ac-adaptiveCard button, .ac-adaptiveCard [role='button']"
VIEWPORT: dict[str, int] = {"width": 1600, "height": 1000}
SUPPORTED_BROWSERS = ("chromium", "firefox")
MAX_DEBUG_ITEMS = 30
DESKTOP_PROMPT_TIMEOUT_MS = 5 * 1000
# "Use the web app instead" link on the desktop-app prompt, in Catalan, Spanish or English.
USE_WEB_APP_PATTERN = re.compile(r"aplicaci[oó] web|web app", re.IGNORECASE)
MAX_LIST_SCROLLS = 40
LIST_SCROLL_STEP_PX = 600
LIST_SCROLL_PAUSE_MS = 500
MAX_TEXT_KEY_CHARS = 2000

# "Go to chat or channel" shortcut (shown in the Teams search box).
GO_TO_CHAT_SHORTCUT = "Meta+Alt+KeyG" if sys.platform == "darwin" else "Control+Alt+KeyG"

# Walks up from a button to the message that contains it and returns a stable key.
# Teams message ids are millisecond timestamps (13 digits) found in data-mid or in id attributes.
# If none is found, falls back to the text of an ancestor a few levels up.
MESSAGE_KEY_JS = """
(el, fallbackLevels) => {
  for (let node = el; node && node.getAttribute; node = node.parentElement) {
    const mid = node.getAttribute('data-mid');
    if (mid) return 'mid:' + mid;
    const match = (node.id || '').match(/\\d{13}/);
    if (match) return 'id:' + match[0];
  }
  let node = el;
  for (let i = 0; i < fallbackLevels && node.parentElement; i++) node = node.parentElement;
  return 'text:' + (node.innerText || '');
}
"""
FALLBACK_ANCESTOR_LEVELS = 8


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat", help="Chat name as shown in the Teams chat list (e.g. Albert)")
    parser.add_argument(
        "--chat-url",
        help="Teams link that opens the chat directly (more reliable than --chat)",
    )
    parser.add_argument(
        "--button",
        action="append",
        default=[],
        help="Only click buttons with this exact label (repeatable). Default: any button on a card",
    )
    parser.add_argument("--button-regex", action="append", default=[], help="Only click labels matching this regex")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_SECONDS,
        help="Stop once no new button has appeared for this many seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--max-clicks",
        type=int,
        default=DEFAULT_MAX_CLICKS,
        help="Safety limit on the number of clicks in one run (default: %(default)s)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Never stop: ignore the card on screen and click buttons on every new card that arrives",
    )
    parser.add_argument("--close-popups", action="store_true", help="Close tabs opened by clicked buttons")
    parser.add_argument("--headless", action="store_true", help="Run without a window (only once logged in)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default="chromium")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help=f"Persistent profile directory (default: {DEFAULT_PROFILE_DIR}-<browser>)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Where already-handled messages are remembered across runs",
    )
    args = parser.parse_args()
    if not args.chat and not args.chat_url:
        parser.error("give --chat or --chat-url")
    return args


def build_label_patterns(args: argparse.Namespace) -> list[re.Pattern[str]]:
    exact = [re.compile(rf"^\s*{re.escape(label)}\s*$") for label in args.button]
    regexes = [re.compile(pattern) for pattern in args.button_regex]
    return exact + regexes


# --- handled-message state -------------------------------------------------------------


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_state(path: Path, handled: set[str]) -> None:
    path.write_text(json.dumps(sorted(handled), indent=2))


# --- browser setup ---------------------------------------------------------------------


def launch(playwright: Any, args: argparse.Namespace) -> BrowserContext:
    # Profiles are not interchangeable between browser engines, so keep one per browser.
    profile_dir = args.profile_dir or Path(f"{DEFAULT_PROFILE_DIR}-{args.browser}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_type = getattr(playwright, args.browser)
    log.info("Launching %s with profile %s", args.browser, profile_dir)
    context = browser_type.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=args.headless,
        viewport=VIEWPORT,
        locale="ca-ES",
    )
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    context.tracing.start(screenshots=True, snapshots=True)
    context.tracing.start_chunk()
    return context


def restart_trace_chunk(context: BrowserContext) -> None:
    """Throw away the trace recorded so far so a long --watch run only keeps recent steps."""
    context.tracing.stop_chunk()
    context.tracing.start_chunk()


def save_trace(context: BrowserContext) -> None:
    TRACES_DIR.mkdir(exist_ok=True)
    path = TRACES_DIR / f"trace-{datetime.now():%Y%m%d-%H%M%S}.zip"
    context.tracing.stop_chunk(path=str(path))
    log.error("Saved trace to %s (open with: playwright show-trace %s)", path, path)


def open_teams(context: BrowserContext, headless: bool, url: str) -> Page:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(url)
    skip_desktop_app_prompt(page)
    log.info("Waiting for Teams to load (sign in in the browser window if asked)...")
    timeout = HEADLESS_LOAD_TIMEOUT_MS if headless else LOGIN_TIMEOUT_MS
    try:
        # The chat list is a tree; it only appears once we're signed in.
        page.get_by_role("tree").first.wait_for(timeout=timeout)
    except PlaywrightTimeoutError as exc:
        if LOGIN_HOST in page.url:
            raise RuntimeError("Teams session expired: rerun without --headless and sign in again") from exc
        raise
    log.info("Teams loaded")
    return page


def skip_desktop_app_prompt(page: Page) -> None:
    """Teams links can land on an "open in the desktop app?" page; choose the web app."""
    use_web = page.get_by_role("link", name=USE_WEB_APP_PATTERN).or_(
        page.get_by_role("button", name=USE_WEB_APP_PATTERN)
    )
    try:
        use_web.first.click(timeout=DESKTOP_PROMPT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return  # no prompt shown: Teams went straight to the web app
    log.info("Chose the web app on the desktop-app prompt")


def handle_popups(context: BrowserContext, close: bool) -> None:
    def on_page(new_page: Page) -> None:
        new_page.wait_for_load_state()
        log.info("Button opened a new tab: %s", new_page.url)
        if close:
            new_page.close()

    context.on("page", on_page)


# --- opening the chat ------------------------------------------------------------------


def open_chat(page: Page, chat_name: str) -> None:
    """Open the chat from the chat list, else the go-to-chat search.

    Bot chats don't show up in the go-to-chat search, so the list comes first.
    """
    if open_chat_from_list(page, chat_name):
        log.info("Opened chat %s from the chat list", chat_name)
    elif open_chat_from_search(page, chat_name):
        log.info("Opened chat %s via the go-to-chat shortcut", chat_name)
    else:
        log_tree_items(page)
        raise RuntimeError(f"Chat {chat_name!r} not found in the chat list or the search; try --chat-url")
    page.wait_for_timeout(CHAT_LOAD_PAUSE_MS)


def open_chat_from_list(page: Page, chat_name: str) -> bool:
    """Click the chat in the left list, scrolling down through it if needed."""
    # Chat entries are tree items; their accessible name varies (status, prefixes...),
    # so match the item that contains an element whose visible text is exactly the chat title.
    item = page.get_by_role("treeitem").filter(has=page.get_by_text(chat_name, exact=True)).first
    chat_list = page.get_by_role("tree").first
    for _ in range(MAX_LIST_SCROLLS):
        if item.count():
            item.click()
            return True
        # The list is virtualised: off-screen chats aren't in the DOM until scrolled to.
        chat_list.hover()
        page.mouse.wheel(0, LIST_SCROLL_STEP_PX)
        page.wait_for_timeout(LIST_SCROLL_PAUSE_MS)
    return False


def open_chat_from_search(page: Page, chat_name: str) -> bool:
    name_pattern = re.compile(rf"\b{re.escape(chat_name)}\b")
    page.keyboard.press(GO_TO_CHAT_SHORTCUT)
    page.keyboard.type(chat_name)
    suggestion = page.get_by_role("option").filter(has_text=name_pattern).first
    try:
        suggestion.click(timeout=GO_TO_CHAT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Close the search popup so it doesn't cover the page; the caller reports the failure.
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return False
    return True


def log_tree_items(page: Page) -> None:
    """Log what the chat list actually exposes, to help fix the selector."""
    items = page.get_by_role("treeitem")
    count = items.count()
    log.error("Chat not found. %s tree items visible:", count)
    for index in range(min(count, MAX_DEBUG_ITEMS)):
        log.error("  treeitem %s: %r", index, items.nth(index).inner_text().replace("\n", " | "))


# --- buttons ---------------------------------------------------------------------------


def find_buttons(page: Page, patterns: list[re.Pattern[str]]) -> Locator:
    """Buttons to consider, in page order: those matching the labels, or any adaptive-card button."""
    if not patterns:
        return page.locator(CARD_BUTTON_SELECTOR)
    # get_by_role pierces shadow DOM; adaptive card actions are real <button>s.
    locator = page.get_by_role("button", name=patterns[0])
    for pattern in patterns[1:]:
        locator = locator.or_(page.get_by_role("button", name=pattern))
    return locator


def message_key(button: Locator) -> str:
    """Stable id of the message (card) a button belongs to."""
    key = button.evaluate(MESSAGE_KEY_JS, FALLBACK_ANCESTOR_LEVELS)
    if key.startswith("text:"):
        text = key[len("text:") :][:MAX_TEXT_KEY_CHARS]
        key = "text:" + hashlib.sha1(text.encode()).hexdigest()
    return key


def newest_card(page: Page, patterns: list[re.Pattern[str]]) -> tuple[str, list[Locator]] | None:
    """The most recent card that has buttons, as (message key, its buttons)."""
    buttons = find_buttons(page, patterns)
    count = buttons.count()
    if count == 0:
        return None
    newest_message = message_key(buttons.nth(count - 1))
    card_buttons = [buttons.nth(index) for index in range(count) if message_key(buttons.nth(index)) == newest_message]
    return newest_message, card_buttons


def next_button(page: Page, patterns: list[re.Pattern[str]], handled: set[str]) -> tuple[str, Locator] | None:
    """First enabled button on the newest card, unless that card was already clicked (one click per card)."""
    card = newest_card(page, patterns)
    if card is None or card[0] in handled:
        return None
    key, card_buttons = card
    for button in card_buttons:
        if button.is_enabled():
            return key, button
    return None


def click_cards(
    context: BrowserContext,
    page: Page,
    patterns: list[re.Pattern[str]],
    handled: set[str],
    state_file: Path,
    idle_seconds: float | None,
    max_clicks: int,
) -> None:
    """Click a button on the newest card, wait for the bot's next card, repeat.

    Stops when no new clickable button shows up for idle_seconds (None: run forever).
    """
    clicks = 0
    last_activity = time.monotonic()
    while clicks < max_clicks:
        candidate = next_button(page, patterns, handled)
        if candidate:
            key, button = candidate
            button.scroll_into_view_if_needed()
            log.info("Clicking %r (%s)", button.inner_text().strip(), key)
            button.click()
            handled.add(key)
            save_state(state_file, handled)
            clicks += 1
            last_activity = time.monotonic()
        elif idle_seconds is not None and time.monotonic() - last_activity > idle_seconds:
            log.info("No new buttons for %ss: done after %s clicks", idle_seconds, clicks)
            return
        restart_trace_chunk(context)
        time.sleep(POLL_SECONDS)
    log.warning("Stopped after reaching --max-clicks (%s)", max_clicks)


def ignore_current_card(page: Page, patterns: list[re.Pattern[str]], handled: set[str], state_file: Path) -> None:
    """For --watch: treat buttons already on screen as handled, so only new cards get clicked."""
    card = newest_card(page, patterns)
    if card is None:
        return
    handled.add(card[0])
    save_state(state_file, handled)
    log.info("Ignoring the card already on screen (%s)", card[0])


# --- main ------------------------------------------------------------------------------


def run(context: BrowserContext, args: argparse.Namespace) -> None:
    patterns = build_label_patterns(args)
    handled = load_state(args.state_file)
    page = open_teams(context, args.headless, args.chat_url or TEAMS_URL)
    handle_popups(context, close=args.close_popups)
    if args.chat_url:
        # The link already opened the chat; give the messages time to render.
        log.info("Opened chat via its link")
        page.wait_for_timeout(CHAT_LOAD_PAUSE_MS)
    else:
        open_chat(page, args.chat)

    if args.watch:
        ignore_current_card(page, patterns, handled, args.state_file)
        log.info("Watching for new cards (Ctrl+C to stop)")
        click_cards(context, page, patterns, handled, args.state_file, None, args.max_clicks)
    else:
        click_cards(context, page, patterns, handled, args.state_file, args.idle_timeout, args.max_clicks)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()

    with sync_playwright() as playwright:
        context = launch(playwright, args)
        try:
            run(context, args)
        except KeyboardInterrupt:
            log.info("Stopped")
        except Exception:
            save_trace(context)
            raise
        finally:
            context.close()


if __name__ == "__main__":
    main()
