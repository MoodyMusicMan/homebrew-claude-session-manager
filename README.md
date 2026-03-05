# claude-session-manager

A floating macOS GUI that shows all active Claude Code sessions across every project folder.

## Install

```bash
brew tap MoodyMusicMan/claude-session-manager
brew install claude-session-manager
```

### PyObjC (optional, recommended)

For menu bar integration (tray icon with Show/Hide, Refresh, Quit):

```bash
$(brew --prefix python@3.12)/bin/pip3.12 install pyobjc-framework-Cocoa
```

Without PyObjC the GUI still works — it just won't have a menu bar icon.

## Usage

```bash
# Run in foreground
claude-session-manager

# Run as a background service
brew services start claude-session-manager

# Control from the terminal
session-ctl state
session-ctl screenshot
session-ctl refresh
session-ctl resize 400x300
session-ctl move 100,50
```

## What It Shows

Per-session cards with:

- **Project name** (decoded from path-encoded directory name)
- **Session name** — first user prompt as default, or custom name via right-click
- **Last prompt** — most recent user message
- **Model** in use (opus, sonnet, haiku) with color coding
- **Status dot**: orange = busy, green = awaiting input, blue = recent, gray = idle
- **Last activity** as relative time
- **Duration**, **token count**, **message count**
- **Docker indicator** for sessions inside containers
- **Fork indicator** for forked sessions

## Features

- **Session management**: right-click to rename, fork, resume, or close sessions
- **Docker support**: auto-discovers Claude Code sessions inside Docker containers
- **New session launcher**: click `+` to start a new Claude session in any known project
- **Click to focus**: click a card to bring its Terminal window to the front
- **Catppuccin Mocha** color theme
- Rounded-corner transparent window, always-on-top
- Draggable and resizable
- Hidden from Dock (accessory app mode)
- Menu bar icon: **◎**

## Screenshots

<!-- Add screenshots here -->

## Requirements

- macOS
- Python 3.12 (installed automatically via Homebrew)
- python-tk (installed automatically via Homebrew)
- Claude Code CLI installed and authenticated

## Data Files

| Path | Purpose |
|------|---------|
| `~/.claude/.session-names.json` | Custom session names and fork relationships |
| `~/.claude/session-screenshot.png` | Latest screenshot |

## See Also

- [claude-usage-monitor](https://github.com/MoodyMusicMan/homebrew-claude-usage-monitor) — floating GUI for Claude Pro/Max quota utilization

## License

MIT
