"""Put the project root on sys.path so `import music_bot` works however pytest is
invoked — a bare `pytest` doesn't add the cwd, only `python -m pytest` does.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
