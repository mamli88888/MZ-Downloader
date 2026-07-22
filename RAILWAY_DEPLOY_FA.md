# راهنمای صفر تا صد انتشار MZ Downloader روی Railway

این راهنما برای همین نسخه از پروژه نوشته شده است. هیچ توکن، API hash یا StringSession را داخل GitHub، پیام عمومی، Issue یا لاگ قرار ندهید.

## 1. پیش‌نیازها

پیش از شروع این موارد را آماده کنید:

- یک حساب GitHub و یک حساب Railway
- یک ربات ساخته‌شده با `@BotFather`
- حداقل یک اکانت عادی Telegram که قبلاً وارد شده و فایل session معتبر دارد
- `api_id` و `api_hash` اکانت‌ها از `my.telegram.org`
- نصب Git و Python 3.12 روی سیستم محلی
- دسترسی مدیریت دو کانال `@MZBOTS_Monitor` و `@ImagePromptLab`

## 2. تعویض توکن قدیمی

توکن قبلی این پروژه قبلاً داخل سورس یا لاگ محلی قرار داشته است؛ قبل از انتشار آن را تعویض کنید:

1. وارد `@BotFather` شوید.
2. `/mybots` را بزنید و ربات را انتخاب کنید.
3. وارد بخش **API Token** شوید.
4. توکن قبلی را Revoke و یک توکن جدید Generate کنید.
5. توکن جدید را فقط در `.env` محلی و Railway Variables بگذارید.

توکن کامل را در ترمینال ضبط‌شده، Screenshot یا GitHub قرار ندهید.

## 3. آماده‌کردن عضویت اجباری

ربات برای بررسی عضویت دیگران باید در هر دو کانال Administrator باشد:

1. وارد تنظیمات کانال `@MZBOTS_Monitor` شوید.
2. بخش Administrators را باز و ربات اصلی را اضافه کنید.
3. همین کار را برای `@ImagePromptLab` انجام دهید.
4. لازم نیست اجازه‌ی ارسال پست بدهید؛ Administrator بودن برای اطمینان از نتیجه‌ی `getChatMember` لازم است.
5. عمومی‌بودن usernameهای کانال و دقیق‌بودن نام‌های بالا را بررسی کنید.

اگر ربات Administrator نباشد، ممکن است حتی کاربر عضو نیز پیام عضویت اجباری دریافت کند.

## 4. تست محلی پروژه

PowerShell را باز کنید:

```powershell
Set-Location 'E:\Super Projects\Bots\MZ Downloader'
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

همه‌ی تست‌ها باید با `OK` تمام شوند. سپس `.env` را تنظیم و ربات را محلی اجرا کنید:

```powershell
.\venv\Scripts\python.exe -B .\bot.py
```

برای توقف `Ctrl+C` را بزنید.

## 5. ساخت StringSession برای Railway

Railway نمی‌تواند به فایل‌های session محلی شما دسترسی داشته باشد. هر session باید به رشته‌ی محرمانه‌ی Telethon تبدیل شود.

1. مطمئن شوید `bot.py` بسته است تا فایل session قفل نباشد.
2. این فرمان را اجرا کنید:

```powershell
.\venv\Scripts\python.exe -B .\export_sessions.py
```

3. برنامه یک خط با شکل زیر می‌سازد:

```text
TELEGRAM_ACCOUNTS=[{"name":"Account-1","api_id":123456,"api_hash":"...","string_session":"..."}]
```

4. کل مقدار بعد از `=` را یک‌خطی و بدون تغییر برای Railway نگه دارید.
5. خروجی را در فایل پروژه، GitHub یا لاگ ذخیره نکنید. StringSession عملاً رمز ورود اکانت Telegram است.

اگر StringSession افشا شد، از Telegram > Settings > Devices نشست را Revoke کنید و session تازه بسازید.

## 6. بررسی فایل‌های محرمانه قبل از Git

این فایل‌ها نباید commit شوند:

- `.env` و فایل‌های `.env.*` به‌جز `.env.example`
- تمام فایل‌های `*.session` و `*.session-journal`
- `bot.log*`
- پوشه‌های `venv/`, `.venv/`, `downloads/` و `__pycache__/`

وجود قوانین را بررسی کنید:

```powershell
git check-ignore -v .env mz_downloader_session_1.session bot.log venv
```

اگر فایل حساسی قبلاً commit شده، فقط اضافه‌کردن آن به `.gitignore` کافی نیست. آن را از index حذف و secret مربوط را تعویض کنید:

```powershell
git rm --cached .env
git rm --cached '*.session'
```

## 7. ساخت repository خصوصی GitHub

در GitHub یک repository جدید و **Private** بسازید. هنگام ساخت، README یا `.gitignore` جدید اضافه نکنید تا conflict ایجاد نشود.

در پوشه‌ی پروژه اجرا کنید:

```powershell
Set-Location 'E:\Super Projects\Bots\MZ Downloader'
git init
git branch -M main
git add .gitignore .dockerignore .env.example bot.py config.py downloader.py instagram_caption.py spotisaver.py routing.py requirements.txt Dockerfile railway.json Procfile README.md RAILWAY_DEPLOY_FA.md export_sessions.py tests
git status --short
```

در خروجی `git status` نباید `.env`، session، log، دانلودها یا `venv` دیده شوند. سپس:

```powershell
git commit -m 'Prepare MZ Downloader for Railway'
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

پس از push، یک بار فایل‌های GitHub را از مرورگر بررسی کنید و مطمئن شوید secret وجود ندارد.

## 8. ساخت پروژه Railway

1. وارد Railway شوید و **New Project** را انتخاب کنید.
2. **Deploy from GitHub Repo** را بزنید.
3. دسترسی GitHub را تأیید و repository خصوصی پروژه را انتخاب کنید.
4. اگر ریشه‌ی repository همین پوشه‌ی `MZ Downloader` است، **Root Directory** را خالی بگذارید.
5. اگر repository شامل چند پروژه است، Root Directory را روی `/MZ Downloader` قرار دهید.
6. Railway باید `Dockerfile` را تشخیص دهد. Builder پروژه روی Dockerfile تنظیم شده است.

## 9. تنظیم Railway Variables

در Service > Variables متغیرهای زیر را اضافه کنید. مقدارها را بدون کوتیشن اضافه وارد کنید:

```text
BOT_TOKEN=توکن-جدید-BotFather
TELEGRAM_ACCOUNTS=[{"name":"Account-1","api_id":123456,"api_hash":"...","string_session":"..."}]
USE_PROXY=false
DOWNLOAD_DIR=/tmp/mz-downloader
MAX_FILE_SIZE_MB=30
MAX_DOWNLOAD_SIZE_MB=0
PRIMARY_DOWNLOADER_BOT=download_it_bot
SECONDARY_DOWNLOADER_BOT=AllSavesBot
SPOTIFY_DOWNLOADER_BOT=spotifysavesbot
SOUNDCLOUD_DOWNLOADER_BOT=scload_bot
DOWNLOADER_BOTS=download_it_bot,AllSavesBot
INSTAGRAM_YOUTUBE_BOTS=allsaverbot,instadowbot,download_it_bot,AllSavesBot
TIKTOK_DOWNLOADER_BOTS=download_it_bot,AllSavesBot
MAX_LINKS_PER_MESSAGE=5
MAX_CONCURRENT_UPDATES=12
MAX_QUEUE_SIZE=50
WORKER_ACQUIRE_TIMEOUT_SECONDS=180
RATE_LIMIT_REQUESTS=8
RATE_LIMIT_WINDOW_SECONDS=60
WAIT_TIMEOUT_SECONDS=90
SELECTION_TTL_SECONDS=600
ALBUM_COLLECT_WINDOW_SECONDS=2.5
PREVIEW_GRACE_SECONDS=3
LATE_RESPONSE_COOLDOWN_SECONDS=180
```

نکات مهم:

- متغیر `PORT` را نسازید؛ Railway آن را خودکار تعیین می‌کند.
- `TELEGRAM_ACCOUNTS` باید JSON معتبر و یک‌خطی باشد.
- `MAX_DOWNLOAD_SIZE_MB=0` سقف داخلی برنامه را حذف می‌کند.
- `MAX_FILE_SIZE_MB=30` سقف هر قطعه‌ی ارسالی است، نه سقف کل دانلود.
- اگر پروکسی لازم است، `USE_PROXY=true` و `PROXY_TYPE`, `PROXY_HOST`, `PROXY_PORT` را تنظیم کنید.

## 10. حجم‌های بسیار بزرگ و Railway Volume

هیچ سرویس ابری فضای واقعاً نامحدود ندارد. این نسخه سقف برنامه‌ای روی مجموع دانلود ندارد، اما ظرفیت دیسک، RAM، زمان اجرا، بات واسط و Telegram همچنان محدودیت فیزیکی دارند.

برای فایل‌های بسیار بزرگ بهتر است در Railway یک Volume بسازید:

1. در Service یک Volume اضافه کنید.
2. Mount Path را مثلاً `/data` قرار دهید.
3. مقدار `DOWNLOAD_DIR` را به `/data/mz-downloader` تغییر دهید.
4. ظرفیت Volume را متناسب با بزرگ‌ترین فایل مورد انتظار انتخاب کنید.

فایل بزرگ‌تر از `MAX_FILE_SIZE_MB` به قطعات `.part001`, `.part002`, ... تبدیل می‌شود. برنامه هر قطعه را جداگانه می‌سازد، می‌فرستد و پاک می‌کند تا مصرف موقت دیسک دو برابر نشود.

## 11. تنظیم Deploy و Health Check

فایل `railway.json` این موارد را از قبل تنظیم کرده است:

- فقط یک replica
- health check روی `/health`
- timeout سلامت 300 ثانیه
- restart در صورت failure
- `overlapSeconds=0` برای جلوگیری از اجرای هم‌زمان دو polling process

تعداد replica را روی `1` نگه دارید. اجرای هم‌زمان دو instance باعث خطای Telegram polling و تداخل sessionها می‌شود.

در Settings > Networking یک Public Domain بسازید. سپس آدرس زیر را باز کنید:

```text
https://YOUR-DOMAIN.up.railway.app/health
```

پاسخ سالم شبیه این است:

```json
{"status":"ok","accounts":3,"active":0,"uptime_seconds":42}
```

مقدار `accounts` باید حداقل `1` و HTTP status باید `200` باشد.

## 12. بررسی لاگ اولین Deploy

در Deploy Logs دنبال این پیام‌ها بگردید:

```text
Connected downloader account
Health endpoint listening on port
Bot initialized with ... downloader account(s)
MZ Downloader is starting
```

هیچ‌وقت کل Variables یا StringSession را در تیکت پشتیبانی و Screenshot لاگ منتشر نکنید.

## 13. تست کامل بعد از انتشار

تست‌ها را به‌ترتیب انجام دهید:

1. با اکانتی که عضو کانال‌ها نیست `/start` را بزنید؛ باید دو دکمه‌ی عضویت نمایش داده شود.
2. در هر دو کانال عضو شوید و دوباره `/start` را بزنید؛ صفحه‌ی اصلی باید باز شود.
3. `/caption` را همراه لینک یک پست عمومی Instagram آزمایش کنید؛ کپشن باید از Instaspeeder ارسال شود.
4. یک لینک Instagram، YouTube و TikTok بفرستید و thumbnail و گزینه‌های کیفیت را بررسی کنید.
5. Twitter/X، Facebook و VK را بررسی کنید؛ فقط واسط اول باید استفاده شود.
6. یک Spotify track، یک Spotify album و یک SoundCloud را بررسی کنید؛ آلبوم باید ZIP شود.
7. یک فایل بزرگ‌تر از 30 MB را آزمایش کنید؛ باید در چند بخش ارسال شود.
8. `/cancel`, `/status`, `/platforms`, `/stats` و `/help` را بررسی کنید.
9. Service را Restart و دوباره `/health` را بررسی کنید.

## 14. رفع خطاهای رایج

### Build موفق است ولی `instagram_caption` پیدا نمی‌شود

Root Directory اشتباه است یا Dockerfile قدیمی deploy شده. آخرین commit را بررسی و Redeploy کنید. Dockerfile فعلی `instagram_caption.py` و `spotisaver.py` را کپی می‌کند.

### `/health` پاسخ 503 و `accounts: 0` می‌دهد

`TELEGRAM_ACCOUNTS` JSON نامعتبر است، StringSession منقضی شده، `api_id/api_hash` اشتباه است یا اتصال Telegram مسدود شده. ابتدا runtime logs را بررسی کنید.

### خطای `Conflict: terminated by other getUpdates request`

یک نسخه‌ی دیگر از ربات محلی یا Railway هنوز اجراست. اجرای محلی را ببندید و replica را روی `1` نگه دارید.

### همه‌ی کاربران همچنان پیام عضویت می‌گیرند

ربات اصلی را در هر دو کانال Administrator کنید و username کانال‌ها را تغییر ندهید. سپس چند ثانیه صبر و دوباره `/start` را اجرا کنید.

### کپشن Instagram پیدا نمی‌شود

فقط پست، Reel و IGTV عمومی پشتیبانی می‌شوند. پاسخ به در دسترس‌بودن Instaspeeder و reCAPTCHA آن وابسته است؛ اگر سایت درخواست را رد کند ربات پیام خطای قابل‌فهم می‌دهد.

### فایل بزرگ کامل نمی‌شود

فضای دیسک/Volume، RAM و زمان پاسخ بات واسط را بررسی کنید. `MAX_DOWNLOAD_SIZE_MB=0` محدودیت داخل برنامه را حذف می‌کند، اما منابع Railway یا Telegram را نامحدود نمی‌کند.

### تغییر Variable اعمال نشده است

پس از تغییر Variables، Deploy یا Restart جدید انجام دهید و active deployment را بررسی کنید.

## 15. انتشار نسخه‌های بعدی

پس از هر تغییر محلی:

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
git status --short
git add bot.py config.py downloader.py instagram_caption.py spotisaver.py routing.py tests README.md RAILWAY_DEPLOY_FA.md
git commit -m 'Update downloader bot'
git push
```

Railway معمولاً پس از push خودکار deploy می‌کند. بعد از هر deploy، لاگ، `/health` و حداقل یک دانلود واقعی را بررسی کنید.

## چک‌لیست نهایی امنیت

- توکن قدیمی BotFather تعویض شده است.
- `.env` و sessionها در GitHub نیستند.
- StringSession فقط داخل Railway Variables است.
- repository خصوصی است.
- ربات در هر دو کانال Administrator است.
- replica دقیقاً `1` است.
- `/health` پاسخ 200 با `accounts > 0` می‌دهد.
- دانلود و `/caption` با یک اکانت عضو و غیرعضو تست شده‌اند.
