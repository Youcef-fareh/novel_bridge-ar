# Novel Bridge AR User Guide / دليل استخدام Novel Bridge AR

This guide is bilingual. The English instructions come first, followed by the Arabic instructions.

## English

Novel Bridge AR downloads chapters from supported novel websites, translates them with an AI provider, and creates an EPUB book for offline reading.

### 1. Install and start

**Windows installer:** Run `NovelBridgeAR_Setup.exe`, then open NovelBridge AR from the Start Menu. The installer includes the application and its embedded GUI runtime. The optional Playwright Chromium runtime is a separate, version-matched release download so it does not inflate the installer. An internet connection and a translation API key are still required.

**Enable browser-based scraping (optional):** Download `NovelBridgeAR_BrowserPack_<version>.zip` from the same GitHub release as the installer, verify its `.sha256` file, and extract the ZIP contents into `%LOCALAPPDATA%\ms-playwright` (create the folder if needed). Do not extract the archive into a new nested folder. Restart NovelBridge AR after extraction. Browser revisions match the app release, so repeat these steps with the matching pack after installing a newer app version. The GUI and ordinary HTTP-based scraping work without this pack; browser-rendered scraping, such as the Galaxy chapter-list expansion, requires it.

**Run from source:** Install Python 3.11 or newer. In PowerShell, from the project folder, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
python run_gui.py
```

If activation is blocked, run `.venv\Scripts\python.exe run_gui.py` using the Python executable inside the environment. The first run creates the local database.

### 1.1 Check for updates

The installed Windows app checks for a stable release after startup. When an update is available, it shows the release notes and download size; nothing is installed unless you choose **Download and Install**. You can also check from **Help → Check for Updates**. The app uses a changed-files-only installer when your version has a matching incremental package, and falls back to the full installer otherwise. Downloads are checked against the release SHA-256 checksum. The app closes during installation and restarts afterward. Database files, EPUB output, and `.env` settings are not part of incremental updates.

Updates are available for packaged Windows releases, not for runs from source.

### 2. Add an API key

You need at least one provider. API keys are created on the provider's website, not inside Novel Bridge AR:

| Provider | Create a key | Main settings |
| --- | --- | --- |
| Google Gemini | https://aistudio.google.com/app/apikey | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| Groq | https://console.groq.com | `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_BASE_URL` |
| xAI Grok | https://console.x.ai | `XAI_API_KEY`, `GROK_MODEL`, `XAI_BASE_URL` |
| TokenRouter | https://tokenrouter.com | `TOKENROUTER_API_KEY`, `TOKENROUTER_MODEL`, `TOKENROUTER_BASE_URL` |
| OrcaRouter | https://orcarouter.ai | `ORCAROUTER_API_KEY`, `ORCAROUTER_MODEL`, `ORCAROUTER_BASE_URL` |

In the application:

1. Open **API Keys**.
2. Find a provider and click **Edit**.
3. Paste the key into the API Key field. Never paste a key into a novel, glossary, or chat message.
4. Confirm the model name and Base URL. For Gemini, use model `gemini-3.5-flash-lite`; the official SDK uses `https://generativelanguage.googleapis.com` internally, so do not add `GEMINI_BASE_URL` to `.env`. Base URL is configurable for OpenAI-compatible providers such as TokenRouter, OrcaRouter, and xAI Grok.
5. Click **Save All**. The provider should show **Connected**.
6. Click **Test Provider** to confirm that a key is present. This checks configuration; a real translation also depends on quota, model availability, and the network.

The values are saved in `.env` in the project root. The interface masks secrets, but `.env` is not encrypted. Do not commit it or share it. To configure the file manually, copy `.env.example` to `.env` and replace the placeholder values.

Gemini example:

```env
GEMINI_API_KEY=your-secret-key
GEMINI_MODEL=gemini-3.5-flash-lite
# No GEMINI_BASE_URL is needed; the Google SDK uses its official endpoint.
```

**Auto provider:** If you choose **Auto**, the application tries TokenRouter, OrcaRouter, Gemini, then Groq. Configure the provider you want to use first, or select a provider explicitly for predictable results.

### 3. Translate a novel

1. In **Library**, click **Add Novel** and either paste a supported novel URL or choose **Import EPUB** and select an EPUB file.
2. Find novels in the bookshelf using search, status filters, or sorting. Select a novel to open its chapter workspace; use **Back to library** to return to the collection.
3. Click **Search Sources** to search a title independently of the saved library. Choose a scraper or search all enabled search-capable adapters, then add a result to the library. Results arrive source by source while the progress bar shows checked sources, the active scraper, and failures. Matching titles are grouped with a language tag and a separate add action for each source; **Stop** keeps results already received.
4. In **Settings**, enable or disable adapters, filter them by language/category, and set the default search filters and result limit. These preferences are saved under `data/source_preferences.json`.
5. For a website novel, start scraping and wait for its chapters to appear. Scraping also loads the novel's cover into the Library when the site provides one. Imported EPUB chapters are added to the library ready to translate.
6. Select the chapters to translate.
7. Choose a provider and model, then start translation.
8. Watch each chapter's status. Retry failed chapters before building the book.

The packaged desktop app loads its interface from a local `file://` page. Qt WebEngine blocks remote requests from local pages by default, which prevented external cover URLs from loading in the bookshelf. The app now explicitly permits those remote image requests; sources that omit covers or block hotlinking can still show the title placeholder. The API cover route fetches and validates the image server-side instead of returning a redirect to the remote host.

EPUB import reads chapter text and reading order from the book. The original file is left unchanged; the translated EPUB is rebuilt by Novel Bridge AR, so original images and formatting are not carried over. DRM-protected or invalid EPUB files cannot be imported.

### 4. Keep names consistent

Open **Glossary**, add the source term and its preferred Arabic translation, then save it. Use **Sync from JSON** to load shared rules from `config/glossary.json`. Test important terms on a short chapter first.

### 5. Build the EPUB

When the required chapters are translated, click **Build EPUB**. Books are saved in `output/` with the novel title as the filename. Only successfully translated chapters are included.

### 6. Save EPUBs to Google Drive

The **Save All to Google Drive** button uploads every `.epub` file in `output/` to a `NovelBridge AR` folder in the signed-in Google Drive. Existing files with the same name are updated instead of duplicated.

Before the first upload:

1. Enable the Google Drive API in Google Cloud Console.
2. Create OAuth credentials for a **Desktop app** and download the client JSON file.
3. Place it at `config/google_client_secret.json`, or set `GOOGLE_DRIVE_CREDENTIALS_FILE` in `.env`.
4. Click **Save All to Google Drive** and complete the browser sign-in. The app caches the refresh token locally for later uploads.

The app requests only the `drive.file` permission, which allows it to create and update files managed by NovelBridge. No Google password is entered into NovelBridge.

### Troubleshooting

| Error or symptom | What to do |
| --- | --- |
| `No translation API key configured` | Open **API Keys**, add at least one key, click **Save All**, and restart the translation. |
| `... API key not configured` | Configure the named provider, or select a provider for which you already have a key. Check spelling in `.env`. |
| Invalid key, unauthorized, or 401/403 | Create a new key on the provider website, check that it is active and has permission, replace it with **Edit**, then **Save All**. |
| Rate limit, quota, or 429 | Wait, reduce the number of jobs, check provider billing/quota, or use another configured provider. |
| Model not found or bad request | Check the exact model name and Base URL. Remove old model entries and add the current provider model. |
| Browser-based scraping reports that its executable is missing | Download the version-matched `NovelBridgeAR_BrowserPack_<version>.zip` from the release and extract its contents into `%LOCALAPPDATA%\ms-playwright`. |
| Scrape/import fails | Confirm the URL is supported and opens in a browser. Retry later if the site is unavailable. Site selectors are in `config/sites.json`; do not bypass access controls. |
| A scraping browser remains open or the app is closed during scraping | Closing the app cancels nodriver scraping and signals its temporary Chrome window to close. Chapters completed before closing remain saved. |
| Application does not start | Activate `.venv`, reinstall with `python -m pip install -r requirements.txt`, install the browser with `python -m playwright install`, then run `python run_gui.py`. |
| EPUB is missing chapters | Check chapter statuses, retry failed chapters, and build the EPUB again. |
| Google Drive OAuth file not found | Download a Desktop OAuth client JSON, place it at `config/google_client_secret.json`, or configure `GOOGLE_DRIVE_CREDENTIALS_FILE`. |
| Google Drive sign-in or upload fails | Confirm the Drive API is enabled, finish the browser consent flow, and check your internet connection. |

For development checks, run `pytest` from the project root. Use the tool only for novels and websites you are authorized to access and translate. Provider, website, and third-party content terms still apply; the MIT license covers this project's source code only.

## العربية

يقوم Novel Bridge AR بتنزيل فصول الروايات من المواقع المدعومة، وترجمتها باستخدام مزود ذكاء اصطناعي، ثم إنشاء كتاب EPUB للقراءة دون اتصال.

### 1. التثبيت والتشغيل

**مثبت Windows:** شغّل الملف `NovelBridgeAR_Setup.exe`، ثم افتح Novel Bridge AR من قائمة Start. يحتوي المثبت على التطبيق وبيئة واجهة المستخدم المضمنة. أما متصفح Playwright الاختياري فيُنشر كتنزيل منفصل مطابق لإصدار التطبيق لتقليل حجم المثبت. يلزم اتصال بالإنترنت ومفتاح API للترجمة.

**تفعيل الجلب باستخدام المتصفح (اختياري):** نزّل الملف `NovelBridgeAR_BrowserPack_<version>.zip` من صفحة إصدار GitHub نفسها التي نزّلت منها المثبت، وتحقق من ملف `.sha256`، ثم فك محتويات ZIP مباشرةً داخل `%LOCALAPPDATA%\ms-playwright` (أنشئ المجلد عند الحاجة). لا تفك الأرشيف داخل مجلد فرعي إضافي. أعد تشغيل Novel Bridge AR بعد فك الملفات. تتطابق إصدارات المتصفح مع إصدار التطبيق؛ لذلك كرر الخطوات باستخدام الحزمة المطابقة بعد تحديث التطبيق. تعمل الواجهة والجلب العادي عبر HTTP دون هذه الحزمة؛ أما الجلب الذي يحتاج إلى عرض الصفحة في متصفح، مثل توسيع قائمة فصول Galaxy، فيتطلبها.

**التشغيل من المصدر:** ثبّت Python 3.11 أو أحدث. افتح PowerShell داخل مجلد المشروع وشغّل:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
python run_gui.py
```

إذا منع PowerShell تفعيل البيئة، شغّل `.venv\Scripts\python.exe run_gui.py` باستخدام Python الموجود داخل البيئة. ينشئ التشغيل الأول قاعدة البيانات المحلية.

### 1.1 التحقق من التحديثات

يتحقق إصدار Windows المثبّت من وجود إصدار مستقر جديد بعد التشغيل. عند توفر تحديث، يعرض التطبيق ملاحظات الإصدار وحجم التنزيل، ولا يثبته إلا بعد اختيار **Download and Install**. ويمكنك التحقق يدوياً من **Help → Check for Updates**. يستخدم التطبيق حزمة ملفات متغيرة فقط إذا توفرت حزمة مناسبة لإصدارك، وإلا يستخدم المثبّت الكامل. يتحقق التطبيق من SHA-256 قبل التثبيت، ثم يُغلق ويُعاد تشغيله. لا تشمل التحديثات قاعدة البيانات أو ملفات EPUB أو إعدادات `.env`.

التحديثات متاحة لإصدارات Windows المثبّتة، وليست للتشغيل من المصدر.

### 2. إضافة مفتاح API

تحتاج إلى مزود واحد على الأقل. يتم إنشاء مفاتيح API من موقع المزود، وليس من داخل التطبيق. الروابط وأسماء الإعدادات موجودة في جدول قسم English أعلاه.

داخل التطبيق:

1. افتح تبويب **API Keys**.
2. اختر مزوداً واضغط **Edit**.
3. الصق المفتاح في حقل API Key. لا تضع المفتاح داخل اسم رواية أو المصطلحات أو رسائل المحادثة.
4. تأكد من اسم النموذج وBase URL. بالنسبة إلى Gemini استخدم النموذج `gemini-3.5-flash-lite`؛ تستخدم المكتبة الرسمية العنوان `https://generativelanguage.googleapis.com` داخلياً، لذلك لا تضف `GEMINI_BASE_URL` إلى ملف `.env`. يمكن تخصيص Base URL للمزودين المتوافقين مع OpenAI مثل TokenRouter وOrcaRouter.
5. اضغط **Save All**. يجب أن تظهر حالة المزود **Connected**.
6. اضغط **Test Provider** للتأكد من وجود المفتاح. هذا الاختبار يتحقق من الإعداد فقط، أما الترجمة الفعلية فتتأثر بالحصة والنموذج والإنترنت.

تُحفظ الإعدادات في ملف `.env` داخل مجلد المشروع. يخفي التطبيق المفاتيح، لكن الملف غير مشفر. لا ترفعه إلى GitHub ولا تشاركه. للإعداد اليدوي، انسخ `.env.example` إلى `.env` واستبدل القيم التجريبية بقيمك.

مثال إعداد Gemini:

```env
GEMINI_API_KEY=your-secret-key
GEMINI_MODEL=gemini-3.5-flash-lite
# لا تحتاج إلى GEMINI_BASE_URL؛ تستخدم مكتبة Google العنوان الرسمي داخلياً.
```

عند اختيار **Auto** يجرب التطبيق المزودين بالترتيب: TokenRouter ثم OrcaRouter ثم Gemini ثم Groq. للحصول على نتيجة متوقعة، اختر المزود يدوياً أو اضبط المزود المطلوب أولاً.

### 3. ترجمة رواية

1. من تبويب **Library** اضغط **Add Novel**، ثم الصق رابط موقع مدعوم أو اختر **Import EPUB** وحدد ملف EPUB.
2. استخدم **Search Sources** للبحث عن عنوان رواية لدى مواقع السحب المفعّلة وإضافة النتيجة إلى المكتبة. تظهر النتائج تدريجياً مع تقدم البحث واسم الموقع الحالي وعدد المواقع التي فشلت. تُجمع الروايات المتطابقة وتُعرض اللغة ومصادرها مع خيار إضافة كل مصدر، ويحافظ **Stop** على النتائج التي وصلت.
3. من **Settings** يمكنك تفعيل أو تعطيل مواقع السحب، والتصفية حسب اللغة أو التصنيف، وضبط خيارات البحث. تُحفظ الإعدادات في `data/source_preferences.json`.
4. للرواية من موقع، ابدأ جلب الفصول وانتظر ظهورها. يجلب التطبيق غلاف الرواية إلى المكتبة إذا كان الموقع يوفره. تُضاف فصول EPUB المستوردة إلى المكتبة لتكون جاهزة للترجمة.
5. اختر الفصول التي تريد ترجمتها.
6. اختر المزود والنموذج ثم ابدأ الترجمة.
7. راقب حالة كل فصل، وأعد محاولة الفصول الفاشلة قبل إنشاء الكتاب.

يقرأ استيراد EPUB نص الفصول وترتيبها من الكتاب. لا يغيّر الملف الأصلي؛ يعيد Novel Bridge AR إنشاء ملف EPUB المترجم، لذلك لا يحتفظ بتنسيق الكتاب الأصلي أو صوره. لا يمكن استيراد ملفات EPUB المحمية بـ DRM أو التالفة.

### 4. تثبيت ترجمة الأسماء والمصطلحات

افتح **Glossary**، وأضف المصطلح الأصلي وترجمته العربية المفضلة، ثم احفظ القاعدة. استخدم **Sync from JSON** لتحميل القواعد المشتركة من `config/glossary.json`. اختبر المصطلحات المهمة على فصل قصير أولاً.

### 5. إنشاء ملف EPUB

بعد ترجمة الفصول المطلوبة اضغط **Build EPUB**. تُحفظ الكتب داخل `output/` باسم الرواية. لا يتم تضمين الفصول التي فشلت ترجمتها.

### حل الأخطاء

- **لا يوجد مفتاح ترجمة:** افتح **API Keys**، أضف مفتاحاً واحداً على الأقل، اضغط **Save All**، ثم أعد الترجمة.
- **مفتاح غير صالح أو 401/403:** أنشئ مفتاحاً جديداً، تأكد من تفعيله وصلاحياته، ثم استبدله من **Edit** واضغط **Save All**.
- **تجاوز الحصة أو 429:** انتظر، خفّض عدد المهام المتزامنة، تحقق من حصة المزود، أو استخدم مزوداً آخر.
- **النموذج غير موجود:** راجع اسم النموذج وBase URL حرفياً، واحذف النماذج القديمة وأضف الاسم الحالي.
- **فشل جلب الموقع:** تأكد أن الرابط مدعوم ويفتح في المتصفح، ثم أعد المحاولة لاحقاً. لا تتجاوز أنظمة الحماية أو شروط الموقع.
- **بقاء نافذة المتصفح بعد الإغلاق:** يؤدي إغلاق التطبيق إلى إلغاء جلب nodriver وإرسال إشارة لإغلاق نافذة Chrome المؤقتة. تبقى الفصول التي اكتمل جلبها محفوظة.
- **التطبيق لا يفتح:** فعّل `.venv`، وثبّت المتطلبات، وثبّت Playwright، ثم شغّل `python run_gui.py` كما في قسم التثبيت.
- **فصول ناقصة في EPUB:** راجع حالات الفصول، أعد ترجمة الفاشل، ثم أنشئ EPUB مرة أخرى.

للتأكد من صحة المشروع شغّل `pytest` من مجلد المشروع. استخدم الأداة فقط للمحتوى والمواقع التي تملك حق الوصول إليها وترجمتها. يغطي ترخيص MIT كود المشروع فقط، بينما تخضع الخدمات والمواقع والمحتوى الخارجي لشروطها الخاصة.