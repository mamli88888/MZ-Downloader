# MZ Downloader

ربات دانلود چنداکانتی تلگرام با مسیریابی اختصاصی، جست‌وجوی YouTube، انتخاب کیفیت، thumbnail، کپشن Instagram، آلبوم ZIP Spotify، نوار پیشرفت، عضویت اجباری و پشتیبانی Railway.

راهنمای کامل انتشار: [RAILWAY_DEPLOY_FA.md](RAILWAY_DEPLOY_FA.md)

## مسیریابی

- Instagram / YouTube: به‌ترتیب `allsaverbot`، `instadowbot`، `download_it_bot` و `AllSavesBot`
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
USE_PROXY=false
DOWNLOAD_DIR=/tmp/mz-downloader
MAX_FILE_SIZE_MB=30
MAX_DOWNLOAD_SIZE_MB=0
PRIMARY_DOWNLOADER_BOT=download_it_bot
SECONDARY_DOWNLOADER_BOT=AllSavesBot
SPOTIFY_DOWNLOADER_BOT=spotifysavesbot
SOUNDCLOUD_DOWNLOADER_BOT=scload_bot
INSTAGRAM_YOUTUBE_BOTS=allsaverbot,instadowbot,download_it_bot,AllSavesBot
TIKTOK_DOWNLOADER_BOTS=download_it_bot,AllSavesBot
```

5. پروژه را از GitHub به Railway متصل کنید. `Dockerfile` و `railway.json` به‌طور خودکار استفاده می‌شوند. اگر همین پوشه ریشه‌ی repository است Root Directory را خالی بگذارید؛ در monorepo مقدار آن را روی `/MZ Downloader` قرار دهید.
6. تعداد replica را همیشه روی `1` نگه دارید؛ polling و sessionهای تلگرام نباید هم‌زمان در چند کانتینر اجرا شوند.

Railway متغیر `PORT` را خودش می‌سازد. مسیر `/health` پس از اتصال حداقل یک اکانت، پاسخ 200 می‌دهد.

`MAX_DOWNLOAD_SIZE_MB=0` فقط سقف داخلی برنامه را غیرفعال می‌کند. فایل بزرگ‌تر از `MAX_FILE_SIZE_MB` بخش‌بندی می‌شود، اما فضای دیسک/زمان اجرای Railway و محدودیت‌های Telegram و بات واسط همچنان واقعی هستند.

## امنیت

- `.env`، StringSession، توکن و فایل‌های `*.session` را commit نکنید.
- دانلودها موقتی هستند و پس از ارسال پاک می‌شوند.
- لینک‌ها برای پردازش به بات واسط مربوط به همان پلتفرم فرستاده می‌شوند.
