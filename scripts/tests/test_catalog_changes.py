"""Meaningful history survives empty refreshes, retries and clock boundaries."""
import copy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "catalog_changes.py"
SPEC = importlib.util.spec_from_file_location("catalog_changes", SCRIPT)
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)
NOW = datetime(2026, 11, 2, 10, 3, tzinfo=changes.KST)


def product(**fields):
    return {"code": "KUJI_1", "productTitleKo": "제일복권 나루토", "productTitle": "一番くじ NARUTO",
            "franchiseKo": "나루토", "releaseDate": "2026-11-10", "releaseDatePrecision": "day", "catalogStatus": "UPCOMING",
            "sourceUrl": "https://1kuji.com/products/naruto11", "checkedAt": "2026-11-01", "summary": "상품 설명",
            "posterPath": "/series/official/naruto/poster.webp", "prizes": [{"letter": "A", "nameKo": "나루토", "imagePath": "/a.webp", "sampleCount": 2}], **fields}


def korea(**fields):
    return {"koreaReleaseDate": "2026-11-21", "koreaReleaseDatePrecision": "day", "koreaReleaseDateText": "11월 21일 입고 예정",
            "koreaReleaseSourceType": "retailer", "koreaReleaseSourceName": "모람모람 안양점", "koreaReleaseDateKind": "arrival",
            "koreaReleaseSourceUrl": "https://www.instagram.com/p/DfB961xh-Kp/", **fields}


class CatalogChangesTests(unittest.TestCase):
    def test_check_timestamps_and_demo_allocations_do_not_create_changes(self):
        old = product()
        new = copy.deepcopy(old)
        new.update(checkedAt="2026-11-02", lastSyncedAt=NOW.isoformat(), koreaReleaseCheckedAt=NOW.isoformat(), introducedRevision=4)
        new["prizes"][0]["sampleCount"] = 8
        self.assertEqual(changes.detect_changes([old], [new], NOW), [])

    def test_new_edition_has_one_added_event(self):
        result = changes.detect_changes([product()], [product(), product(code="KUJI_2")], NOW)
        self.assertEqual([event["type"] for event in result], ["added"])
        self.assertEqual(result[0]["code"], "KUJI_2")

    def test_postponement_records_old_and_new_dates(self):
        result = changes.detect_changes([product()], [product(releaseDate="2026-11-20")], NOW)
        self.assertEqual(result[0]["type"], "japan-date")
        self.assertIn("연기", result[0]["summary"])
        self.assertEqual((result[0]["before"], result[0]["after"]), ("2026.11.10", "2026.11.20"))

    def test_date_precision_is_not_promoted_to_an_invented_day(self):
        old = product(releaseDate="2026-11", releaseDatePrecision="month")
        result = changes.detect_changes([old], [copy.deepcopy(old)], NOW)
        self.assertEqual(result, [])
        result = changes.detect_changes([old], [product(releaseDate="2026-11-10")], NOW)
        self.assertEqual(result[0]["before"], "2026.11 (일자 미정)")

    def test_daily_release_promotion_uses_saved_status_and_korean_clock(self):
        old = product(releaseDate="2026-11-02")
        new = product(releaseDate="2026-11-02", catalogStatus="ON_SALE")
        result = changes.detect_changes([old], [new], NOW)
        self.assertEqual([event["type"] for event in result], ["japan-released"])
        self.assertEqual(changes.detect_changes([new], [new], NOW), [])

    def test_missing_legacy_status_uses_previous_check_day(self):
        old = product(releaseDate="2026-11-02")
        del old["catalogStatus"]
        result = changes.detect_changes([old], [dict(old, checkedAt="2026-11-02")], NOW)
        self.assertEqual(result[0]["type"], "japan-released")

    def test_korean_date_requires_valid_provenance_and_keeps_arrival_scope(self):
        result = changes.detect_changes([product()], [product(**korea())], NOW)
        self.assertEqual(result[0]["type"], "korea-date")
        self.assertIn("처음 확인", result[0]["summary"])
        self.assertIn("모람모람 안양점 · 입고예정일 2026.11.21", result[0]["after"])
        self.assertEqual(result[0]["sourceUrl"], korea()["koreaReleaseSourceUrl"])
        for source in ["https://evil.test/post", "https://www.instagram.com.evil.test/p/AB/", "https://www.instagram.com/name/", "https://x.com/OTHER/status/1"]:
            new = product(**korea(koreaReleaseSourceUrl=source, koreaReleaseSourceType="official"))
            self.assertEqual(changes.detect_changes([product()], [new], NOW), [])

    def test_changed_korean_source_does_not_claim_new_release(self):
        old = product(**korea())
        new = product(**korea(koreaReleaseSourceUrl="https://www.instagram.com/p/NEW_NOTICE/"))
        result = changes.detect_changes([old], [new], NOW)
        self.assertEqual(result[0]["type"], "korea-source")
        self.assertIn("출처", result[0]["summary"])

    def test_merchant_to_official_date_does_not_claim_postponement(self):
        old = product(**korea(koreaReleaseDate="2026-11-01"))
        new = product(**korea(koreaReleaseDate="2026-11-10", koreaReleaseSourceType="official", koreaReleaseSourceName="이치방쿠지 공식 직영점",
                               koreaReleaseDateKind="release", koreaReleaseSourceUrl="https://x.com/ICHIBAN_KUJI_/status/123", koreaReleaseDateText="11월 10일 판매 개시"))
        result = changes.detect_changes([old], [new], NOW)
        self.assertEqual(result[0]["type"], "korea-date")
        self.assertNotIn("연기", result[0]["summary"])
        self.assertIn("모람모람", result[0]["before"])
        self.assertIn("공식 직영점", result[0]["after"])

    def test_legitimate_recurring_transition_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            current = root / "official-catalog.json"
            a, b = product(releaseDate="2026-11-10"), product(releaseDate="2026-11-20")
            for previous, following in ((a, b), (b, a), (a, b)):
                before.write_text(json.dumps([previous]), encoding="utf-8")
                current.write_text(json.dumps([following]), encoding="utf-8")
                self.assertEqual(changes.run(root, before, NOW)["added"], 1)
            path = root / "catalog-changes.json"
            history = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(history["events"]), 3)
            latest, _, first = history["events"]
            self.assertEqual(latest["transition"], first["transition"])
            self.assertNotEqual(latest["id"], first["id"])
            self.assertEqual([event["sequence"] for event in history["events"]], [3, 2, 1])
            self.assertEqual(changes.run(root, before, NOW + timedelta(hours=1))["added"], 0)
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["events"]), 3)

    def test_recurring_detail_hash_transition_and_source_transition_are_distinct(self):
        for field, first_value, second_value in (("summary", "처음 소개", "변경 소개"), ("koreaReleaseSourceUrl", "https://www.instagram.com/p/FIRST/", "https://www.instagram.com/p/SECOND/")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = root / "before.json"
                a, b = product(**korea()), product(**korea())
                a[field], b[field] = first_value, second_value
                for previous, following in ((a, b), (b, a), (a, b)):
                    before.write_text(json.dumps([previous]), encoding="utf-8")
                    (root / "official-catalog.json").write_text(json.dumps([following]), encoding="utf-8")
                    self.assertEqual(changes.run(root, before, NOW)["added"], 1)
                self.assertEqual(changes.run(root, before, NOW)["added"], 0)
                history = json.loads((root / "catalog-changes.json").read_text(encoding="utf-8"))
                self.assertEqual(len({event["id"] for event in history["events"]}), 3)

    def test_release_promotion_retry_stays_once_after_other_types_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            old = product(releaseDate="2026-11-02", catalogStatus="UPCOMING")
            new = product(releaseDate="2026-11-02", catalogStatus="ON_SALE")
            before.write_text(json.dumps([old]), encoding="utf-8")
            (root / "official-catalog.json").write_text(json.dumps([new]), encoding="utf-8")
            self.assertEqual(changes.run(root, before, NOW)["added"], 1)
            updated = dict(new, **korea())
            (root / "official-catalog.json").write_text(json.dumps([updated]), encoding="utf-8")
            self.assertEqual(changes.run(root, before, NOW + timedelta(minutes=1))["added"], 1)
            self.assertEqual(changes.run(root, before, NOW + timedelta(days=1))["added"], 0)
            events = json.loads((root / "catalog-changes.json").read_text(encoding="utf-8"))["events"]
            self.assertEqual(sum(event["type"] == "japan-released" for event in events), 1)

    def test_prize_images_and_full_lineup_are_meaningful(self):
        old = product(prizeDetailsPending=True)
        new = product(prizeDetailsPending=False)
        result = changes.detect_changes([old], [new], NOW)
        self.assertEqual(result[0]["type"], "details")
        self.assertEqual(result[0]["after"], "전체 경품 공개")
        self.assertNotRegex(result[0]["before"], "[a-f0-9]{12}")
        new = copy.deepcopy(old)
        new["prizes"][0]["imagePath"] = "/updated.webp"
        self.assertEqual(changes.detect_changes([old], [new], NOW)[0]["type"], "details")

    def test_repeated_run_is_idempotent_and_keeps_observed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            before.write_text(json.dumps([product()]), encoding="utf-8")
            (root / "official-catalog.json").write_text(json.dumps([product(**korea())]), encoding="utf-8")
            self.assertEqual(changes.run(root, before, NOW)["added"], 1)
            first = (root / "catalog-changes.json").read_bytes()
            self.assertEqual(changes.run(root, before, NOW + timedelta(hours=1))["added"], 0)
            self.assertEqual((root / "catalog-changes.json").read_bytes(), first)

    def test_empty_history_starts_without_inventing_past_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            before.write_text(json.dumps([product()]), encoding="utf-8")
            (root / "official-catalog.json").write_text(json.dumps([product()]), encoding="utf-8")
            self.assertEqual(changes.run(root, before, NOW), {"added": 0, "retained": 0})
            history = json.loads((root / "catalog-changes.json").read_text(encoding="utf-8"))
            self.assertEqual(history["events"], [])

    def test_corrupt_history_is_preserved_and_fails_visibly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.json"
            before.write_text(json.dumps([product()]), encoding="utf-8")
            (root / "official-catalog.json").write_text(json.dumps([product(**korea())]), encoding="utf-8")
            path = root / "catalog-changes.json"
            path.write_bytes(b'{"events": []}')
            with self.assertRaisesRegex(ValueError, "preserved"):
                changes.run(root, before, NOW)
            self.assertEqual(path.read_bytes(), b'{"events": []}')

    def test_ambiguous_catalog_rejected_before_history_write(self):
        with self.assertRaises(ValueError):
            changes.detect_changes([product()], [product(), product()], NOW)
        with self.assertRaises(ValueError):
            changes.detect_changes([product()], [], NOW)

    def test_history_is_bounded_deterministically(self):
        events = [changes.create_event(product(), "details", str(index), str(index + 1), "상품 정보 갱신", NOW + timedelta(seconds=index)) for index in range(300)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = {"version": 1, "startedAt": NOW.isoformat(), "updatedAt": NOW.isoformat(), "events": events}
            (root / "catalog-changes.json").write_text(json.dumps(history), encoding="utf-8")
            before = root / "before.json"
            before.write_text(json.dumps([product()]), encoding="utf-8")
            (root / "official-catalog.json").write_text(json.dumps([product(**korea())]), encoding="utf-8")
            changes.run(root, before, NOW + timedelta(hours=1))
            final = json.loads((root / "catalog-changes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(final["events"]), 300)
            self.assertEqual(final["events"][0]["type"], "korea-date")
            self.assertNotIn(events[0]["id"], {event["id"] for event in final["events"]})


if __name__ == "__main__":
    unittest.main()
