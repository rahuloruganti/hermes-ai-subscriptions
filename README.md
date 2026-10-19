# Hermes AI Subscriptions

See every AI subscription you pay for and how much of it you have used, inside Hermes Desktop: a page with Overview, Usage and Plans tabs, plus a status-bar chip where you choose which subscriptions to keep.

There are two editions in this repo.

| | Catalog edition (`catalog/`) | Full edition (repo root) |
| --- | --- | --- |
| Quota source | Hermes's own RPCs | Direct provider calls |
| Providers with quota | whatever Hermes reports (Claude, Nous Plan credits) | Claude, Grok, Nous, Copilot |
| Reads credentials | no | yes (see below) |
| Token usage (Hermes, Claude Code, Grok CLI) | yes | yes |
| Status-bar picker, tabs | yes | yes |

## What it does

- A page with **Overview**, **Usage** and **Plans** tabs, and a status-bar chip whose popover lets you pick which subscriptions stay in the status bar.
- **Usage** covers 7, 30 or 90 days across Hermes, Claude Code and the Grok CLI, per day, per model and per source, with a list-price estimate and measured tokens/second (per model, per day and overall).
- **Click any day in the chart** for that day's breakdown: tokens, calls, cost, per-source split with the input/output/cache mix, the models that ran, and an hour-by-hour strip with the busiest hour.

## Install

Full edition (reads local logins, read this section first):

```
hermes plugins install rahuloruganti/hermes-ai-subscriptions
```

Then enable it, restart Hermes once, and reload desktop plugins. After that, edits to `dashboard/core.py` reload on their own.

The catalog edition is installed from the Hermes plugin catalog by name once listed (`hermes plugins install ai-subscriptions`).

## What the full edition reads

Everything is read-only. Tokens are used for outbound usage calls only and are never returned by the API or written anywhere.

- `<HERMES_HOME>/auth.json`: which providers you are signed in to, and the OAuth token used for the xAI and Nous usage calls.
- `~/.claude/.credentials.json`: Claude Code's token for Anthropic's usage endpoint. Never written or refreshed.
- `~/.claude.json`, `~/.grok/auth.json`: your account email and plan label.
- `gh auth token`: the GitHub CLI token for Copilot's premium request count.
- Token counts only, from `state.db`, `~/.claude/projects`, `~/.grok/logs`, and `~/.tokentracker/cache/pricing.json`.
- One observer hook, `post_api_request`: after each Hermes model call it appends one line to `$HERMES_HOME/plugin-data/ai-subscriptions/api_speed.jsonl` holding the end time, model, output tokens, duration and time to first chunk. Numbers only. This is what lets the dashboard show a measured tokens/second rate for Hermes' own models; it starts from the first call after a restart and is not backfilled. The file rotates at 5 MB.
- Calls: `api.anthropic.com/api/oauth/usage` (paced to one a minute, rate limits honoured across restarts), `cli-chat-proxy.grok.com/v1/billing`, the Nous portal billing endpoint, `api.github.com/copilot_internal/user`.

The last good reading of each provider is saved to `<HERMES_HOME>/plugin-data/ai-subscriptions/state.json` and shown labelled with its age, never as live.

## Limits

Dollar figures are list-price estimates. Gemini and Antigravity keep no token counts on disk. The Copilot endpoint is internal and may change.
