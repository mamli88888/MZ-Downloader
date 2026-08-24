# MZ Downloader

ربات دانلود چنداکانتی تلگرام با مسیریابی اختصاصی، جست‌وجوی YouTube، انتخاب کیفیت، thumbnail، کپشن Instagram، آلبوم ZIP Spotify، نوار پیشرفت، عضویت اجباری و پشتیبانی Railway.

راهنمای کامل انتشار: [RAILWAY_DEPLOY_FA.md](RAILWAY_DEPLOY_FA.md)
راهنمای ارتقای ۱۴۰۴ (پلتفرم‌های جدید Apify، هشدار توکن، بوکمارک، زمان‌بندی، AI و …): [README_NEW_FEATURES.md](README_NEW_FEATURES.md)

## مسیریابی

- Instagram / YouTube: اگر `APIFY_TOKENS` تنظیم باشد، ابتدا منوی دکمه‌ای کیفیت یا فقط‌صدا از Apify نمایش داده می‌شود؛ YouTube با `streamers/youtube-video-downloader` و Instagram با `apify/instagram-scraper` اجرا می‌شوند. توکن‌ها چرخشی‌اند و در خطا سریع به توکن بعدی می‌روند. اگر Apify خطا دهد یا تنظیم نشده باشد، همان مسیرهای قبلیِ `allsaverbot`، `instadowbot`، `download_it_bot` و `AllSavesBot` به‌عنوان fallback اجرا می‌شوند.
- TikTok: `download_it_bot` و سپس `AllSavesBot`
- Twitter/X / Facebook / VK: فقط `download_it_bot`
- Spotify track: فقط `spotifysavesbot`
- Spotify album: دریافت ترتیبی ترک‌ها از `spotisaver.net` و ساخت ZIP
- SoundCloud: فقط `scload_bot`
- دامنه‌های دیگر پیش از ارسال به واسط رد می‌شوند.
- اگر مسیر اختصاصی شکست بخورد، تمام واسط‌های یکتا یک‌بار به‌عنوان fallback اضطراری امتحان می‌شوند.

## کپشن و عضویت

- فرمان `/caption لینک` کپشن پست، Reel یا IGTV عمومی Instagram را از فرم `instaspeeder.com` دریافت می‌کند.
- استفاده از ربات به عضویت در `@MZBOTS_Monitor` و `@ImagePromptLab` نیاز دارد.
- ربات اصلی باید در هر دو کانال Administrator باشد تا Telegram اجازه‌ی بررسی عضویت کاربران را بدهد.

## جست‌وجوی YouTube

- در چت خصوصی کافی است عبارت جست‌وجو را به‌صورت متن عادی بفرستید؛ فرمان `/search عبارت` و میان‌بر `/yt عبارت` نیز در خصوصی و گروه قابل استفاده‌اند.
- ربات حداکثر ۳۰ نتیجهٔ مرتبط را در پنج صفحه نمایش می‌دهد. هر صفحه یک تصویر شامل شش thumbnail شماره‌دار و سه ردیف دکمهٔ دوتایی دارد.
- فقط ویدیوهای معمولی و Shorts نگه داشته می‌شوند؛ Live، پخش آینده/ضبط‌شدهٔ زنده، آهنگ و خروجی‌های صوتی از نتایج حذف می‌شوند.
- دکمه‌های صفحهٔ قبل و بعد همان نتیجه‌های دریافت‌شده را ورق می‌زنند و جست‌وجوی تازه‌ای انجام نمی‌دهند.
- انتخاب «محتوا» URL استاندارد همان ویدیو را وارد مسیر دانلود فعلی می‌کند؛ منوی کیفیت، فقط صدا و روند ارسال فایل تغییری نمی‌کنند.
- جست‌وجو به API پولی یا کلید API نیاز ندارد. برای هماهنگی با تغییرات YouTube، نسخهٔ `yt-dlp` را هنگام نگهداری دوره‌ای پروژه به‌روز نگه دارید.

## اجرای محلی

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe bot.py
```

## انتشار روی Railway

1. توکن فعلی را در BotFather تعویض کنید؛ نسخه‌ی قدیمی قبلاً در سورس و لاگ محلی بوده است.
2. روی سیستم محلی sessionهای فعلی را به StringSession تبدیل کنید:

```powershell
.\venv\Scripts\python.exe export_sessions.py
```

3. خروجی `TELEGRAM_ACCOUNTS=...` را فقط در Railway Variables قرار دهید.
4. متغیرهای زیر را در Railway تنظیم کنید:

```text
BOT_TOKEN=...
TELEGRAM_ACCOUNTS=[...]
APIFY_ENABLED=true
APIFY_TOKENS=token-اول,token-دوم
APIFY_RUN_TIMEOUT_SECONDS=360
APIFY_POLL_INTERVAL_SECONDS=3
APIFY_TOKEN_COOLDOWN_SECONDS=600
USE_PROXY=false
DOWNLOAD_DIR=/tmp/mz-downloader
MAX_FILE_SIZE_MB=30
MAX_DOWNLOAD_SIZE_MB=0
PRIMARY_DOWNLOADER_BOT=download_it_bot
SECONDARY_DOWNLOADER_BOT=AllSavesBot
SPOTIFY_DOWNLOADER_BOT=spotifysavesbot
SOUNDCLOUD_DOWNLOADER_BOT=scload_bot
INSTAGRAM_YOUTUBE_BOTS=ziyotech_instagram_downloaderbot,allsaverbot,instadowbot,download_it_bot,AllSavesBot
TIKTOK_DOWNLOADER_BOTS=download_it_bot,AllSavesBot
```

5. پروژه را از GitHub به Railway متصل کنید. `Dockerfile` و `railway.json` به‌طور خودکار استفاده می‌شوند. اگر همین پوشه ریشه‌ی repository است Root Directory را خالی بگذارید؛ در monorepo مقدار آن را روی `/MZ Downloader` قرار دهید.
6. تعداد replica را همیشه روی `1` نگه دارید؛ polling و sessionهای تلگرام نباید هم‌زمان در چند کانتینر اجرا شوند.

Railway متغیر `PORT` را خودش می‌سازد. مسیر `/health` پس از اتصال حداقل یک اکانت، پاسخ 200 می‌دهد.

`MAX_DOWNLOAD_SIZE_MB=0` فقط سقف داخلی برنامه را غیرفعال می‌کند. فایل بزرگ‌تر از `MAX_FILE_SIZE_MB` روی Gofile آپلود می‌شود و کاربر لینک Cloudflare Worker را می‌گیرد؛ اگر تنظیمات Gofile/Worker ناقص باشد یا آپلود شکست بخورد، فایل طبق رفتار قبلی بخش‌بندی می‌شود.

راهنمای کامل ساخت Token و تنظیم Actorهای Apify در [APIFY_SETUP_FA.md](APIFY_SETUP_FA.md) قرار دارد. Token را فقط در Variables سرویس نگه دارید و هرگز commit نکنید.

## راه‌اندازی Gofile با Cloudflare Worker

در این روش Gofile همچنان فایل را نگه می‌دارد، اما کاربر هیچ لینک Gofile دریافت نمی‌کند. Worker از مسیر Cloudflare به Gofile وصل می‌شود، لینک مستقیم را می‌گیرد و فایل را به‌صورت streaming به کاربر برمی‌گرداند. حذف خودکار فایل از Gofile توسط خود ربات و پس از `GOFILE_DELETE_DELAY_SECONDS` انجام می‌شود.

### ۱) ساخت Worker

1. در Cloudflare یک حساب بسازید یا وارد شوید.
2. از بخش **Workers & Pages → Create application → Create Worker** یک Worker بسازید.
3. فایل `cloudflare_worker/src/index.js` همین repository را به‌عنوان کد Worker قرار دهید.
4. در بخش **Settings → Variables and Secrets** این دو Secret را بسازید:

```text
GOFILE_API_TOKEN=توکن API حساب Gofile
WORKER_ACCESS_KEY=یک کلید تصادفی طولانی
```

برای ساخت کلید تصادفی می‌توانید از `openssl rand -hex 32` استفاده کنید. مقدار Secret را در GitHub یا چت قرار ندهید.

5. Worker را Deploy کنید و URL آن را کپی کنید؛ معمولاً شبیه این است:
   `https://mz-gofile-proxy.<account>.workers.dev`

### ۲) تنظیم ربات

در Railway یا Replit این متغیرها را تنظیم کنید:

```text
GOFILE_TOKENS=توکن-اول,توکن-دوم
GOFILE_DELETE_DELAY_SECONDS=3600
CLOUDFLARE_WORKER_URL=https://mz-gofile-proxy.example.workers.dev
CLOUDFLARE_WORKER_ACCESS_KEY=همان-WORKER_ACCESS_KEY
```

`CLOUDFLARE_WORKER_ACCESS_KEY` باید دقیقاً با `WORKER_ACCESS_KEY` داخل Cloudflare یکی باشد. `CLOUDFLARE_WORKER_URL` را بدون `/` پایانی وارد کنید.

### ۳) تنظیم حساب Gofile

از حساب Gofile به صفحه Profile/API بروید و API token بسازید. API token را فقط در Secretهای Worker و متغیر `GOFILE_TOKENS` سرویس ربات قرار دهید. اگر چند token دارید، با کاما جدا کنید تا ربات بین آن‌ها چرخش کند.

### رفتار حذف و خطا

- ربات فایل را روی Gofile آپلود می‌کند.
- لینک ارسالی به کاربر فقط `CLOUDFLARE_WORKER_URL/download/<folderId>/<fileId>` است.
- Worker با token امن خودش لینک مستقیم Gofile را می‌گیرد و پاسخ را stream می‌کند؛ فایل روی Worker ذخیره نمی‌شود.
- پس از زمان `GOFILE_DELETE_DELAY_SECONDS`، ربات فایل Gofile را حذف می‌کند و لینک دیگر کار نمی‌کند.
- اگر Worker یا Gofile تنظیم نشده باشد، ربات به‌صورت خودکار به ارسال پارت‌های Telegram برمی‌گردد.
- Worker برای هر درخواست کلید مشترک را بررسی می‌کند و از دسترسی تصادفی به شناسه فایل جلوگیری می‌شود.

### تست نهایی

یک فایل بزرگ‌تر از ۳۰ مگابایت بفرستید. باید پیام لینک دانلود با دامنه `workers.dev` دریافت کنید. لینک را بدون VPN/فیلترشکن در مرورگر باز کنید. سپس بعد از زمان تعیین‌شده، حذف فایل را با باز کردن دوباره لینک بررسی کنید.

## امنیت

- `.env`، StringSession، توکن و فایل‌های `*.session` را commit نکنید.
- دانلودها موقتی هستند و پس از ارسال پاک می‌شوند.
- لینک‌ها برای پردازش به بات واسط مربوط به همان پلتفرم فرستاده می‌شوند.
