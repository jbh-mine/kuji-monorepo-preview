#!/usr/bin/env python3
"""Verify Korean direct-store releases against the official public X account.

WAKU is a discovery index only. Its dates, Japanese franchise links and prices
never become release evidence. A matching full edition title, an official
account embed, a sale announcement and its publication year must all agree.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

KST = timezone(timedelta(hours=9))
INDEX_URL = "https://wakuwaku.app/kr"
ACCOUNT = "ICHIBAN_KUJI_"
EMBED_HOSTS = frozenset({"publish.twitter.com", "publish.x.com"})
MAX_REQUESTS = 40
MAX_SECONDS = 120
MAX_BYTES = 1024 * 1024
FIELDS = ("koreaReleaseDate", "koreaReleaseDatePrecision", "koreaReleaseDateText",
          "koreaReleaseSourceUrl", "koreaReleaseCheckedAt")
UNKNOWN = dict(zip(FIELDS, ("", "unknown", "", "", "")))
TWEET_ID = r"[0-9]{16,22}"


class KoreaReleaseError(ValueError):
    """Only safe classification codes are exposed in public health reports."""


def canonical_tweet(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise KoreaReleaseError("invalid_official_source")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise KoreaReleaseError("invalid_official_source") from None
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    match = re.fullmatch(r"/ICHIBAN_KUJI_/status/(" + TWEET_ID + r")/?", parsed.path, re.I)
    if (parsed.scheme != "https" or parsed.hostname not in {"x.com", "twitter.com"}
            or not valid_port or parsed.username or parsed.password or not match):
        raise KoreaReleaseError("invalid_official_source")
    return f"https://x.com/{ACCOUNT}/status/{match.group(1)}"


def embed_url(tweet_id: str) -> str:
    if not re.fullmatch(TWEET_ID, tweet_id):
        raise KoreaReleaseError("invalid_tweet_id")
    # The official service now redirects Twitter embeds here. Start at the
    # observed canonical host so a normal check uses one request, not two.
    return "https://publish.x.com/oembed?" + urllib.parse.urlencode({
        "url": f"https://twitter.com/{ACCOUNT}/status/{tweet_id}", "omit_script": "true", "dnt": "true"})


def checked_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise KoreaReleaseError("disallowed_request_url")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise KoreaReleaseError("disallowed_request_url") from None
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not valid_port or parsed.username or parsed.password
            or parsed.fragment or urllib.parse.unquote(parsed.path) != parsed.path):
        raise KoreaReleaseError("disallowed_request_url")
    if parsed.hostname == "wakuwaku.app" and parsed.path in {"/kr", "/kr/"} and not parsed.query:
        return url
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.hostname in EMBED_HOSTS and parsed.path == "/oembed"
            and set(query) == {"url", "omit_script", "dnt"}
            and query["omit_script"] == ["true"] and query["dnt"] == ["true"]
            and len(query["url"]) == 1):
        canonical_tweet(query["url"][0])
        return url
    raise KoreaReleaseError("disallowed_request_url")


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, reserve=None):
        super().__init__()
        self.reserve = reserve

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl)
        old_host = urllib.parse.urlsplit(request.full_url).hostname
        new_host = urllib.parse.urlsplit(newurl).hostname
        if new_host != old_host and not {old_host, new_host} <= EMBED_HOSTS:
            raise KoreaReleaseError("cross_host_redirect")
        if self.reserve:
            self.reserve()
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, *, max_requests: int = MAX_REQUESTS, max_seconds: float = MAX_SECONDS):
        if not 0 < max_requests <= MAX_REQUESTS or not 0 < max_seconds <= MAX_SECONDS:
            raise KoreaReleaseError("invalid_request_budget")
        self.max_requests = max_requests
        self.requests = 0
        self.deadline = time.monotonic() + max_seconds
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirects(self.reserve))

    def reserve(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.requests >= self.max_requests or remaining <= 0:
            raise KoreaReleaseError("request_budget_exhausted")
        self.requests += 1
        return remaining

    def __call__(self, url: str) -> bytes:
        checked_url(url)
        remaining = self.reserve()
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json" if "oembed?" in url else "text/html"})
        try:
            with self.opener.open(request, timeout=min(6, remaining)) as response:
                checked_url(response.geturl())
                mime = response.headers.get_content_type()
                expected = {"application/json"} if "oembed?" in url else {"text/html", "application/xhtml+xml"}
                if mime not in expected:
                    raise KoreaReleaseError("unexpected_response_type")
                length = response.headers.get("Content-Length")
                if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                    raise KoreaReleaseError("response_size_limit")
                chunks, size = [], 0
                reader = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() >= self.deadline:
                        raise KoreaReleaseError("request_budget_exhausted")
                    chunk = reader(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise KoreaReleaseError("response_size_limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except urllib.error.HTTPError as error:
            raise KoreaReleaseError(f"http_{error.code}") from None
        except (OSError, urllib.error.URLError):
            raise KoreaReleaseError("network_error") from None


def title_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    prefixes = ("제일복권", "이치방쿠지", "一番くじ", "tv 애니메이션", "애니메이션")
    while True:
        found = next((prefix for prefix in prefixes if value.startswith(prefix)), None)
        if found is None:
            break
        value = value[len(found):].lstrip()
    return "".join(character for character in value if character.isalnum())


FRANCHISE_ONLY = frozenset(title_key(value) for value in (
    "귀멸의 칼날", "鬼滅の刃", "진격의 거인", "進撃の巨人", "체인소맨", "チェンソーマン",
    "드래곤볼", "ドラゴンボール", "원피스", "ワンピース", "ONE PIECE", "나루토", "NARUTO", "ナルト",
    "나루토 질풍전", "NARUTO 疾風伝", "주술회전", "呪術廻戦", "헌터×헌터", "HUNTER×HUNTER", "HUNTERxHUNTER",
    "나의 히어로 아카데미아", "僕のヒーローアカデミア", "데스노트", "DEATH NOTE", "블리치", "BLEACH",
    "하이큐", "ハイキュー", "죠죠의 기묘한 모험", "ジョジョの奇妙な冒険", "에반게리온", "エヴァンゲリオン",
    "ヱヴァンゲリヲン", "약사의 혼잣말", "薬屋のひとりごと", "페어리 테일", "FAIRY TAIL"))


def product_titles(product: dict) -> set[str]:
    values = [product.get("productTitleKo"), product.get("productTitle")]
    if not any(values):
        values = [product.get("nameKo"), product.get("nameJp")]
    return {title_key(value) for value in values if isinstance(value, str) and title_key(value)
            and title_key(value) not in FRANCHISE_ONLY}


def has_verified_saved_fields(product: dict) -> bool:
    value, precision = product.get("koreaReleaseDate"), product.get("koreaReleaseDatePrecision")
    if not isinstance(value, str) or precision not in {"day", "month"}:
        return False
    try:
        if precision == "day" and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        elif precision == "month" and re.fullmatch(r"20\d{2}-\d{2}", value):
            date.fromisoformat(value + "-01")
        else:
            return False
        canonical_tweet(product.get("koreaReleaseSourceUrl"))
        stamp = product.get("koreaReleaseCheckedAt")
        if not isinstance(stamp, str) or len(stamp) > 64 or datetime.fromisoformat(stamp).utcoffset() != timedelta(hours=9):
            return False
        phrase = product.get("koreaReleaseDateText")
        return isinstance(phrase, str) and 0 < len(phrase) <= 300
    except (ValueError, TypeError):
        return False


class ListingParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.candidates = []
        self.current = None
        self.title_depth = 0
        self.depth = 0
        self.nodes = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 30000 or self.depth > 100:
            raise KoreaReleaseError("discovery_parser_limit")
        if tag not in {"br", "img", "input", "meta", "link", "hr", "source"}:
            self.depth += 1
        attributes = dict(attrs)
        if tag == "a" and "kr-row" in (attributes.get("class") or "").split():
            url = urllib.parse.urljoin("https://wakuwaku.app/", attributes.get("href") or "")
            parsed = urllib.parse.urlsplit(url)
            match = re.fullmatch(r"/kr/(" + TWEET_ID + r")(?:\.html)?", parsed.path)
            if parsed.scheme == "https" and parsed.netloc == "wakuwaku.app" and match and not parsed.query and not parsed.fragment:
                self.current = {"tweetId": match.group(1), "parts": []}
        if self.current and "kr-title" in (attributes.get("class") or "").split():
            self.title_depth = self.depth

    def handle_endtag(self, tag):
        if self.title_depth == self.depth:
            self.title_depth = 0
        if tag == "a" and self.current:
            title = " ".join(self.current["parts"]).strip()
            if title and len(title) <= 300:
                self.candidates.append({"tweetId": self.current["tweetId"], "title": title})
            self.current = None
            self.title_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.current and self.title_depth:
            self.current["parts"].append(data)


class TweetParser(HTMLParser):
    def __init__(self, source: str, tweet_id: str):
        super().__init__(convert_charrefs=True)
        self.tweet_id = tweet_id
        self.in_blockquote = self.in_paragraph = self.in_footer = False
        self.body_parts, self.footer_parts = [], []
        self.nodes = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 5000:
            raise KoreaReleaseError("official_parser_limit")
        attributes = dict(attrs)
        if tag == "blockquote" and "twitter-tweet" in (attributes.get("class") or "").split():
            self.in_blockquote = True
        if self.in_blockquote and tag == "p":
            self.in_paragraph = True
        if self.in_paragraph and tag == "br":
            self.body_parts.append("\n")
        if self.in_blockquote and not self.in_paragraph and tag == "a":
            try:
                self.in_footer = canonical_tweet(attributes.get("href", "")).endswith("/" + self.tweet_id)
            except KoreaReleaseError:
                self.in_footer = False

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_paragraph = False
        if tag == "a":
            self.in_footer = False
        if tag == "blockquote":
            self.in_blockquote = False

    def handle_data(self, data):
        if self.in_paragraph:
            self.body_parts.append(data)
        elif self.in_footer:
            self.footer_parts.append(data)


def official_title(body: str) -> str | None:
    match = re.search(r"<\s*([^<>\n]{2,180})\s*>", body)
    if not match:
        return None
    parts = [match.group(1)]
    first, separator, remainder = body[match.end():].partition("\n")
    lines = ([first] if first.strip() else []) + (remainder.splitlines() if separator else [])
    for line in lines[:4]:
        line = line.strip()
        if not line or re.search(r"[💸📌📍]|[0-9,]+\s*원|판매|발매|구매|https?://", line):
            break
        parts.append(line)
    return " ".join(parts)


DAY_PHRASE = re.compile(r"(?:(?P<year>20\d{2})년\s*)?(?P<month>\d{1,2})월\s*(?P<day>\d{1,2})일\s*(?:\((?P<weekday>[월화수목금토일])(?:요일)?\))?\s*(?:부터\s*)?(?:순차(?:적으로)?\s*)?(?:판매\s*개시|판매\s*시작|발매)(?:\s*예정|합니다[!.]?|됩니다[!.]?)?")
MONTH_PHRASE = re.compile(r"(?:(?P<year>20\d{2})년\s*)?(?P<month>\d{1,2})월\s*(?:중|초|말|상순|중순|하순)?\s*(?:판매\s*개시|판매\s*시작|발매)(?:\s*예정)?")


def release_phrase(body: str, posted: date) -> tuple[str, str, str] | None:
    if re.search(r"연기|취소|일정\s*(?:변경|미정)", body):
        raise KoreaReleaseError("release_schedule_changed")
    matches = list(DAY_PHRASE.finditer(body))
    precision = "day"
    if not matches:
        matches = list(MONTH_PHRASE.finditer(body))
        precision = "month"
    if not matches:
        return None
    results = []
    for match in matches:
        month = int(match.group("month"))
        day = int(match.group("day")) if precision == "day" else 1
        explicit_year = match.group("year")
        years = [int(explicit_year)] if explicit_year else [posted.year - 1, posted.year, posted.year + 1]
        candidates = []
        for year in years:
            try:
                value = date(year, month, day)
            except ValueError:
                continue
            delta = (value - posted).days
            if not (-31 if precision == "month" else -14) <= delta <= 120:
                continue
            weekday = match.groupdict().get("weekday")
            if weekday and "월화수목금토일"[value.weekday()] != weekday:
                continue
            candidates.append(value)
        if len(candidates) != 1:
            raise KoreaReleaseError("unverified_release_calendar")
        value = candidates[0].isoformat()
        results.append((value if precision == "day" else value[:7], precision, match.group(0)))
    if len({(value, kind) for value, kind, phrase in results}) != 1:
        raise KoreaReleaseError("ambiguous_release_calendar")
    return results[0]


def parse_announcement(payload: dict, tweet_id: str, product: dict, *, now: datetime,
                       discovered_title: str | None = None) -> dict | None:
    if not isinstance(payload, dict):
        raise KoreaReleaseError("invalid_official_embed")
    author_url = payload.get("author_url", "")
    if not isinstance(author_url, str):
        raise KoreaReleaseError("unverified_official_author")
    try:
        author = urllib.parse.urlsplit(author_url)
    except ValueError:
        raise KoreaReleaseError("unverified_official_author") from None
    if (author.scheme != "https" or author.netloc not in {"x.com", "twitter.com"}
            or author.path.rstrip("/").lower() != "/ichiban_kuji_" or author.query or author.fragment):
        raise KoreaReleaseError("unverified_official_author")
    source = payload.get("html")
    if not isinstance(source, str) or len(source) > 128000:
        raise KoreaReleaseError("invalid_official_embed")
    document = TweetParser(source, tweet_id)
    body = "".join(document.body_parts)
    title = official_title(body)
    if not title or title_key(title) not in product_titles(product):
        return None
    if discovered_title is not None and title_key(discovered_title) != title_key(title):
        return None
    footer = "".join(document.footer_parts).strip()
    posted = None
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            posted = datetime.strptime(footer, pattern).date()
            break
        except ValueError:
            pass
    if posted is None or posted < date(2020, 1, 1) or posted > now.astimezone(KST).date() + timedelta(days=1):
        raise KoreaReleaseError("unverified_post_year")
    release = release_phrase(body, posted)
    if release is None:
        return None
    value, precision, phrase = release
    return {"koreaReleaseDate": value, "koreaReleaseDatePrecision": precision,
            "koreaReleaseDateText": ("이치방쿠지 직영점 공지: " + phrase)[:300],
            "koreaReleaseSourceUrl": canonical_tweet(f"https://x.com/{ACCOUNT}/status/{tweet_id}"),
            "koreaReleaseCheckedAt": now.astimezone(KST).isoformat(timespec="seconds")}


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(root: Path, *, fetcher=None, now: datetime | None = None) -> dict:
    root = Path(root)
    now = now or datetime.now(KST)
    if now.tzinfo is None or now.utcoffset() is None:
        raise KoreaReleaseError("timezone_required")
    catalog = json.loads((root / "official-catalog.json").read_text(encoding="utf-8"))
    status = json.loads((root / "sync-status.json").read_text(encoding="utf-8"))
    if not isinstance(catalog, list) or not catalog or len(catalog) > 200 or not all(isinstance(item, dict) for item in catalog) or not isinstance(status, dict):
        raise KoreaReleaseError("invalid_saved_catalog")
    catalog = copy.deepcopy(catalog)
    previous = status.get("korea") if isinstance(status.get("korea"), dict) else {}
    report = {"status": "ok", "checkedAt": now.astimezone(KST).isoformat(timespec="seconds"),
              "lastSuccessAt": previous.get("lastSuccessAt", ""), "confirmed": 0, "unconfirmed": 0,
              "checked": 0, "failed": 0, "errors": []}
    fetcher = fetcher or Fetcher()
    candidates = []
    def failure(stage: str, code: str, source: str = ""):
        report["failed"] += 1
        if len(report["errors"]) < MAX_REQUESTS:
            report["errors"].append({"stage": stage, "code": code, "sourceUrl": source})
    try:
        candidates = ListingParser(fetcher(INDEX_URL).decode("utf-8")).candidates
        if not candidates:
            raise KoreaReleaseError("discovery_unrecognized")
    except (KoreaReleaseError, UnicodeError, OSError) as error:
        failure("discovery", str(error) if isinstance(error, KoreaReleaseError) else "discovery_parse_error", INDEX_URL)
    cache = {}
    successes = 0
    title_owners = {}
    for index, product in enumerate(catalog):
        for title in product_titles(product):
            title_owners.setdefault(title, set()).add(index)
    for product in catalog:
        for field, value in UNKNOWN.items():
            product.setdefault(field, value)
        wanted = product_titles(product)
        selected = [candidate for candidate in candidates if title_key(candidate["title"]) in wanted
                    and len(title_owners[title_key(candidate["title"])]) == 1]
        saved_source = product.get("koreaReleaseSourceUrl", "")
        if saved_source:
            try:
                identifier = canonical_tweet(saved_source).rsplit("/", 1)[1]
                if not any(candidate["tweetId"] == identifier for candidate in selected):
                    selected.append({"tweetId": identifier, "title": None})
            except KoreaReleaseError:
                failure("source", "invalid_saved_source")
        selected = sorted({candidate["tweetId"]: candidate for candidate in selected}.values(), key=lambda candidate: int(candidate["tweetId"]), reverse=True)
        if selected:
            report["checked"] += 1
        for candidate in selected:
            identifier = candidate["tweetId"]
            source = canonical_tweet(f"https://x.com/{ACCOUNT}/status/{identifier}")
            try:
                if identifier not in cache:
                    try:
                        cache[identifier] = json.loads(fetcher(embed_url(identifier)).decode("utf-8"))
                    except (KoreaReleaseError, UnicodeError, ValueError, OSError) as error:
                        cache[identifier] = error
                if isinstance(cache[identifier], Exception):
                    error = cache[identifier]
                    raise KoreaReleaseError(str(error) if isinstance(error, KoreaReleaseError) else "official_parse_error")
                fields = parse_announcement(cache[identifier], identifier, product, now=now, discovered_title=candidate["title"])
                if fields:
                    product.update(fields)
                    successes += 1
                    break
            except KoreaReleaseError as error:
                failure("official", str(error), source)
        confirmed = has_verified_saved_fields(product)
        report["confirmed" if confirmed else "unconfirmed"] += 1
    if report["failed"]:
        report["status"] = "partial" if successes else "failed"
    else:
        report["lastSuccessAt"] = report["checkedAt"]
    old_country_failed = previous.get("failed", 0)
    base_failed = status.get("failed", 0)
    base_failed = max(0, base_failed - old_country_failed) if isinstance(base_failed, int) and isinstance(old_country_failed, int) else 0
    status["failed"] = base_failed + report["failed"]
    if status.get("status") == "ok" and report["failed"]:
        status["status"] = "partial"
    elif previous and status.get("status") == "partial" and not status["failed"]:
        status["status"] = "ok"
    status["korea"] = report
    atomic_json(root / "official-catalog.json", catalog)
    atomic_json(root / "sync-status.json", status)
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        status = run(args.root)
        print(json.dumps({"korea": {key: value for key, value in status["korea"].items() if key != "errors"}}, ensure_ascii=False))
        return 1 if status["korea"]["failed"] else 0
    except (KoreaReleaseError, OSError, ValueError):
        print("Korean release check could not read or save the catalog.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
