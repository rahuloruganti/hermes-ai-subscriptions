"""AI Subscriptions — desktop dashboard for AI subscriptions + local usage stats.

The UI half lives in ``desktop-plugins/ai-subscriptions/plugin.js``; this package
hosts the read-only API half (``dashboard/plugin_api.py``) which the page reaches
via ``ctx.rest``. No agent tools or hooks.
"""


def register(ctx):
    """No agent tools or hooks; the desktop page talks to dashboard/plugin_api.py."""
    return None
