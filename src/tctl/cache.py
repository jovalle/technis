"""File mtime-based caching to skip redundant work."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tctl.config import CACHE_DIR


def _cache_file() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "cache.json"


def _load_store() -> dict[str, Any]:
    cf = _cache_file()
    if cf.exists():
        try:
            return json.loads(cf.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_store(store: dict[str, Any]) -> None:
    _cache_file().write_text(json.dumps(store, indent=2))


def _file_sig(path: Path) -> str | None:
    """Return mtime-based signature for a file, or None if missing."""
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return None


def cache_key(namespace: str, *watch_files: Path) -> str:
    """Build a cache key from a namespace and the mtimes of watched files."""
    parts = [namespace]
    for f in sorted(watch_files):
        sig = _file_sig(f) or "missing"
        parts.append(f"{f}={sig}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def get(key: str) -> Any | None:
    store = _load_store()
    return store.get(key)


def put(key: str, value: Any) -> None:
    store = _load_store()
    store[key] = value
    _save_store(store)


def invalidate_all() -> None:
    cf = _cache_file()
    if cf.exists():
        cf.unlink()
