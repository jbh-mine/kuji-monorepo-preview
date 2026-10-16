"""Offline tests: compact fixtures preserve the selectors and facts of official HTML.

The chain-saw product headings, dates, prices and first poster/prize URLs below
come from https://1kuji.com/products/chainsawman3 (checked 2026-10-16).
The partial C/D announcement is from https://1kuji.com/products/deathnote2.
Only public factual fields are retained; promotional prose and tracking scripts
are deliberately omitted. Tests never access a network or a real user database.
"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "catalog_sync.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("catalog_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

# Catalog fixtures exercise the full translation path without external services.
# Unknown terms deliberately stay marked pending; glossary terms are Korean.
_online_translator = sync.KoreanTranslator
sync.KoreanTranslator = lambda *args, **kwargs: _online_translator(*args, max_requests=0, **kwargs)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=sync.JST)
URL = "https://1kuji.com/products/chainsawman3"
POSTER = "https://assets.1kuji.com/uploads/product/top_banner/10738/a9d03dca-75b7-4bf6-bd54-ccc832f5dd36.webp"
FIRST_IMAGE = "https://assets.1kuji.com/uploads/product_item/image/106448/dd5b4752-138f-4687-bfb7-c09d67e40d70.webp"
HEADINGS = [
    ("A", "デンジ＆レゼ フィギュア"), ("B", "レゼ MASTERLISE"),
    ("C", "ちょこのっこフィギュアセット"), ("D", "描きおろしビジュアルボード～デンジ＆レゼ～"),
    ("E", "描きおろしビジュアルボード～早川アキ＆天使の悪魔～"),
    ("F", "きゅんキャラアクリルスタンド"), ("G", "ホログラムステッカーセット"),
    ("H", "クリアポスター"), ("I", "ラバーコースター"),
    ("LAST", "チェンソーマン＆ビーム MEGAIMPACT スペシャルカラーver."),
]
# A minimal recognizable WebP container is sufficient for magic-byte validation;
# actual image decode/rendering belongs to browser/product visual QA.
IMAGE = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 40


def page(title="一番くじ 劇場版『チェンソーマン レゼ篇』第2弾", release="2026年09月18日(金)より順次発売予定",
         headings=None, price="1回880円(税10％込)", online="2026年09月18日(金)11:00より販売開始予定"):
    headings = HEADINGS if headings is None else headings
    blocks = []
    for letter, name in headings:
        label = f"{letter}賞" if letter != "LAST" else "ラストワン賞"
        image = FIRST_IMAGE if letter == "A" else f"https://assets.1kuji.com/uploads/product_item/image/fixture/{letter.lower()}.webp"
        blocks.append(f'''<div class="itemColList"><h4 class="name sp">{label} {name}</h4>
          <div class="itemColGallery"><ul class="slider-item slider-images"><li><img src="{image}"></li></ul></div>
          <div class="itemColDetail"><h4 class="name pc">{label} {name}</h4><ul class="data">
          <li>■全1種</li><li>■サイズ：約10cm（台座込み 約12cm）</li></ul></div></div>''')
    return f'''<!doctype html><main><section class="mvCol"><img src="{POSTER}"></section>
       <section id="aboutCol"><h2>{title}</h2><div class="detail glBox"><ul>
       <li>■発売日：<br>店頭販売：{release}<br>オンライン販売：{online}</li>
       <li>■メーカー希望小売価格：{price}</li><li>■取扱店：ローソン、書店、一番くじONLINEなど</li></ul></div></section>
       <section id="listCol">{''.join(blocks)}</section>
       <section id="wcampaignsCol"><p class="imgCol"><img src="https://assets.1kuji.com/uploads/campaign/image/fixture/dc.webp"></p>
       <p class="itemName">チェンソーマン＆ビーム MEGAIMPACT スペシャルカラーver.</p><p>■当選数：50個</p><p class="limit">発売日～2026年12月末日</p></section>
       <p class="productCopy">© 2025 MAPPA／チェンソーマンプロジェクト ©藤本タツキ／集英社</p></main>'''


GOLDEN_PAGE = page()
PARTIAL_ANNOUNCEMENT = page(title="一番くじ DEATH NOTE 2", release="2027年01月16日(土)より順次発売予定",
                            headings=[("C", "ニア フィギュア"), ("D", "メロ フィギュア")], price="1回850円(税10％込)", online="")
LISTING = '''<main><ul class="itemList">
  <li><a href="/products/chainsawman3"><p class="itemName">一番くじ 劇場版『チェンソーマン レゼ篇』第2弾</p></a></li>
  <li><a href="/products/deathnote2"><p class="itemName">一番くじ DEATH NOTE 2</p></a></li>
  <li><a href="/products/unrelated"><p class="itemName">一番くじ 無関係な作品</p></a></li>
  <li><a href="https://attacker.invalid/products/fake"><p>一番くじ NARUTO</p></a></li>
</ul><a href="/products?sale_month=11&amp;sale_year=2026">11月</a>
<a href="/products?sale_year=plan">発売日未定のみ表示</a></main>'''


class FakeFetcher:
    def __init__(self, pages=None, fail_image=None, fail_page=None):
        self.pages = pages or {URL: GOLDEN_PAGE}
        self.fail_image = fail_image
        self.fail_page = fail_page
        self.requests = []

    def __call__(self, url, image=False):
        self.requests.append((url, image))
        if (image and self.fail_image and self.fail_image in url) or (not image and self.fail_page):
            raise sync.CatalogError("Fixture network failure")
        if image:
            return sync.FetchResult(IMAGE, "image/webp")
        if url not in self.pages:
            raise sync.CatalogError("Fixture page unavailable")
        return sync.FetchResult(self.pages[url].encode(), "text/html")


class ParserTests(unittest.TestCase):
    def test_real_markup_title_channels_price_and_lineup(self):
        product = sync.parse_product(GOLDEN_PAGE, URL)
        self.assertEqual(product["productTitle"], "一番くじ 劇場版『チェンソーマン レゼ篇』第2弾")
        self.assertEqual(product["releaseDate"], "2026-09-18")
        self.assertEqual(product["onlineReleaseDate"], "2026-09-18")
        self.assertEqual(product["releaseDatePrecision"], "day")
        self.assertEqual(product["priceJpy"], 880)
        self.assertEqual(product["posterUrl"], POSTER)
        self.assertEqual([item["letter"] for item in product["prizes"]], list("ABCDEFGHI") + ["LAST"])
        self.assertEqual(product["prizes"][0]["name"], "デンジ＆レゼ フィギュア")
        self.assertEqual(product["prizes"][0]["images"], [FIRST_IMAGE])
        self.assertTrue(product["complete"])
        self.assertIsNotNone(product["doubleChance"])
        self.assertEqual(product["doubleChance"]["awardCount"], 50)
        self.assertNotIn("ダブルチャンス", " ".join(item["name"] for item in product["prizes"]))

    def test_exact_japanese_prize_characters_are_not_transliterated(self):
        raw = page(headings=[("A", "フィギュア ② Ｖｏｌ.２"), ("LAST", "最後 Ⅱ")])
        parsed = sync.parse_product(raw, URL)
        self.assertEqual(parsed["prizes"][0]["name"], "フィギュア ② Ｖｏｌ.２")
        self.assertEqual(parsed["prizes"][1]["name"], "最後 Ⅱ")

    def test_official_parenthesized_selection_labels_are_detected(self):
        blind = GOLDEN_PAGE.replace("■全1種", "■全24種（選べない）")
        choose = GOLDEN_PAGE.replace("■全1種", "■全24種（選べる）")
        self.assertEqual(sync.parse_product(blind, URL)["prizes"][0]["selection"], "blind")
        self.assertEqual(sync.parse_product(choose, URL)["prizes"][0]["selection"], "choose")

    def test_partial_real_announcement_never_invents_missing_grades(self):
        parsed = sync.parse_product(PARTIAL_ANNOUNCEMENT, "https://1kuji.com/products/deathnote2")
        product = sync.merge_product(parsed, None, "https://1kuji.com/products/deathnote2", NOW)
        self.assertEqual([prize["letter"] for prize in product["prizes"]], ["C", "D"])
        self.assertTrue(product["prizeDetailsPending"])
        self.assertTrue(all(prize["sampleCount"] == 0 for prize in product["prizes"]))
        self.assertEqual(product["catalogStatus"], "UPCOMING")
        self.assertEqual(product["nameKo"], "一番くじ DEATH NOTE 2")

    def test_unannounced_date_and_price_are_not_filled_with_defaults(self):
        parsed = sync.parse_product(page(release="発売日未定", headings=[], price="未定", online=""), URL)
        product = sync.merge_product(parsed, None, URL, NOW)
        self.assertEqual(product["releaseDate"], "")
        self.assertEqual(product["releaseDatePrecision"], "unknown")
        self.assertIsNone(product["priceJpy"])
        self.assertEqual(product["prizes"], [])
        self.assertTrue(product["prizeDetailsPending"])

    def test_month_only_release_stays_upcoming_even_after_month_ended(self):
        parsed = sync.parse_product(page(release="2026年09月発売予定"), URL)
        self.assertEqual(parsed["releaseDate"], "")
        self.assertEqual(parsed["releaseDatePrecision"], "month")
        self.assertEqual(sync.catalog_status("", "month", NOW), "UPCOMING")

    def test_changed_japanese_product_title_does_not_reuse_an_old_korean_translation(self):
        old = sync.merge_product(sync.parse_product(GOLDEN_PAGE, URL), None, URL, NOW)
        old["productTitleKo"] = "예전 번역 제목"
        parsed = sync.parse_product(page(title="一番くじ チェンソーマン 新タイトル"), URL)
        refreshed = sync.merge_product(parsed, old, URL, NOW)
        self.assertEqual(refreshed["nameKo"], "一番くじ チェンソーマン 新タイトル")
        self.assertEqual(refreshed["productTitleKo"], "一番くじ チェンソーマン 新タイトル")
        self.assertEqual(refreshed["code"], old["code"])

    def test_release_switches_on_japan_korea_date_not_utc_or_host_date(self):
        before = datetime(2026, 10, 16, 14, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(sync.catalog_status("2026-10-17", "day", before), "UPCOMING")
        self.assertEqual(sync.catalog_status("2026-10-17", "day", after), "ON_SALE")
        self.assertEqual(sync.catalog_status("", "unknown", after), "UPCOMING")

    def test_changed_campaign_name_cannot_retain_another_prizes_human_korean_name(self):
        old = sync.merge_product(sync.parse_product(GOLDEN_PAGE, URL), None, URL, NOW)
        old["doubleChance"]["nameKo"] = "옛 캠페인 경품"
        parsed = sync.parse_product(GOLDEN_PAGE, URL)
        parsed["doubleChance"]["name"] = "レゼ フィギュア"
        updated = sync.merge_product(parsed, old, URL, NOW)
        self.assertNotIn("nameKo", updated["doubleChance"])
        localized = sync.translate_catalog([updated], _online_translator(max_requests=0))[0][0]
        self.assertEqual(localized["doubleChance"]["nameKo"], "레제 피규어")

    def test_missing_size_keeps_exact_japanese_fact_instead_of_korean_value_as_original(self):
        old = sync.merge_product(sync.parse_product(GOLDEN_PAGE, URL), None, URL, NOW)
        old["prizes"][0].update(size="높이 약 10cm", sizeJp="高さ 約10cm")
        parsed = sync.parse_product(GOLDEN_PAGE, URL)
        parsed["prizes"][0]["size"] = ""
        updated = sync.merge_product(parsed, old, URL, NOW)
        self.assertEqual(updated["prizes"][0]["sizeJp"], "高さ 約10cm")

    def test_newly_completed_lineup_refreshes_an_obsolete_undisclosed_summary(self):
        old = sync.merge_product(sync.parse_product(PARTIAL_ANNOUNCEMENT, "https://1kuji.com/products/deathnote2"), None, "https://1kuji.com/products/deathnote2", NOW)
        old["summary"] = "전체 경품 구성은 아직 미공개입니다."
        full_page = page(title="一番くじ DEATH NOTE 2", headings=[("A", "ニア フィギュア"), ("B", "メロ フィギュア"), ("C", "ニア フィギュア"), ("D", "メロ フィギュア"), ("LAST", "メロ フィギュア")])
        updated = sync.merge_product(sync.parse_product(full_page, "https://1kuji.com/products/deathnote2"), old, "https://1kuji.com/products/deathnote2", NOW)
        localized = sync.translate_catalog([updated], _online_translator(max_requests=0))[0][0]
        self.assertFalse(localized["prizeDetailsPending"])
        self.assertNotIn("미공개", localized["summary"])
        self.assertIn("니아 피규어", localized["summary"])

    def test_calendar_discovery_only_accepts_official_allowlisted_product_links(self):
        links, pages = sync.discover_products(LISTING)
        self.assertEqual(links, [URL, "https://1kuji.com/products/deathnote2"])
        self.assertIn("https://1kuji.com/products?sale_year=plan", pages)
        self.assertIn("https://1kuji.com/products?sale_month=11&sale_year=2026", pages)
        self.assertEqual(sync.franchise_for("一番くじ HUNTERxHUNTER")[0], "HUNTER_HUNTER")
        self.assertEqual(sync.franchise_for("一番くじ ONE PIECE")[0], "ONE_PIECE")

    def test_foreign_redirects_non_https_and_path_traversal_are_blocked(self):
        for url in ["http://1kuji.com/products/a", "https://1kuji.com.attacker.invalid/products/a",
                    "https://user:pass@1kuji.com/products/a", "https://1kuji.com:444/products/a",
                    "https://1kuji.com/products/%2e%2e/a", "file:///tmp/file", "https://127.0.0.1/x"]:
            with self.subTest(url=url), self.assertRaises(sync.CatalogError): sync.checked_url(url)
        handler = sync.OfficialRedirects(False)
        with self.assertRaises(sync.CatalogError):
            handler.redirect_request(None, None, 302, "", {}, "https://attacker.invalid/x")
        with tempfile.TemporaryDirectory() as directory:
            for path in ["/series/official/../../outside", "C:/outside.png", "/series/official/slug/../other/a.png", "/series/official\\bad.png"]:
                with self.subTest(path=path), self.assertRaises(sync.CatalogError): sync.safe_asset_path(Path(directory), path)

    def test_truncated_or_missing_prize_image_is_a_parse_failure(self):
        with self.assertRaises(sync.CatalogError): sync.parse_product("<html><title>One</title></html>", URL)
        with self.assertRaises(sync.CatalogError): sync.parse_product(GOLDEN_PAGE.replace(FIRST_IMAGE, ""), URL)
        with self.assertRaises(sync.CatalogError): sync.parse_release("2026年02月31日")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog_path = self.root / "official-catalog.json"
        self.status_path = self.root / "sync-status.json"
        self.original = sync.merge_product(sync.parse_product(GOLDEN_PAGE, URL), None, URL, NOW)
        self.original["code"] = "KEEP_CODE"
        self.original["slug"] = "keep-slug"
        self.original["summary"] = "직접 작성한 한국어 요약"
        self.original["productTitleKo"] = "극장판 체인소맨 레제편 제2탄"
        self.original["prizes"][0]["nameKo"] = "덴지와 레제 피규어"
        self.original["prizes"][0]["description"] = "직접 작성한 상품 설명"
        self.original["checkedAt"] = "2026-10-15"
        self.original["lastSyncedAt"] = "2026-10-15T10:10:00+09:00"
        sync.atomic_json(self.catalog_path, [self.original])

    def tearDown(self):
        self.temporary.cleanup()

    def read_catalog(self):
        return json.loads(self.catalog_path.read_text(encoding="utf-8"))

    def test_success_preserves_identity_korean_content_and_sample_allocation(self):
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(), now=NOW, discover=False)
        saved = self.read_catalog()[0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(saved["code"], "KEEP_CODE")
        self.assertEqual(saved["slug"], "keep-slug")
        self.assertEqual(saved["summary"], self.original["summary"])
        self.assertEqual(saved["nameKo"], self.original["productTitleKo"])
        self.assertEqual(saved["prizes"][0]["nameKo"], "덴지와 레제 피규어")
        self.assertEqual(saved["prizes"][0]["description"], "직접 작성한 상품 설명")
        self.assertEqual([p["sampleCount"] for p in saved["prizes"]], [p["sampleCount"] for p in self.original["prizes"]])
        self.assertEqual(sum(p["sampleCount"] for p in saved["prizes"]), 80)
        self.assertEqual(saved["prizes"][-1]["sampleCount"], 0)
        self.assertTrue(saved["sampleAllocation"])
        self.assertEqual(saved["checkedAt"], "2026-10-16")
        self.assertTrue(all(sync.safe_asset_path(self.root, path).exists() for _, path in sync.image_refs(saved)))

    def test_store_only_page_preserves_known_online_release_from_additional_official_source(self):
        self.original["onlineReleaseDate"] = "2026-09-21"
        sync.atomic_json(self.catalog_path, [self.original])
        store_only = page(online="").replace("<br>オンライン販売：", "")
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher({URL: store_only}), now=NOW, discover=False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.read_catalog()[0]["onlineReleaseDate"], "2026-09-21")

    def test_page_failure_preserves_every_saved_field_and_reports_health(self):
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(fail_page=True), now=NOW, discover=False)
        self.assertEqual(self.read_catalog(), [self.original])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["checked"], 1)
        self.assertTrue(self.status_path.exists())
        self.assertEqual(self.read_catalog()[0]["checkedAt"], "2026-10-15")

    def test_one_missing_asset_rolls_back_the_entire_product(self):
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(fail_image="/b.webp"), now=NOW, discover=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.read_catalog(), [self.original])
        self.assertEqual(list(self.root.glob("series/official/**/*")), [])

    def test_partial_page_cannot_erase_previously_known_full_lineup(self):
        partial = page(headings=HEADINGS[:2])
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher({URL: partial}), now=NOW, discover=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.read_catalog(), [self.original])
        self.assertIn("partial", result["errors"][0]["message"])

    def test_image_url_changes_create_new_files_without_overwriting_old_snapshot_paths(self):
        old_image = self.original["prizes"][0]["imagePath"]
        old_path = sync.safe_asset_path(self.root, old_image)
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(IMAGE)
        replacement = GOLDEN_PAGE.replace(FIRST_IMAGE, "https://assets.1kuji.com/uploads/product_item/image/new/replacement.webp")
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher({URL: replacement}), now=NOW, discover=False)
        self.assertEqual(result["status"], "ok")
        new_image = self.read_catalog()[0]["prizes"][0]["imagePath"]
        self.assertNotEqual(new_image, old_image)
        self.assertEqual(old_path.read_bytes(), IMAGE)
        self.assertTrue(sync.safe_asset_path(self.root, new_image).exists())

    def test_discovery_adds_partial_future_product_without_creating_fake_lottery_counts(self):
        listing = LISTING.split("</ul>", 1)[0] + "</ul></main>"
        fetcher = FakeFetcher({URL: GOLDEN_PAGE, "https://1kuji.com/products": listing,
                               "https://1kuji.com/products/deathnote2": PARTIAL_ANNOUNCEMENT})
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=fetcher, now=NOW)
        saved = self.read_catalog()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["catalogStatus"], "UPCOMING")
        self.assertEqual(saved[1]["introducedRevision"], 4)
        self.assertTrue(saved[1]["prizeDetailsPending"])
        self.assertEqual(sum(p["sampleCount"] for p in saved[1]["prizes"]), 0)
        first_code = saved[1]["code"]
        sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=fetcher, now=NOW)
        self.assertEqual(self.read_catalog()[1]["code"], first_code)
        self.assertEqual(len(self.read_catalog()), 2)

    def test_discovery_failure_still_refreshes_known_products_and_reports_partial(self):
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(), now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.read_catalog()[0]["checkedAt"], "2026-10-16")

    def test_daily_addition_cap_and_deferred_count(self):
        links = "".join(f'<a href="/products/new{i}">一番くじ NARUTO 第{i}弾</a>' for i in range(12))
        pages = {URL: GOLDEN_PAGE, "https://1kuji.com/products": links}
        pages.update({f"https://1kuji.com/products/new{i}": page(title=f"一番くじ NARUTO 第{i}弾") for i in range(12)})
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(pages), now=NOW, max_new=8)
        self.assertEqual(result["added"], 8)
        self.assertEqual(result["discovery"]["deferred"], 4)
        self.assertEqual(len(self.read_catalog()), 9)

    def test_failed_candidate_does_not_consume_the_eight_successful_addition_budget(self):
        links = '<a href="/products/bad">一番くじ NARUTO</a>' + "".join(f'<a href="/products/new{i}">一番くじ NARUTO 第{i}弾</a>' for i in range(9))
        pages = {URL: GOLDEN_PAGE, "https://1kuji.com/products": links, "https://1kuji.com/products/bad": "<html>truncated</html>"}
        pages.update({f"https://1kuji.com/products/new{i}": page(title=f"一番くじ NARUTO 第{i}弾") for i in range(9)})
        result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(pages), now=NOW, max_new=8)
        self.assertEqual(result["added"], 8)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["discovery"]["deferred"], 1)

    def test_non_image_or_oversized_asset_does_not_replace_saved_product(self):
        for data, content_type in [(b"<html>error</html>", "text/html"), (b"x" * (sync.MAX_IMAGE_BYTES + 1), "image/webp")]:
            fetcher = FakeFetcher()
            original_fetch = fetcher.__call__
            def fetch(url, image=False):
                return sync.FetchResult(data, content_type) if image else original_fetch(url, image=False)
            with self.subTest(content_type=content_type):
                result = sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=fetch, now=NOW, discover=False)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.read_catalog(), [self.original])

    def test_dry_run_does_not_change_catalog_status_or_assets(self):
        before = self.catalog_path.read_bytes()
        sync.sync_catalog(self.root, self.catalog_path, self.status_path, fetch=FakeFetcher(), now=NOW, discover=False, dry_run=True)
        self.assertEqual(self.catalog_path.read_bytes(), before)
        self.assertFalse(self.status_path.exists())
        self.assertFalse((self.root / "series").exists())

    def test_gallery_is_bounded_without_dropping_any_primary_prize_image(self):
        parsed = sync.parse_product(GOLDEN_PAGE, URL)
        for prize in parsed["prizes"]:
            prize["images"] += [f'https://assets.1kuji.com/detail/{prize["letter"]}-{i}.webp' for i in range(8)]
        parsed["galleryUrls"] = [f'https://assets.1kuji.com/gallery/{i}.webp' for i in range(10)]
        product = sync.merge_product(parsed, self.original, URL, NOW)
        self.assertEqual(len(product["prizes"]), len(self.original["prizes"]))
        self.assertTrue(all(len(prize["gallery"]) <= 2 and prize["imageUrl"] for prize in product["prizes"]))
        self.assertEqual(len(product["gallery"]), 4)

    def test_verified_large_official_poster_fits_the_image_bound(self):
        self.assertGreater(sync.MAX_IMAGE_BYTES, 13_217_332)


if __name__ == "__main__":
    unittest.main()
