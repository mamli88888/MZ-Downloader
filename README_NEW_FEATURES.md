# MZ Downloader — ارتقای ۱۴۰۴ (Upgrade Pack)

این بسته شامل تمام فایل‌های **جدید**، **تغییر یافته** و **پیکربندی** مربوط به ارتقای درخواستی است. ساختار دایرکتوری‌ها دقیقاً مطابق ریپوی اصلی است؛ فایل‌ها را روی ریشه‌ی ریپو کپی کنید (overlay).

## 🔧 نسخه ۲ — رفع اشکال (Fix Round)

1. **دانلود ۴ پلتفرم اصلاح شد** — ریشه مشکل: Actor های قبلی SoundCloud/Facebook عمدتاً «استخراج‌کننده اطلاعات» بودند نه دانلودر، و ورودی Spotify با قرارداد واقعی Actor هم‌خوان نبود (باید آبجکت `{url}` می‌بود نه رشته). همه با دانلودرهای واقعی جایگزین/اصلاح شدند و قرارداد ورودی/خروجی هرکدام از README رسمی همان Actor راستی‌آزمایی شد. لینک خروجی Spotify (که روی api.apify.com است) حالا خودکار با توکنِ اجراکننده مجوز می‌گیرد. پلی‌لیست ساوندکلاد و آلبوم اسپاتیفای عمداً به مسیر قبلی (yt-dlp / ربات‌های تلگرامی) می‌روند چون Actor های دانلودر فقط لینک تکی می‌پذیرند.
2. **AI اصلاح شد** — اگر فقط `AI_API_KEY` را بگذارید و `AI_PROVIDER` را فراموش کنید، حالا خودکار فعال می‌شود (کلید `hf_` → HuggingFace؛ در غیر این صورت HuggingFace با هشدار در لاگ). مقادیر مجاز: `auto | huggingface | cohere | mistral | off`. پیام راهنمای غیرفعال‌بودن هم حالا دقیقاً می‌گوید چه متغیرهایی را تنظیم کنید.
3. **متن‌های رو به کاربر پاک‌سازی شد** — هیچ پیام/نوار پیشرفت/عنوانی که کاربر عادی ببیند دیگر نام Apify یا Actor را ذکر نمی‌کند (به «سرویس پردازش/دانلود» تغییر یافت). پیام‌های PV هشدار توکن و داشبورد `/tokens` فقط برای مدیر (خود شما) فرستاده می‌شوند و فنی باقی مانده‌اند چون درباره مدیریت توکن‌های خودتان‌اند.
4. **جداول SQL، دیتابیس و بقیه رفتارها بدون تغییر.**

## 🔧 نسخه ۳ — دور سوم رفع اشکال

1. **اسپاتیفای → Actor جدید `maximedupre/spotify-downloader`** — Actor پیشنهادی شما (`musicae/spotify-extended-scraper`) بررسی شد و صرفاً استخراج‌کنندهٔ metadata است (فقط mp3-preview سی‌ثانیه‌ای می‌دهد، نه فایل کامل)؛ بنابراین طبق خودِ صفحهٔ آن Actor، دانلودر واقعی `maximedupre/spotify-downloader` انتخاب شد: ورودی `trackUrls` + مود `fast_links`، خروجی لینک مستقیم MP3 (CDN خارجی، بدون نیاز به دانلود از storage) به‌همراه **حجم دقیق بایت** (`downloadContentLength`) که مستقیم در حسابداری حجم استفاده می‌شود، و متادیتای کامل (trackName/artistNames/albumName/durationMs/coverImageUrl).
2. **پینترست — منوی هوشمند بر اساس نوع پین** — ریشه مشکل: منو ثابت بود و همیشه هر دو گزینهٔ «تصویر» و «ویدیو» را نشان می‌داد. حالا هنگام ساخت منو، صفحهٔ پین خوانده می‌شود و بر اساس تگ `og:video` فقط گزینهٔ مرتبط نمایش داده می‌شود (پین تصویری → فقط تصویر؛ پین ویدیویی → فقط ویدیو؛ اگر صفحه بسته بود → هر دو به‌عنوان حالت امن).
3. **هوش مصنوعی دقیق‌تر** — سه ریشه‌ی نادرستی برطرف شد:
   - پاسخ‌های محلی از «اولین کلمهٔ مچ‌شده» به **امتیازدهی دومطبقه** تغییر کرد (موضوع اختصاصی همیشه بر «دانلود/چطور» عمومی مقدم است) + ۱۰ موضوع جدید (پینترست، توییتر، فیسبوک، ساوندکلاد، تیک‌تاک، آمار، اشتراک‌گذاری، خلاصه، توقف و…) + پوشش غلط‌های املایی رایج.
   - سوالاتی که محلی پاسخ ندارند حالا با **کانتکست کامل ربات** (پلتفرم‌ها، همهٔ دستورها، رفتارها) به مدل داده می‌شوند — قبلاً مدل هیچ اطلاعی از ربات نداشت و از خودش می‌ساخت. پرامپت هم فارسی و مقید به راهنما شد.
   - HuggingFace به **endpoint مدرن Inference Providers** (`router.huggingface.co/v1/chat/completions`) منتقل شد با زنجیرهٔ fallback مدل (Qwen2.5 → Phi-3.5 → Zephyr) — قبلی روی مسیر legacy بود و برای خیلی از مدل‌ها 404 می‌داد.
4. **تست سلامت + ارسال به PV مدیر** — بعد از هر راه‌اندازی، گزارش سلامت کامل (اکانت‌های متصل، توکن‌ها، دیتابیس، هوش مصنوعی، بریکرها، قابلیت‌های فعال، پایداری) به PV مدیر اصلی ارسال می‌شود؛ دستور `/health` هم همین گزارش را همان‌جا و در PV شما نشان می‌دهد. (نیازمند `BOT_ADMIN_CHAT_ID`).

> **سازگاری با پلن رایگان Railway:** همه‌ی قابلیت‌ها in-process و فایل‌محور (SQLite + JSON) هستند. هیچ سرویس خارجی، دیتابیس ابری، worker جدا یا Cron اضافه‌ای لازم نیست. `replica=1` همان‌طور که باید، حفظ شده است.

---

## ۱) خلاصه تغییرات

| # | قابلیت | وضعیت | پرچم |
|---|---|---|---|
| ۱ | Apify برای Spotify / SoundCloud / Twitter / Facebook / Pinterest | ✅ پیاده‌سازی شد | `APIFY_NEW_PLATFORMS_ENABLED` |
| ۲ | اطلاع‌رسانی PV توکن خراب + یادآوری ۱۵ دقیقه‌ای (حداکثر ۵ بار) + داشبورد | ✅ پیاده‌سازی شد* | `TOKEN_ALERTS_ENABLED` |
| ۳ | بهینه‌سازی: connection pooling، کش TTL، circuit breaker، gzip، rate limiter، لاگ ساختاریافته JSON | ✅ پیاده‌سازی شد | `PERF_CACHE_ENABLED`, `CIRCUIT_BREAKER_ENABLED` |
| ۴ | بوکمارک، اشتراک‌گذاری خودکار، آمار شخصی، تشخیص duplicate، زمان‌بندی دانلود | ✅ پیاده‌سازی شد | `BOOKMARKS_ENABLED`, `AUTOSHARE_ENABLED`, `USER_STATS_ENABLED`, `DEDUPE_ENABLED`, `SCHEDULER_ENABLED` |
| ۵ | AI رایگان (HuggingFace / Cohere / Mistral) — خلاصه فارسی + پرسش‌پاسخ | ✅ پیاده‌سازی شد | `AI_SUMMARY_ENABLED` (+ کلید رایگان) |
| ۶ | اصلاح دقیق حجم ویدیوها (Content-Length، تلورانس ۵MB، تخمین HLS با ضریب ۰.۹۵) | ✅ پیاده‌سازی شد | `EXACT_SIZES_ENABLED` |

\* محدودیت فنی تلگرام: Bot API رسید «خوانده‌شدن» (seen) پیام‌های ربات را نمی‌دهد. تشخیص «سین» با دو مکانیزم انجام می‌شود: دکمه‌ی «✅ دیدم / رسیدگی شد» روی خود پیام، و هرگونه تعامل مدیر با ربات (هر آپدتی از آیدی مدیر = خواندن خودکار همه‌ی هشدارهای باز). سایر رفتارها (یادآوری ۱۵ دقیقه‌ای، حداکثر ۵ بار، توقف بعد از سین) دقیقاً طبق مشخصات است.

### قابلیت‌هایی که عمداً انجام نشد (احتمال موفقیت < ۹۵٪)

- **فیلتر تاریخ/محبوبیت توییتر برای لینک مستقیم:** فیلدهای `start`/`end`/`minimumFavorites` در actor فقط برای حالت **جستجو** (`searchTerms`) معنا دارند، نه دانلود یک لینک مشخص. ورودی‌های Actor عیناً از schema رسمی استخراج و برای دانلود لینک استفاده شده‌اند؛ فیلتر جستجو در این ربات کاربرد ندارد و fake-pass کردنش صادقانه نبود.
- **پیش‌نمایش thumbnail توییتر/فیسبوک در منو:** این دو پلتفرم scrape بدون لاگین را می‌بندند؛ منو بدون عکس نمایش داده می‌شود (بهترین تلاش، بدون شکست).

---

## ۲) فایل‌های جدید

```
feature_flags.py        پرچم‌های همه‌ی قابلیت‌های جدید (خواندن از env)
apify_platforms.py      Actorها + منوها + ورودی‌ها + نرمال‌ساز خروجی ۵ پلتفرم جدید
token_alerts.py         هشدار PV توکن خراب + حلقه‌ی یادآوری + داشبورد /tokens
user_features.py        بوکمارک، آمار شخصی، autoshare، dedupe، زمان‌بندی، AI دکمه/پرسش
store.py                دیتابیس SQLite افزایشی (async، WAL، migration خودکار)
perf.py                 TTL Cache، Circuit Breaker، Connection Pool، Rate Limiter
media_size.py           حجم دقیق: Content-Length، تلورانس ۵MB، تخمین HLS×0.95، فرمت دو رقم اعشار
ai_service.py           یکپارچه‌سازی HuggingFace / Cohere / Mistral + FAQ محلی + کش و rate limit
structured_logging.py   لاگ JSON ساختاریافته (اختیاری با LOG_FORMAT=json)
migrations/0001_new_features.sql   مهاجرت افزایشی دیتابیس
README_NEW_FEATURES.md  همین فایل
```

## ۳) فایل‌های تغییر یافته (سurgical — رفتار فعلی حفظ شده)

```
bot.py                  ثبت دستورات/callbackهای جدید، dedupe در مسیر ارسال، هوک‌های after_success،
                        حلقه‌ی scheduler، ادمین‌واچر، پلتفرم‌های جدید Apify در use_apify، متن help
apify_gateway.py        پلتفرم‌های جدید در request/select، هوک on_token_failure/success،
                        client مشترک (pooling)، audit حجم، circuit breaker، rate limit هر پلتفرم
config.py               فیلد جدید bot_admin_chat_id (فقط additive)
Dockerfile              COPY فایل‌ها و پوشه‌ی migrations
.env.example            مستندسازی همه‌ی متغیرهای جدید
tests/test_apify_gateway.py  هماهنگی mock با امضای جدید _run_actor (per-request auth)
README.md               لینک به همین راهنما
```

**هیچ فایل یا مسیر دیگری تغییر نکرده است؛ `users_db.py` و `users.json` دست‌نخورده‌اند.**

---

## ۴) نصب dependency های جدید

**هیچ پکیج جدیدی لازم نیست.** تمام کد جدید فقط از stdlib پایتون ۳.۱۲ (sqlite3, zoneinfo, contextvars, …) و `httpx` موجود در `requirements.txt` استفاده می‌کند. `pip install -r requirements.txt` کافی است.

## ۵) Migration دیتابیس

- فایل: `migrations/0001_new_features.sql`
- اجرای **خودکار** در اولین راه‌اندازی (`store.init_store()` در `post_init`)؛ نسخه با `PRAGMA user_version` ردیابی می‌شود.
- صرفاً **افزایشی**: ۹ جدول جدید + ایندکس‌ها (`apify_token_status`, `token_alerts`, `bookmarks`, `user_download_events`, `media_dedupe`, `autoshare_targets`, `scheduled_jobs`, `size_audit_log`, `ai_cache`).
- دیتابیس پیش‌فرض: `mz_data.db` کنار پروژه؛ با `MZ_DB_PATH` قابل تغییر است.
- نکته‌ی Railway: دیسک Railway پس از هر redeploy پاک می‌شود؛ استور خودش را بازمی‌سازد (همان الگوی users.json کنونی). برای ماندگاری کامل می‌توانید `MZ_DB_PATH` را به یک Volume متصل کنید (اختیاری).

---

## ۶) راهنمای Feature Flag ها

همه‌ی پرچم‌ها در Railway Variables (یا `.env`) تنظیم می‌شوند و مقدار پیش‌فرض **فعال** است؛ یعنی بدون هیچ تنظیمی قابلیت‌ها بعد از deploy فعال‌اند و برای **غیرفعال‌سازی پیش از فعال‌سازی** کافی است مقدار `false` بگذارید:

```text
APIFY_NEW_PLATFORMS_ENABLED=true|false   # Apify برای ۵ پلتفرم جدید (YouTube/Instagram همیشه باقی است)
TOKEN_ALERTS_ENABLED=true|false          # هشدار PV توکن خراب + یادآوری‌ها
BOOKMARKS_ENABLED=true|false             # 🔖 ذخیره محتوا + منوی /bookmarks
AUTOSHARE_ENABLED=true|false             # ارسال خودکار به کانال/گروه
USER_STATS_ENABLED=true|false            # آمار شخصی /mystats
DEDUPE_ENABLED=true|false                # تشخیص محتوای تکراری (ارسال با file_id)
SCHEDULER_ENABLED=true|false             # دانلود زمان‌بندی‌شده /schedule
AI_SUMMARY_ENABLED=true|false            # دکمه خلاصه فارسی + /ask (نیازمند کلید)
EXACT_SIZES_ENABLED=true|false           # حسابداری دقیق حجم (تلورانس ۵MB)
PERF_CACHE_ENABLED=true|false            # کش TTL پاسخ‌های تکراری
CIRCUIT_BREAKER_ENABLED=true|false       # بریکر برای سرویس‌های خارجی
LOG_FORMAT=text|json                     # لاگ ساختاریافته (پیش‌فرض text)
```

## ۷) متغیرهای محیطی جدید (غیر از پرچم‌ها)

```text
BOT_ADMIN_CHAT_ID=123456789      # آیدی عددی مدیر اصلی (برای PV هشدار توکن‌ها) — از @userinfوبات بگیرید
MZ_DB_PATH=mz_data.db            # مسیر دیتابیس SQLite
AI_PROVIDER=huggingface|cohere|mistral|off   # پیش‌فرض off
AI_API_KEY=hf_xxx                # کلید رایگان: HF settings/tokens یا Cohere trial یا Mistral free tier
AI_MODEL=                        # اختیاری؛ پیش‌فرض هر provider
AI_RATE_PER_MINUTE=10            # محدودیت آزادانه‌ی free tier
AI_TIMEOUT_SECONDS=20
```

---

## ۸) پلتفرم‌های جدید — جزئیات فنی

| پلتفرم | Actor (v2 — همه دانلودر واقعی و تأییدشده) | منوی کیفیت | Fallback (بدون تغییر) |
|---|---|---|---|
| Spotify | `scraper-mind/spotify-music-downloader` — ورودی اصلاح‌شده `track_urls=[{url}]` + پراکسی residential (طبق README خود Actor)؛ خروجی فیلد `Download Audio` با احراز هویت خودکار توکن | MP3 320kbps با متادیتای کامل (فقط لینک track؛ آلبوم/پلی‌لیست خودکار به مسیر قبلی می‌رود) | ربات‌های تلگرامی + Spotisaver |
| SoundCloud | `easyapi/soundcloud-mp3-downloader` (جایگزین شد — قبلی فقط metadata می‌داد) — ورودی `links`، خروجی لینک مستقیم امضاشده CDN | بهترین کیفیت موجود (MP3) — ترک‌ها؛ پلی‌لیست‌ها (/sets/) خودکار به مسیر yt-dlp قبلی | scload_bot + yt-dlp |
| Twitter/X | `apidojo/tweet-scraper` (بدون تغییر — کار می‌کند) | ویدیو بالاترین کیفیت (انتخاب variant بر اساس bitrate) / کم‌حجم / همه تصاویر / متن+ریپلای+کوت با آمار | AllSavesBot و download_it_bot + yt-dlp |
| Facebook | `apple_yang/facebook-video-audio-downloader` (جایگزین شد — قبلی فقط متن پست می‌داد) — ورودی `videoUrls`، خروجی `videoUrl`/`audioUrl` مستقیم | ویدیو / فقط صدا MP3 (اگر Actor فقط ویدیو بدهد، با ffmpeg استخراج می‌شود) | download_it_bot + yt-dlp |
| Pinterest | ویدیو: `easyapi/pinterest-video-downloader` (ورودی `links`) · تصاویر: `fatihtahta/pinterest-scraper-search` (URL مستقیم i.pinimg.com) | تصویر با بزرگ‌ترین رزولوشن (انتخاب max area) / ویدیوی پین | yt-dlp |

- فرمت منو، دکمه‌ها، نوار پیشرفت و «انتخاب اول، اجرای Actor بعد از کلیک» دقیقاً مثل YouTube/Instagram است (هیچ کردیتی قبل از انتخاب مصرف نمی‌شود).
- چرخش توکن و fallback به توکن بعدی همان منطق موجود است؛ به‌علاوه هر خطای توکن → ثبت وضعیت (فعال/مشکوک/خراب) + هشدار PV مدیر.
- خروجی Actor ها schema متغیری دارد؛ استخراج رسانه با نرمال‌ساز بازگشتی امتیازدهی‌شده (همان تکنیک carousel اینستاگرام) انجام می‌شود تا تغییرات جزئی خروجی، دانلود را نشکند.
- Rate limit پیش‌فرض هر پلتفرم: Twitter ۱۰ و بقیه ۶ اجرا در دقیقه (داخل `apify_platforms.PLATFORM_RATE_PER_MINUTE`).

## ۹) دستورات جدید ربات

```text
/bookmarks        لیست محتواهای ذخیره‌شده (صفحه‌بندی + حذف + دکمه باز کردن)
/mystats          آمار شخصی: تعداد، حجم، نمودار میله‌ای پلتفرم‌ها، اسپارک‌لاین ۱۴ روز
/autoshare        مدیریت مقصدهای اشتراک‌گذاری؛ داخل کانال/گروه: /autoshare add
/schedule         /schedule <لینک> <90m|12h|1d|7d|2w> + لیست/حذف (حداکثر ۲۰ برای هر کاربر)
/ask <سوال>       پاسخ سریع از FAQ محلی؛ در صورت نبود، از مدل AI (اگر فعال باشد)
/tokens           [مدیر] داشبورد وضعیت توکن‌های Apify + هشدارهای باز
```

رفتارهای خودکار جدید: دکمه‌ی «🔖 ذخیره» زیر هر محتوای موفق، دکمه‌ی «🤖 خلاصه فارسی» زیر کپشن‌ها/متن توییت (اگر AI فعال باشد)، ارسال آنی محتوای تکراری از کش (بدون دانلود مجدد)، ارسال خودکار محتوای جدید به مقصدهای autoshare.

## ۱۰) حسابداری دقیق حجم (EXACT_SIZES)

- منبع حجم نمایشی: هدر `Content-Length` استریم؛ اگر نبود → metadata؛ اگر نبود → تخمین `bitrate × duration × 0.95`.
- تلورانس ۵MB: اختلاف بیشتر → حجم واقعی پس از دانلود اندازه‌گیری و در جدول `size_audit_log` ثبت می‌شود.
- فرمت انسانی دو رقم اعشار (`5.00 MB`, `1.20 GB`) از `media_size.fmt_size_exact`.
- خروجی HLS/DASH به‌جای ارسال فایل playlist، به‌صورت امن به مسیر fallback (yt-dlp/ffmpeg) واگذار می‌شود.

## ۱۱) تست

- هر ۲۷ تست موجود ریپو **پاس** می‌شوند (`python -m unittest discover -s tests`).
- تست یکپارچه‌ی دستی (منو → Actor → استخراج → دانلود → audit → هشدار توکن → داشبورد → زمان‌بندی) با موفقیت اجرا شد.

## ۱۲) فعال‌سازی سریع (چک‌لیست)

1. فایل‌ها را روی ریپو overlay کنید و deploy بگیرید (Dockerfile خودش فایل‌های جدید را COPY می‌کند).
2. در Railway Variables اضافه کنید: `BOT_ADMIN_CHAT_ID=<آیدی عددی شما>` (برای هشدارهای PV).
3. اختیاری: `AI_PROVIDER=huggingface` + `AI_API_KEY=hf_...` برای خلاصه‌سازی/پرسش‌پاسخ هوشمند.
4. برای غیرفعال‌کردن هر قابلیت قبل از فعال‌سازی، پرچم مربوطه را `false` بگذارید — هیچ چیز دیگری لازم نیست.
