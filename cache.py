"""File-based caching for enrichment results to avoid redundant API calls."""

import json
import os
import time

from config import CACHE_TTL_DAYS
from logger import log

CACHE_DIR = ".leadgen_cache"


def _cache_path(cache_type: str, key: str) -> str:
    """Return the file path for a cache entry."""
    subdir = os.path.join(CACHE_DIR, cache_type)
    os.makedirs(subdir, exist_ok=True)
    # Sanitize key for filesystem
    safe_key = key.replace("/", "_").replace("\\", "_")
    return os.path.join(subdir, f"{safe_key}.json")


def get_cached(key: str, cache_type: str) -> dict | None:
    """Retrieve a cached result, or None if missing/expired."""
    path = _cache_path(cache_type, key)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.debug("Cache read error for %s/%s: %s", cache_type, key, e)
        return None

    # Check TTL
    cached_at = entry.get("_cached_at", 0)
    age_days = (time.time() - cached_at) / 86400
    if age_days > CACHE_TTL_DAYS:
        log.debug("Cache expired for %s/%s (%.1f days old)", cache_type, key, age_days)
        return None

    log.debug("Cache hit for %s/%s", cache_type, key)
    return entry.get("data")


def set_cached(key: str, cache_type: str, data: dict):
    """Write a result to cache."""
    path = _cache_path(cache_type, key)
    entry = {
        "_cached_at": time.time(),
        "data": data,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        log.debug("Cached %s/%s", cache_type, key)
    except OSError as e:
        log.warning("Cache write error for %s/%s: %s", cache_type, key, e)


def clear_cache():
    """Delete all cached files."""
    import shutil
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        log.info("Cache cleared")
