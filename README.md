# Novel Bridge AR

A Python-based app for fetching, translating, and packaging novels from supported web sources into EPUB files.

## Features

- Scrape chapters from supported novel sites
- Normalize source content for processing
- Translate chapters using configurable providers
- Build EPUB output files
- Save all generated EPUBs to Google Drive
- Manage glossary entries and novel metadata
- Desktop GUI for managing novels and chapters

## Project structure

- `backend/` – scraping, pipeline, database, translation logic
- `gui/` – desktop application UI
- `config/` – site and glossary configuration files
- `data/` – sample data and downloaded HTML examples
- `output/` – generated EPUB files
- `tests/` – smoke tests

## Requirements

Install the Python dependencies from the project root:

```bash
pip install -r requirements.txt
```

## Run the app

```bash
python run_gui.py
```

## Run tests

```bash
pytest
```

## Google Drive export

The Library detail view includes **Save All to Google Drive**. To enable it:

1. Enable the Google Drive API in a Google Cloud project.
2. Create OAuth credentials for a Desktop app and download the JSON file.
3. Place it at `config/google_client_secret.json`, or set `GOOGLE_DRIVE_CREDENTIALS_FILE` in `.env`.
4. Click **Save All to Google Drive** and complete the Google sign-in once.

The app stores the refresh token in the user's `.novelbridge` folder and uploads EPUBs to a `NovelBridge AR` folder. It uses the restricted `drive.file` scope, so it can manage files created by this app without requesting access to the user's entire Drive.

## Notes

- Some sites require provider API keys for translation services.
- Update configuration files in `config/` to match your target sources and glossary.
- Output EPUBs are written under the `output/` directory.
- See the [User Guide](docs/USER_GUIDE.md) for installation, configuration, translation, and troubleshooting steps.

## License

This project is released under the [MIT License](LICENSE). The license applies to the source code only; third-party content and services remain subject to their own terms.
