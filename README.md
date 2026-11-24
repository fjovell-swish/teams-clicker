# teams-bot-clicker

Automate clicking the buttons on a chatbot's cards in Microsoft Teams (web), using [Playwright](https://playwright.dev/python/).

Open the bot's chat, click a button on its newest card, wait for the bot's next card, click again — and stop once the bot stops sending cards with buttons.

> Use it only with your own account and within your organization's policies.

## Requirements

- Python 3.10+
- A Microsoft Teams account that can sign in at [teams.cloud.microsoft](https://teams.cloud.microsoft/)

## Installation

```bash
pip install -r requirements.txt
playwright install chromium   # or: playwright install firefox
```

## Quick start

1. **Get the chat link.** Get a Teams link to your chat with the bot (for example from the browser's address bar while the chat is open). It looks like `https://teams.cloud.microsoft/l/chat/19%3A.../conversations?...`. The link only works for your own account.
2. **Run the script:**

   ```bash
   python teams_bot_clicker.py --chat-url "https://teams.cloud.microsoft/l/chat/..." --close-popups
   ```

3. **First run only:** a browser window opens on the Microsoft sign-in page. Sign in by hand (SSO/MFA included); you have 5 minutes. The session is saved in a local browser profile, so later runs start already signed in.

## Usage

```bash
# Click through every card the bot sends (any button), then stop
python teams_bot_clicker.py --chat-url "<chat link>"

# Only click buttons with these labels
python teams_bot_clicker.py --chat-url "<chat link>" \
    --button "Start your next course" --button "I loved it"

# Labels as regular expressions
python teams_bot_clicker.py --chat-url "<chat link>" --button-regex "^Start"

# Find the chat by its name in the chat list instead of by link
python teams_bot_clicker.py --chat "Albert"

# Never stop: click buttons on every new card as it arrives (Ctrl+C to quit)
python teams_bot_clicker.py --chat-url "<chat link>" --watch

# Use Firefox, without a window (only once you're signed in)
python teams_bot_clicker.py --chat-url "<chat link>" --browser firefox --headless
```

### Options

| Option | Description |
|---|---|
| `--chat-url URL` | Teams link that opens the chat directly. Most reliable way to reach the chat. |
| `--chat NAME` | Chat name as shown in the chat list. Used when `--chat-url` isn't given. |
| `--button LABEL` | Only click buttons with this exact label. Repeatable. Default: any button on a card. |
| `--button-regex REGEX` | Only click buttons whose label matches this regex. Repeatable. |
| `--idle-timeout SECONDS` | Stop once no new button has appeared for this long. Default: 30. |
| `--max-clicks N` | Safety limit on clicks per run. Default: 50. |
| `--watch` | Never stop; ignore the card already on screen and click every new one. |
| `--close-popups` | Close tabs opened by clicked buttons (e.g. links to courses or forms). |
| `--browser {chromium,firefox}` | Browser engine. Default: `chromium`. |
| `--headless` | Run without a window. Needs an existing signed-in profile. |
| `--profile-dir PATH` | Browser profile location. Default: `~/.teams-playwright-profile-<browser>`. |
| `--state-file PATH` | Where clicked cards are remembered. Default: `~/.teams-bot-clicker-state.json`. |

## How it works

- **Opening the chat:** with `--chat-url` it loads the link directly (choosing "use the web app" if Teams asks). With `--chat` it looks for the chat in the left list, scrolling as needed, then falls back to the "go to chat" search (`Ctrl+Alt+G` / `⌥⌘G`). Bot chats usually don't appear in that search, so prefer `--chat-url`.
- **Finding buttons:** by default, any button inside an adaptive card (the cards bots send, rendered with the `ac-adaptiveCard` class). With `--button`/`--button-regex`, buttons are matched by accessible name instead.
- **One click per card:** only the newest card with buttons is considered, and only its first enabled button is clicked. Older cards further up the chat are never touched.
- **Remembering clicks:** each card is identified by its Teams message id and saved to the state file, so the same card isn't clicked twice, even across runs.
- **Stopping:** after `--idle-timeout` seconds without a new clickable card (unless `--watch`), or after `--max-clicks`.

## Troubleshooting

**Traces on failure.** On any error the script saves a Playwright trace in `./traces/` and prints the command to open it:

```bash
playwright show-trace traces/trace-YYYYMMDD-HHMMSS.zip
```

It shows a screenshot and the page structure at every step: the fastest way to see what went wrong.

| Symptom | Fix |
|---|---|
| `Teams session expired` | The saved sign-in is gone. Run once without `--headless` and sign in again. |
| `Chat '...' not found` | The log lists the chat-list items it could see. Use `--chat-url` instead. |
| `done after 0 clicks` while a card with buttons is on screen | Teams didn't render the card the expected way. Pass the label with `--button "..."`. |
| It stops after a button that opens a web page | Expected: the bot may only send its next card once you finish what that page asked. Run it again afterwards. |
| State file entries start with `text:` | Teams message ids weren't found, so cards are identified by their text. Works, but two identical cards would count as one. |

The Teams interface is set to Catalan (`locale="ca-ES"` in `launch()`). Change it if your labels are in another language: `--button` labels must match what Teams displays.

## Limitations

- It drives the Teams web UI, so changes to Teams' markup can break selectors. The trace on failure makes these quick to diagnose.
- Only the first enabled button on each card is clicked. To pick a specific answer on multi-button cards, use `--button`.
- Playwright uses its own browser builds, not your installed Chrome/Firefox, so your existing browser sign-ins aren't reused.
