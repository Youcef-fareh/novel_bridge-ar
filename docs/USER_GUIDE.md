# Novel Bridge AR User Guide / دليل استخدام Novel Bridge AR

This guide is bilingual. The English instructions come first, followed by the Arabic instructions.

## English

Novel Bridge AR downloads chapters from supported novel websites, translates them with an AI provider, and creates an EPUB book for offline reading.

### 1. Install and start

**Windows installer:** Run `NovelBridgeAR_Setup.exe`, then open NovelBridge AR from the Start Menu. The installer normally includes Python and the browser runtime, but an internet connection and a translation API key are still required.

**Run from source:** Install Python 3.11 or newer. In PowerShell, from the project folder, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
python run_gui.py
```

If activation is blocked, run `.venv\Scripts\python.exe run_gui.py` using the Python executable inside the environment. The first run creates the local database.

### 2. Add an API key

You need at least one provider. API keys are created on the provider's website, not inside Novel Bridge AR:

| Provider | Create a key | Main settings |
| --- | --- | --- |
| Google Gemini | https://aistudio.google.com/app/apikey | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| Groq | https://console.groq.com | `GROQ_API_KEY`, `GROQ_MODEL` |
| TokenRouter | https://tokenrouter.com | `TOKENROUTER_API_KEY`, `TOKENROUTER_MODEL`, `TOKENROUTER_BASE_URL` |
| OrcaRouter | https://orcarouter.ai | `ORCAROUTER_API_KEY`, `ORCAROUTER_MODEL`, `ORCAROUTER_BASE_URL` |

In the application:

1. Open **API Keys**.
2. Find a provider and click **Edit**.
3. Paste the key into the API Key field. Never paste a key into a novel, glossary, or chat message.
4. Confirm the model name and Base URL. For Gemini, use model `gemini-3.5-flash-lite`; the official SDK uses `https://generativelanguage.googleapis.com` internally, so do not add `GEMINI_BASE_URL` to `.env`. Base URL is configurable for OpenAI-compatible providers such as TokenRouter and OrcaRouter.
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

1. In **Library**, paste a supported novel URL.
2. Start the import/scrape operation and wait for the chapters to appear.
3. Select the chapters to translate.
4. Choose a provider and model, then start translation.
5. Watch each chapter's status. Retry failed chapters before building the book.

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
| Scrape/import fails | Confirm the URL is supported and opens in a browser. Retry later if the site is unavailable. Site selectors are in `config/sites.json`; do not bypass access controls. |
| Application does not start | Activate `.venv`, reinstall with `python -m pip install -r requirements.txt`, install the browser with `python -m playwright install`, then run `python run_gui.py`. |
| EPUB is missing chapters | Check chapter statuses, retry failed chapters, and build the EPUB again. |
| Google Drive OAuth file not found | Download a Desktop OAuth client JSON, place it at `config/google_client_secret.json`, or configure `GOOGLE_DRIVE_CREDENTIALS_FILE`. |
| Google Drive sign-in or upload fails | Confirm the Drive API is enabled, finish the browser consent flow, and check your internet connection. |

For development checks, run `pytest` from the project root. Use the tool only for novels and websites you are authorized to access and translate. Provider, website, and third-party content terms still apply; the MIT license covers this project's source code only.

## العربية

يقوم Novel Bridge AR بتنزيل فصول الروايات من المواقع المدعومة، وترجمتها باستخدام مزود ذكاء اصطناعي، ثم إنشاء كتاب EPUB للقراءة دون اتصال.

### 1. التثبيت والتشغيل

**مثبت Windows:** شغّل الملف `NovelBridgeAR_Setup.exe`، ثم افتح Novel Bridge AR من قائمة Start. يحتوي المثبت عادةً على Python وبيئة المتصفح، لكن يلزم اتصال بالإنترنت ومفتاح API للترجمة.

**التشغيل من المصدر:** ثبّت Python 3.11 أو أحدث. افتح PowerShell داخل مجلد المشروع وشغّل:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
python run_gui.py
```

إذا منع PowerShell تفعيل البيئة، شغّل `.venv\Scripts\python.exe run_gui.py` باستخدام Python الموجود داخل البيئة. ينشئ التشغيل الأول قاعدة البيانات المحلية.

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

1. من تبويب **Library** الصق رابط رواية مدعوم.
2. شغّل الاستيراد/الجلب وانتظر ظهور الفصول.
3. اختر الفصول التي تريد ترجمتها.
4. اختر المزود والنموذج ثم ابدأ الترجمة.
5. راقب حالة كل فصل، وأعد محاولة الفصول الفاشلة قبل إنشاء الكتاب.

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
- **التطبيق لا يفتح:** فعّل `.venv`، وثبّت المتطلبات، وثبّت Playwright، ثم شغّل `python run_gui.py` كما في قسم التثبيت.
- **فصول ناقصة في EPUB:** راجع حالات الفصول، أعد ترجمة الفاشل، ثم أنشئ EPUB مرة أخرى.

للتأكد من صحة المشروع شغّل `pytest` من مجلد المشروع. استخدم الأداة فقط للمحتوى والمواقع التي تملك حق الوصول إليها وترجمتها. يغطي ترخيص MIT كود المشروع فقط، بينما تخضع الخدمات والمواقع والمحتوى الخارجي لشروطها الخاصة.