"""conftest.py — makes the backend root importable when pytest runs from tests/."""
import sys
import os

# Add the backend root directory to sys.path so `from main import app` works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
