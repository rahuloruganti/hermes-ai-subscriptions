# AI Subscriptions

A Hermes Desktop page, a status-bar chip and a popover that show your AI subscriptions and how much of them you have used.

- **Overview, Usage and Plans tabs.** Pick 7, 30 or 90 days, toggle sources, click a day to see which models used it, sort the model table.
- **Status-bar chip.** Shows your tightest quota by default. Tick the subscriptions you want to keep there, and optionally today's token count. Your choice is saved.
- **Quota** for every signed-in provider that reports it, taken from Hermes itself (`model.options` and `usage.bars`). Nothing here reads a credential.
- **Token usage across this PC:** Hermes, Claude Code and the Grok CLI, with an estimated dollar value from Hermes's own price list. Estimates are list prices, not what you were billed.

## What it reads

Read-only, local, no network calls of its own:

| Path | Why |
| --- | --- |
| `<HERMES_HOME>/state.db` | Hermes session token counts |
| `<HERMES_HOME>/models_dev_cache.json` | list prices for the dollar estimate |
| `~/.claude/projects/**/*.jsonl` | per-message token counts Claude Code already logs |
| `~/.grok/logs/unified.jsonl`, `~/.grok/sessions/*/*/summary.json` | token counts the Grok CLI already logs |

It writes nothing outside the plugin's own browser storage (tab, range, status-bar choice).

## What it does not do

No credential, `.env` or `auth.json` reads. No provider API calls from the plugin. No self-update. Gemini and Antigravity keep no token counts on disk, so they are not included.

## Full edition

A fuller version with live Claude, Grok, Nous and Copilot polling lives in the repository root. It reads local logins and so is not part of the catalog.
