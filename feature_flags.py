"""Feature flags for the 1404 upgrade.

Every capability added in this upgrade is gated behind a boolean flag read
from the environment. Flags default to the values requested by the operator
(enabled), so a fresh deployment with only the legacy variables behaves
exactly as before while each new capability can be disabled individually
before activation by setting the matching variable to ``false``/``0``.

Flag name                    → Environment variable
apify_new_platforms          → APIFY_NEW_PLATFORMS_ENABLED
token_alerts                 → TOKEN_ALERTS_ENABLED
bookmarks                    → BOOKMARKS_ENABLED
autoshare                    → AUTOSHARE_ENABLED
user_stats                   → USER_STATS_ENABLED
dedupe                       → DEDUPE_ENABLED
scheduler                    → SCHEDULER_ENABLED
ai_summary                   → AI_SUMMARY_ENABLED
exact_sizes                  → EXACT_SIZES_ENABLED
perf_cache                   → PERF_CACHE_ENABLED
circuit_breaker              → CIRCUIT_BREAKER_ENABLED
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _flag(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return default


@dataclass(frozen=True)
class FeatureFlags:
    apify_new_platforms: bool
    token_alerts: bool
    bookmarks: bool
    autoshare: bool
    user_stats: bool
    dedupe: bool
    scheduler: bool
    ai_summary: bool
    exact_sizes: bool
    perf_cache: bool
    circuit_breaker: bool

    def as_dict(self) -> dict[str, bool]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def load_flags(environ: dict[str, str] | None = None) -> FeatureFlags:
    env = dict(os.environ if environ is None else environ)
    return FeatureFlags(
        # Apify for Spotify/SoundCloud/Twitter/Facebook/Pinterest. Requires
        # APIFY_TOKENS as before; the flag only controls the NEW platforms.
        apify_new_platforms=_flag(env, "APIFY_NEW_PLATFORMS_ENABLED", True),
        token_alerts=_flag(env, "TOKEN_ALERTS_ENABLED", True),
        bookmarks=_flag(env, "BOOKMARKS_ENABLED", True),
        autoshare=_flag(env, "AUTOSHARE_ENABLED", True),
        user_stats=_flag(env, "USER_STATS_ENABLED", True),
        dedupe=_flag(env, "DEDUPE_ENABLED", True),
        scheduler=_flag(env, "SCHEDULER_ENABLED", True),
        # AI needs a free-tier API key on top of the flag; ai_available()
        # stays False without one, so this flag is safe to default on.
        ai_summary=_flag(env, "AI_SUMMARY_ENABLED", True),
        exact_sizes=_flag(env, "EXACT_SIZES_ENABLED", True),
        perf_cache=_flag(env, "PERF_CACHE_ENABLED", True),
        circuit_breaker=_flag(env, "CIRCUIT_BREAKER_ENABLED", True),
    )


FLAGS = load_flags()
