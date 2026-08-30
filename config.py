from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AccountConfig:
    name: str
    api_id: int
    api_hash: str
    phone: str
    session: str
    string_session: str = ""

    @property
    def session_path(self) -> Path:
        path = Path(self.session)
        return path if path.is_absolute() else PROJECT_DIR / path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    accounts: tuple[AccountConfig, ...]
    downloader_bots: tuple[str, ...]
    primary_bot: str
    secondary_bot: str
    spotify_bot: str
    soundcloud_bot: str
    instagram_youtube_bots: tuple[str, ...]
    tiktok_bots: tuple[str, ...]
    twitter_bots: tuple[str, ...]
    spotify_track_bots: tuple[str, ...]
    spotify_collection_primary_bot: str
    fallback_bots: tuple[str, ...]
    music_finder_bots: tuple[str, ...]
    download_root: Path
    max_file_size: int
    max_download_size: int
    wait_timeout: float
    selection_ttl: float
    album_collect_window: float
    preview_grace: float
    late_response_cooldown: float
    max_links_per_message: int
    max_concurrent_updates: int
    max_queue_size: int
    worker_acquire_timeout: float
    rate_limit_requests: int
    rate_limit_window: float
    use_proxy: bool
    proxy_type: str
    proxy_host: str
    proxy_port: int
    pixeldrain_delete_delay: float
    pixeldrain_api_key: str | None
    # AHM7 gateway (primary downloader for TikTok / Instagram / Facebook /
    # X-Twitter / Reddit / Snapchat / SoundCloud / CapCut / SnackVideo /
    # Douyin via https://ahm7xmakki.com/api/alldl).
    ahm7_enabled: bool
    ahm7_api_url: str
    # Yoinku gateway (fallback #1 for YouTube via https://yoinku.com/api/v1).
    # Multi-key rotation with per-key daily + per-minute rate limits.
    yoinku_enabled: bool
    yoinku_api_base: str
    yoinku_api_keys: tuple[str, ...]
    yoinku_daily_limit: int
    yoinku_per_minute_limit: int
    # VoidDL gateway (PRIMARY downloader for YouTube via
    # https://voiddl.app). Per key: 20 downloads/minute and 10 GB of
    # daily bandwidth — multiple keys rotate instantly when one hits
    # either cap. Fallback chain: VoidDL → Yoinku → Apify → Telegram
    # bots.
    voiddl_enabled: bool
    voiddl_api_base: str
    voiddl_api_keys: tuple[str, ...]
    voiddl_daily_bandwidth_mb: int
    voiddl_per_minute_limit: int
    # VoidDL speed layer: parallel ranged lanes + background prefetch.
    # The server prepares every request fresh (15-35 s, NOT cached) but
    # preparations for concurrent requests run in parallel, so firing N
    # lanes at once yields ~N x per-connection throughput. The prefetch
    # starts the most likely qualities while the user is still looking
    # at the quality card, hiding the preparation wait.
    voiddl_parallel_lanes: int
    voiddl_prefetch_enabled: bool
    voiddl_prefetch_count: int
    voiddl_prefetch_lanes: int
    voiddl_prefetch_max_mb: int
    voiddl_prefetch_min_remaining_mb: int
    voiddl_prefetch_ttl: int
    # Apify Actors for public YouTube and Instagram downloads. Tokens rotate
    # when a token-side error, quota problem, or billing limit is reported.
    apify_enabled: bool
    apify_tokens: tuple[str, ...]
    apify_run_timeout: float
    apify_poll_interval: float
    apify_token_cooldown: float
    # 1404 upgrade — numeric chat id of the main admin receiving token alerts
    bot_admin_chat_id: int
    # CreatorCrawl (Instagram /profile feature). Keys are listed as
    # "API_KEY|EMAIL" pairs (the email identifies the account each key
    # belongs to, used in the admin PV when a key burns its quota).
    creatorcrawl_keys: tuple[str, ...]
    creatorcrawl_key_limit: int
    # Persistent KV for CreatorCrawl usage counters — Upstash Redis REST
    # survives restarts, redeploys and deploys on a different Railway
    # account (the local cc_usage.json fallback does NOT).
    upstash_rest_url: str
    upstash_rest_token: str


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value: {value!r}")


def _as_int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _as_float(env: Mapping[str, str], name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(env.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _load_accounts(env: Mapping[str, str]) -> tuple[AccountConfig, ...]:
    raw = (env.get("TELEGRAM_ACCOUNTS") or "[]").strip()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("TELEGRAM_ACCOUNTS must be valid JSON") from exc
    if not isinstance(entries, list):
        raise ConfigError("TELEGRAM_ACCOUNTS must be a JSON list")

    accounts: list[AccountConfig] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"Account {index} must be a JSON object")
        try:
            account = AccountConfig(
                name=str(entry.get("name") or f"Account-{index}"),
                api_id=int(entry["api_id"]),
                api_hash=str(entry["api_hash"]).strip(),
                phone=str(entry.get("phone") or "").strip(),
                session=str(entry.get("session") or f"mz_downloader_session_{index}"),
                string_session=str(entry.get("string_session") or entry.get("session_string") or "").strip(),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Account {index} is incomplete") from exc
        if not account.api_hash:
            raise ConfigError(f"Account {index} has empty credentials")
        accounts.append(account)
    return tuple(accounts)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = dict(os.environ if environ is None else environ)

    def bot_list(name: str, default: str) -> tuple[str, ...]:
        values = (
            item.strip().lstrip("@").lower()
            for item in env.get(name, default).split(",")
            if item.strip()
        )
        return tuple(dict.fromkeys(values))

    bots = bot_list("DOWNLOADER_BOTS", "download_it_bot,AllSavesBot")
    if not bots:
        raise ConfigError("At least one DOWNLOADER_BOTS entry is required")

    def bot_name(name: str, default: str) -> str:
        value = env.get(name, default).strip().lstrip("@").lower()
        if not value:
            raise ConfigError(f"{name} must not be empty")
        return value

    primary_bot = bot_name("PRIMARY_DOWNLOADER_BOT", bots[0])
    secondary_bot = bot_name("SECONDARY_DOWNLOADER_BOT", bots[1] if len(bots) > 1 else "AllSavesBot")
    spotify_bot = bot_name("SPOTIFY_DOWNLOADER_BOT", "spotifysavesbot")
    soundcloud_bot = bot_name("SOUNDCLOUD_DOWNLOADER_BOT", "scload_bot")
    instagram_youtube_bots = bot_list(
        "INSTAGRAM_YOUTUBE_BOTS",
        "ziyotech_instagram_downloaderbot,allsaverbot,instadowbot,download_it_bot,AllSavesBot",
    )
    tiktok_bots = bot_list("TIKTOK_DOWNLOADER_BOTS", "download_it_bot,AllSavesBot")
    twitter_bots = bot_list("TWITTER_BOTS", "AllSavesBot,download_it_bot")
    spotify_track_bots = bot_list(
        "SPOTIFY_TRACK_BOTS", "SpotSeekBot,Dr_downloader_bot,spotifysavesbot"
    )
    spotify_collection_primary_bot = bot_name(
        "SPOTIFY_COLLECTION_PRIMARY_BOT", "Dr_downloader_bot"
    )
    music_finder_bots = bot_list("MUSIC_FINDER_BOTS", "whatisthismusicbot")

    fallback_bots = tuple(
        dict.fromkeys(
            (
                *instagram_youtube_bots,
                *tiktok_bots,
                *twitter_bots,
                *spotify_track_bots,
                primary_bot,
                secondary_bot,
                spotify_bot,
                soundcloud_bot,
            )
        )
    )

    proxy_type = env.get("PROXY_TYPE", "socks5").strip().lower()
    if proxy_type not in {"socks5", "http"}:
        raise ConfigError("PROXY_TYPE must be socks5 or http")

    download_dir = Path(env.get("DOWNLOAD_DIR", "downloads"))
    download_root = download_dir if download_dir.is_absolute() else PROJECT_DIR / download_dir

    return Settings(
        bot_token=env.get("BOT_TOKEN", "").strip(),
        accounts=_load_accounts(env),
        downloader_bots=bots,
        primary_bot=primary_bot,
        secondary_bot=secondary_bot,
        spotify_bot=spotify_bot,
        soundcloud_bot=soundcloud_bot,
        instagram_youtube_bots=instagram_youtube_bots,
        tiktok_bots=tiktok_bots,
        twitter_bots=twitter_bots,
        spotify_track_bots=spotify_track_bots,
        spotify_collection_primary_bot=spotify_collection_primary_bot,
        fallback_bots=fallback_bots,
        music_finder_bots=music_finder_bots,
        pixeldrain_delete_delay=_as_float(env, "PIXELDRAIN_DELETE_DELAY_SECONDS", 1800.0),
        pixeldrain_api_key=env.get("PIXELDRAIN_API_KEY", "").strip() or None,
        ahm7_enabled=_as_bool(env.get("AHM7_ENABLED"), True),
        ahm7_api_url=env.get("AHM7_API_URL", "https://ahm7xmakki.com/api/alldl").strip(),
        yoinku_enabled=_as_bool(env.get("YOINKU_ENABLED"), True),
        yoinku_api_base=env.get("YOINKU_API_BASE", "https://yoinku.com/api/v1").strip(),
        yoinku_api_keys=tuple(
            k.strip()
            for k in env.get("YOINKU_API_KEYS", "").split(",")
            if k.strip()
        ),
        yoinku_daily_limit=_as_int(env, "YOINKU_DAILY_LIMIT", 30, minimum=1),
        yoinku_per_minute_limit=_as_int(env, "YOINKU_PER_MINUTE_LIMIT", 5, minimum=1),
        voiddl_enabled=_as_bool(env.get("VOIDDL_ENABLED"), True),
        voiddl_api_base=env.get("VOIDDL_API_BASE", "https://voiddl.app").strip(),
        # Comma-separated VoidDL API keys. When empty, the primary key
        # shipped with the reference voiddl.py CLI is used so the bot
        # works out of the box; set VOIDDL_API_KEYS to override/extend.
        voiddl_api_keys=tuple(
            dict.fromkeys(
                k.strip()
                for k in (
                    env.get("VOIDDL_API_KEYS")
                    or "vd_3RwouwKvrvuVfDo4_iakuaHaN-FuerC4"
                ).split(",")
                if k.strip()
            )
        ),
        voiddl_daily_bandwidth_mb=_as_int(env, "VOIDDL_DAILY_BANDWIDTH_MB", 10240, minimum=1),
        voiddl_per_minute_limit=_as_int(env, "VOIDDL_PER_MINUTE_LIMIT", 20, minimum=1),
        # Speed layer — see the dataclass comment above for the rationale.
        voiddl_parallel_lanes=_as_int(env, "VOIDDL_PARALLEL_LANES", 8, minimum=1),
        voiddl_prefetch_enabled=_as_bool(env.get("VOIDDL_PREFETCH"), True),
        voiddl_prefetch_count=_as_int(env, "VOIDDL_PREFETCH_COUNT", 2, minimum=0),
        voiddl_prefetch_lanes=_as_int(env, "VOIDDL_PREFETCH_LANES", 4, minimum=1),
        voiddl_prefetch_max_mb=_as_int(env, "VOIDDL_PREFETCH_MAX_MB", 512, minimum=1),
        voiddl_prefetch_min_remaining_mb=_as_int(env, "VOIDDL_PREFETCH_MIN_REMAINING_MB", 2048, minimum=0),
        voiddl_prefetch_ttl=_as_int(env, "VOIDDL_PREFETCH_TTL", 720, minimum=60),
        apify_enabled=_as_bool(env.get("APIFY_ENABLED"), True),
        # APIFY_TOKEN remains accepted for backward compatibility. Prefer the
        # comma-separated APIFY_TOKENS variable for rotation and failover.
        apify_tokens=tuple(dict.fromkeys(
            token.strip()
            for token in (env.get("APIFY_TOKENS") or env.get("APIFY_TOKEN", "")).split(",")
            if token.strip()
        )),
        apify_run_timeout=_as_float(env, "APIFY_RUN_TIMEOUT_SECONDS", 360.0, minimum=30.0),
        apify_poll_interval=_as_float(env, "APIFY_POLL_INTERVAL_SECONDS", 3.0, minimum=0.2),
        apify_token_cooldown=_as_float(env, "APIFY_TOKEN_COOLDOWN_SECONDS", 600.0, minimum=5.0),
        bot_admin_chat_id=max(0, int(env.get("BOT_ADMIN_CHAT_ID", "0") or 0)),
        creatorcrawl_keys=tuple(
            k.strip()
            for k in env.get("CREATORCRAWL_API_KEYS", "").split(",")
            if k.strip()
        ),
        creatorcrawl_key_limit=_as_int(env, "CREATORCRAWL_KEY_LIMIT", 50, minimum=1),
        upstash_rest_url=env.get("UPSTASH_REDIS_REST_URL", "").strip(),
        upstash_rest_token=env.get("UPSTASH_REDIS_REST_TOKEN", "").strip(),
        download_root=download_root,
        max_file_size=_as_int(env, "MAX_FILE_SIZE_MB", 30) * 1024 * 1024,
        # Zero disables the application-level source-size cap. Telegram upload
        # limits are handled separately by split_file/send_large_file.
        max_download_size=_as_int(env, "MAX_DOWNLOAD_SIZE_MB", 0, minimum=0) * 1024 * 1024,
        wait_timeout=_as_float(env, "WAIT_TIMEOUT_SECONDS", 90.0),
        selection_ttl=_as_float(env, "SELECTION_TTL_SECONDS", 600.0),
        album_collect_window=_as_float(env, "ALBUM_COLLECT_WINDOW_SECONDS", 2.5),
        preview_grace=_as_float(env, "PREVIEW_GRACE_SECONDS", 3.0),
        late_response_cooldown=_as_float(env, "LATE_RESPONSE_COOLDOWN_SECONDS", 180.0),
        max_links_per_message=_as_int(env, "MAX_LINKS_PER_MESSAGE", 5),
        max_concurrent_updates=_as_int(env, "MAX_CONCURRENT_UPDATES", 12),
        max_queue_size=_as_int(env, "MAX_QUEUE_SIZE", 50),
        worker_acquire_timeout=_as_float(env, "WORKER_ACQUIRE_TIMEOUT_SECONDS", 180.0),
        rate_limit_requests=_as_int(env, "RATE_LIMIT_REQUESTS", 8),
        rate_limit_window=_as_float(env, "RATE_LIMIT_WINDOW_SECONDS", 60.0),
        use_proxy=_as_bool(env.get("USE_PROXY"), False),
        proxy_type=proxy_type,
        proxy_host=env.get("PROXY_HOST", "127.0.0.1").strip(),
        proxy_port=_as_int(env, "PROXY_PORT", 10808),
    )


SETTINGS = load_settings()
