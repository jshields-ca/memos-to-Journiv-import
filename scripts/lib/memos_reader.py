"""
Reads memos from either a local JSON export file or live from the Memos REST API.

Memos API v1 returns a list of memo objects. Each memo has:
  name        - resource path, e.g. "memos/abc123"
  content     - Markdown text (may contain #hashtags)
  createTime  - RFC3339 timestamp string
  updateTime  - RFC3339 timestamp string
  tags        - list of tag strings extracted from content (output-only)
  state       - "NORMAL" | "ARCHIVED"
  visibility  - "PRIVATE" | "PROTECTED" | "PUBLIC"
  pinned      - bool
"""

import json
import sys
from typing import Iterator
from urllib.parse import urlencode

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


def load_from_file(path: str) -> list[dict]:
    """
    Load memos from a JSON export file.

    Handles two shapes:
      - {"memos": [...]}  (API list response)
      - [...]             (bare array, sometimes produced by built-in export)
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # API v1 list response wraps entries under "memos"
        if "memos" in data:
            return data["memos"]
        # Some export shapes use different keys
        for key in ("entries", "notes", "items"):
            if key in data:
                return data[key]
    raise ValueError(
        f"Unrecognised Memos export format in {path!r}. "
        "Expected a JSON array or an object with a 'memos' key."
    )


def load_from_api(base_url: str, token: str, page_size: int = 200) -> Iterator[dict]:
    """
    Fetch all memos from a live Memos instance via the REST API.

    Paginates automatically until all memos are retrieved.

    Args:
        base_url: Memos instance URL, e.g. "http://localhost:5230"
        token:    Bearer access token (create in Memos Settings → Account → Access Tokens)
        page_size: Number of memos per page (max 1000)
    """
    if not _HAS_REQUESTS:
        print("ERROR: 'requests' package is required for API mode. Run: pip install requests", file=sys.stderr)
        sys.exit(1)

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    page_token = None
    total = 0

    while True:
        params: dict = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token

        url = f"{base_url}/api/v1/memos?{urlencode(params)}"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()

        memos = body.get("memos", [])
        for memo in memos:
            yield memo
        total += len(memos)

        page_token = body.get("nextPageToken")
        if not page_token:
            break

    print(f"Fetched {total} memos from API.", file=sys.stderr)
