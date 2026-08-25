<div align="center">

# MZ Downloader

**ربات چندمنظورهٔ دانلود محتوای تلگرام با معماری چندلایه، هوش مصنوعی و پشتیبانی از ۱۵+ پلتفرم**

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](Dockerfile)
[![Railway](https://img.shields.io/badge/Deploy-Railway-7B3FE4.svg)](railway.json)

[راهنمای استقرار Railway](RAILWAY_DEPLOY_FA.md) · [راهنمای Apify](APIFY_SETUP_FA.md) · [ویژگی‌های جدید](README_NEW_FEATURES.md)

</div>

---

## فهرست مطالب

- [نگاه کلی](#-نگاه-کلی)
- [ویژگی‌های کلیدی](#-ویژگیهای-کلیدی)
- [پلتفرم‌های پشتیبانی‌شده](#-پلتفرمهای-پشتیبانیشده)
- [معماری سیستم](#-معماری-سیستم)
- [ساختار پروژه](#-ساختار-پروژه)
- [لایه‌های دانلود و مسیریابی](#-لایههای-دانلود-و-مسیریابی)
- [ویژگی‌های پیشرفته](#-ویژگیهای-پیشرفته)
- [فرمان‌های ربات](#-فرمانهای-ربات)
- [پیش‌نیازها](#-پیشنیازها)
- [نصب و اجرای محلی](#-نصب-و-اجرای-محلی)
- [استقرار روی Railway](#-استقرار-روی-railway)
- [متغیرهای محیطی](#-متغیرهای-محیطی)
- [امنیت](#-امنیت)
- [تکنولوژی‌ها](#-تکنولوژیها)

---

## 👁 نگاه کلی

MZ Downloader یک ربات تلگرام پیشرفته برای دانلود محتوا از پلتفرم‌های مختلف است. این ربات با **معماری چندلایهٔ مسیریابی**، **دانلود موازی**، **پری‌فچ پس‌زمینه** و **هوش مصنوعی** ساخته شده و از بیش از ۱۵ پلتفرم محتوایی پشتیبانی می‌کند. سورس کد شامل بیش از **۲۰,۰۰۰ خط پایتون** در ۲۶ ماژول است.

---

## ✨ ویژگی‌های کلیدی

| ویژگی | توضیح |
|--------|--------|
| 🎯 **انتخاب کیفیت** | منوی دکمه‌ای کیفیت (۱۴۴p تا 4K) + فقط صدا (MP3) با نمایش حجم تقریبی |
| ⚡ **دانلود موازی** | تقسیم فایل‌های بزرگ به چند لاین همزمان با VoidDL (تا ۸ لاین موازی) |
| 🔮 **پری‌فچ پس‌زمینه** | شروع دانلود احتمالی‌ترین کیفیت‌ها قبل از انتخاب کاربر |
| 🔀 **چرخش کلید API** | مدیریت هوشمند سهمیه و نرخ چندین کلید API به‌صورت خودکار |
| 🤖 **هوش مصنوعی** | خلاصه‌سازی فارسی، پیشنهاد هشتگ، سیستم پرسش‌وپاسخ و FAQ هوشمند |
| 🔍 **جست‌وجوی YouTube** | جست‌وجوی ویدیو با کالاج ۶ تایی thumbnail و حجم تقریبی هر کیفیت |
| 🎵 **جست‌وجوی آهنگ (Shazam)** | جست‌وجوی آهنگ با ShazamIO + iTunes، تبدیل خودکار به لینک YouTube |
| 📖 **زیرنویس YouTube** | دریافت زیرنویس فارسی و انگلیسی از طریق downsub.com |
| 📸 **پروفایل و استوری اینستاگرام** | نمایش اطلاعات پروفایل، نرخ رشد و دانلود استوری‌ها |
| 🎵 **آلبوم/پلی‌لیست Spotify** | دانلود ترتیبی آلبوم و پلی‌لیست با ساخت فایل ZIP |
| 🔖 **بوکمارک** | ذخیرهٔ لینک‌های دانلود‌شده برای دسترسی سریع |
| 📤 **اشتراک‌گذاری خودکار** | ارسال خودکار محتوای دانلود‌شده به کانال/گروه مشخص |
| 📊 **آمار شخصی** | گزارش ۳۰ روزهٔ دانلودها، حجم، پلتفرم‌ها و نمودار روزانه |
| ⏰ **زمان‌بندی دانلود** | دانلود خودکار دوره‌ای لینک‌ها (از ۹۰ دقیقه تا ۲ هفته) |
| 🛡️ **حذف تکراری** | جلوگیری از دانلود مجدد فایل‌های یکسان |
| 🚨 **هشدار توکن** | اطلاع‌رسانی خودکار ادمین در خرابی توکن‌های Apify |
| 📏 **حجم دقیق** | محاسبه و نمایش حجم واقعی فایل‌ها با حسابرسی |
| 🔌 **Circuit Breaker** | قطع‌کنندهٔ مدار برای جلوگیری از فشردن سرویس‌های خراب |
| 📊 **لاگ‌سازی ساختاریافته** | خروجی JSON لاگ‌ها برای تحلیل آسان |
| ☁️ **آپلود ابری** | آپلود خودکار فایل‌های بزرگ روی Pixeldrain / Gofile + Cloudflare Worker |

---

## 🌐 پلتفرم‌های پشتیبانی‌شده

| پلتفرم | نوع محتوا | مسیر اصلی | مسیر پشتیبان |
|---------|-----------|------------|---------------|
| **YouTube** | ویدیو، Shorts، صدا | VoidDL → Yoinku | Apify → ربات‌های تلگرامی |
| **Instagram** | پست، ریلز، IGTV، استوری، کروسل تصویری | AHM7 / Apify | yt-dlp → ربات‌های تلگرامی |
| **TikTok** | ویدیو (بدون واترمارک) | AHM7 / tikwm.com | ربات‌های تلگرامی |
| **Twitter / X** | ویدیو، تصویر، متن | Apify / AHM7 | yt-dlp → ربات‌های تلگرامی |
| **Facebook** | ویدیو | Apify / AHM7 | yt-dlp → ربات‌های تلگرامی |
| **Spotify** | تراک (MP3)، آلبوم (ZIP)، پلی‌لیست (ZIP) | spotisaver.net / Apify | ربات‌های تلگرامی |
| **SoundCloud** | تراک (MP3) | yt-dlp / Apify | ربات تلگرامی |
| **Pinterest** | تصویر (کیفیت اصلی)، ویدیو | yt-dlp / Apify | — |
| **VK** | ویدیو | ربات تلگرامی | — |
| **Reddit** | ویدیو، تصویر | AHM7 | Apify / yt-dlp |
| **Snapchat** | ویدیو | AHM7 | — |
| **CapCut** | ویدیو | AHM7 | — |
| **SnackVideo** | ویدیو | AHM7 | — |
| **Douyin** | ویدیو | AHM7 | — |
| **یوتیوب جنریک** | Vimeo, Dailymotion, Twitch, Kick, Bilibili و... | yt-dlp | — |

> **توجه:** هر لینک HTTPS ناشناخته‌ای به‌صورت خودکار از طریق yt-dlp امتحان می‌شود.

---

## 🏗 معماری سیستم

```
┌──────────────────────────────────────────────────────────────────┐
│                         پیام کاربر                               │
│                    (لینک / فرمان / متن جستجو)                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                        bot.py (۵,۷۴۴ خط)                         │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐   │
│  │ تشخیص پلتفرم │  │ اعتبارسنجی   │  │ مدیریت نشست کاربر      │   │
│  │ (routing.py) │  │ عضویت اجباری│  │ (Quality Card/Session) │   │
│  └──────┬──────┘  └──────────────┘  └────────────────────────┘   │
└─────────┼────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│                   لایهٔ مسیریابی (Routing)                       │
│                                                                  │
│  YouTube ──► VoidDL ──► Yoinku ──► Apify ──► ربات‌های تلگرامی    │
│  Instagram ► AHM7 ──► Apify ──► yt-dlp ──► ربات‌های تلگرامی      │
│  TikTok ───► AHM7 ──► tikwm ──► ربات‌های تلگرامی                │
│  Spotify ──► spotisaver / Apify ──► ربات‌های تلگرامی            │
│  سایر ────► AHM7 / yt-dlp / Apify ──► ربات‌های تلگرامی          │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│                    لایهٔ ارسال به کاربر                          │
│  ┌────────────┐ ┌─────────────┐ ┌──────────────┐ ┌───────────┐   │
│  │ ارسال مستقیم│ │ بخش‌بندی    │ │ آپلود ابری  │ │ نوار پیشرفت│   │
│  │ (<50MB)    │ │ (Telegram)  │ │ (Pixeldrain) │ │           │   │
│  └────────────┘ └─────────────┘ └──────────────┘ └───────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 📁 ساختار پروژه

```
MZ-Downloader/
├── bot.py                    # نقطهٔ ورود اصلی — ۵,۷۴۴ خط (مدیریت ربات، فرمان‌ها، رویدادها)
├── config.py                 # پیکربندی از متغیرهای محیطی (dataclass Settings)
├── routing.py                # تشخیص پلتفرم از URL + تعریف enum Platform
├── downloader.py             # هستهٔ دانلود — AccountPool, Worker, GatewayResult
│
├── voiddl_gateway.py         # دروازهٔ VoidDL (YouTube) — ۱,۶۸۸ خط — دانلود موازی + پری‌فچ
├── yoinku_gateway.py         # دروازهٔ Yoinku (YouTube) — ۷۴۴ خط
├── ahm7_gateway.py           # دروازهٔ AHM7 (چندپلتفرمی) — ۷۱۸ خط
├── apify_gateway.py          # دروازهٔ Apify Actors — ۱,۱۰۴ خط
├── apify_platforms.py        # تعریف Actorهای Apify برای ۵ پلتفرم جدید
├── social_gateway.py         # دروازهٔ yt-dlp — ۱,۳۱۲ خط (TikTok/SC/IG/Pinterest/Twitter/FB)
│
├── youtube_search.py         # جست‌وجوی YouTube + کالاج thumbnail — ۷۳۳ خط
├── youtube_subtitle.py       # دریافت زیرنویس YouTube (downsub.com + AES)
├── mz_shazam_search.py       # جست‌وجوی آهنگ (ShazamIO + iTunes) + کالاج کاور
├── instagram_profile.py      # پروفایل، استوری و آخرین پست اینستاگرام
├── instagram_caption.py      # استخراج کپشن پست اینستاگرام (instaspeeder.com)
├── spotisaver.py             # دانلود آلبوم/پلی‌لیست Spotify (ZIP)
│
├── ai_service.py             # سرویس AI — خلاصه، هشتگ، FAQ (HuggingFace/Cohere/Mistral)
├── user_features.py          # بوکمارک، اشتراک، آمار، زمان‌بندی، حذف تکراری
├── feature_flags.py          # Feature flags برای ارتقای ۱۴۰۴
├── token_alerts.py           # سیستم هشدار توکن Apify به ادمین
├── users_db.py               # مدیریت لیست کاربران (JSON + env)
├── store.py                  # لایهٔ ذخیره‌سازی SQLite — ۱,۱۲۱ خط
├── media_size.py             # محاسبه و حسابرسی حجم فایل‌ها
├── perf.py                   # TTL Cache, Circuit Breaker, Connection Pool, Rate Limiter
├── pixeldrain_upload.py      # آپلود فایل‌های بزرگ روی Pixeldrain
├── structured_logging.py     # فرمت‌دهندهٔ JSON لاگ‌ها
│
├── migrations/               # فایل‌های SQL مهاجرت
│   └── 0001_new_features.sql
├── Dockerfile                # تصویر داکر (Python 3.12-slim + ffmpeg)
├── railway.json              # تنظیمات استقرار Railway
├── Procfile                  # فرمان شروع Railway/Heroku
├── requirements.txt          # وابستگی‌های پایتون
│
├── README.md                 # این فایل
├── INSTALL.md                # راهنمای نصب
├── RAILWAY_DEPLOY_FA.md      # راهنمای استقرار فارسی Railway
├── APIFY_SETUP_FA.md         # راهنمای تنظیم Apify فارسی
├── README_NEW_FEATURES.md    # مستندات ویژگی‌های جدید ۱۴۰۴
└── replit.md                 # راهنمای Replit
```

---

## 🛣 لایه‌های دانلود و مسیریابی

### VoidDL (YouTube — اصلی)

**محدودیت هر کلید:** ۲۰ دانلود/دقیقه + ۱۰ گیگابایت پهنای باند/روز

- **دانلود چندلاینه:** فایل‌های ≥۱۶ مگابایت با چند درخواست Range همزمان دانلود می‌شوند (تا ۸ لاین). سرعت تجمعی ≈ تعداد کلید × ۱۲–۱۵ مگابایت بر ثانیه
- **پری‌فچ پس‌زمینه:** به‌محض نمایش کارت کیفیت، دانلود ۱۰۸۰p، ۷۲۰p و MP3 شروع می‌شود
- **مدیریت سهمیه:** چرخش خودکار کلیدها در صورت محدودیت نرخ یا اتمام پهنای باند روزانه

### Yoinku (YouTube — پشتیبان اول)

**محدودیت هر کلید:** ۳۰ درخواست/روز + ۵ درخواست/دقیقه (پنجرهٔ لغزان)

### Apify Actors (چندپلتفرمی — پشتیبان)

از Actorهای ابری Apify برای دانلود استفاده می‌کند:

| پلتفرم | Actor |
|---------|-------|
| Instagram | `apify/instagram-scraper` |
| YouTube | Actor اختصاصی کیفیت |
| Spotify | `maximedupre/spotify-downloader` |
| SoundCloud | `easyapi/soundcloud-mp3-downloader` |
| Twitter/X | `apidojo/tweet-scraper` |
| Facebook | `apple_yang/facebook-video-audio-downloader` |
| Pinterest (ویدیو) | `easyapi/pinterest-video-downloader` |
| Pinterest (تصویر) | `fatihtahta/pinterest-scraper-search` |

### AHM7 (چندپلتفرمی — اصلی برای TikTok/IG/FB/X/Reddit و...)

از API `ahm7xmakki.com/api/alldl` استفاده می‌کند. پشتیبانی از: TikTok، Instagram، Facebook، X/Twitter، Reddit، Snapchat، SoundCloud، CapCut، SnackVideo، Douyin.

### Social Gateway / yt-dlp

استفاده مستقیم از yt-dlp با قابلیت impersonate مرورگر (curl_cffi) برای: TikTok (tikwm.com)، SoundCloud، Instagram، Pinterest، Twitter/X، Facebook و هر URL جنریک.

### ربات‌های واسط تلگرامی

لایهٔ نهایی: ارسال لینک به ربات‌های تلگرامی واسط و دریافت فایل از طریق Telethon.

---

## 🚀 ویژگی‌های پیشرفته

### کارت کیفیت YouTube

وقتی کاربر لینک YouTube می‌فرستد:

1. تامبنیل با بالاترین کیفیت (maxresdefault) دانلود و به‌صورت عکس ارسال می‌شود
2. عنوان، مدت و منبع ویدیو به‌عنوان کپشن نمایش داده می‌شود
3. جدول حجم تقریبی هر کیفیت نمایش داده می‌شود
4. دکمه‌های شمارهٔ کیفیت نمایش داده می‌شوند (مثلاً `480` یا `720` یا `MP3`)
5. پری‌فچ پس‌زمینه بلافاصله شروع می‌شود

### هوش مصنوعی

- **خلاصه‌سازی فارسی:** دکمهٔ «خلاصه کن» زیر کپشن اینستاگرام و توییت
- **پیشنهاد هشتگ:** تولید هشتگ‌های فارسی و انگلیسی مرتبط
- **پاسخ به سوالات:** دستور `/ask` با FAQ محلی + پاسخ AI
- **پشتیبانی:** HuggingFace Inference Providers، Cohere v2، Mistral

### مدیریت توکن Apify

- ردیابی وضعیت هر توکن (فعال / مشکوک / خراب) در SQLite
- هشدار PV خودکار به ادمین در خرابی توکن با جزئیات کامل
- چرخهٔ یادآوری: ۵ بار هر ۱۵ دقیقه تا خوانده شدن
- داشبورد ادمین با دستور `/tokens`

### Feature Flags

هر ویژگی جدید از ارتقای ۱۴۰۴ قابل فعال/غیرفعال‌سازی مستقل:

| پرچم | متغیر محیطی | پیش‌فرض |
|-------|-------------|----------|
| پلتفرم‌های جدید Apify | `APIFY_NEW_PLATFORMS_ENABLED` | فعال |
| هشدار توکن | `TOKEN_ALERTS_ENABLED` | فعال |
| بوکمارک | `BOOKMARKS_ENABLED` | فعال |
| اشتراک‌گذاری خودکار | `AUTOSHARE_ENABLED` | فعال |
| آمار شخصی | `USER_STATS_ENABLED` | فعال |
| حذف تکراری | `DEDUPE_ENABLED` | فعال |
| زمان‌بندی | `SCHEDULER_ENABLED` | فعال |
| خلاصه AI | `AI_SUMMARY_ENABLED` | فعال |
| حجم دقیق | `EXACT_SIZES_ENABLED` | فعال |
| کش عملکرد | `PERF_CACHE_ENABLED` | فعال |
| Circuit Breaker | `CIRCUIT_BREAKER_ENABLED` | فعال |

---

## 📋 فرمان‌های ربات

| فرمان | توضیح |
|-------|--------|
| `/start` | شروع ربات + معرفی |
| `/help` | راهنمای کامل |
| `/dl <لینک>` | دانلود لینک (در گروه‌ها) |
| `/search <عبارت>` یا `/yt <عبارت>` | جست‌وجوی YouTube |
| `/song <عبارت>` | جست‌وجوی آهنگ (Shazam/iTunes) |
| `/caption <لینک>` | دریافت کپشن پست اینستاگرام |
| `/profile <یوزرنیم>` | اطلاعات پروفایل + استوری اینستاگرام |
| `/bookmarks` | لیست بوکمارک‌ها |
| `/mystats` | آمار شخصی ۳۰ روزه |
| `/ask <سوال>` | پرسش از AI |
| `/schedule <لینک> <بازه>` | دانلود زمان‌بندی‌شده |
| `/autoshare add/del/list` | مدیریت اشتراک‌گذاری خودکار |
| `/cancel` | لغو دانلود در حال انجام |
| `/tokens` | داشبورد وضعیت توکن‌ها (ادمین) |
| `/adduser <id>` | افزودن کاربر (ادمین) |
| `/health` | بررسی سلامت (HTTP) |

---

## 📦 پیش‌نیازها

- **Python 3.12+**
- **ffmpeg** (برای تبدیل صدا توسط yt-dlp)
- **Telegram Bot Token** — از [@BotFather](https://t.me/botfather) دریافت کنید
- **اکانت‌های Telethon** — ۱ یا چند اکانت تلگرام با StringSession
- **Cloudflare Workers** (اختیاری) — برای پروکسی دانلود فایل‌های بزرگ

---

## 🛠 نصب و اجرای محلی

### ۱. کلون و نصب وابستگی‌ها

```bash
git clone https://github.com/mamli88888/MZ-Downloader2.git
cd MZ-Downloader
python -m venv venv
source venv/bin/activate   # Linux/macOS
# venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### ۲. تنظیم متغیرهای محیطی

فایل `.env` در مسیر ریشهٔ پروژه بسازید:

```env
BOT_TOKEN=توکن-ربات-شما
TELEGRAM_ACCOUNTS=[]
```

حداقل یک اکانت Telethon نیاز است. برای راهنمای تبدیل Session به StringSession:

```bash
python export_sessions.py
```

### ۳. اجرا

```bash
python bot.py
```

ربات شروع می‌شود و لاگ‌ها در ترمینال نمایش داده می‌شود. در محیط غیرRailway، لاگ‌ها در `bot.log` هم ذخیره می‌شوند.

---

## 🚂 استقرار روی Railway

### مراحل سریع

1. **توکن جدید** از BotFather بگیرید و در `BOT_TOKEN` قرار دهید
2. **Sessionها** را تبدیل و در `TELEGRAM_ACCOUNTS` قرار دهید
3. **متغیرها** را در Railway Variables تنظیم کنید (بخش [متغیرهای محیطی](#-متغیرهای-محیطی))
4. **اتصال GitHub** — پروژه را از GitHub به Railway متصل کنید
5. `Dockerfile` و `railway.json` به‌طور خودکار شناسایی می‌شوند

### نکات مهم

- **تعداد replica** همیشه روی `1` نگه دارید (session تلگرام نباید هم‌زمان اجرا شود)
- **Root Directory** را خالی بگذارید (اگر ریشهٔ ریپو است) یا `/MZ Downloader` (اگر monorepo)
- مسیر `/health` پس از اتصال حداقل یک اکانت، پاسخ `200` می‌دهد
- `PORT` توسط Railway خودش ساخته می‌شود

راهنمای کامل: [RAILWAY_DEPLOY_FA.md](RAILWAY_DEPLOY_FA.md)

---

## ⚙️ متغیرهای محیطی

### متغیرهای ضروری

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `BOT_TOKEN` | توکن ربات تلگرام | (اجباری) |
| `TELEGRAM_ACCOUNTS` | لیست JSON اکانت‌ها (api_id, api_hash, string_session) | `[]` |

### VoidDL — دانلودر اصلی YouTube

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `VOIDDL_ENABLED` | فعال‌سازی دروازه | `true` |
| `VOIDDL_API_KEYS` | کلیدهای API (کاما‌جدا) | ۱ کلید مرجع |
| `VOIDDL_DAILY_BANDWIDTH_MB` | سهمیهٔ پهنای باند روزانه هر کلید | `10240` (۱۰ گیگ) |
| `VOIDDL_PER_MINUTE_LIMIT` | محدودیت نرخ هر کلید | `20` |
| `VOIDDL_PARALLEL_LANES` | تعداد لاین‌های موازی | `8` |
| `VOIDDL_PREFETCH` | پری‌فچ پس‌زمینه | `true` |
| `VOIDDL_PREFETCH_COUNT` | تعداد کیفیت‌های پری‌فچ | `2` |
| `VOIDDL_PREFETCH_LANES` | لاین‌های هر پری‌فچ | `4` |
| `VOIDDL_PREFETCH_MAX_MB` | حداکثر حجم فایل برای پری‌فچ | `512` |
| `VOIDDL_PREFETCH_MIN_REMAINING_MB` | حداقل پهنای باند باقیمانده | `2048` |
| `VOIDDL_PREFETCH_TTL` | عمر سشن پری‌فچ (ثانیه) | `720` |

### Yoinku — پشتیبان YouTube

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `YOINKU_ENABLED` | فعال‌سازی | `true` |
| `YOINKU_API_KEYS` | کلیدهای API | (خالی) |
| `YOINKU_DAILY_LIMIT` | محدودیت روزانه هر کلید | `30` |
| `YOINKU_PER_MINUTE_LIMIT` | محدودیت دقیقه‌ای هر کلید | `5` |

### AHM7 — دانلودر چندپلتفرمی

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `AHM7_ENABLED` | فعال‌سازی | `true` |
| `AHM7_API_URL` | آدرس API | `https://ahm7xmakki.com/api/alldl` |

### Apify

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `APIFY_ENABLED` | فعال‌سازی دروازه | `true` |
| `APIFY_TOKENS` | توکن‌ها (کاما‌جدا) | (خالی) |
| `APIFY_RUN_TIMEOUT_SECONDS` | حداکثر مدت اجرای Actor | `360` |
| `APIFY_POLL_INTERVAL_SECONDS` | فاصلهٔ بررسی نتیجه | `3` |
| `APIFY_TOKEN_COOLDOWN_SECONDS` | زمان cooling توکن خراب | `600` |

### ربات‌های واسط تلگرامی

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `PRIMARY_DOWNLOADER_BOT` | ربات اصلی | `download_it_bot` |
| `SECONDARY_DOWNLOADER_BOT` | ربات پشتیبان | `AllSavesBot` |
| `SPOTIFY_DOWNLOADER_BOT` | ربات اسپاتیفای | `spotifysavesbot` |
| `SOUNDCLOUD_DOWNLOADER_BOT` | ربات ساوندکلاد | `scload_bot` |

### هوش مصنوعی

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `AI_API_KEY` | کلید API ارائه‌دهنده | (خالی = خاموش) |
| `AI_PROVIDER` | `huggingface` / `cohere` / `mistral` / `auto` | `auto` |
| `AI_MODEL` | نام مدل | مدل پیش‌فرض هر ارائه‌دهنده |

### آپلود ابری

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `PIXELDRAIN_API_KEY` | کلید API Pixeldrain | (خالی) |
| `CLOUDFLARE_WORKER_URL` | URL Worker کلادفلر | (خالی) |
| `CLOUDFLARE_WORKER_ACCESS_KEY` | کلید دسترسی Worker | (خالی) |

### متغیرهای عمومی

| متغیر | توضیح | پیش‌فرض |
|-------|--------|--------|
| `DOWNLOAD_DIR` | مسیر دانلود موقت | `/tmp/mz-downloader` |
| `MAX_FILE_SIZE_MB` | سقف ارسال مستقیم تلگرام | `30` |
| `MAX_DOWNLOAD_SIZE_MB` | سقف داخلی برنامه (`0` = بدون محدودیت) | `0` |
| `USE_PROXY` | استفاده از پروکسی | `false` |
| `PROXY_TYPE` | نوع پروکسی (`socks5` / `http`) | `socks5` |
| `PROXY_HOST` | آدرس پروکسی | `127.0.0.1` |
| `PROXY_PORT` | پورت پروکسی | `10808` |
| `BOT_ADMIN_CHAT_ID` | شناسه عددی ادمین (برای هشدارها) | `0` |
| `KNOWN_USERS` | لیست ID کاربران مجاز (کاما‌جدا) | (خالی) |
| `LOG_FORMAT` | فرمت لاگ (`json` / سایر) | (ساده) |

---

## 🔒 امنیت

- **هرگز** فایل‌های `.env`، `*.session` و StringSession را commit نکنید
- **هرگز** توکن‌ها و کلیدهای API را در کد یا لاگ قرار ندهید
- فایل‌های دانلود‌شده پس از ارسال پاک می‌شوند
- لینک‌ها فقط به سرویس‌های مربوط به همان پلتفرم ارسال می‌شوند
- Cloudflare Worker از کلید دسترسی مشترک برای جلوگیری از دسترسی تصادفی استفاده می‌کند
- فایل‌های ابری پس از زمان تعیین‌شده به‌صورت خودکار حذف می‌شوند

---

## 🔧 تکنولوژی‌ها

| تکنولوژی | نقش |
|-----------|--------|
| **Python 3.12** | زبان اصلی |
| **python-telegram-bot** | فریمورک ربات تلگرام (asyncio) |
| **Telethon** | کلاینت تلگرام برای ربات‌های واسط |
| **httpx** | HTTP client ناهمگام (HTTP/2 + SOCKS5) |
| **yt-dlp** | دانلودر جنریک (SoundCloud, Pinterest, Twitter, ...) |
| **curl_cffi** | Impersonate مرورگر Chrome (دور زدن bot detection) |
| **Pillow** | پردازش تصویر و ساخت کالاج |
| **ShazamIO** | جست‌وجوی آهنگ |
| **pycryptodome** | رمزگشایی AES زیرنویس YouTube |
| **SQLite** | ذخیره‌سازی داده‌های کاربر و ویژگی‌ها |
| **ffmpeg** | تبدیل فرمت صدا/تصویر |
| **Docker** | بسته‌بندی و استقرار |
| **Railway** | پلتفرم استقرار ابری |

---

## 📄 مجوز

این پروژه تحت مجوز MIT منتشر شده است.
