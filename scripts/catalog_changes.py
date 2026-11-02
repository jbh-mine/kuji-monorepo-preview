#!/usr/bin/env python3
"""Record meaningful public catalog changes after the Japanese and Korean syncs.

    python scripts/catalog_changes.py --root . --before /tmp/catalog-before.json

The workflow copies official-catalog.json before either collector runs. This
script never edits that catalog: failed collectors can retain their data and
only actual differences appear in the public, bounded change history.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

KST = timezone(timedelta(hours=9))
MAX_EVENTS = 300
MAX_CATALOG_BYTES = 20 * 1024 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
CHANGE_TYPES = {"added", "japan-date", "japan-released", "korea-date", "korea-source", "details"}


def read_json(path: Path, limit: int):
    if path.stat().st_size > limit:
        raise ValueError(f"File exceeds the permitted size: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ""


def catalog_by_code(value):
    if not isinstance(value, list) or not value or len(value) > 200:
        raise ValueError("A nonempty catalog with at most 200 products is required")
    result = {}
    for product in value:
        if not isinstance(product, dict):
            raise ValueError("Invalid catalog product")
        code = product.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,100}", code) or code in result:
            raise ValueError("Invalid or duplicate product code")
        if not text(product.get("productTitleKo") or product.get("nameKo"), 300):
            raise ValueError("Every product needs a display title")
        result[code] = product
    return result


def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def valid_month(value):
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value))


def safe_source(value):
    if not isinstance(value, str) or len(value) > 300:
        return False
    return bool(re.fullmatch(r"https://(?:www\.)?1kuji\.com/products/[A-Za-z0-9_-]+/?", value)
                or re.fullmatch(r"https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/[0-9]{1,30}/?", value)
                or re.fullmatch(r"https://www\.instagram\.com/p/[A-Za-z0-9_-]{1,64}/?", value))


def japan_facts(product):
    value = text(product.get("releaseDate"))
    precision = product.get("releaseDatePrecision") or product.get("releasePrecision") or ("day" if valid_day(value) else "unknown")
    if precision == "day" and valid_day(value):
        return ("day", value)
    if precision == "month" and valid_month(value):
        return ("month", value)
    return ("unknown", text(product.get("releaseDateText") or product.get("releaseLabel")))


def korea_facts(product):
    source = text(product.get("koreaReleaseSourceUrl"), 300)
    kind = product.get("koreaReleaseSourceType") or "official"
    match = re.fullmatch(r"https://(?:x|twitter)\.com/(ICHIBAN_KUJI_|ICHIBAN_KUJI_JS)/status/[0-9]{1,30}/?", source)
    retailer = kind == "retailer" and safe_source(source) and not "/products/" in source and bool(text(product.get("koreaReleaseSourceName"))) and product.get("koreaReleaseDateKind") in {"arrival", "release"}
    trusted = bool(match) if kind == "official" else retailer
    value, precision = text(product.get("koreaReleaseDate")), product.get("koreaReleaseDatePrecision")
    if not trusted or not (precision == "day" and valid_day(value) or precision == "month" and valid_month(value)):
        return ("unknown", "", "", "", "", "")
    scope = text(product.get("koreaReleaseSourceName"), 80) or ("이치방쿠지 공식 잠실점" if "/ICHIBAN_KUJI_JS/" in source else "이치방쿠지 공식 직영점")
    date_kind = product.get("koreaReleaseDateKind") or "release"
    planned = "예정" in text(product.get("koreaReleaseDateText"))
    label = ("입고예정일" if planned else "입고일") if date_kind == "arrival" else (("판매예정일" if planned else "판매개시일") if kind == "retailer" else ("발매예정일" if planned else "발매일"))
    return (precision, value, scope, source, label, kind)


def date_label(facts):
    if facts[0] == "day":
        return facts[1].replace("-", ".")
    if facts[0] == "month":
        return facts[1].replace("-", ".") + " (일자 미정)"
    return facts[1] or "미확인"


def korea_label(facts):
    return "미확인" if facts[0] == "unknown" else f"{facts[2]} · {facts[4]} {date_label(facts)}"


def previous_status(product, now):
    status = product.get("catalogStatus") or product.get("releaseStatus")
    if status in {"ON_SALE", "UPCOMING"}:
        return status
    checked = product.get("lastSyncedAt") or product.get("checkedAt")
    try:
        previous_day = datetime.fromisoformat(checked).astimezone(KST).date() if "T" in checked else date.fromisoformat(checked)
    except (ValueError, TypeError):
        previous_day = now.astimezone(KST).date()
    facts = japan_facts(product)
    return "ON_SALE" if facts[0] == "day" and facts[1] <= previous_day.isoformat() else "UPCOMING"


def current_status(product, now):
    facts = japan_facts(product)
    return "ON_SALE" if facts[0] == "day" and facts[1] <= now.astimezone(KST).date().isoformat() else "UPCOMING"


def details(product):
    prizes = product.get("prizes") or product.get("previewPrizes") or []
    def image(item):
        return item.get("imageUrl") or item.get("imagePath") or ""
    return {
        "pending": bool(product.get("prizeDetailsPending")),
        "title": text(product.get("productTitleKo") or product.get("nameKo"), 300),
        "summary": product.get("summary"),
        "poster": product.get("posterUrl") or product.get("posterPath"),
        "prizes": sorted([{key: prize.get(key) for key in ("letter", "name", "nameKo", "description", "descriptionJp", "size", "variants")}
                          | {"image": image(prize), "gallery": [image(item) for item in prize.get("gallery", [])]}
                          for prize in prizes], key=lambda prize: prize.get("letter") or ""),
        "gallery": [image(item) for item in product.get("gallery", [])],
    }


def create_event(product, kind, before, after, summary, now, source=None):
    code = product["code"]
    url = source or product.get("sourceUrl")
    identity = json.dumps([code, kind, before, after, url if safe_source(url) else ""], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:24]
    result = {"id": fingerprint, "transition": fingerprint, "at": now.astimezone(KST).isoformat(timespec="seconds"),
              "code": code, "type": kind, "titleKo": text(product.get("productTitleKo") or product.get("nameKo"), 300),
              "titleJp": text(product.get("productTitle") or product.get("nameJp"), 300),
              "franchiseKo": text(product.get("franchiseKo"), 100), "summary": text(summary), "before": text(before), "after": text(after)}
    if safe_source(url):
        result["sourceUrl"] = url
    return result


def detect_changes(before, after, now):
    old_products, new_products = catalog_by_code(before), catalog_by_code(after)
    result = []
    for code in sorted(new_products):
        product = new_products[code]
        old = old_products.get(code)
        emit = lambda kind, previous, following, summary, source=None: result.append(create_event(product, kind, previous, following, summary, now, source))
        if old is None:
            emit("added", "", date_label(japan_facts(product)), "새 쿠지 정보가 추가됐습니다.")
            continue
        old_jp, new_jp = japan_facts(old), japan_facts(product)
        if old_jp != new_jp:
            delay = old_jp[0] == new_jp[0] == "day" and old_jp[1] < new_jp[1]
            emit("japan-date", date_label(old_jp), date_label(new_jp), "일본 발매일이 연기됐습니다." if delay else "일본 발매 일정이 변경됐습니다.")
        if previous_status(old, now) == "UPCOMING" and current_status(product, now) == "ON_SALE":
            emit("japan-released", f"발매예정 · {date_label(new_jp)}", f"발매중 · {date_label(new_jp)}", "일본 발매일이 되어 발매중으로 전환됐습니다.")
        old_kr, new_kr = korea_facts(old), korea_facts(product)
        if old_kr != new_kr:
            if old_kr[:2] != new_kr[:2] or old_kr[4:] != new_kr[4:]:
                summary = "한국 일정이 처음 확인됐습니다." if old_kr[0] == "unknown" and new_kr[0] != "unknown" else "한국 일정이 변경됐습니다."
                if new_kr[0] == "unknown":
                    summary = "한국 일정의 근거를 다시 확인하고 있습니다."
                emit("korea-date", korea_label(old_kr), korea_label(new_kr), summary, new_kr[3] or old_kr[3])
            else:
                emit("korea-source", korea_label(old_kr), korea_label(new_kr), "한국 일정의 확인 출처가 갱신됐습니다.", new_kr[3])
        old_details, new_details = details(old), details(product)
        if old_details != new_details:
            complete = old_details["pending"] and not new_details["pending"]
            old_hash = hashlib.sha256(json.dumps(old_details, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
            new_hash = hashlib.sha256(json.dumps(new_details, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
            event = create_event(product, "details", old_hash, new_hash, "전체 경품 구성과 상세 정보가 공개됐습니다." if complete else "포스터·경품 이미지 또는 상품 설명이 갱신됐습니다.", now)
            # Content hashes identify repeat updates without exposing unreadable hashes in the UI.
            event["before"], event["after"] = "", "전체 경품 공개" if complete else "상품·경품 정보 갱신"
            result.append(event)
    return result


def valid_stamp(value):
    try:
        return isinstance(value, str) and len(value) <= 40 and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def validate_history(value):
    if not isinstance(value, dict) or value.get("version") != 1 or not valid_stamp(value.get("startedAt")) or not valid_stamp(value.get("updatedAt")):
        raise ValueError("Invalid catalog change history; original file is preserved")
    events = value.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid catalog change event count")
    ids = set()
    for event in events:
        if not isinstance(event, dict) or not re.fullmatch(r"[a-f0-9]{24}", str(event.get("id", ""))) or event["id"] in ids:
            raise ValueError("Invalid or duplicate change event ID")
        ids.add(event["id"])
        if event.get("type") not in CHANGE_TYPES or not valid_stamp(event.get("at")):
            raise ValueError("Invalid change event type or timestamp")
        if "transition" in event and not re.fullmatch(r"[a-f0-9]{24}", str(event["transition"])):
            raise ValueError("Invalid change transition fingerprint")
        if "sequence" in event and (type(event["sequence"]) is not int or not 1 <= event["sequence"] <= 2**53 - 1):
            raise ValueError("Invalid change occurrence sequence")
        for key, limit in (("code", 100), ("titleKo", 300), ("titleJp", 300), ("franchiseKo", 100), ("summary", 500), ("before", 500), ("after", 500)):
            if not isinstance(event.get(key), str) or len(event[key]) > limit:
                raise ValueError("Invalid change event display value")
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,100}", event["code"]) or not event["titleKo"] or not event["summary"]:
            raise ValueError("Invalid change event identity")
        if event.get("sourceUrl") is not None and not safe_source(event["sourceUrl"]):
            raise ValueError("Invalid change event source")
    return value


def event_order(event):
    return (datetime.fromisoformat(event["at"].replace("Z", "+00:00")).timestamp(), event.get("sequence", 0), event["id"])


def run(root: Path, before: Path, now=None):
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    path = root / "catalog-changes.json"
    previous = read_json(before, MAX_CATALOG_BYTES)
    current = read_json(root / "official-catalog.json", MAX_CATALOG_BYTES)
    changes = detect_changes(previous, current, now)
    stamp = now.astimezone(KST).isoformat(timespec="seconds")
    history = validate_history(read_json(path, MAX_HISTORY_BYTES)) if path.exists() else {"version": 1, "startedAt": stamp, "updatedAt": stamp, "events": []}
    # A transition can legitimately recur (A→B→A→B). Comparing all historical
    # fingerprints would hide its second occurrence. Dedupe only against the
    # latest transition for the same edition/type; chain the new occurrence ID
    # to that predecessor, so retries still match the latest fingerprint.
    latest = {}
    for event in sorted(history["events"], key=event_order):
        latest[(event["code"], event["type"])] = event
    sequence = max((event.get("sequence", 0) for event in history["events"]), default=0)
    additions = []
    for event in changes:
        key = (event["code"], event["type"])
        predecessor = latest.get(key)
        fingerprint = event["transition"]
        if predecessor and predecessor.get("transition", predecessor["id"]) == fingerprint:
            continue
        sequence += 1
        event["sequence"] = sequence
        if predecessor:
            occurrence = json.dumps([predecessor["id"], fingerprint], separators=(",", ":"))
            event["id"] = hashlib.sha256(occurrence.encode()).hexdigest()[:24]
        additions.append(event)
        latest[key] = event
    if additions:
        history = {**history, "updatedAt": stamp, "events": sorted(additions + history["events"], key=event_order, reverse=True)[:MAX_EVENTS]}
    validate_history(history)
    if additions or not path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".catalog-changes-", suffix=".tmp", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(history, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"added": len(additions), "retained": len(history["events"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.root.resolve(), args.before.resolve())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
