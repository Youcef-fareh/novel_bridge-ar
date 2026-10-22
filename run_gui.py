"""
NovelBridge — Entry point.
Run: python run_gui.py
"""
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on the Python path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_environment_file() -> Path:
    """Create first-run settings without overwriting an existing user file."""
    app_dir = application_dir()
    env_file = app_dir / ".env"
    template = app_dir / ".env.example"
    if not env_file.exists() and template.exists():
        try:
            env_file.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        except PermissionError:
            user_dir = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
            env_file = user_dir / "NovelBridge" / ".env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            if not env_file.exists():
                env_file.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            logging.getLogger("novelbridge.startup").warning(
                "Application directory is not writable; using per-user environment file %s",
                env_file,
            )
    return env_file





load_dotenv(dotenv_path=ensure_environment_file())

from backend.logging_setup import setup_logging

setup_logging()

from gui.app import run_app

if __name__ == "__main__":
    run_app()
