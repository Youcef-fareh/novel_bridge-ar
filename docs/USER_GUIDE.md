# Novel Bridge AR User Guide

Novel Bridge AR downloads chapters from supported web novel sources, translates them with a configured language model, and creates an EPUB file for offline reading.

## Before You Start

- Install Python 3.11 or newer.
- Make sure you have permission to access and translate the novels you use.
- Create an API key for at least one supported translation provider.
- Keep API keys private. Do not commit your `.env` file or share screenshots containing keys.

If you downloaded a Windows installer from a release that includes Playwright, you do not need to install Python, the packages in `requirements.txt`, or Chromium separately. The installer is built with PyInstaller and includes the application runtime and browser. Older installers may not include Chromium; update to the latest release if a browser-based site adapter reports a missing browser.

## Installation

### Windows Installer

Run the `NovelBridgeAR_Setup.exe` file and launch NovelBridge AR from the Start Menu or desktop shortcut. You still need an internet connection and an API key for translation providers.

### Run from Source

Open PowerShell in the project folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
```

If PowerShell blocks activation, run the application with the Python executable inside `.venv` directly or adjust your local execution-policy settings.

## Start the Application

Run:

```powershell
python run_gui.py
```

The application opens as a desktop window. The first run creates the local database and uses the project folders for configuration and generated files.

## Configure Translation

1. Open the **API Keys** tab.
2. Enter an API key for one or more providers.
3. Set the matching model name when a provider requires one.
4. Click **Save**.

Supported provider settings include:

- Gemini: `GEMINI_API_KEY`, `GEMINI_MODEL`
- Groq: `GROQ_API_KEY`, `GROQ_MODEL`
- TokenRouter: `TOKENROUTER_API_KEY`, `TOKENROUTER_MODEL`, `TOKENROUTER_BASE_URL`
- OrcaRouter: `ORCAROUTER_API_KEY`, `ORCAROUTER_MODEL`, `ORCAROUTER_BASE_URL`

The settings are stored in `.env` in the project root. The application masks secret values in the interface, but the file itself is not encrypted.

## Translate a Novel

1. Paste a supported novel URL into the novel input field.
2. Start the scrape/import operation and wait for the novel and chapter list to load.
3. Review the chapter list and select the chapters to process.
4. Choose the translation provider and start translation.
5. Monitor chapter status and review any errors shown by the application.

Supported site configuration is stored in `config/sites.json`. Sites can change their layouts or access rules, so scraping may stop working until an adapter or selector is updated.

## Use the Glossary

The glossary keeps names, terms, and phrases consistent during translation.

1. Open the **Glossary** tab.
2. Add a source term and its preferred Arabic translation.
3. Save the rule.
4. Sync from `config/glossary.json` when you want to load the shared project glossary.

Use exact, clear rules for names and recurring terminology. Test important terms in a short chapter before processing a large novel.

## Build and Find the EPUB

After the required chapters have been translated, choose **Build EPUB**. Generated books are written to the `output/` folder using the novel title as the filename. The EPUB is intended for use with an external ebook reader.

## Troubleshooting

### The application does not start

Confirm that the virtual environment is active and dependencies are installed:

```powershell
python -m pip install -r requirements.txt
python run_gui.py
```

### Translation fails

Check the API key, model name, provider quota, internet connection, and the error shown in the application. Try another configured provider if the current provider is unavailable or rate-limited.

### Scraping fails

Verify that the URL belongs to a configured site and that the source is reachable in a browser. Do not bypass access controls or violate a website's terms. Site adapters and selectors are maintained in `backend/adapters/` and `config/sites.json`.

### The EPUB is missing chapters

Only successfully translated chapters can be included. Check chapter statuses, retry failed chapters, and build the EPUB again.

## Development Checks

Run the smoke tests from the project root:

```powershell
pytest
```

## Legal and Responsible Use

The MIT license applies to this project's source code only. It does not grant rights to third-party novels, websites, translations, fonts, models, or API services. Follow copyright law, provider terms, website terms, and applicable rate limits. Use the tool for content you are authorized to access and translate.