#!/usr/bin/env python3
"""Verify Korean official and named retailer dates against public primary posts.

WAKU is a discovery index only. Its dates, Japanese franchise links and prices
never become release evidence. A matching full edition title, an official
account embed, a sale announcement and its publication year must all agree.
Curated sources bind localized edition aliases to one Japanese product URL.
Retailer calendars/opening notices retain their shop and arrival/release scope;
neither a registration date nor a bare arrival post becomes a release date.
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
OFFICIAL_ACCOUNTS = frozenset({ACCOUNT, "ICHIBAN_KUJI_JS"})
REGISTRY_PATH = Path(__file__).with_name("korea_release_sources.json")
EMBED_HOSTS = frozenset({"publish.twitter.com", "publish.x.com"})
MAX_REQUESTS = 40
MAX_SECONDS = 120
MAX_BYTES = 1024 * 1024
MAX_INSTAGRAM_BYTES = 2 * 1024 * 1024
FIELDS = ("koreaReleaseDate", "koreaReleaseDatePrecision", "koreaReleaseDateText",
          "koreaReleaseSourceUrl", "koreaReleaseCheckedAt", "koreaReleaseSourceType",
          "koreaReleaseSourceName", "koreaReleaseDateKind")
UNKNOWN = dict(zip(FIELDS, ("", "unknown", "", "", "", "official", "이치방쿠지 공식 직영점", "release")))
TWEET_ID = r"[0-9]{16,22}"


class KoreaReleaseError(ValueError):
    """Only safe classification codes are exposed in public health reports."""


def canonical_tweet(url: str, allowed_accounts=None) -> str:
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
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,30})/status/(" + TWEET_ID + r")/?", parsed.path)
    accounts = {value.lower(): value for value in (allowed_accounts if allowed_accounts is not None else {ACCOUNT})}
    if (parsed.scheme != "https" or parsed.hostname not in {"x.com", "twitter.com"}
            or not valid_port or parsed.username or parsed.password or not match
            or match.group(1).lower() not in accounts):
        raise KoreaReleaseError("invalid_official_source")
    return f"https://x.com/{accounts[match.group(1).lower()]}/status/{match.group(2)}"


def source_account(source: str) -> str:
    if not isinstance(source, str) or len(source) > 300:
        raise KoreaReleaseError("invalid_registry_source")
    match = re.fullmatch(r"https://(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/status/" + TWEET_ID, source or "")
    if not match:
        raise KoreaReleaseError("invalid_registry_source")
    return match.group(1)


def instagram_source(source: str) -> bool:
    return isinstance(source, str) and bool(re.fullmatch(r"https://www\.instagram\.com/p/[A-Za-z0-9_-]{5,50}/", source))


def validate_sources(sources) -> list[dict]:
    if not isinstance(sources, list) or len(sources) > 200:
        raise KoreaReleaseError("invalid_source_registry")
    checked, seen = [], set()
    for raw in sources:
        if not isinstance(raw, dict):
            raise KoreaReleaseError("invalid_source_registry")
        product_url, source = raw.get("productSourceUrl"), raw.get("sourceUrl")
        if not isinstance(product_url, str) or not re.fullmatch(r"https://1kuji\.com/products/[A-Za-z0-9_-]{1,100}", product_url):
            raise KoreaReleaseError("invalid_registry_product")
        instagram = instagram_source(source)
        if instagram:
            account = raw.get("sourceAccount")
            if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", account):
                raise KoreaReleaseError("invalid_registry_account")
            canonical = source
        else:
            account = source_account(source)
            canonical = canonical_tweet(source, {account})
        kind, name, date_kind = raw.get("sourceType"), raw.get("sourceName"), raw.get("dateKind")
        if kind not in {"official", "retailer"} or date_kind not in {"release", "arrival"}:
            raise KoreaReleaseError("invalid_registry_metadata")
        if kind == "official" and (instagram or account.lower() not in {value.lower() for value in OFFICIAL_ACCOUNTS}):
            raise KoreaReleaseError("invalid_official_registry_account")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or re.search(r"[\x00-\x1f]", name):
            raise KoreaReleaseError("invalid_registry_metadata")
        aliases = raw.get("aliases")
        if not isinstance(aliases, list) or not 1 <= len(aliases) <= 10 or any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in aliases):
            raise KoreaReleaseError("invalid_registry_aliases")
        key = (product_url, canonical)
        if key in seen:
            raise KoreaReleaseError("duplicate_registry_source")
        seen.add(key)
        checked.append({"productSourceUrl": product_url, "sourceUrl": canonical, "aliases": aliases,
                        "sourceType": kind, "sourceName": name, "dateKind": date_kind, "sourceAccount": account})
    return checked


def load_sources(path: Path = REGISTRY_PATH) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise KoreaReleaseError("unavailable_source_registry") from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise KoreaReleaseError("invalid_source_registry")
    return validate_sources(payload.get("sources"))


def approved_accounts(sources=None) -> set[str]:
    sources = load_sources() if sources is None else sources
    return set(OFFICIAL_ACCOUNTS) | {source_account(item["sourceUrl"]) for item in sources if not instagram_source(item["sourceUrl"])}


def embed_url(tweet_id: str, account: str = ACCOUNT) -> str:
    if not re.fullmatch(TWEET_ID, tweet_id) or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", account):
        raise KoreaReleaseError("invalid_tweet_id")
    # The official service now redirects Twitter embeds here. Start at the
    # observed canonical host so a normal check uses one request, not two.
    return "https://publish.x.com/oembed?" + urllib.parse.urlencode({
        "url": f"https://twitter.com/{account}/status/{tweet_id}", "omit_script": "true", "dnt": "true"})


def checked_url(url: str, accounts=None, registered_urls=None) -> str:
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
    registered_urls = {entry["sourceUrl"] for entry in load_sources()} if registered_urls is None else registered_urls
    if instagram_source(url) and url in registered_urls:
        return url
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.hostname in EMBED_HOSTS and parsed.path == "/oembed"
            and set(query) == {"url", "omit_script", "dnt"}
            and query["omit_script"] == ["true"] and query["dnt"] == ["true"]
            and len(query["url"]) == 1):
        canonical_tweet(query["url"][0], approved_accounts() if accounts is None else accounts)
        return url
    raise KoreaReleaseError("disallowed_request_url")


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, reserve=None, accounts=None, registered_urls=None):
        super().__init__()
        self.reserve = reserve
        self.accounts = accounts
        self.registered_urls = registered_urls

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl, self.accounts, self.registered_urls)
        old_host = urllib.parse.urlsplit(request.full_url).hostname
        new_host = urllib.parse.urlsplit(newurl).hostname
        if new_host != old_host and not {old_host, new_host} <= EMBED_HOSTS:
            raise KoreaReleaseError("cross_host_redirect")
        if self.reserve:
            self.reserve()
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, *, max_requests: int = MAX_REQUESTS, max_seconds: float = MAX_SECONDS, accounts=None, registered_urls=None):
        if not 0 < max_requests <= MAX_REQUESTS or not 0 < max_seconds <= MAX_SECONDS:
            raise KoreaReleaseError("invalid_request_budget")
        self.max_requests = max_requests
        self.requests = 0
        self.deadline = time.monotonic() + max_seconds
        self.accounts = approved_accounts() if accounts is None else accounts
        self.registered_urls = {entry["sourceUrl"] for entry in load_sources()} if registered_urls is None else registered_urls
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirects(self.reserve, self.accounts, self.registered_urls))

    def reserve(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.requests >= self.max_requests or remaining <= 0:
            raise KoreaReleaseError("request_budget_exhausted")
        self.requests += 1
        return remaining

    def __call__(self, url: str) -> bytes:
        checked_url(url, self.accounts, self.registered_urls)
        remaining = self.reserve()
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json" if "oembed?" in url else "text/html"})
        try:
            with self.opener.open(request, timeout=min(6, remaining)) as response:
                checked_url(response.geturl(), self.accounts, self.registered_urls)
                mime = response.headers.get_content_type()
                expected = {"application/json"} if "oembed?" in url else {"text/html", "application/xhtml+xml"}
                if mime not in expected:
                    raise KoreaReleaseError("unexpected_response_type")
                limit = MAX_INSTAGRAM_BYTES if instagram_source(url) else MAX_BYTES
                length = response.headers.get("Content-Length")
                if length and (not length.isdecimal() or int(length) > limit):
                    raise KoreaReleaseError("response_size_limit")
                chunks, size = [], 0
                reader = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() >= self.deadline:
                        raise KoreaReleaseError("request_budget_exhausted")
                    chunk = reader(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
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


def product_titles(product: dict, sources=()) -> set[str]:
    values = [product.get("productTitleKo"), product.get("productTitle")]
    if not any(values):
        values = [product.get("nameKo"), product.get("nameJp")]
    titles = {title_key(value) for value in values if isinstance(value, str) and title_key(value)
              and title_key(value) not in FRANCHISE_ONLY}
    for entry in sources:
        if entry["productSourceUrl"] == product.get("sourceUrl"):
            titles.update(title_key(value) for value in entry["aliases"])
    return titles


def has_verified_saved_fields(product: dict, sources=None) -> bool:
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
        sources = load_sources() if sources is None else sources
        source_type = product.get("koreaReleaseSourceType", "official")
        source_name = product.get("koreaReleaseSourceName", "이치방쿠지 공식 직영점")
        date_kind = product.get("koreaReleaseDateKind", "release")
        if source_type not in {"official", "retailer"} or date_kind not in {"release", "arrival"}:
            return False
        if not isinstance(source_name, str) or not source_name.strip() or len(source_name) > 80:
            return False
        if source_type == "official":
            canonical_tweet(product.get("koreaReleaseSourceUrl"), OFFICIAL_ACCOUNTS)
        else:
            registered = [entry for entry in sources if entry["productSourceUrl"] == product.get("sourceUrl")
                          and entry["sourceUrl"] == product.get("koreaReleaseSourceUrl")
                          and entry["sourceType"] == source_type and entry["sourceName"] == source_name
                          and entry["dateKind"] == date_kind]
            if not registered:
                return False
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
    def __init__(self, source: str, tweet_id: str, account: str = ACCOUNT):
        super().__init__(convert_charrefs=True)
        self.tweet_id = tweet_id
        self.account = account
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
                self.in_footer = canonical_tweet(attributes.get("href", ""), {self.account}).endswith("/" + self.tweet_id)
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
DATE_START = r"(?:(?P<year>20\d{2})[년./-]\s*)?(?P<month>\d{1,2})(?:월\s*|[./])(?P<day>\d{1,2})일?\s*(?:\((?P<weekday>[월화수목금토일])(?:요일)?\)|(?P<weekday_word>[월화수목금토일])요일)?"
OPEN_PHRASE = re.compile(DATE_START + r"\s*(?:부터\s*)?(?:\d{1,2}(?:시|:)\s*\d{0,2}분?\s*(?:에|부터)?\s*)?(?:판매\s*(?:개시|시작)|발매|오픈)(?:\s*예정|합니다[!.]?|됩니다[!.]?)?")
ARRIVAL_PHRASE = re.compile(DATE_START + r"[^\n]{0,40}?입고(?:\s*(?:완료|예정|안내|되었습니다|됩니다))?")


def release_phrase(body: str, posted: date, date_kind: str = "release") -> tuple[str, str, str] | None:
    if re.search(r"연기|취소|일정\s*(?:변경|미정)", body):
        raise KoreaReleaseError("release_schedule_changed")
    if date_kind == "arrival" and "재입고" in body:
        raise KoreaReleaseError("restock_is_not_first_release")
    matches = list(ARRIVAL_PHRASE.finditer(body)) if date_kind == "arrival" else list(DAY_PHRASE.finditer(body)) + list(OPEN_PHRASE.finditer(body))
    precision = "day"
    if not matches and date_kind == "release":
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
            weekday = match.groupdict().get("weekday") or match.groupdict().get("weekday_word")
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
                       discovered_title: str | None = None, source_spec: dict | None = None,
                       sources=()) -> dict | None:
    if source_spec is not None:
        source_spec = validate_sources([source_spec])[0]
        if source_spec["productSourceUrl"] != product.get("sourceUrl"):
            return None
        account = source_account(source_spec["sourceUrl"])
        if not source_spec["sourceUrl"].endswith("/" + tweet_id):
            raise KoreaReleaseError("unverified_source_identifier")
        source_type, source_name, date_kind = (source_spec[key] for key in ("sourceType", "sourceName", "dateKind"))
    else:
        account, source_type, source_name, date_kind = ACCOUNT, "official", "이치방쿠지 공식 직영점", "release"
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
            or author.path.rstrip("/").lower() != "/" + account.lower() or author.query or author.fragment):
        raise KoreaReleaseError("unverified_official_author")
    source = payload.get("html")
    if not isinstance(source, str) or len(source) > 128000:
        raise KoreaReleaseError("invalid_official_embed")
    if payload.get("url") is not None and canonical_tweet(payload["url"], {account}) != f"https://x.com/{account}/status/{tweet_id}":
        raise KoreaReleaseError("unverified_source_identifier")
    document = TweetParser(source, tweet_id, account)
    body = "".join(document.body_parts)
    title = official_title(body)
    wanted = product_titles(product, sources)
    if source_spec is not None:
        wanted.update(title_key(value) for value in source_spec["aliases"])
    if source_spec is not None and source_type == "retailer":
        # A curated, SKU-bound full name may occur inside a multi-item store
        # notice, but a franchise-only keyword never establishes identity.
        aliases = {title_key(value) for value in source_spec["aliases"] if title_key(value) not in FRANCHISE_ONLY}
        if not aliases or not any(value in title_key(body) for value in aliases):
            return None
    elif not title or title_key(title) not in wanted:
        return None
    if discovered_title is not None and title_key(discovered_title) not in wanted:
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
    release = release_phrase(body, posted, date_kind)
    if release is None:
        return None
    value, precision, phrase = release
    return {"koreaReleaseDate": value, "koreaReleaseDatePrecision": precision,
            "koreaReleaseDateText": (source_name + " 공지: " + phrase)[:300],
            "koreaReleaseSourceUrl": canonical_tweet(f"https://x.com/{account}/status/{tweet_id}", {account}),
            "koreaReleaseCheckedAt": now.astimezone(KST).isoformat(timespec="seconds"),
            "koreaReleaseSourceType": source_type, "koreaReleaseSourceName": source_name,
            "koreaReleaseDateKind": date_kind}


class InstagramMetadata(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.metadata, self.nodes, self.in_head = {}, 0, True
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 30000:
            raise KoreaReleaseError("instagram_parser_limit")
        if tag == "body":
            self.in_head = False
        if self.in_head and tag == "meta":
            attributes = dict(attrs)
            key = attributes.get("property", attributes.get("name", ""))
            if key in {"og:description", "og:title"}:
                value = attributes.get("content", "")
                if len(value) > 128000 or (key in self.metadata and self.metadata[key] != value):
                    raise KoreaReleaseError("invalid_instagram_metadata")
                self.metadata[key] = value

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


INSTAGRAM_HEADER = re.compile(r"^(?:[0-9.,KMBkmb]+ likes?, [0-9.,KMBkmb]+ comments? - )?(?P<account>[A-Za-z0-9_.]{1,30}) on (?P<posted>[A-Za-z]+ \d{1,2}, 20\d{2}):\s*[\"“]")
DAY_ONLY = re.compile(r"(?<![0-9])(?P<day>\d{1,2})일\s*\((?P<weekday>[월화수목금토일])(?:요일)?\)")
SALES_CHANGE = re.compile(
    r"(?:판매|발매|출시|오픈)\s*(?:일정|일자|시작일|예정일|일시|일)?\s*(?:이|가|은|는|을|를)?\s*"
    r"(?:연기|보류|미정|취소(?!\s*(?:불가|불가능))|변경(?!\s*(?:될\s*수|될\s*가능|가능|할\s*수|되지)))")


def day_only_sale(block: str, posted: date) -> tuple[str, str, str] | None:
    matches = list(DAY_ONLY.finditer(block))
    if len(matches) > 1:
        raise KoreaReleaseError("ambiguous_release_calendar")
    if not matches:
        return None
    match = matches[0]
    candidates = []
    ordinal = posted.year * 12 + posted.month - 1
    for delta in (-1, 0, 1):
        year, zero_month = divmod(ordinal + delta, 12)
        try:
            candidate = date(year, zero_month + 1, int(match.group("day")))
        except ValueError:
            continue
        if -7 <= (candidate - posted).days <= 31 and "월화수목금토일"[candidate.weekday()] == match.group("weekday"):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise KoreaReleaseError("unverified_release_calendar")
    return candidates[0].isoformat(), "day", " ".join(block.split())


def parse_instagram(source: str, product: dict, *, now: datetime, source_spec: dict) -> dict | None:
    spec = validate_sources([source_spec])[0]
    if not instagram_source(spec["sourceUrl"]) or spec["sourceType"] != "retailer":
        raise KoreaReleaseError("unapproved_instagram_source")
    if spec["productSourceUrl"] != product.get("sourceUrl"):
        return None
    description = InstagramMetadata(source).metadata.get("og:description", "")
    header = INSTAGRAM_HEADER.match(description)
    if not header or header.group("account").lower() != spec["sourceAccount"].lower():
        raise KoreaReleaseError("unverified_instagram_author")
    posted = None
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            posted = datetime.strptime(header.group("posted"), pattern).date()
            break
        except ValueError:
            pass
    if posted is None or posted < date(2020, 1, 1) or posted > now.astimezone(KST).date() + timedelta(days=1):
        raise KoreaReleaseError("unverified_post_year")
    body = description[header.end():]
    aliases = {title_key(value) for value in spec["aliases"] if title_key(value) not in FRANCHISE_ONLY}
    if not aliases or not any(alias in title_key(body) for alias in aliases):
        return None
    release = None
    evidence = ""
    if spec["dateKind"] == "arrival" and re.search(r"일정\s*캘린더|입고\s*일정|월\s*일정", body):
        # Each calendar line belongs to one edition. An unrelated product's
        # hold/cancellation elsewhere in the caption cannot change this date.
        facts = []
        for line in body.splitlines():
            if not any(alias in title_key(line) for alias in aliases):
                continue
            if re.search(r"보류|연기|취소|일정\s*(?:변경|미정)", line):
                raise KoreaReleaseError("release_schedule_changed")
            match = re.match(r"^[^\w0-9]{0,10}(" + DATE_START + r")", line)
            if match:
                parsed = release_phrase(match.group(1) + " 입고 예정", posted, "arrival")
                if parsed:
                    facts.append((parsed, line.strip()))
        if not facts:
            return None
        if len({(fact[0][0], fact[0][1]) for fact in facts}) != 1:
            raise KoreaReleaseError("ambiguous_release_calendar")
        release, evidence = facts[0]
        evidence = "입고 예정 캘린더: " + evidence
    elif spec["dateKind"] == "release":
        if SALES_CHANGE.search(body):
            raise KoreaReleaseError("release_schedule_changed")
        heading = re.search(r"판매\s*(?:시작|개시)\s*(?:시간|일시)", body)
        if heading:
            block = body[heading.end():].lstrip(" ]\t\r\n")[:200].split("\n\n", 1)[0]
            absolute = list(re.finditer(DATE_START, block))
            if len(absolute) > 1:
                raise KoreaReleaseError("ambiguous_release_calendar")
            release = release_phrase(absolute[0].group(0) + " 판매 개시", posted) if absolute else day_only_sale(block, posted)
            evidence = "판매 개시: " + block
        if release is None:
            release = release_phrase(body, posted, "release")
            evidence = release[2] if release else ""
    else:
        release = release_phrase(body, posted, "arrival")
        evidence = release[2] if release else ""
    if release is None:
        return None
    value, precision, _ = release
    return {"koreaReleaseDate": value, "koreaReleaseDatePrecision": precision,
            "koreaReleaseDateText": (spec["sourceName"] + " 공지: " + " ".join(evidence.split()))[:300],
            "koreaReleaseSourceUrl": spec["sourceUrl"], "koreaReleaseCheckedAt": now.astimezone(KST).isoformat(timespec="seconds"),
            "koreaReleaseSourceType": "retailer", "koreaReleaseSourceName": spec["sourceName"],
            "koreaReleaseDateKind": spec["dateKind"]}


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


def run(root: Path, *, fetcher=None, now: datetime | None = None, sources=None) -> dict:
    root = Path(root)
    now = now or datetime.now(KST)
    if now.tzinfo is None or now.utcoffset() is None:
        raise KoreaReleaseError("timezone_required")
    catalog = json.loads((root / "official-catalog.json").read_text(encoding="utf-8"))
    status = json.loads((root / "sync-status.json").read_text(encoding="utf-8"))
    if not isinstance(catalog, list) or not catalog or len(catalog) > 200 or not all(isinstance(item, dict) for item in catalog) or not isinstance(status, dict):
        raise KoreaReleaseError("invalid_saved_catalog")
    catalog = copy.deepcopy(catalog)
    sources = load_sources() if sources is None else validate_sources(sources)
    accounts = approved_accounts(sources)
    previous = status.get("korea") if isinstance(status.get("korea"), dict) else {}
    report = {"status": "ok", "checkedAt": now.astimezone(KST).isoformat(timespec="seconds"),
              "lastSuccessAt": previous.get("lastSuccessAt", ""), "confirmed": 0, "unconfirmed": 0,
              "checked": 0, "failed": 0, "errors": [], "officialConfirmed": 0, "retailerConfirmed": 0}
    fetcher = fetcher or Fetcher(accounts=accounts, registered_urls={entry["sourceUrl"] for entry in sources})
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
        for title in product_titles(product, sources):
            title_owners.setdefault(title, set()).add(index)
    for product in catalog:
        for field, value in UNKNOWN.items():
            product.setdefault(field, value)
        wanted = product_titles(product, sources)
        registered = [entry for entry in sources if entry["productSourceUrl"] == product.get("sourceUrl")]
        selected = [{**candidate, "account": ACCOUNT, "sourceSpec": None, "pinned": False}
                    for candidate in candidates if title_key(candidate["title"]) in wanted
                    and len(title_owners[title_key(candidate["title"])]) == 1]
        for entry in registered:
            selected.append({"tweetId": None if instagram_source(entry["sourceUrl"]) else entry["sourceUrl"].rsplit("/", 1)[1], "title": None,
                             "account": entry["sourceAccount"], "sourceSpec": entry, "pinned": True})
        saved_source = product.get("koreaReleaseSourceUrl", "")
        if saved_source:
            try:
                if instagram_source(saved_source):
                    if not any(entry["sourceUrl"] == saved_source for entry in registered):
                        raise KoreaReleaseError("unregistered_saved_retailer_source")
                else:
                    canonical = canonical_tweet(saved_source, accounts)
                    identifier = canonical.rsplit("/", 1)[1]
                    account = source_account(canonical)
                    if not any(candidate["tweetId"] == identifier for candidate in selected):
                        if product.get("koreaReleaseSourceType", "official") == "retailer":
                            raise KoreaReleaseError("unregistered_saved_retailer_source")
                        selected.append({"tweetId": identifier, "title": None, "account": account,
                                         "sourceSpec": None, "pinned": True})
            except KoreaReleaseError:
                failure("source", "invalid_saved_source")
        def candidate_source(candidate):
            return candidate["sourceSpec"]["sourceUrl"] if candidate["sourceSpec"] else f'https://x.com/{candidate["account"]}/status/{candidate["tweetId"]}'
        selected = sorted({candidate_source(candidate): candidate for candidate in selected}.values(),
                          key=lambda candidate: (candidate["sourceSpec"] is None or candidate["sourceSpec"]["sourceType"] == "official",
                                                 int(candidate["tweetId"] or 0)), reverse=True)
        if selected:
            report["checked"] += 1
        for candidate in selected:
            identifier = candidate["tweetId"]
            account = candidate["account"]
            source = candidate_source(candidate)
            stage = "retailer" if candidate["sourceSpec"] and candidate["sourceSpec"]["sourceType"] == "retailer" else "official"
            try:
                if source not in cache:
                    try:
                        response = fetcher(source if instagram_source(source) else embed_url(identifier, account)).decode("utf-8")
                        cache[source] = response if instagram_source(source) else json.loads(response)
                    except (KoreaReleaseError, UnicodeError, ValueError, OSError) as error:
                        cache[source] = error
                if isinstance(cache[source], Exception):
                    error = cache[source]
                    raise KoreaReleaseError(str(error) if isinstance(error, KoreaReleaseError) else "official_parse_error")
                if instagram_source(source):
                    fields = parse_instagram(cache[source], product, now=now, source_spec=candidate["sourceSpec"])
                else:
                    fields = parse_announcement(cache[source], identifier, product, now=now,
                                                discovered_title=candidate["title"], source_spec=candidate["sourceSpec"], sources=sources)
                if fields:
                    if not (has_verified_saved_fields(product, sources)
                            and product.get("koreaReleaseSourceType", "official") == "official"
                            and fields["koreaReleaseSourceType"] == "retailer"):
                        product.update(fields)
                    successes += 1
                    break
                if candidate["pinned"]:
                    failure(stage, "registered_release_fact_missing", source)
            except KoreaReleaseError as error:
                failure(stage, str(error), source)
        confirmed = has_verified_saved_fields(product, sources)
        report["confirmed" if confirmed else "unconfirmed"] += 1
        if confirmed:
            report["retailerConfirmed" if product.get("koreaReleaseSourceType", "official") == "retailer" else "officialConfirmed"] += 1
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
