# iTerm2 File Tree

A file tree navigator that lives in a split pane inside iTerm2, synchronized with your active terminal.

![Tree showing backend directory](https://img.shields.io/badge/iTerm2-Python_API-blue)

## What it does

- Opens a 36-column split pane on the left of your current terminal
- Displays a navigable file tree with vim-style keybindings and mouse support
- Automatically updates the tree root when you switch between terminals or run `cd`
- Ignores focus changes to background sessions (Claude Code, etc.) that sit at `$HOME`

## Requirements

- iTerm2 with Python API enabled: **Settings → General → Magic → Enable Python API**
- Python 3.10+
- iTerm2 shell integration (recommended for accurate CWD detection)

```bash
pip3 install iterm2

# Optional but recommended
curl -L https://iterm2.com/shell_integration/install_shell_integration.sh | bash
```

## Usage

```bash
python3 launch_filetree.py
```

The script daemonizes immediately — your prompt returns at once and the tree panel appears on the left. The daemon runs in the background and logs to `/tmp/filetree_daemon.log`.

To stop it:

```bash
pkill -f launch_filetree.py
```

Running the script again automatically kills any previous instance before starting a new one.

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `l` / `→` / `Enter` | Expand directory / open file |
| `h` / `←` | Collapse / go to parent |
| `g` / `Home` | Jump to top |
| `G` / `End` | Jump to bottom |
| `Page Up/Down` | Scroll page |
| `/` | Search (filter by name) |
| `Esc` | Exit search |
| `H` | Toggle hidden files |
| `y` / `c` | Copy absolute path to clipboard |
| `Y` | Copy relative path to clipboard |
| `r` | Reload tree |
| `q` | Quit |

Mouse support: click to select, double-click to expand/open, right-click for context menu, scroll wheel to navigate.

## Context menu (right-click)

- Copy absolute path
- Copy relative path
- Copy filename
- Open in Finder
- CD to this directory
- Reload tree

## Sync behavior

The tree root updates when:

1. **You switch focus** to a different terminal pane or tab — the tree updates to that session's CWD
2. **You run `cd`** in any terminal — the tree updates when shell integration reports the new path

The tree does **not** update when:

- You focus the tree panel itself
- A background session at `$HOME` gains focus (Claude Code, passive terminals, etc.)
- A session briefly reports `$HOME` due to shell integration lag — a 400ms retry resolves this

## Files

| File | Description |
|------|-------------|
| `filetree.py` | The curses TUI. Can be run standalone: `python3 filetree.py [path]` |
| `launch_filetree.py` | Daemon that manages the split pane and syncs the tree with your terminals |
