"""
NovelBridge — Entry point.
Run: python run_gui.py
"""
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
        env_file.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return env_file


load_dotenv(dotenv_path=ensure_environment_file())

from gui.app import run_app

if __name__ == "__main__":
    run_app()
