"""Minimal .env loader (stdlib only — no python-dotenv dependency).

Loads KEY=VALUE lines from a .env file sitting next to this module into
os.environ, WITHOUT overriding variables already set in the real environment
(real env wins, so `LLM_PROVIDER=foo python3 server.py` still overrides .env).
Lets you keep a personal config (e.g. LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY)
without re-exporting it every shell session. The entry points (server.py,
career_agent.py) call load() before reading provider settings.
"""

import os
from pathlib import Path


def load(path=None):
    path = Path(path) if path else Path(__file__).with_name(".env")
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]  # strip optional surrounding quotes
        if key:
            os.environ.setdefault(key, val)  # real environment wins
