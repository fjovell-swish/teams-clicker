import argparse
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import Playwright

import teams_bot_clicker

# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def card_patterns() -> list[re.Pattern[str]]:
    """No label filter: any adaptive-card button."""
    return []


@pytest.fixture
def chat_list_html() -> str:
    """A chat list that only renders "Albert" once scrolled far enough (like Teams' list)."""
    items = "".join(f'<div role="treeitem"><span>Chat {i}</span></div>' for i in range(10))
    return f"""
    <div role="tree" id="tree" style="height: 200px; overflow: auto">
      <div id="inner" style="height: 3000px">{items}</div>
    </div>
    <script>
      document.getElementById('tree').addEventListener('scroll', event => {{
        if (event.target.scrollTop > 1000 && !document.getElementById('albert')) {{
          const item = document.createElement('div');
          item.id = 'albert';
          item.setAttribute('role', 'treeitem');
          item.innerHTML = '<span>Albert</span> Ha enviat una targeta';
          item.onclick = () => window.opened = 'Albert';
          document.getElementById('inner').appendChild(item);
        }}
      }});
    </script>
    """


@pytest.fixture
def teams_home_html() -> str:
    return '<div role="tree"><div role="treeitem"><span>Albert</span></div></div>'


@pytest.fixture
def desktop_prompt_html() -> str:
    return """
    <a href="#" onclick="document.body.innerHTML = '<div role=tree>Chats</div>'; return false">
      Utilitzeu l'aplicació web
    </a>
    """


@pytest.fixture
def chat_url() -> str:
    return "https://teams.cloud.microsoft/l/chat/19%3Afake%40unq.gbl.spaces/conversations"


@pytest.fixture
def run_args(chat_url: str, state_file: Path) -> argparse.Namespace:
    return argparse.Namespace(
        button=[],
        button_regex=[],
        chat=None,
        chat_url=chat_url,
        close_popups=False,
        headless=True,
        idle_timeout=1.5,
        max_clicks=50,
        state_file=state_file,
        watch=False,
    )


@pytest.fixture
def cli_args(monkeypatch: pytest.MonkeyPatch) -> Callable[..., argparse.Namespace]:
    def _parse(*argv: str) -> argparse.Namespace:
        monkeypatch.setattr("sys.argv", ["teams_bot_clicker.py", *argv])
        return teams_bot_clicker.parse_args()

    return _parse


# --- parse_args ------------------------------------------------------------------------


def test_parse_args_requires_chat_or_chat_url(
    cli_args: Callable[..., argparse.Namespace], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        cli_args()

    assert "give --chat or --chat-url" in capsys.readouterr().err


def test_parse_args_buttons_are_optional(
    cli_args: Callable[..., argparse.Namespace], chat_url: str
) -> None:
    args = cli_args("--chat-url", chat_url)

    assert args.button == []
    assert args.button_regex == []


def test_parse_args_defaults(cli_args: Callable[..., argparse.Namespace]) -> None:
    args = cli_args("--chat", "Albert")

    assert args.browser == "chromium"
    assert args.idle_timeout == teams_bot_clicker.DEFAULT_IDLE_SECONDS
    assert args.max_clicks == teams_bot_clicker.DEFAULT_MAX_CLICKS
    assert args.state_file == teams_bot_clicker.DEFAULT_STATE_FILE
    assert args.watch is False


def test_parse_args_collects_repeated_buttons(cli_args: Callable[..., argparse.Namespace]) -> None:
    args = cli_args("--chat", "Albert", "--button", "Sí", "--button", "No")

    assert args.button == ["Sí", "No"]


# --- build_label_patterns --------------------------------------------------------------


def test_build_label_patterns_exact_labels_are_escaped_and_anchored() -> None:
    args = argparse.Namespace(button=["Start (now)?"], button_regex=[])

    [pattern] = teams_bot_clicker.build_label_patterns(args)

    assert pattern.match("  Start (now)?  ")
    assert not pattern.match("Start now")
    assert not pattern.match("Start (now)? please")


def test_build_label_patterns_regexes_are_used_as_given() -> None:
    args = argparse.Namespace(button=["Sí"], button_regex=["^Comença"])

    exact, regex = teams_bot_clicker.build_label_patterns(args)

    assert exact.match("Sí")
    assert regex.search("Comença el teu proper curs")


def test_build_label_patterns_empty_means_any_card_button() -> None:
    args = argparse.Namespace(button=[], button_regex=[])

    assert teams_bot_clicker.build_label_patterns(args) == []


# --- state -----------------------------------------------------------------------------


def test_load_state_missing_file_is_empty(state_file: Path) -> None:
    assert teams_bot_clicker.load_state(state_file) == set()


def test_save_state_round_trips(state_file: Path) -> None:
    teams_bot_clicker.save_state(state_file, {"mid:2", "mid:1"})

    assert teams_bot_clicker.load_state(state_file) == {"mid:1", "mid:2"}


# --- message_key -----------------------------------------------------------------------


def test_message_key_uses_data_mid(page: Page) -> None:
    page.set_content('<div data-mid="1764000000001"><div><button>Sí</button></div></div>')

    assert teams_bot_clicker.message_key(page.get_by_role("button")) == "mid:1764000000001"


def test_message_key_uses_timestamp_in_id(page: Page) -> None:
    page.set_content('<div id="message-body-1764000000002"><button>Sí</button></div>')

    assert teams_bot_clicker.message_key(page.get_by_role("button")) == "id:1764000000002"


def test_message_key_falls_back_to_hashed_text(page: Page) -> None:
    page.set_content(
        "<div><p>Same card</p><button>Sí</button></div>"
        "<div><p>Same card</p><button>Sí</button></div>"
    )
    buttons = page.get_by_role("button")

    first = teams_bot_clicker.message_key(buttons.nth(0))

    assert re.fullmatch(r"text:[0-9a-f]{40}", first)
    assert teams_bot_clicker.message_key(buttons.nth(1)) == first


# --- find_buttons ----------------------------------------------------------------------


def test_find_buttons_default_only_card_buttons(
    page: Page, bot_conversation_html: str, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(bot_conversation_html)

    labels = teams_bot_clicker.find_buttons(page, card_patterns).all_inner_texts()

    assert labels == ["Comença el teu proper curs", "Sí", "No"]


def test_find_buttons_with_patterns_matches_any_of_them(
    page: Page, bot_conversation_html: str
) -> None:
    page.set_content(bot_conversation_html)
    patterns = [re.compile("^No$"), re.compile("^Configuració$")]

    labels = teams_bot_clicker.find_buttons(page, patterns).all_inner_texts()

    assert labels == ["Configuració", "No"]


# --- newest_card / next_button ---------------------------------------------------------


def test_newest_card_none_without_buttons(
    page: Page, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content("<p>No cards here</p>")

    assert teams_bot_clicker.newest_card(page, card_patterns) is None


def test_newest_card_returns_only_the_last_cards_buttons(
    page: Page, bot_conversation_html: str, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(bot_conversation_html)

    card = teams_bot_clicker.newest_card(page, card_patterns)

    assert card is not None
    key, buttons = card
    assert key == "mid:1764000000001"
    assert [button.inner_text() for button in buttons] == ["Sí", "No"]


def test_next_button_first_enabled_button_of_newest_card(
    page: Page, bot_conversation_html: str, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(bot_conversation_html)

    candidate = teams_bot_clicker.next_button(page, card_patterns, set())

    assert candidate is not None
    key, button = candidate
    assert key == "mid:1764000000001"
    assert button.inner_text() == "Sí"


def test_next_button_skips_disabled_buttons(
    page: Page, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(
        '<div data-mid="1764000000001"><div class="ac-adaptiveCard">'
        "<button disabled>Sí</button><button>No</button></div></div>"
    )

    candidate = teams_bot_clicker.next_button(page, card_patterns, set())

    assert candidate is not None
    assert candidate[1].inner_text() == "No"


def test_next_button_none_when_all_disabled(
    page: Page, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(
        '<div data-mid="1764000000001"><div class="ac-adaptiveCard">'
        "<button disabled>Sí</button></div></div>"
    )

    assert teams_bot_clicker.next_button(page, card_patterns, set()) is None


def test_next_button_none_when_newest_card_already_handled(
    page: Page, bot_conversation_html: str, card_patterns: list[re.Pattern[str]]
) -> None:
    page.set_content(bot_conversation_html)

    assert teams_bot_clicker.next_button(page, card_patterns, {"mid:1764000000001"}) is None


# --- click_cards -----------------------------------------------------------------------


def test_click_cards_clicks_one_button_per_card_until_done(
    context: BrowserContext,
    page: Page,
    bot_conversation_html: str,
    card_patterns: list[re.Pattern[str]],
    state_file: Path,
    clicks: Callable[[], list[str]],
) -> None:
    page.set_content(bot_conversation_html)
    handled: set[str] = set()

    teams_bot_clicker.click_cards(
        context, page, card_patterns, handled, state_file, idle_seconds=1.5, max_clicks=50
    )

    assert clicks() == ["Sí 1", "Sí 2", "Sí 3"]
    assert handled == {"mid:1764000000001", "mid:1764000000002", "mid:1764000000003"}
    assert teams_bot_clicker.load_state(state_file) == handled


def test_click_cards_respects_label_patterns(
    context: BrowserContext,
    page: Page,
    bot_conversation_html: str,
    state_file: Path,
    clicks: Callable[[], list[str]],
) -> None:
    page.set_content(bot_conversation_html)

    teams_bot_clicker.click_cards(
        context, page, [re.compile("^No$")], set(), state_file, idle_seconds=1.5, max_clicks=50
    )

    assert clicks() == ["No 1", "No 2", "No 3"]


def test_click_cards_stops_at_max_clicks(
    context: BrowserContext,
    page: Page,
    bot_conversation_html: str,
    card_patterns: list[re.Pattern[str]],
    state_file: Path,
    clicks: Callable[[], list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    page.set_content(bot_conversation_html)

    teams_bot_clicker.click_cards(
        context, page, card_patterns, set(), state_file, idle_seconds=1.5, max_clicks=2
    )

    assert clicks() == ["Sí 1", "Sí 2"]
    assert "--max-clicks" in caplog.text


def test_click_cards_does_nothing_without_cards(
    context: BrowserContext,
    page: Page,
    card_patterns: list[re.Pattern[str]],
    state_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page.set_content("<script>window.clicks = []</script><p>Nothing to click</p>")

    teams_bot_clicker.click_cards(
        context, page, card_patterns, set(), state_file, idle_seconds=0.3, max_clicks=50
    )

    assert "done after 0 clicks" in caplog.text


# --- ignore_current_card ---------------------------------------------------------------


def test_ignore_current_card_marks_newest_card_handled(
    page: Page,
    bot_conversation_html: str,
    card_patterns: list[re.Pattern[str]],
    state_file: Path,
) -> None:
    page.set_content(bot_conversation_html)
    handled: set[str] = set()

    teams_bot_clicker.ignore_current_card(page, card_patterns, handled, state_file)

    assert handled == {"mid:1764000000001"}
    assert teams_bot_clicker.next_button(page, card_patterns, handled) is None


def test_ignore_current_card_without_cards_changes_nothing(
    page: Page, card_patterns: list[re.Pattern[str]], state_file: Path
) -> None:
    page.set_content("<p>Nothing</p>")
    handled: set[str] = set()

    teams_bot_clicker.ignore_current_card(page, card_patterns, handled, state_file)

    assert handled == set()
    assert not state_file.exists()


# --- opening the chat ------------------------------------------------------------------


def test_open_chat_from_list_scrolls_until_found(page: Page, chat_list_html: str) -> None:
    page.set_content(chat_list_html)

    assert teams_bot_clicker.open_chat_from_list(page, "Albert") is True
    assert page.evaluate("window.opened") == "Albert"


def test_open_chat_from_list_missing_chat(page: Page, chat_list_html: str) -> None:
    page.set_content(chat_list_html)

    assert teams_bot_clicker.open_chat_from_list(page, "Nobody") is False


def test_open_chat_from_search_picks_suggestion(page: Page) -> None:
    page.set_content("""
        <input id="search" autofocus>
        <div role="listbox">
          <div role="option" onclick="window.opened = 'Bob'">Bob Smith</div>
        </div>
    """)

    assert teams_bot_clicker.open_chat_from_search(page, "Bob") is True
    assert page.evaluate("window.opened") == "Bob"


def test_open_chat_from_search_no_suggestion(page: Page) -> None:
    page.set_content("<p>No search here</p>")

    assert teams_bot_clicker.open_chat_from_search(page, "Albert") is False


def test_open_chat_uses_the_list(page: Page, chat_list_html: str) -> None:
    page.set_content(chat_list_html)

    teams_bot_clicker.open_chat(page, "Albert")

    assert page.evaluate("window.opened") == "Albert"


def test_open_chat_falls_back_to_search(page: Page) -> None:
    page.set_content("""
        <div role="tree"><div role="treeitem"><span>Someone else</span></div></div>
        <input autofocus>
        <div role="option" onclick="window.opened = 'Albert'">Albert</div>
    """)

    teams_bot_clicker.open_chat(page, "Albert")

    assert page.evaluate("window.opened") == "Albert"


def test_open_chat_not_found_raises_and_logs_list(
    page: Page, chat_list_html: str, caplog: pytest.LogCaptureFixture
) -> None:
    page.set_content(chat_list_html)

    with pytest.raises(RuntimeError, match="try --chat-url"):
        teams_bot_clicker.open_chat(page, "Nobody")

    assert "treeitem 0: 'Chat 0'" in caplog.text


# --- open_teams ------------------------------------------------------------------------


def test_open_teams_waits_for_chat_list(
    context: BrowserContext, serve: Callable[[str], None], teams_home_html: str
) -> None:
    serve(teams_home_html)

    page = teams_bot_clicker.open_teams(context, True, teams_bot_clicker.TEAMS_URL)

    assert page.get_by_role("tree").count() == 1


def test_open_teams_skips_desktop_app_prompt(
    context: BrowserContext, serve: Callable[[str], None], desktop_prompt_html: str, chat_url: str
) -> None:
    serve(desktop_prompt_html)

    page = teams_bot_clicker.open_teams(context, True, chat_url)

    assert page.get_by_role("tree").count() == 1


def test_open_teams_expired_session(context: BrowserContext, serve: Callable[[str], None]) -> None:
    serve("<p>Sign in</p>")

    with pytest.raises(RuntimeError, match="session expired"):
        teams_bot_clicker.open_teams(context, True, "https://login.microsoftonline.com/common")


def test_open_teams_other_timeouts_are_reraised(
    context: BrowserContext, serve: Callable[[str], None]
) -> None:
    serve("<p>Teams is broken today</p>")

    with pytest.raises(teams_bot_clicker.PlaywrightTimeoutError):
        teams_bot_clicker.open_teams(context, True, teams_bot_clicker.TEAMS_URL)


# --- popups ----------------------------------------------------------------------------


@pytest.fixture
def popup_handler() -> Callable[[bool], Callable[[Page], None]]:
    """The "page" event handler that handle_popups registers on the context."""

    def _handler(close: bool) -> Callable[[Page], None]:
        context = Mock()
        teams_bot_clicker.handle_popups(context, close=close)
        event, handler = context.on.call_args.args
        assert event == "page"
        return cast(Callable[[Page], None], handler)

    return _handler


def test_handle_popups_closes_new_tabs(
    context: BrowserContext, popup_handler: Callable[[bool], Callable[[Page], None]]
) -> None:
    popup = context.new_page()

    popup_handler(True)(popup)

    assert popup.is_closed()


def test_handle_popups_keeps_new_tabs_by_default(
    context: BrowserContext,
    popup_handler: Callable[[bool], Callable[[Page], None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    popup = context.new_page()

    popup_handler(False)(popup)

    assert not popup.is_closed()
    assert "Button opened a new tab" in caplog.text


# --- tracing ---------------------------------------------------------------------------


def test_save_trace_writes_zip(
    context: BrowserContext,
    page: Page,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(teams_bot_clicker, "TRACES_DIR", tmp_path / "traces")
    page.set_content("<p>Something failed here</p>")

    teams_bot_clicker.save_trace(context)

    [trace] = (tmp_path / "traces").glob("trace-*.zip")
    assert trace.stat().st_size > 0


# --- launch ----------------------------------------------------------------------------


def test_launch_uses_a_profile_per_browser(
    playwright: Playwright, browser_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(teams_bot_clicker, "DEFAULT_PROFILE_DIR", tmp_path / "profile")
    args = argparse.Namespace(browser=browser_name, headless=True, profile_dir=None)

    context = teams_bot_clicker.launch(playwright, args)
    context.tracing.stop_chunk()
    context.close()

    assert (tmp_path / f"profile-{browser_name}").is_dir()


# --- run (end to end against a fake Teams) ---------------------------------------------


@pytest.fixture
def run_context(
    context: BrowserContext, monkeypatch: pytest.MonkeyPatch
) -> Callable[[argparse.Namespace], BrowserContext]:
    """run() opens its page through open_teams; hand it the test context."""

    def _run(args: argparse.Namespace) -> BrowserContext:
        teams_bot_clicker.run(context, args)
        return context

    return _run


def test_run_with_chat_url_clicks_through_conversation(
    run_context: Callable[[argparse.Namespace], BrowserContext],
    serve: Callable[[str], None],
    bot_conversation_html: str,
    run_args: argparse.Namespace,
) -> None:
    serve(bot_conversation_html)

    context = run_context(run_args)

    assert context.pages[0].evaluate("window.clicks") == ["Sí 1", "Sí 2", "Sí 3"]


def test_run_with_chat_name_opens_chat_from_list(
    run_context: Callable[[argparse.Namespace], BrowserContext],
    serve: Callable[[str], None],
    bot_conversation_html: str,
    run_args: argparse.Namespace,
) -> None:
    serve(bot_conversation_html)
    run_args.chat_url = None
    run_args.chat = "Albert"
    run_args.max_clicks = 1

    context = run_context(run_args)

    assert context.pages[0].evaluate("window.clicks") == ["Sí 1"]


def test_run_watch_ignores_card_on_screen(
    run_context: Callable[[argparse.Namespace], BrowserContext],
    serve: Callable[[str], None],
    run_args: argparse.Namespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    serve(
        '<div role="tree">Chats</div><div data-mid="1764000000001"><div class="ac-adaptiveCard">'
        '<button onclick="window.clicked = true">Sí</button></div></div>'
    )
    run_args.watch = True
    run_args.max_clicks = 0  # stop the never-ending watch loop straight away

    context = run_context(run_args)

    assert "Ignoring the card already on screen" in caplog.text
    assert context.pages[0].evaluate("window.clicked") is None


# --- main ------------------------------------------------------------------------------


@pytest.fixture
def main_with_test_browser(
    playwright: Playwright,
    context: BrowserContext,
    monkeypatch: pytest.MonkeyPatch,
    chat_url: str,
    state_file: Path,
    tmp_path: Path,
) -> Callable[[], None]:
    """main(), but on the tests' Playwright and context instead of launching its own."""
    monkeypatch.setattr(teams_bot_clicker, "sync_playwright", lambda: nullcontext(playwright))
    monkeypatch.setattr(teams_bot_clicker, "launch", lambda playwright, args: context)
    monkeypatch.setattr(teams_bot_clicker, "TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(
        "sys.argv",
        ["teams_bot_clicker.py", "--chat-url", chat_url, "--state-file", str(state_file)],
    )
    return teams_bot_clicker.main


def test_main_runs_and_closes_browser(
    main_with_test_browser: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
    context: BrowserContext,
) -> None:
    run = Mock()
    monkeypatch.setattr(teams_bot_clicker, "run", run)

    main_with_test_browser()

    run.assert_called_once()
    assert context.pages == []


def test_main_ctrl_c_stops_quietly(
    main_with_test_browser: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(teams_bot_clicker, "run", Mock(side_effect=KeyboardInterrupt))

    main_with_test_browser()

    assert "Stopped" in caplog.text


def test_main_error_saves_trace_and_reraises(
    main_with_test_browser: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(teams_bot_clicker, "run", Mock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        main_with_test_browser()

    assert list((tmp_path / "traces").glob("trace-*.zip"))
