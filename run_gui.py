"""
NovelBridge — Entry point.
Run: python run_gui.py
"""
import sys
from pathlib import Path

# Ensure project root is on the Python path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from gui.app import run_app

if __name__ == "__main__":
    run_app()
