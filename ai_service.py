"""Free-tier AI helpers for MZ-Downloader (standalone; stdlib + httpx only).

Persian summaries (summarize_persian), Persian/English tag suggestions
(suggest_tags) and FAQ answering (faq_answer — local keywords first, AI
fallback). Providers: HuggingFace / Cohere / Mistral via AI_PROVIDER env;
config is read at call time and cached in-module.

Design rule: every public function degrades gracefully — any failure,
timeout, rate refusal or parse error returns ``None``; nothing ever raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque

import httpx

logger = logging.getLogger("MZDownloader.ai")

_DEFAULT_MODELS: dict[str, str] = {
    "huggingface": "Qwen/Qwen2.5-7B-Instruct",
    "cohere": "command-r-08-2024",
    "mistral": "mistral-small-latest",
}

# Free-tier fallback models tried in order when the primary HF model 404s
# or is not served on the Inference Providers router.
_HF_MODEL_FALLBACKS: list[str] = [
    "Qwen/Qwen2.5-7B-Instruct",
    "microsoft/Phi-3.5-mini-instruct",
    "HuggingFaceH4/zephyr-7b-beta",
]

_CFG: dict | None = None


def _detect_provider(raw: str, api_key: str) -> str:
    """Resolve the effective provider.

    Fixes the "I set the token but it says token missing" confusion: the
    previous logic required AI_PROVIDER to name a provider explicitly and
    silently treated everything else as "off". Now:

      * explicit huggingface/cohere/mistral wins;
      * "auto" (or unset/off) + a key that looks like HuggingFace (``hf_…``)
        → huggingface;
      * "auto" (or unset/off) + any other non-empty key → huggingface (the
        default free option) with a warning that names the fix;
      * "off" with no key → off.
    """
    normalized = (raw or "").strip().lower()
    if normalized in _DEFAULT_MODELS:
        return normalized
    if normalized == "off":
        if not api_key:
            return "off"
        logger.warning(
            "AI_PROVIDER is 'off' but AI_API_KEY is set — auto-enabling provider 'huggingface'. "
            "Set AI_PROVIDER=cohere or AI_PROVIDER=mistral if that key belongs to another service."
        )
        return "huggingface"
    # unset / "auto" / typo + key present → auto-detect
    if not api_key:
        return "off"
    if api_key.startswith("hf_"):
        return "huggingface"
    return "huggingface"


def _get_cfg() -> dict:
    """Read env on each call; rebuild the cached dict only when values change."""
    global _CFG

    def _num(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, "") or default)
        except (TypeError, ValueError):
            return default

    api_key = (os.getenv("AI_API_KEY", "") or "").strip()
    raw_provider = (os.getenv("AI_PROVIDER", "") or "").strip().lower()
    # Normalize the legacy alias so docs stay valid.
    if raw_provider == "hugging face":
        raw_provider = "huggingface"
    provider = _detect_provider(raw_provider, api_key)
    fresh = {
        "provider": provider,
        "api_key": api_key,
        "model": (os.getenv("AI_MODEL", "") or "").strip() or _DEFAULT_MODELS.get(provider, ""),
        "rate": _num("AI_RATE_PER_MINUTE", 10.0),
        "timeout": _num("AI_TIMEOUT_SECONDS", 20.0),
        "max_input": int(_num("AI_MAX_INPUT_CHARS", 6000.0)),
    }
    sig = tuple(fresh.values())
    if _CFG is None or _CFG.get("sig") != sig:
        fresh["sig"] = sig
        _CFG = fresh
    return _CFG


def ai_available() -> bool:
    """True when a provider is selected and an API key is configured."""
    cfg = _get_cfg()
    return cfg["provider"] != "off" and bool(cfg["api_key"])


# Sliding-window rate limiter + TTL result cache + health counters.
_CALL_TIMES: deque[float] = deque()
_CACHE: dict[str, tuple[float, str | list[str]]] = {}
_CACHE_TTL = 24 * 3600.0
_CACHE_MAX = 500
_STATS = {"calls_total": 0, "refused_rate": 0, "failures": 0, "cache_hits": 0}


def _allow_call(rate: float) -> bool:
    """Sync sliding-window limiter (60s window). Instant refuse — never awaits."""
    now = time.monotonic()
    while _CALL_TIMES and now - _CALL_TIMES[0] >= 60.0:
        _CALL_TIMES.popleft()
    if rate <= 0 or len(_CALL_TIMES) >= rate:
        _STATS["refused_rate"] += 1
        return False
    _CALL_TIMES.append(now)
    return True


def _cache_key(kind: str, *parts: object) -> str:
    raw = "\x00".join(str(p) for p in parts)
    return kind + ":" + hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def _cache_get(key: str) -> str | list[str] | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    expires, value = hit
    if expires < time.monotonic():
        _CACHE.pop(key, None)
        return None
    _STATS["cache_hits"] += 1
    return value


def _cache_set(key: str, value: str | list[str]) -> None:
    now = time.monotonic()
    for stale in [k for k, (exp, _) in _CACHE.items() if exp < now]:
        del _CACHE[stale]
    _CACHE[key] = (now + _CACHE_TTL, value)
    while len(_CACHE) > _CACHE_MAX:  # evict oldest inserted entry
        _CACHE.pop(next(iter(_CACHE)))


def _messages(prompt: str, system: str) -> list[dict]:
    msgs = [{"role": "system", "content": system}] if system else []
    return msgs + [{"role": "user", "content": prompt}]


def _build_payload(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> dict:
    # All three providers now speak the OpenAI chat-completions dialect:
    # Cohere v2, Mistral and the HuggingFace Inference Providers router.
    return {"model": model, "messages": _messages(prompt, system), "max_tokens": max_tokens}


def _extract(provider: str, data: object, payload: dict) -> str | None:
    """Best-effort text extraction per provider response schema."""
    try:
        # OpenAI-compatible shape (mistral + huggingface router + cohere v2
        # all return choices[0].message.content; cohere v2 may also return
        # message.content as a list of {type: text} parts).
        text: object = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    text = message.get("content")
            if text is None and isinstance(data.get("message"), dict):
                content = data["message"].get("content")
                if isinstance(content, list):
                    text = next(
                        (part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text"),
                        None,
                    )
                else:
                    text = content
        if text is None and isinstance(data, list) and data and isinstance(data[0], dict):
            # Legacy hf-inference shape: [{"generated_text": ...}]
            text = data[0].get("generated_text")
        if not isinstance(text, str):
            return None
        return text.strip() or None
    except Exception:
        return None


def _fail(msg: str):
    """Count a failure, log at debug level, signal None to the caller."""
    _STATS["failures"] += 1
    logger.debug("ai %s", msg)
    return None


async def _chat(prompt: str, system: str = "", max_tokens: int = 400) -> str | None:
    """Single entry point for all provider calls. Returns reply text or None."""
    cfg = _get_cfg()
    provider, key = cfg["provider"], cfg["api_key"]
    if provider == "off" or not key:
        return None
    if provider == "cohere":
        url = "https://api.cohere.com/v2/chat"
    elif provider == "mistral":
        url = "https://api.mistral.ai/v1/chat/completions"
    else:  # huggingface — Inference Providers router (OpenAI-compatible)
        url = "https://router.huggingface.co/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    # Free-tier robustness: if the configured HF model is unavailable (404 /
    # not-served), retry once with each documented fallback model.
    model_chain: list[str] = [cfg["model"]]
    if provider == "huggingface":
        for fallback in _HF_MODEL_FALLBACKS:
            if fallback not in model_chain:
                model_chain.append(fallback)
    last_failure = ""
    for model in model_chain:
        payload = _build_payload(provider, model, prompt, system, max_tokens)
        _STATS["calls_total"] += 1
        try:
            async with httpx.AsyncClient(timeout=cfg["timeout"], headers=headers) as client:
                resp = await asyncio.wait_for(client.post(url, json=payload), timeout=cfg["timeout"] * 1.2)
        except Exception as exc:  # network / DNS / timeout / cancellation
            last_failure = f"{provider} request failed: {exc}"
            continue
        if resp.status_code != 200:
            last_failure = f"{provider} http {resp.status_code}: {resp.text[:200]}"
            # 404/400 on the router usually means the model isn't served —
            # the fallback chain is exactly for this. Other codes (401/429)
            # will fail on every model, but one extra attempt is cheap.
            continue
        try:
            data = resp.json()
        except Exception:
            last_failure = f"{provider} returned non-json body"
            continue
        text = _extract(provider, data, payload)
        if text:
            return text
        last_failure = f"{provider} response had no usable text"
    return _fail(last_failure or f"{provider} produced no answer")


def _strip_quotes(text: str) -> str:
    return text.strip().strip("\"'`«»“”").strip()


def _clip_sentences(text: str, limit: int) -> str:
    """Hard ceiling: cut at last sentence boundary if within reasonable range."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind("."), cut.rfind("!"), cut.rfind("؟"), cut.rfind("?"), cut.rfind("\n"))
    if best < int(limit * 0.5):
        best = limit
    return text[:best].rstrip(" ،؛.") + "…"


_SUMMARY_SYSTEM = (
    "You are a helpful assistant. Summarize the following content in "
    "Persian (Farsi). Be concise, factual, use bullet points with '•'. "
    "No preamble."
)


async def summarize_persian(text: str, *, max_chars: int = 900) -> str | None:
    """Summarize text content into Persian. Returns None on any problem."""
    try:
        if not isinstance(text, str) or not ai_available() or len(text.strip()) < 120:
            return None
        cfg = _get_cfg()
        key = _cache_key("sum", cfg["provider"], cfg["model"], max_chars, text)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if not _allow_call(cfg["rate"]):
            return None
        prompt = text[: cfg["max_input"]] + f"\n\nخلاصه را حداکثر در {max_chars} کاراکتر بنویس."
        raw = await _chat(prompt, system=_SUMMARY_SYSTEM) or ""
        result = _clip_sentences(_strip_quotes(raw), int(max_chars * 1.6))
        if result:
            _cache_set(key, result)
        return result or None
    except Exception as exc:
        logger.debug("summarize_persian failed: %s", exc)
        return None


_TAGS_SYSTEM = (
    "You are a tagging assistant. Reply with ONLY a JSON array of short "
    "hashtags (no '#' symbol), mixing Persian and English tags."
)


def _parse_tags(raw: str, count: int) -> list[str] | None:
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(raw[i : j + 1])
        if not isinstance(data, list):
            return None
    except Exception:
        return None
    tags = [t.strip().lstrip("#").strip() for t in data if isinstance(t, str) and t.strip()]
    return tags[:count] or None


async def suggest_tags(text: str, *, count: int = 8) -> list[str] | None:
    """Suggest Persian+English tags for content. Returns None on any problem."""
    try:
        if not isinstance(text, str) or not ai_available() or len(text.strip()) < 60:
            return None
        cfg = _get_cfg()
        key = _cache_key("tags", cfg["provider"], cfg["model"], count, text)
        cached = _cache_get(key)
        if cached is not None:
            return list(cached)
        if not _allow_call(cfg["rate"]):
            return None
        prompt = (
            f"{text[: cfg['max_input']]}\n\nReturn ONLY a JSON array of exactly {count} short tags "
            "for this content, mixing Persian and English, without the '#' symbol. Array only."
        )
        raw = await _chat(prompt, system=_TAGS_SYSTEM)
        tags = _parse_tags(raw, count) if raw else None
        if tags:
            _cache_set(key, tags)
        return tags
    except Exception as exc:
        logger.debug("suggest_tags failed: %s", exc)
        return None


# (keywords, answer, is_generic). Matching is two-tier & score-based:
# tier 1 = specific topics (platforms/features), tier 2 = generic catch-alls
# («دانلود/لینک/چطور»). A specific match ALWAYS wins over the generic row —
# otherwise «چطور از پینترست دانلود کنم» would score the generic words higher.
_FAQ: list[tuple[list[str], str, bool]] = [
    (
        ["زیرنویس", "subtitle", "زیر نویس", "sub"],
        "برای زیرنویس یوتیوب: لینک ویدیو را بفرستید و بعد از نمایش منو، دکمهٔ «🈶 زیرنویس» را بزنید تا فهرست زبان‌ها (فارسی/انگلیسی/…) نمایش داده شود و فایل زیرنویس برایتان ارسال می‌شود.",
        False,
    ),
    (
        ["اسپاتیفای", "spotify", "spoti"],
        "لینک تراک اسپاتیفای را بفرستید تا منوی دانلود MP3 با کیفیت بالا و متادیتای کامل (نام تراک، خواننده، آلبوم و کاور) نمایش داده شود. آلبوم و پلی‌لیست هم پشتیبانی می‌شود و به‌صورت فایل ZIP ارسال می‌گردد.",
        False,
    ),
    (
        ["پینترست", "pinterest", "پین ترست"],
        "لینک پین پینترست را بفرستید؛ ربات خودش تشخیص می‌دهد پین تصویری است یا ویدیویی و فقط گزینه‌های مرتبط را نشان می‌دهد. تصاویر با کیفیت اصلی ارسال می‌شوند.",
        False,
    ),
    (
        ["توییتر", "twitter", "ایکس", " x ", "x.com"],
        "لینک توییت را بفرستید تا منوی گزینه‌ها نمایش داده شود: ویدیوی باکیفیت، ویدیوی کم‌حجم، همهٔ تصاویر و حتی متن کامل توییت همراه آمار (لایک/ریتوییت/ریپلای).",
        False,
    ),
    (
        ["ساوندکلاد", "soundcloud", "ساند کلاد"],
        "لینک تراک ساوندکلاد را بفرستید تا فایل MP3 با بهترین کیفیت موجود دریافت و ارسال شود. پلی‌لیست‌ها هم از مسیر پشتیبان دانلود می‌شوند.",
        False,
    ),
    (
        ["فیسبوک", "facebook", "فیس بوک"],
        "لینک ویدیوی عمومی فیسبوک (watch، reel یا ویدیوی پیج) را بفرستید تا منوی «ویدیو» یا «فقط صدا (MP3)» نمایش داده شود.",
        False,
    ),
    (
        ["اینستاگرام", "instagram", "اینستا", "ریلز", "reel"],
        "لینک پست، ریلز یا IGTV عمومی اینستاگرام را بفرستید؛ منوی کیفیت، فقط صدا، کپشن پست و موزیک ریلز نمایش داده می‌شود. با /profile یوزرنیم هم می‌توانید پروفایل و استوری‌ها را ببینید.",
        False,
    ),
    (
        ["یوتیوب", "youtube"],
        "لینک ویدیو یا شورتز یوتیوب را بفرستید تا منوی کیفیت (تا 4K) یا MP3 نمایش داده شود. برای جستجو کافی است نام ویدیو را همین‌جا بنویسید یا /search بزنید.",
        False,
    ),
    (
        ["تیک تاک", "تیکتاک", "tiktok"],
        "لینک ویدیوی تیک‌تاک را بفرستید تا بدون واترمارک و با بهترین کیفیت دانلود و ارسال شود.",
        False,
    ),
    (
        ["بوکمارک", "bookmark", "ذخیره"],
        "بعد از هر دانلود موفق دکمهٔ «🔖 ذخیره» زیر فایل نمایش داده می‌شود؛ با ذخیره کردن، لینک در فهرست شخصی شما می‌رود و هر وقت خواستید با /bookmarks می‌بینید و دوباره دریافت می‌کنید.",
        False,
    ),
    (
        ["زمانبندی", "زمان بندی", "schedule", "زمان‌بندی"],
        "با دستور /schedule می‌توانید دانلود خودکار بسازید: «/schedule لینک 7d» یعنی هر ۷ روز یک‌بار آن لینک به‌صورت خودکار دانلود و همین‌جا برایتان ارسال شود. بازه‌های مجاز: 90m، 12h، 1d، 7d و 2w.",
        False,
    ),
    (
        ["آمار شخصی", "امار شخصی", "امار من", "آمار من", "mystats", "گزارش شخصی"],
        "با دستور /mystats آمار شخصی ۳۰ روز اخیر خود را ببینید: تعداد دانلودها، حجم کل، پلتفرم‌های پراستفاده و نمودار روزانه.",
        False,
    ),
    (
        ["اشتراک", "autoshare", "کانال", "ارسال خودکار"],
        "با /autoshare add داخل کانال یا گروه خودتان (ربات باید ادمین باشد) مقصد ثبت کنید؛ از این بعد هر محتوایی که دانلود کنید به‌صورت خودکار به آنجا هم ارسال می‌شود.",
        False,
    ),
    (
        ["خلاصه", "summarize", "خلاصه کن"],
        "زیر کپشن‌های اینستاگرام و متن توییت‌ها دکمهٔ «🤖 خلاصه کن» نمایش داده می‌شود؛ با زدن آن خلاصهٔ فارسی همان محتوا برایتان ارسال می‌شود.",
        False,
    ),
    (
        ["کیفیت", "quality", "رزولوشن", "720", "1080", "4k"],
        "بعد از فرستادن لینک، منوی کیفیت‌ها (مثلاً 144p تا 4K یا فقط صدا) نمایش داده می‌شود؛ حجم تقریبی هر گزینه کنار دکمه نوشته شده و کافی است روی گزینهٔ دلخواه بزنید.",
        False,
    ),
    (
        ["حجم", "مگابایت", "گیگ", "لیمیت", "بزرگ", "limit"],
        "فایل‌های بزرگ‌تر از حد تلگرام به‌صورت خودکار روی فضای ابری آپلود می‌شوند و لینک دانلود موقت برایتان ارسال می‌شود؛ پس عملاً محدودیتی حس نمی‌کنید.",
        False,
    ),
    (
        ["توقف", "لغو", "cancel", "کنسل"],
        "با دستور /cancel می‌توانید دانلودهای در حال انجام خودتان را متوقف کنید.",
        False,
    ),
    (
        ["شروع", "start", "چه کاری", "چیکار", "قابلیت", "معرفی"],
        "این ربات دانلودر است: لینک محتوا (ویدیو، موسیقی، پست، پین و…) را بفرستید تا دانلود و برایتان ارسال شود. پلتفرم‌ها: اینستاگرام، یوتیوب، تیک‌تاک، توییتر/X، فیسبوک، اسپاتیفای، ساوندکلاد، پینترست، VK و… . راهنمای کامل: /help",
        True,
    ),
    (
        ["دانلود", "لینک", "download", "چطور", "چطوری", "نصب", "استفاده"],
        "فقط کافی است لینک محتوای دلخواهتان را در همین گفتگو بفرستید؛ ربات نوع محتوا را تشخیص می‌دهد، منوی گزینه‌ها را نشان می‌دهد و پس از انتخاب شما، فایل را ارسال می‌کند. در گروه‌ها هم با /dl لینک یا ریپلای کار می‌کند.",
        True,
    ),
]


def _norm(s: str) -> str:
    s = s.lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", "")
    return " ".join(s.split())


def _best_faq(q: str) -> str | None:
    """Two-tier score-based match: specific topics always outrank the
    generic «how to download» catch-all rows."""
    best_specific: tuple[int, str] | None = None
    best_generic: tuple[int, str] | None = None
    for keywords, answer, is_generic in _FAQ:
        score = 0
        for keyword in keywords:
            k = _norm(keyword)
            if k and k in q:
                score += len(k) + 2
        if score <= 0:
            continue
        bucket = (score, answer)
        if is_generic:
            if best_generic is None or score > best_generic[0]:
                best_generic = bucket
        else:
            if best_specific is None or score > best_specific[0]:
                best_specific = bucket
    if best_specific is not None:
        return best_specific[1]
    return best_generic[1] if best_generic is not None else None


async def faq_answer(question: str, bot_help_text: str = "") -> str | None:
    """Answer common usage questions: score-matched local FAQ first, AI with
    full bot context as fallback."""
    try:
        if not isinstance(question, str) or not question.strip():
            return None
        q = _norm(question)
        local = _best_faq(q)
        if local is not None:
            return local  # matched locally — no AI call, no rate usage
        if not ai_available():
            return None
        cfg = _get_cfg()
        key = _cache_key("faq", cfg["provider"], cfg["model"], question)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if not _allow_call(cfg["rate"]):
            return None
        system = (
            "تو دستیار پشتیبانی یک ربات دانلودر تلگرام هستی. فقط بر اساس «راهنمای ربات» پایین پاسخ بده؛ "
            "اگر پاسخ در راهنما نیست، صادقانه بگو این مورد را نمی‌دانی و به /help ارجاع بده. "
            "پاسخ را کوتاه، دقیق و کاملاً به زبان فارسی بنویس.\n\nراهنمای ربات:\n"
            + (bot_help_text or "")[:3000]
        )
        raw = await _chat(question[: cfg["max_input"]], system=system, max_tokens=300)
        if raw and (answer := _strip_quotes(raw)):
            answer = _clip_sentences(answer, 900)
            _cache_set(key, answer)
            return answer
        return None
    except Exception as exc:
        logger.debug("faq_answer failed: %s", exc)
        return None


async def ai_health() -> dict:
    """Health/counters snapshot for monitoring."""
    cfg = _get_cfg()
    stats = dict(_STATS)
    stats.update({"enabled": ai_available(), "provider": cfg["provider"], "model": cfg["model"]})
    return stats
