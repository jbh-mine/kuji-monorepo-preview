"""Validate the browser-visible account configuration before publishing it."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def validate_account_config(value) -> bool:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or value.get("provider") != "supabase":
        raise ValueError("Invalid public account configuration")
    if set(value) != {"version", "provider", "url", "publishableKey"}:
        raise ValueError("Unexpected public account configuration fields")
    url, key = value.get("url"), value.get("publishableKey")
    if not isinstance(url, str) or not isinstance(key, str) or len(url) > 240 or len(key) > 4096:
        raise ValueError("Invalid public account configuration values")
    if url == key == "":
        return False
    if url != url.strip() or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("Invalid account service origin")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not re.fullmatch(r"[a-z0-9]{1,63}\.supabase\.co", parsed.netloc)
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Account service must use a native HTTPS Supabase origin")
    if re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]{8,240}", key):
        return True
    # Legacy anonymous keys are public too. Elevated keys must never be copied
    # into a browser bundle or a public repository, even during setup.
    try:
        parts = key.split(".")
        if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(payload, dict) or payload.get("role") != "anon":
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Only a publishable or legacy anonymous key is permitted") from None
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    path = args.root / "account-sync-config.json"
    if path.stat().st_size > 16384:
        raise ValueError("Public account configuration is too large")
    enabled = validate_account_config(json.loads(path.read_text(encoding="utf-8-sig")))
    print("Account sync configuration: " + ("configured" if enabled else "not connected; local records retained"))


if __name__ == "__main__":
    main()
