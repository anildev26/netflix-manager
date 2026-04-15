import sys
import os

# Add parent directory to path so server.py can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import app  # noqa: F401 — Vercel needs this name
