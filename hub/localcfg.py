"""
hub/localcfg.py — optional machine-local personal config (gitignored).

Keeps identifying values (keyring account name, email addresses, personal
paths) OUT of the public repo. Real values live in hub/.hub_local.json
(gitignored) or environment variables; the committed hub_local.example.json
shows the shape. Every lookup falls back to a generic placeholder, so a fresh
clone runs without any local config.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent / ".hub_local.json"


def load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)
