# claude-usage-swiftbar

A [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin that shows your claude.ai subscription usage limits in the macOS menu bar.

```text
7·42·13%
─────────────────────────────────────────
Session 7% · resets 17:10 (in 2h 10m)
Weekly (all models) 42% · resets Sun 18:00 (in 5d 3h)
Fable 13% · resets Sun 18:00 (in 5d 3h)
Credits: $0.00 / $50.00
─────────────────────────────────────────
Open claude.ai usage
Updated 14:59
Refresh
```

The menu bar shows the three limit percentages (5-hour session, weekly all models, weekly model-scoped). The title turns orange or red when any limit reaches warning or exceeded severity. The dropdown adds reset times in your local timezone, countdowns, and your usage-credit balance.

## How it works

Claude Code stores an OAuth token for your claude.ai subscription in the macOS Keychain. This plugin reads that token (read-only) and calls the same usage endpoint the official clients use, so the numbers match the claude.ai settings page exactly. Nothing is scraped or estimated.

## Requirements

- macOS with [SwiftBar](https://github.com/swiftbar/SwiftBar) (`brew install swiftbar`)
- Python 3.9+ (the system `python3` that ships with the Xcode Command Line Tools is enough; no packages needed)
- [Claude Code](https://claude.com/claude-code) installed and logged in; that login is where the token comes from

## Install

Copy or symlink the plugin into your SwiftBar plugin folder and make it executable:

```bash
git clone https://github.com/archulan/claude-usage-swiftbar.git
cd claude-usage-swiftbar
chmod +x claude-usage.1m.py
ln -s "$PWD/claude-usage.1m.py" "$(defaults read com.ameba.SwiftBar PluginDirectory)/"
```

On first run macOS may ask for Keychain access to "Claude Code-credentials". Click **Always Allow**, otherwise the prompt returns every minute.

The refresh interval is the `1m` in the filename; rename to `claude-usage.5m.py` for a 5-minute cadence.

## Stale token behaviour

The plugin is passive by design. When the access token expires (you have not used Claude Code recently) or the API errors, it keeps showing the last known snapshot greyed out with a clock or warning symbol and a hint line. Open Claude Code and run anything to refresh the token. The plugin never uses the refresh token, so it cannot interfere with Claude Code's session.

## Troubleshooting

- Run it manually to see the raw output: `./claude-usage.1m.py`
- "Claude Code credentials not found": log in to Claude Code. The plugin reads the Keychain item `Claude Code-credentials`, falling back to `~/.claude/.credentials.json`.
- Grey title with a clock symbol: token expired, open Claude Code.
- Grey title with a warning triangle: the usage endpoint failed; the dropdown shows the reason.
- Cached snapshot lives in SwiftBar's per-plugin cache folder (or `~/.cache/claude-usage/usage.json`). It is the last usage response stored verbatim, so it includes your credit balance alongside the percentages and reset times. The file is written user-only (0600); delete it any time.

## Disclaimer

This is an unofficial tool, not affiliated with or endorsed by Anthropic. It relies on an undocumented endpoint that may change or break without notice. Your token is read locally and only ever sent to `api.anthropic.com`; nothing else is collected or transmitted.

## License

[MIT](LICENSE)
