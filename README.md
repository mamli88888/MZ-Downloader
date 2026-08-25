# MZ Downloader

ربات دانلود چنداکانتی تلگرام با مسیریابی اختصاصی، جست‌وجوی YouTube، انتخاب کیفیت، thumbnail، کپشن Instagram، آلبوم ZIP Spotify، نوار پیشرفت، عضویت اجباری و پشتیبانی Railway.

راهنمای کامل انتشار: [RAILWAY_DEPLOY_FA.md](RAILWAY_DEPLOY_FA.md)
راهنمای ارتقای ۱۴۰۴ (پلتفرم‌های جدید Apify، هشدار توکن، بوکمارک، زمان‌بندی، AI و …): [README_NEW_FEATURES.md](README_NEW_FEATURES.md)

## مسیریابی

- YouTube: مسیر اصلی **VoidDL** (`https://voiddl.app`) است. هر API Key اجازه‌ی ۲۰ دانلود در دقیقه و ۱۰ گیگابایت دانلود در روز دارد؛ چند کلید را در `VOIDDL_API_KEYS` بگذارید تا لحظه‌ای که یکی محدود شد، بلافاصله کلید بعدی استفاده شود. اگر VoidDL شکست بخورد مسیر بعدی **Yoinku**، سپس **Apify** و در نهایت بات‌های تلگرامی (`allsaverbot`، `instadowbot`، `download_it_bot` و `AllSavesBot`) اجرا می‌شوند.
- Instagram: اگر `APIFY_TOKENS` تنظیم باشد، ابتدا منوی دکمه‌ای کیفیت یا فقط‌صدا از Apify نمایش داده می‌شود؛ Instagram با `apify/instagram-scraper` اجرا می‌شود. اگر Apify خطا دهد یا تنظیم نشده باشد، همان مسیرهای قبلی اجرا می‌شوند.
- TikTok: `download_it_bot` و سپس `AllSavesBot`
- Twitter/X / Facebook / VK: فقط `download_it_bot`
- Spotify track: فقط `spotifysavesbot`
- Spotify album: دریافت ترتیبی ترک‌ها از `spotisaver.net` و ساخت ZIP
- SoundCloud: فقط `scload_bot`
- دامنه‌های دیگر پیش از ارسال به واسط رد می‌شوند.
- اگر مسیر اختصاصی شکست بخورد، تمام واسط‌های یکتا یک‌بار به‌عنوان fallback اضطراری امتحان می‌شوند.

### کارت کیفیت یوتیوب

وقتی کاربر لینک یوتیوب می‌فرستد، ربات بهترین کیفیت تامبنیل (maxresdefault) را دانلود و به‌صورت عکس می‌فرستد؛ پیام منبع (مثلاً `youtu.be`)، عنوان و مدت ویدیو، جدول حجم تقریبی هر کیفیت و مهلت انتخاب (پیش‌فرض ۱۰ دقیقه) به‌صورت کپشن زیر همان عکس نمایش داده می‌شود و دکمه‌های کیفیت فقط عدد کیفیت را نشان می‌دهند (مثلاً `480` یا `720` یا `MP3`). حجم‌های نمایش‌داده‌شده از خود VoidDL گرفته می‌شوند.

### لایه سرعت VoidDL (دانلود موازی + پری‌فچ)

اندازه‌گیری زنده روی API نشان داد: سرور voiddl هر درخواست دانلود را از نو آماده می‌کند (۱۵–۷۶ ثانیه بسته به حجم فایل و بار سرور؛ کش نمی‌شود)، اما آماده‌سازی درخواست‌های هم‌زمان موازی انجام می‌شود، درخواست‌های Range (HTTP 206) پشتیبانی می‌شوند و پهنای باند هر کلید حدود ۱۲–۱۵MB/s سقف دارد. بر همین اساس:

1. **دانلود چندلایه:** فایل‌های بزرگ (≥۱۶MB) با چند درخواست Range هم‌زمان دانلود می‌شوند (پیش‌فرض تا ۸ لاین با `VOIDDL_PARALLEL_LANES`)؛ لاین‌ها به‌طور خودکار روی کلیدهای موجود پخش می‌شوند و aggregate سرعت تقریباً برابر تعداد کلیدها × ۱۲–۱۵MB/s می‌شود. فایل‌های کوچک تک‌استریم می‌مانند تا توکن محدودیت ۲۰/دقیقه هدر نرود.
2. **پری‌فچ پس‌زمینه:** به‌محض نمایش کارت کیفیت (حتی قبل از دانلود تامبنیل)، دانلود دو کیفیت محتمل‌تر (اولویت 1080p سپس 720p) به‌همراه MP3 در پس‌زمینه شروع می‌شود (`VOIDDL_PREFETCH_COUNT` و `VOIDDL_PREFETCH_LANES`). اگر کاربر همان کیفیت را انتخاب کند، ربات به دانلود در جریان وصل می‌شود و نتیجه فوری یا با کمترین انتظار می‌رسد.
3. **صرفه‌جویی:** به‌محض انتخاب کیفیت (یا منقضی شدن سشن)، پری‌فچ‌های بلااستفاده لغو می‌شوند تا پهنای باند روزانه (۱۰ گیگ به ازای هر کلید) نسوزد؛ اگر باقی‌مانده پهنای باند از `VOIDDL_PREFETCH_MIN_REMAINING_MB` (پیش‌فرض ۲ گیگ) کمتر باشد پری‌فچ اصلاً شروع نمی‌شود و فایل‌های بزرگ‌تر از `VOIDDL_PREFETCH_MAX_MB` (پیش‌فرض ۵۱۲ مگ) پری‌فچ نمی‌شوند.

نکته عملی: برای فایل‌های ۲۰۰–۳۰۰ مگی، افزودن ۳–۴ کلید به `VOIDDL_API_KEYS` بیشترین اثر را دارد چون هر کلید یک لوله مستقل سرعت و سهمیه است.

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
VOIDDL_ENABLED=true
VOIDDL_API_KEYS=vd_کلید-اول,vd_کلید-دوم,vd_کلید-سوم
VOIDDL_DAILY_BANDWIDTH_MB=10240
VOIDDL_PER_MINUTE_LIMIT=20
# لایه سرعت: دانلود موازی + پری‌فچ پس‌زمینه (پیش‌فرض‌ها مناسب است)
VOIDDL_PARALLEL_LANES=8
VOIDDL_PREFETCH=true
VOIDDL_PREFETCH_COUNT=2
VOIDDL_PREFETCH_LANES=4
VOIDDL_PREFETCH_MAX_MB=512
VOIDDL_PREFETCH_MIN_REMAINING_MB=2048
VOIDDL_PREFETCH_TTL=720
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
