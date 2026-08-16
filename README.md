# Novel Bridge AR

A Python-based app for fetching, translating, and packaging novels from supported web sources into EPUB files.

## Features

- Scrape chapters from supported novel sites
- Normalize source content for processing
- Translate chapters using configurable providers
- Build EPUB output files
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

## Notes

- Some sites require provider API keys for translation services.
- Update configuration files in `config/` to match your target sources and glossary.
- Output EPUBs are written under the `output/` directory.

## License

This project is for local use and experimentation unless specified otherwise by the repository owner.
