# claude-usage-swiftbar

A [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin that shows your claude.ai subscription usage limits in the macOS menu bar.

On a plan with a 5-hour session limit (Pro and Max):

```text
S7·W42·F13%
─────────────────────────────────────────
Max plan
Session 7% · resets 17:10 (in 2h 10m)
Weekly (all models) 42% · resets Sun 18:00 (in 5d 3h)
Fable 13% · resets Sun 18:00 (in 5d 3h)
Usage credits: $0.00 / $50.00
─────────────────────────────────────────
Open claude.ai usage
Updated 14:59
Refresh
```

On Ultra, which has no session limit for verified accounts:

```text
W1·F2%
─────────────────────────────────────────
Ultra plan
Weekly (all models) 1% · resets Sun 19:00 (in 6d 10h)
Fable 2% · resets Sun 19:00 (in 6d 10h)
Cloud session credits: $246 of $250 left · expires Thu 18:59 (in 3d 9h)
─────────────────────────────────────────
Open claude.ai usage
Updated 09:00
Refresh
```

The menu bar shows one percentage for each limit your plan has, tagged `S` for the 5-hour session, `W` for weekly across all models, and the model's initial for the weekly model-scoped limit. A warning symbol joins them when any limit needs attention. The dropdown adds your plan, reset times in your local timezone, countdowns, any included cloud session credits, and your usage-credit balance.

The title itself is never coloured. The menu bar is translucent, so the wallpaper behind it decides the real contrast, and custom text colours are resolved from the system light/dark setting, which cannot see that backdrop. Symbols are resolved against the status bar itself, so they stay readable over anything. Dropdown rows, which sit on a solid background, do use severity colours.

## How it works

Claude Code stores an OAuth token for your claude.ai subscription in the macOS Keychain. This plugin reads that token (read-only) and calls the same usage endpoint the official clients use, so the numbers match the claude.ai settings page exactly. Nothing is scraped or estimated. The plan name comes from the profile endpoint, which the plugin reads at most once a day.

## Requirements

- macOS with [SwiftBar](https://github.com/swiftbar/SwiftBar) (`brew install swiftbar`)
- Python 3.9+ (the system `python3` that ships with the Xcode Command Line Tools is enough; no packages needed)
- [Claude Code](https://claude.com/claude-code) installed and logged in; that login is where the token comes from

## Install

Copy or symlink the plugin into your SwiftBar plugin folder and make it executable:

```bash
git clone https://github.com/archulan/claude-usage-swiftbar.git
cd claude-usage-swiftbar
chmod +x claude-usage.5m.py
ln -s "$PWD/claude-usage.5m.py" "$(defaults read com.ameba.SwiftBar PluginDirectory)/"
```

On first run macOS may ask for Keychain access to "Claude Code-credentials". Click **Always Allow**, otherwise the prompt returns on every refresh.

The refresh interval is the `5m` in the filename. Take care before shortening it further: the usage endpoint is shared with the official Claude clients and rate limits well below a per-minute cadence, and a plugin that polls too hard will get itself (and the usage view in your other Claude apps) throttled. A one-minute interval was measured triggering a 429 within half an hour.

## Rate limiting

If the endpoint answers with a 429, the plugin stops requesting rather than retrying on the next tick. It waits 15 minutes, doubling up to 4 hours if the refusals continue, and the dropdown shows when it will try again. The wait is deliberately not driven by the server's `Retry-After` value alone, which has been observed as `0`. A successful fetch clears the backoff.

The plan name lookup adds about one request a day. It runs only after a successful usage fetch, and a 429 from it starts the same wait.

## Stale token behaviour

The plugin is passive by design. When the access token expires (you have not used Claude Code recently) or the API errors, it keeps showing the last known snapshot marked with a clock or warning symbol and a hint line. Open Claude Code and run anything to refresh the token. The plugin never uses the refresh token, so it cannot interfere with Claude Code's session.

## Troubleshooting

- Run it manually to see the raw output: `./claude-usage.5m.py`
- "Claude Code credentials not found": log in to Claude Code. The plugin reads the Keychain item `Claude Code-credentials`, falling back to `~/.claude/.credentials.json`.
- Clock symbol in the title: token expired, open Claude Code.
- Warning triangle in the title: either a limit needs attention or the usage endpoint failed; the dropdown distinguishes them.
- Cached snapshot lives in SwiftBar's per-plugin cache folder (or `~/.cache/claude-usage/usage.json`). It is the last usage response stored verbatim, so it includes your credit balance alongside the percentages and reset times. The file is written user-only (0600); delete it any time.
- `profile.json` in the same folder holds only the plan label and the next lookup time. Delete it to pick up a plan change before the daily lookup.
- A credits row with an unfamiliar label: the response files dollar allowances under opaque keys that can change, so an unknown key shows a label made from the key itself.

## Disclaimer

This is an unofficial tool, not affiliated with or endorsed by Anthropic. It relies on undocumented endpoints that may change or break without notice. Your token is read locally and only ever sent to `api.anthropic.com`; nothing else is collected or transmitted.

## License

[MIT](LICENSE)
