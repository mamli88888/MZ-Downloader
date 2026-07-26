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
    music_finder_bot: str
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
    google_drive_service_account_json: str
    google_drive_folder_id: str
    google_drive_delete_delay: float


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
        "allsaverbot,instadowbot,download_it_bot,AllSavesBot",
    )
    tiktok_bots = bot_list("TIKTOK_DOWNLOADER_BOTS", "download_it_bot,AllSavesBot")
    twitter_bots = bot_list("TWITTER_BOTS", "AllSavesBot,download_it_bot")
    spotify_track_bots = bot_list(
        "SPOTIFY_TRACK_BOTS", "SpotSeekBot,Dr_downloader_bot,spotifysavesbot"
    )
    spotify_collection_primary_bot = bot_name(
        "SPOTIFY_COLLECTION_PRIMARY_BOT", "Dr_downloader_bot"
    )
    music_finder_bot = bot_name("MUSIC_FINDER_BOT", "whatisthismusicbot")
    google_drive_service_account_json = env.get(
        "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", ""
    ).strip()
    google_drive_folder_id = env.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
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
        music_finder_bot=music_finder_bot,
        google_drive_service_account_json=google_drive_service_account_json,
        google_drive_folder_id=google_drive_folder_id,
        google_drive_delete_delay=_as_float(
            env, "GOOGLE_DRIVE_DELETE_DELAY_SECONDS", 3600.0
        ),
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
