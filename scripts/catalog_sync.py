#!/usr/bin/env python3
"""Refresh a static Ichiban Kuji catalog using public official pages only.

Python 3.11+, standard library only. Run in the published site repository:
    python scripts/catalog_sync.py --root .

Each product is a transaction: parse and validate its announced lineup, stage all
referenced images, then replace its JSON. A failed refresh preserves that product.
Images are immutable: a changed source URL receives a new hash-suffixed filename.
Ordinary ticket allocations are explicitly arbitrary demo values, never lot data.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable

from translation_ko import KoreanTranslator, load_cache, translate_catalog

JST = timezone(timedelta(hours=9))  # Japan and Korea share UTC+09:00; no host-local dates.
PAGE_HOSTS = frozenset({"1kuji.com", "www.1kuji.com"})
IMAGE_HOSTS = PAGE_HOSTS | {"assets.1kuji.com", "sf.1kuji.com", "on-line.1kuji.com"}
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024  # The official MHA poster is a verified 13.2 MB PNG.
MAX_PRODUCT_IMAGE_BYTES = 80 * 1024 * 1024
MAX_IMAGES_PER_PRODUCT = 250
MAX_PRODUCTS = 200
MAX_DISCOVERY_PAGES = 6
DEFAULT_MAX_NEW = 8
MAX_CANDIDATE_CHECKS = 40
MAX_RUN_SECONDS = 15 * 60
USER_AGENT = "KujiCatalogDaily/1.0 (+public official product information; daily refresh)"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
FRANCHISES = (
    ("DEMON_SLAYER", ("鬼滅の刃",), "#ef4444"),
    ("ATTACK_ON_TITAN", ("進撃の巨人",), "#1d4ed8"),
    ("CHAINSAW_MAN", ("チェンソーマン",), "#ea580c"),
    ("DRAGON_BALL", ("ドラゴンボール",), "#ea580c"),
    ("ONE_PIECE", ("ワンピース", "ONE PIECE"), "#0369a1"),
    ("NARUTO", ("NARUTO", "ナルト"), "#d97706"),
    ("JUJUTSU_KAISEN", ("呪術廻戦",), "#6d28d9"),
    ("HUNTER_HUNTER", ("HUNTER×HUNTER", "HUNTERxHUNTER"), "#15803d"),
    ("MY_HERO_ACADEMIA", ("僕のヒーローアカデミア",), "#166534"),
    ("DEATH_NOTE", ("DEATH NOTE",), "#334155"),
    ("BLEACH", ("BLEACH",), "#7c3aed"),
    ("HAIKYU", ("ハイキュー!!", "ハイキュー！！"), "#f97316"),
)


class CatalogError(ValueError):
    """A page or asset cannot safely replace the saved product."""


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def franchise_for(title: str) -> tuple[str, str] | None:
    key = normalize(title).upper().replace("×", "X").replace(" ", "")
    for code, aliases, color in FRANCHISES:
        if any(normalize(alias).upper().replace("×", "X").replace(" ", "") in key for alias in aliases):
            return code, color
    return None


def checked_url(url: str, image: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise CatalogError("Invalid official URL")
    parsed = urllib.parse.urlsplit(url)
    hosts = IMAGE_HOSTS if image else PAGE_HOSTS
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if parsed.scheme != "https" or parsed.hostname not in hosts or not valid_port or parsed.username or parsed.password:
        raise CatalogError("URL must use HTTPS on an explicitly permitted official host")
    if ".." in urllib.parse.unquote(parsed.path).split("/"):
        raise CatalogError("Unsafe official URL path")
    return urllib.parse.urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def product_url(url: str, base: str = "https://1kuji.com/products") -> str:
    absolute = checked_url(urllib.parse.urljoin(base, html.unescape(url)))
    parsed = urllib.parse.urlsplit(absolute)
    match = re.fullmatch(r"/products/([A-Za-z0-9_-]{1,100})/?", parsed.path)
    if not match:
        raise CatalogError("Not an official product detail link")
    return "https://1kuji.com/products/" + match.group(1)


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, image: bool):
        super().__init__()
        self.image = image

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        checked_url(newurl, image=self.image)  # Validate before following, not after fetching.
        return super().redirect_request(request, fp, code, msg, headers, newurl)


@dataclass
class FetchResult:
    data: bytes
    content_type: str


class Fetcher:
    def __init__(self, deadline: float | None = None):
        self.deadline = deadline or time.monotonic() + MAX_RUN_SECONDS

    def __call__(self, url: str, image: bool = False) -> FetchResult:
        checked_url(url, image=image)
        limit = MAX_IMAGE_BYTES if image else MAX_HTML_BYTES
        last_error: Exception | None = None
        for attempt in range(2):
            if time.monotonic() >= self.deadline:
                raise CatalogError("Daily refresh time limit reached")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*" if image else "text/html"})
                opener = urllib.request.build_opener(OfficialRedirects(image))
                with opener.open(request, timeout=15) as response:
                    checked_url(response.geturl(), image=image)
                    content_type = response.headers.get_content_type()
                    if image and not content_type.startswith("image/"):
                        raise CatalogError("Official image returned a non-image response")
                    if not image and content_type not in {"text/html", "application/xhtml+xml"}:
                        raise CatalogError("Official page returned a non-HTML response")
                    length = response.headers.get("Content-Length")
                    if length and int(length) > limit:
                        raise CatalogError("Official response exceeds the size limit")
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise CatalogError("Official response exceeds the size limit")
                    return FetchResult(data, content_type)
            except (OSError, urllib.error.URLError) as error:
                last_error = error
                if attempt == 0:
                    time.sleep(0.5)
        raise CatalogError(f"Official request failed: {type(last_error).__name__}") from last_error


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def text(self) -> str:
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)

    def find(self, *, tag: str | None = None, class_name: str | None = None, id_: str | None = None) -> Node | None:
        return next((node for node in self.walk()
                     if (tag is None or node.tag == tag)
                     and (class_name is None or node.has_class(class_name))
                     and (id_ is None or node.attrs.get("id") == id_)), None)


class Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.nodes = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 100000 or len(self.stack) > 150:
            raise CatalogError("Official HTML exceeds parser limits")
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag == "br":
            self.stack[-1].children.append("\n")
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def unique_images(node: Node | None, base: str) -> list[str]:
    if node is None:
        return []
    images: list[str] = []
    for item in node.walk():
        if item.tag != "img":
            continue
        value = item.attrs.get("src") or item.attrs.get("data-src")
        if value:
            url = checked_url(urllib.parse.urljoin(base, value), image=True)
            if url not in images:
                images.append(url)
    return images


def parse_release(value: str) -> tuple[str, str]:
    value = normalize(value)
    day = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", value)
    if day:
        try:
            return date(*map(int, day.groups())).isoformat(), "day"
        except ValueError as error:
            raise CatalogError("Invalid official release date") from error
    month = re.search(r"(20\d{2})年\s*(\d{1,2})月", value)
    if month:
        if not 1 <= int(month.group(2)) <= 12:
            raise CatalogError("Invalid official release month")
        return "", "month"
    return "", "unknown"


def catalog_status(release: str, precision: str, now: datetime) -> str:
    return "ON_SALE" if precision == "day" and release and date.fromisoformat(release) <= now.astimezone(JST).date() else "UPCOMING"


def parse_product(source: str, url: str) -> dict:
    url = product_url(url)
    if len(source.encode("utf-8")) > MAX_HTML_BYTES:
        raise CatalogError("Official HTML exceeds the size limit")
    document = Document(source).root
    about = document.find(id_="aboutCol")
    title_node = about.find(tag="h2") if about else None
    if not title_node:
        raise CatalogError("Official product heading is missing")
    title = re.sub(r"\s+", " ", title_node.text()).strip()
    if not title.startswith("一番くじ") or not franchise_for(title):
        raise CatalogError("Product is outside the permitted anime franchises")
    detail = about.find(class_name="detail")
    facts = [normalize(node.text()) for node in detail.walk() if node.tag == "li"] if detail else []
    release_line = next((line for line in facts if "発売日" in line or "発売時期" in line), "")
    # Match store and online channels separately; never substitute an online date for a store date.
    store_match = re.search(r"店頭販売\s*[:：]\s*(.*?)(?=オンライン販売|$)", release_line)
    online_match = re.search(r"オンライン販売\s*[:：]\s*(.*)$", release_line)
    store_text = store_match.group(1).strip() if store_match else ("" if online_match else release_line)
    release, precision = parse_release(store_text)
    online_date, _ = parse_release(online_match.group(1) if online_match else "")
    price_line = next((line for line in facts if "価格" in line), "")
    price_match = re.search(r"1回\s*([\d,]+)\s*円", price_line)
    price = int(price_match.group(1).replace(",", "")) if price_match else None
    if price is not None and not 100 <= price <= 10000:
        raise CatalogError("Official price is outside the expected range")
    poster_images = unique_images(document.find(class_name="mvCol"), url)
    if not poster_images:
        raise CatalogError("Official product poster is missing")
    list_section = document.find(id_="listCol")
    prizes = []
    seen: set[str] = set()
    if list_section:
        for block in list_section.walk():
            if not block.has_class("itemColList"):
                continue
            heading = block.find(tag="h4", class_name="name")
            label = re.sub(r"\s+", " ", heading.text()).strip() if heading else ""
            match = re.fullmatch(r"([A-YＡ-Ｙ])賞\s*(.+)", label)
            last = re.fullmatch(r"ラストワン賞\s*(.+)", label)
            if not (match or last):
                raise CatalogError("Unrecognized official prize heading")
            letter, name = (normalize(match.group(1)), match.group(2)) if match else ("LAST", last.group(1))
            if letter in seen:
                raise CatalogError("Duplicate official prize grade")
            seen.add(letter)
            images = unique_images(block.find(class_name="slider-item"), url)
            if not images:
                raise CatalogError(f"Official {letter} prize image is missing")
            data_node = block.find(class_name="data")
            data = normalize(data_node.text()) if data_node else ""
            variants = re.search(r"全\s*(\d+)\s*種", data)
            size = re.search(r"サイズ\s*[:：]\s*(.+)", data)
            detail_text = normalize(block.find(class_name="itemColDetail").text()) if block.find(class_name="itemColDetail") else ""
            selection = "blind" if re.search(r"選べない|選べません|選ぶことはできません|クローズドパッケージ", detail_text) else "choose" if re.search(r"選べる|選べます|お選びいただけます|お好きな.*選", detail_text) else "single" if variants and int(variants.group(1)) == 1 else "unknown"
            prizes.append({"letter": letter, "name": name, "images": images,
                           "variantCount": int(variants.group(1)) if variants else None,
                           "size": size.group(1).strip("■ ") if size else "", "selection": selection})
    prizes.sort(key=lambda prize: (prize["letter"] == "LAST", prize["letter"]))
    letters = [prize["letter"] for prize in prizes if prize["letter"] != "LAST"]
    complete = bool(letters) and letters == [chr(65 + i) for i in range(len(letters))] and "LAST" in seen
    campaign = document.find(id_="wcampaignsCol")
    double_chance = None
    if campaign:
        campaign_name = campaign.find(class_name="itemName")
        campaign_images = unique_images(campaign.find(class_name="imgCol"), url)
        if campaign_name and campaign_images:
            limit_node = campaign.find(class_name="limit")
            count_match = re.search(r"当選数\s*[:：]\s*([\d,]+)\s*個", normalize(campaign.text()))
            double_chance = {"name": normalize(campaign_name.text()), "imageUrl": campaign_images[0],
                             "deadline": normalize(limit_node.text()) if limit_node else "",
                             "awardCount": int(count_match.group(1).replace(",", "")) if count_match else None}
    copyright_node = document.find(class_name="productCopy")
    online_link = next((node.attrs.get("href", "") for node in document.walk()
                        if node.tag == "a" and "online" in node.attrs.get("class", "").split()
                        and urllib.parse.urlsplit(node.attrs.get("href", "")).hostname == "on-line.1kuji.com"), "")
    return {"productTitle": title, "releaseDate": release, "releaseDatePrecision": precision,
            "releaseDateText": store_text, "onlineReleaseDate": online_date, "priceJpy": price,
            "hasReleaseFact": bool(release_line), "hasPriceFact": bool(price_match),
            "age": next((line.split("：", 1)[-1].split(":", 1)[-1].lstrip("■ ") for line in facts if "対象年齢" in line), ""),
            "posterUrl": poster_images[0], "prizes": prizes, "complete": complete,
            "retailers": next((line.split("：", 1)[-1].split(":", 1)[-1].lstrip("■ ") for line in facts if "取扱店" in line), ""),
            "copyright": normalize(copyright_node.text()) if copyright_node else "",
            "onlineSourceUrl": online_link, "doubleChance": double_chance,
            "galleryUrls": unique_images(document.find(id_="galleryCol"), url)}


def discover_products(source: str, base: str = "https://1kuji.com/products", now: datetime | None = None) -> tuple[list[str], list[str]]:
    document = Document(source).root
    products: list[str] = []
    pages: list[str] = []
    priorities = {}
    today = (now or datetime.now(JST)).astimezone(JST).date()
    for node in document.walk():
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        href = node.attrs["href"]
        try:
            candidate = product_url(href, base)
        except CatalogError:
            candidate = ""
        if candidate:
            labels = node.text() + " " + " ".join(child.attrs.get("alt", "") for child in node.walk() if child.tag == "img")
            if franchise_for(labels) and candidate not in products:
                products.append(candidate)
                first_date = node.find(class_name="date")
                release, precision = parse_release(first_date.text() if first_date else "")
                if precision == "day":
                    distance = (date.fromisoformat(release) - today).days
                    priorities[candidate] = (0 if distance >= 0 else 2, abs(distance))
                else:
                    priorities[candidate] = (1, 0)
            continue
        absolute = urllib.parse.urljoin(base, html.unescape(href))
        try:
            checked_url(absolute)
        except CatalogError:
            continue
        parsed = urllib.parse.urlsplit(absolute)
        query = urllib.parse.parse_qs(parsed.query)
        # Follow only same official product-calendar/list pagination, never arbitrary links.
        if parsed.path == "/products" and query and set(query) <= {"page", "sale_month", "sale_year"}:
            if len(absolute) <= 300 and absolute not in pages:
                pages.append(absolute)
    products.sort(key=lambda candidate: priorities[candidate])
    return products, pages


def image_signature(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
    if data.startswith(b"\xff\xd8\xff"): return ".jpg"
    if data[:6] in {b"GIF87a", b"GIF89a"}: return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return ".webp"
    raise CatalogError("Image bytes are not a supported raster image")


def safe_asset_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
        raise CatalogError("Invalid catalog image path")
    parts = PurePosixPath(relative.lstrip("/")).parts
    if len(parts) < 4 or parts[:2] != ("series", "official") or any(part in {"..", "."} for part in parts):
        raise CatalogError("Catalog image path escapes official assets")
    destination = (root / Path(*parts)).resolve()
    base = (root / "series/official").resolve()
    if not base.is_relative_to(root.resolve()) or not destination.is_relative_to(base):
        raise CatalogError("Catalog image path escapes official assets")
    return destination


def image_refs(value):
    if isinstance(value, dict):
        for url_key, path_key in (("posterUrl", "posterPath"), ("imageUrl", "imagePath")):
            if value.get(url_key) and value.get(path_key):
                yield value[url_key], value[path_key]
        for child in value.values(): yield from image_refs(child)
    elif isinstance(value, list):
        for child in value: yield from image_refs(child)


def sample_counts(prizes: list[dict], previous: list[dict]) -> dict[str, int]:
    letters = [prize["letter"] for prize in prizes if prize["letter"] != "LAST"]
    old = {prize["letter"]: prize.get("sampleCount", 0) for prize in previous}
    if set(old) == set(letters) | {"LAST"} and old.get("LAST") == 0 and sum(old.values()) == 80:
        return old
    if not letters:
        return {"LAST": 0}
    return {letter: 80 // len(letters) + (index < 80 % len(letters)) for index, letter in enumerate(letters)} | {"LAST": 0}


def merge_product(parsed: dict, existing: dict | None, url: str, now: datetime) -> dict:
    old = copy.deepcopy(existing) if existing else {}
    source = product_url(url)
    source_id = urllib.parse.urlsplit(source).path.rsplit("/", 1)[-1]
    digest = hashlib.sha256(source.encode()).hexdigest()[:10]
    franchise, color = franchise_for(parsed["productTitle"])
    old_franchise = franchise_for(old.get("productTitle", ""))
    if old_franchise and old_franchise[0] != franchise:
        raise CatalogError("Product changed anime franchise; preserved saved identity")
    old_prizes = {prize["letter"]: prize for prize in old.get("prizes", [])}
    new_letters = {prize["letter"] for prize in parsed["prizes"]}
    if old_prizes and not set(old_prizes).issubset(new_letters):
        raise CatalogError("Official page returned a smaller/partial lineup; preserved saved product")
    if old.get("releaseDate") and not parsed["hasReleaseFact"]:
        raise CatalogError("Official release fact disappeared; preserved saved product")
    if old.get("priceJpy") and not parsed["hasPriceFact"]:
        raise CatalogError("Official price disappeared; preserved saved product")
    if old_prizes and "LAST" in old_prizes and not parsed["complete"]:
        raise CatalogError("Complete saved lineup cannot be replaced by a partial page")
    slug = old.get("slug") or "auto-" + source_id.lower()[:64] + "-" + digest
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", slug):
        raise CatalogError("Unsafe product asset slug")
    code = old.get("code") or "AUTO_" + source_id.upper().replace("-", "_")[:30] + "_" + digest.upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,50}", code):
        raise CatalogError("Unsafe product code")
    known_images = dict(image_refs(old))

    def asset_ref(remote: str, stem: str) -> dict:
        remote = checked_url(remote, image=True)
        # URL changes always use a fresh filename. Old ticket snapshots can still load old images.
        extension = Path(urllib.parse.urlsplit(remote).path).suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}: extension = ".webp"
        suffix = hashlib.sha256(remote.encode()).hexdigest()[:16]
        path = known_images.get(remote) or f"/series/official/{slug}/{stem}-{suffix}{extension}"
        return {"imageUrl": remote, "imagePath": path}

    counts = sample_counts(parsed["prizes"], list(old_prizes.values())) if parsed["complete"] else {}
    prizes = []
    for raw in parsed["prizes"]:
        previous = old_prizes.get(raw["letter"], {})
        identity_same = previous.get("name") == raw["name"]
        count = raw["variantCount"] if raw["variantCount"] is not None else previous.get("variantCount") if identity_same else None
        selection = raw["selection"] if raw["selection"] != "unknown" else previous.get("selection", "unknown") if identity_same else "unknown"
        variants = f"총 {count}종" if count is not None else "종류 수 미공개"
        variants += " · 디자인 선택 가능" if selection == "choose" else " · 무작위 디자인" if selection == "blind" else ""
        size = raw["size"] or (previous.get("sizeJp", previous.get("size", "")) if identity_same else "")
        factual_description = "공식 공개 경품. " + " · ".join(value for value in (variants, size) if value)
        new_gallery = [asset_ref(image, raw["letter"].lower() + f"-detail-{i + 1}") for i, image in enumerate(raw["images"][1:3])]
        if identity_same:
            new_gallery = list({ref["imageUrl"]: ref for ref in new_gallery + previous.get("gallery", [])}.values())[:2]
        prize = {**previous, "letter": raw["letter"], "name": raw["name"],
                 "nameKo": previous.get("nameKo", raw["name"]) if identity_same else raw["name"],
                 "description": previous.get("description", factual_description) if identity_same else factual_description,
                 "size": size, "sizeJp": size, "variants": variants, "variantCount": count, "selection": selection,
                 **asset_ref(raw["images"][0], raw["letter"].lower()), "sampleCount": counts.get(raw["letter"], 0),
                 "gallery": new_gallery}
        prizes.append(prize)
    own_summary = old.get("summary") or "공식 상품 페이지에 공개된 일본어 정식 상품명과 경품 정보를 표시합니다."
    # A previously incomplete announcement must not retain a now-false promise
    # that its full lineup is still undisclosed. Regenerate factual Korean text.
    if old.get("prizeDetailsPending") and parsed["complete"]:
        own_summary = "공식 상품 페이지에 공개된 일본어 정식 상품명과 경품 정보를 표시합니다."
    title_same = old.get("productTitle") == parsed["productTitle"]
    korean_title = old.get("productTitleKo") if title_same else None
    korean_title = korean_title or parsed["productTitle"]
    product_gallery = [asset_ref(image, f"gallery-{i + 1}") for i, image in enumerate(parsed["galleryUrls"][:4])]
    product_gallery = list({ref["imageUrl"]: ref for ref in product_gallery + old.get("gallery", [])}.values())[:4]
    result = {**old, "code": code, "slug": slug, "franchiseCode": old.get("franchiseCode", franchise),
              "nameKo": korean_title, "nameJp": parsed["productTitle"],
              "productTitle": parsed["productTitle"], "productTitleKo": korean_title,
              "frameColor": old.get("frameColor", color), "sourceUrl": source,
              "releaseDate": parsed["releaseDate"], "releaseDatePrecision": parsed["releaseDatePrecision"],
              "releaseDateText": parsed["releaseDateText"], "onlineReleaseDate": parsed["onlineReleaseDate"] or old.get("onlineReleaseDate", ""),
              "releaseDateTextJp": parsed["releaseDateText"],
              "priceJpy": parsed["priceJpy"], "samplePriceKrw": old.get("samplePriceKrw", 12000),
              "catalogStatus": catalog_status(parsed["releaseDate"], parsed["releaseDatePrecision"], now),
              "prizeDetailsPending": not parsed["complete"], "sampleAllocation": True,
              "sampleAllocationMethod": old.get("sampleAllocationMethod", "equal-80-demo"),
              "posterUrl": parsed["posterUrl"], "posterPath": asset_ref(parsed["posterUrl"], "poster")["imagePath"],
              "summary": own_summary, "descriptionHtml": old.get("descriptionHtml") or "<p>" + html.escape(own_summary) + "</p><p>원화 가격과 티켓 수량은 체험용 예시입니다.</p>",
              "onlineSourceUrl": parsed["onlineSourceUrl"] or old.get("onlineSourceUrl", ""),
              "retailers": parsed["retailers"] or old.get("retailers", ""),
              "retailersJp": parsed["retailers"] or old.get("retailersJp", old.get("retailers", "")),
              "age": parsed["age"] or old.get("age", ""),
              "ageJp": parsed["age"] or old.get("ageJp", old.get("age", "")),
              "copyright": parsed["copyright"] or old.get("copyright", ""),
              "checkedAt": now.astimezone(JST).date().isoformat(), "lastSyncedAt": now.astimezone(JST).isoformat(timespec="seconds"),
              "prizes": prizes, "gallery": product_gallery}
    if existing is None:
        result["introducedRevision"] = 4
    if parsed["doubleChance"]:
        raw = parsed["doubleChance"]
        previous = old.get("doubleChance", {})
        award_count = raw["awardCount"] if raw["awardCount"] is not None else previous.get("awardCount")
        campaign_description = previous.get("description", "공식 별도 캠페인 경품입니다. 이 프로그램에서는 응모를 제공하지 않습니다.")
        if award_count is not None and (previous.get("awardCount") != award_count or previous.get("name") != raw["name"]):
            campaign_description = f"공식 별도 캠페인 당첨 수 {award_count}개. 이 프로그램에서는 응모를 제공하지 않습니다."
        result["doubleChance"] = {**previous, "name": raw["name"], "deadline": raw["deadline"], "deadlineJp": raw["deadline"],
                                  "description": campaign_description, "awardCount": award_count,
                                  **asset_ref(raw["imageUrl"], "double-chance")}
        if previous.get("name") != raw["name"]:
            result["doubleChance"].pop("nameKo", None)
    return result


def stage_assets(product: dict, root: Path, fetch: Callable, staging: Path) -> list[tuple[Path, Path]]:
    references = list(dict.fromkeys(image_refs(product)))
    if len(references) > MAX_IMAGES_PER_PRODUCT:
        raise CatalogError("Product exceeds the image-count limit")
    path_sources = {}
    for remote, relative in references:
        if relative in path_sources and path_sources[relative] != remote:
            raise CatalogError("Different official images share an immutable asset path")
        path_sources[relative] = remote

    def download(pair):
        remote, relative = pair
        checked_url(remote, image=True)
        destination = safe_asset_path(root, relative)
        if destination.is_file():
            if destination.stat().st_size > MAX_IMAGE_BYTES:
                raise CatalogError("Saved immutable image exceeds the size limit")
            data = destination.read_bytes()
            if len(data) <= MAX_IMAGE_BYTES:
                image_signature(data)
                return None, len(data)
        response = fetch(remote, image=True)
        if not response.content_type.startswith("image/") or not 32 <= len(response.data) <= MAX_IMAGE_BYTES:
            raise CatalogError("Official image response is invalid")
        image_signature(response.data)
        temporary = staging / hashlib.sha256(relative.encode()).hexdigest()
        temporary.write_bytes(response.data)
        return (temporary, destination), len(response.data)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(download, references))
    if sum(size for _, size in results) > MAX_PRODUCT_IMAGE_BYTES:
        raise CatalogError("Product exceeds the total image-size limit")
    return [result for result, _ in results if result is not None]


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sync_catalog(root: Path, catalog_path: Path, status_path: Path, *, fetch: Callable | None = None,
                 now: datetime | None = None, discover: bool = True, max_new: int = DEFAULT_MAX_NEW,
                 dry_run: bool = False, translator: KoreanTranslator | None = None) -> dict:
    root = root.resolve()
    now = (now or datetime.now(JST)).astimezone(JST)
    fetch = fetch or Fetcher()
    cache_path = root / "translation-cache.json"
    translator = translator or KoreanTranslator(load_cache(cache_path))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, list) or not catalog or len(catalog) > MAX_PRODUCTS:
        raise CatalogError("Saved catalog must be a nonempty bounded product array")
    if len({item.get("code") for item in catalog}) != len(catalog):
        raise CatalogError("Duplicate saved product codes")
    try:
        old_status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        if not isinstance(old_status, dict): old_status = {}
    except (OSError, ValueError):
        old_status = {}
    status = {"checkedAt": now.isoformat(timespec="seconds"), "lastSuccessAt": old_status.get("lastSuccessAt"),
              "status": "ok", "checked": 0, "released": 0, "upcoming": 0, "added": 0, "updated": 0,
              "failed": 0, "errors": [], "discovery": {"checkedPages": 0, "found": 0, "deferred": 0},
              "schedule": {"timezone": "Asia/Seoul", "hour": 10, "minute": 10}}
    known: dict[str, int] = {}
    for index, item in enumerate(catalog):
        source = product_url(item.get("sourceUrl", ""))
        if source in known: raise CatalogError("Duplicate saved product source URL")
        known[source] = index

    def failure(source: str, stage: str, error: Exception) -> None:
        status["failed"] += 1
        # Do not serialize response bodies, credentials, headers or traceback paths.
        message = str(error) if isinstance(error, CatalogError) else type(error).__name__
        status["errors"].append({"sourceUrl": source, "stage": stage, "message": message[:300]})

    candidates: list[str] = []
    product_successes = 0
    if discover:
        queue = ["https://1kuji.com/products"]
        visited: set[str] = set()
        while queue and len(visited) < MAX_DISCOVERY_PAGES:
            listing = queue.pop(0)
            if listing in visited: continue
            visited.add(listing)
            try:
                response = fetch(listing)
                found, pages = discover_products(response.data.decode("utf-8"), listing, now)
                status["discovery"]["checkedPages"] += 1
                for source in found:
                    if source not in known and source not in candidates: candidates.append(source)
                queue.extend(page for page in pages if page not in visited and page not in queue)
                def calendar_priority(page):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(page).query)
                    year = query.get("sale_year", [""])[0]
                    month = query.get("sale_month", [""])[0]
                    if year == "plan": return (0, 0)
                    if year.isdigit() and month.isdigit():
                        ordinal = int(year) * 12 + int(month)
                        current = now.year * 12 + now.month
                        return (1 if ordinal > current else 2, abs(ordinal - current))
                    return (3, 0)
                queue.sort(key=calendar_priority)
            except Exception as error:
                failure(listing, "discovery", error)
        status["discovery"]["found"] = len(candidates)
    selected = candidates[:MAX_CANDIDATE_CHECKS]
    status["discovery"]["deferred"] = max(0, len(candidates) - len(selected))
    candidate_checked = 0
    for source in list(known) + selected:
        existing = catalog[known[source]] if source in known else None
        if existing is None:
            if status["added"] >= max_new or len(catalog) >= MAX_PRODUCTS:
                break
            candidate_checked += 1
        status["checked"] += 1
        try:
            response = fetch(source)
            parsed = parse_product(response.data.decode("utf-8"), source)
            product = merge_product(parsed, existing, source, now)
            product = translate_catalog([product], translator)[0][0]
            with tempfile.TemporaryDirectory(prefix="kuji-catalog-") as directory:
                files = stage_assets(product, root, fetch, Path(directory))
                if not dry_run:
                    for temporary, destination in files:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if destination.exists():
                            # Never overwrite an image which a historical ticket might reference.
                            if destination.read_bytes() != temporary.read_bytes():
                                raise CatalogError("Immutable asset path collision")
                        else:
                            # Stage on the destination filesystem as well, avoiding cross-device moves.
                            with tempfile.NamedTemporaryFile(prefix=".catalog-", dir=destination.parent, delete=False) as output:
                                local_temporary = Path(output.name)
                                output.write(temporary.read_bytes())
                            try:
                                os.replace(local_temporary, destination)
                            finally:
                                local_temporary.unlink(missing_ok=True)
                    next_catalog = list(catalog)
                    if existing is None: next_catalog.append(product)
                    else: next_catalog[known[source]] = product
                    atomic_json(catalog_path, next_catalog)
                else:
                    next_catalog = list(catalog)
                    if existing is None: next_catalog.append(product)
                    else: next_catalog[known[source]] = product
                catalog = next_catalog
            if existing is None:
                status["added"] += 1
            elif {k: v for k, v in product.items() if k not in {"checkedAt", "lastSyncedAt"}} != {k: v for k, v in existing.items() if k not in {"checkedAt", "lastSyncedAt"}}:
                status["updated"] += 1
            product_successes += 1
        except Exception as error:
            failure(source, "product", error)
    status["discovery"]["deferred"] = max(0, len(candidates) - candidate_checked)
    for product in catalog:
        precision = product.get("releaseDatePrecision", "day" if product.get("releaseDate") else "unknown")
        category = catalog_status(product.get("releaseDate", ""), precision, now)
        status["released" if category == "ON_SALE" else "upcoming"] += 1
    status["translation"] = {"language": "ko", "pending": sum(bool(product.get("translationPending")) for product in catalog),
                             "requests": translator.requests, "cacheHits": translator.hits, "failedRequests": translator.failed}
    if status["failed"]:
        status["status"] = "failed" if product_successes == 0 else "partial"
    else:
        status["lastSuccessAt"] = status["checkedAt"]
    if not dry_run:
        atomic_json(cache_path, translator.cache)
        atomic_json(status_path, status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Published static-site root")
    parser.add_argument("--catalog", default="official-catalog.json", help="Catalog JSON relative to the site root")
    parser.add_argument("--status", default="sync-status.json", help="Health JSON relative to the site root")
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    catalog = (root / args.catalog).resolve()
    status_path = (root / args.status).resolve()
    if not catalog.is_relative_to(root) or not status_path.is_relative_to(root) or not 0 <= args.max_new <= DEFAULT_MAX_NEW:
        parser.error("Outputs must remain inside the site root; --max-new must be 0..8")
    try:
        status = sync_catalog(root, catalog, status_path, discover=not args.no_discovery, max_new=args.max_new, dry_run=args.dry_run)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1 if status["status"] == "failed" else 0
    except Exception as error:
        print(f"Catalog refresh aborted without deleting saved data: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
