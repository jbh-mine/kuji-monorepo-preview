"""Offline factual fixtures from official Korean-account public embeds (2026-11-02).

Only edition titles, sale-date phrases and the footer date are retained. WAKU
dates intentionally differ in fixtures: they must never become official dates.
"""
import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "korea_release_sync.py"
SPEC = importlib.util.spec_from_file_location("korea_release_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=sync.KST)
DB = "2102270576467521960"
TITAN = "2115728363193340045"
JUJUTSU = "2112464678421184910"


def product(title="제일복권 드래곤볼 GT", code="DRAGON_BALL", source="db_gt"):
    return {"code": code, "productTitleKo": title, "productTitle": "一番くじ ドラゴンボールGT",
            "nameKo": title, "sourceUrl": "https://1kuji.com/products/" + source,
            "releaseDate": "2026-08-22", "posterPath": "/series/official/db/poster.webp",
            "prizes": [{"letter": "A", "imagePath": "/series/official/db/a.webp"}],
            "administratorField": {"keep": True}}


def announcement(title="드래곤볼 GT", sale="9월 24일(목) 판매 개시합니다!", footer="September 22, 2026", identifier=DB):
    body = "[신상][이치방쿠지]<br><br>&lt; " + title + " &gt;<br><br>📌" + sale + "<br><br>이치방쿠지 직영점에서 만나요!"
    return {"author_url": "https://x.com/ICHIBAN_KUJI_", "author_name": "이치방쿠지",
            "html": '<blockquote class="twitter-tweet"><p lang="ko">' + body + '</p>&mdash;이치방쿠지 <a href="https://x.com/ICHIBAN_KUJI_/status/' + identifier + '?ref_src=twsrc">' + footer + '</a></blockquote>'}


def listing(rows=None):
    rows = rows or [(DB, "드래곤볼 GT")]
    return ("<main>" + "".join('<a class="kr-row" href="kr/' + identifier + '.html"><div class="kr-name"><div class="kr-title">' + title + '</div><div class="kr-sub">작품 전체</div></div><span class="kr-date">2027-01-01</span></a>' for identifier, title in rows) + "</main>").encode()


class FakeFetcher:
    def __init__(self, rows=None, embeds=None, fail_index=False):
        self.rows = rows
        self.embeds = embeds if embeds is not None else {DB: announcement()}
        self.fail_index = fail_index
        self.calls = []

    def __call__(self, url):
        sync.checked_url(url)
        self.calls.append(url)
        if url == sync.INDEX_URL:
            if self.fail_index:
                raise sync.KoreaReleaseError("http_503")
            return listing(self.rows)
        identifier = sync.canonical_tweet(sync.urllib.parse.parse_qs(sync.urllib.parse.urlsplit(url).query)["url"][0]).rsplit("/", 1)[1]
        result = self.embeds[identifier]
        if isinstance(result, Exception):
            raise result
        return json.dumps(result, ensure_ascii=False).encode()


class URLTests(unittest.TestCase):
    def test_only_exact_official_account_status_sources_are_accepted(self):
        self.assertEqual(sync.canonical_tweet(f"https://twitter.com/ICHIBAN_KUJI_/status/{DB}?ref_src=test"), f"https://x.com/ICHIBAN_KUJI_/status/{DB}")
        for url in (f"http://x.com/ICHIBAN_KUJI_/status/{DB}", f"https://x.com/another/status/{DB}", f"https://x.com.evil.test/ICHIBAN_KUJI_/status/{DB}", f"https://u:p@x.com/ICHIBAN_KUJI_/status/{DB}", f"https://x.com:444/ICHIBAN_KUJI_/status/{DB}", f"https://x.com/ICHIBAN_KUJI_/status/{DB}/photo/1"):
            with self.subTest(url=url), self.assertRaises(sync.KoreaReleaseError):
                sync.canonical_tweet(url)

    def test_request_allowlist_rejects_arbitrary_queries_paths_and_hosts(self):
        self.assertEqual(sync.checked_url(sync.INDEX_URL), sync.INDEX_URL)
        self.assertTrue(sync.embed_url(DB).startswith("https://publish.x.com/oembed?"))
        self.assertEqual(sync.checked_url(sync.embed_url(DB)), sync.embed_url(DB))
        for url in ("https://wakuwaku.app/kr?token=secret", "https://wakuwaku.app/ip/174.html", "https://publish.twitter.com/oembed?url=https://evil.test/", "https://publish.twitter.com/oembed?url=https://twitter.com/another/status/123&omit_script=true&dnt=true", sync.embed_url(DB) + "&url=https://evil.test/", "https://publish.twitter.com/%6fembed", "https://127.0.0.1/kr", "https://wakuwaku.app/kr#fragment"):
            with self.subTest(url=url), self.assertRaises(sync.KoreaReleaseError):
                sync.checked_url(url)

    def test_redirect_is_rejected_before_following_an_unapproved_host(self):
        request = sync.urllib.request.Request(sync.INDEX_URL)
        with self.assertRaises(sync.KoreaReleaseError):
            sync.SafeRedirects().redirect_request(request, None, 302, "", {}, "http://127.0.0.1/secret")

    def test_official_embed_rebrand_redirect_is_checked_and_counts_against_budget(self):
        url = sync.embed_url(DB).replace("publish.x.com", "publish.twitter.com")
        redirected = url.replace("publish.twitter.com", "publish.x.com")
        self.assertEqual(sync.checked_url(redirected), redirected)
        fetcher = sync.Fetcher(max_requests=1)
        fetcher.reserve()
        with self.assertRaisesRegex(sync.KoreaReleaseError, "budget"):
            sync.SafeRedirects(fetcher.reserve).redirect_request(sync.urllib.request.Request(url), None, 301, "", {}, redirected)
        target = sync.SafeRedirects().redirect_request(sync.urllib.request.Request(url), None, 301, "", {}, redirected)
        self.assertEqual(target.full_url, redirected)

    def test_request_and_time_budgets_stop_without_network(self):
        fetcher = sync.Fetcher(max_requests=1)
        fetcher.requests = 1
        with self.assertRaisesRegex(sync.KoreaReleaseError, "budget"):
            fetcher(sync.INDEX_URL)
        fetcher = sync.Fetcher()
        fetcher.deadline = 0
        with self.assertRaisesRegex(sync.KoreaReleaseError, "budget"):
            fetcher(sync.INDEX_URL)


class AnnouncementTests(unittest.TestCase):
    def test_dragon_ball_gt_uses_official_sale_day_not_japanese_or_discovery_day(self):
        result = sync.parse_announcement(announcement(), DB, product(), now=NOW, discovered_title="드래곤볼 GT")
        self.assertEqual(result["koreaReleaseDate"], "2026-09-24")
        self.assertEqual(result["koreaReleaseDatePrecision"], "day")
        self.assertIn("직영점", result["koreaReleaseDateText"])
        self.assertIn("9월 24일(목) 판매 개시", result["koreaReleaseDateText"])
        self.assertEqual(result["koreaReleaseSourceUrl"], f"https://x.com/ICHIBAN_KUJI_/status/{DB}")

    def test_subtitles_after_angle_bracket_title_are_required(self):
        embed = announcement("진격의 거인", "10월 30일(금) 판매 개시합니다!", "October 29, 2026", TITAN)
        embed["html"] = embed["html"].replace("거인 &gt;<br><br>", "거인 &gt;<br>-심장을 바쳐라-<br><br>")
        correct = product("제일복권 진격의 거인 ~심장을 바쳐라~", "HEARTS", "shingeki16")
        self.assertEqual(sync.parse_announcement(embed, TITAN, correct, now=NOW)["koreaReleaseDate"], "2026-10-30")
        wrong = product("제일복권 진격의 거인 ~선택과 결과~", "CHOICE", "shingeki15")
        self.assertIsNone(sync.parse_announcement(embed, TITAN, wrong, now=NOW))

    def test_animation_prefix_and_punctuation_do_not_hide_exact_edition(self):
        embed = announcement("주술회전", "10월 21일(수) 판매 개시합니다!", "October 20, 2026", JUJUTSU)
        embed["html"] = embed["html"].replace("주술회전 &gt;<br><br>", "주술회전 &gt;<br>~5주년 FINAL!~<br><br>")
        correct = product("제일복권 애니메이션 주술회전 5주년 FINAL!", "JUJUTSU", "jujutsu19")
        self.assertEqual(sync.parse_announcement(embed, JUJUTSU, correct, now=NOW)["koreaReleaseDate"], "2026-10-21")
        self.assertIsNone(sync.parse_announcement(embed, JUJUTSU, product("제일복권 주술회전 회상록"), now=NOW))

    def test_franchise_only_and_missing_product_identity_never_match(self):
        self.assertIsNone(sync.parse_announcement(announcement("드래곤볼"), DB, product(), now=NOW))
        self.assertIsNone(sync.parse_announcement(announcement("드래곤볼"), DB, product("제일복권 드래곤볼"), now=NOW))
        self.assertIsNone(sync.parse_announcement(announcement(), DB, product("제일복권 드래곤볼 SPIRIT OF THE SAIYAN"), now=NOW))
        self.assertIsNone(sync.parse_announcement(announcement(), DB, product(), now=NOW, discovered_title="드래곤볼 SPIRIT OF THE SAIYAN"))

    def test_author_footer_and_publication_year_are_mandatory(self):
        for change in ({"author_url": "https://x.com/unofficial"}, {"author_url": {"url": "https://x.com/ICHIBAN_KUJI_"}}):
            embed = announcement()
            embed.update(change)
            with self.assertRaises(sync.KoreaReleaseError):
                sync.parse_announcement(embed, DB, product(), now=NOW)
        for embed in (announcement(footer="unavailable"), announcement(footer="September 22, 2027"), announcement(identifier=TITAN)):
            with self.assertRaises(sync.KoreaReleaseError):
                sync.parse_announcement(embed, DB, product(), now=NOW)

    def test_date_requires_sale_phrase_and_valid_calendar_weekday(self):
        self.assertIsNone(sync.parse_announcement(announcement(sale="9월 24일(목) 이벤트 안내"), DB, product(), now=NOW))
        for sale in ("9월 24일(금) 판매 개시합니다!", "2월 30일(월) 판매 개시합니다!", "6월 1일 판매 개시합니다!"):
            with self.subTest(sale=sale), self.assertRaises(sync.KoreaReleaseError):
                sync.parse_announcement(announcement(sale=sale), DB, product(), now=NOW)

    def test_year_rollover_uses_nearby_published_year_and_never_japanese_year(self):
        embed = announcement(sale="1월 2일(토) 판매 개시합니다!", footer="December 30, 2026")
        now = datetime(2027, 1, 3, tzinfo=sync.KST)
        self.assertEqual(sync.parse_announcement(embed, DB, product(), now=now)["koreaReleaseDate"], "2027-01-02")

    def test_month_only_announcement_stays_month_precision(self):
        result = sync.parse_announcement(announcement(sale="11월 중 판매 개시 예정", footer="October 30, 2026"), DB, product(), now=NOW)
        self.assertEqual(result["koreaReleaseDate"], "2026-11")
        self.assertEqual(result["koreaReleaseDatePrecision"], "month")

    def test_postponed_or_ambiguous_dates_are_not_published_as_confirmed(self):
        with self.assertRaisesRegex(sync.KoreaReleaseError, "schedule_changed"):
            sync.parse_announcement(announcement(sale="9월 24일 판매 개시 예정이었으나 연기되었습니다."), DB, product(), now=NOW)
        with self.assertRaises(sync.KoreaReleaseError):
            sync.parse_announcement(announcement(sale="9월 24일(목) 판매 개시, 9월 25일(금) 판매 개시"), DB, product(), now=NOW)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def save(self, catalog=None, status=None):
        (self.root / "official-catalog.json").write_text(json.dumps(catalog or [product()], ensure_ascii=False), encoding="utf-8")
        (self.root / "sync-status.json").write_text(json.dumps(status or {"status": "ok", "failed": 0, "lastSuccessAt": "2026-11-02T10:03:31+09:00", "translation": {"pending": 0}}), encoding="utf-8")

    def catalog(self):
        return json.loads((self.root / "official-catalog.json").read_text(encoding="utf-8"))

    def test_validated_release_adds_only_country_fields_and_retains_every_asset(self):
        before = product()
        self.save([before])
        fetch = FakeFetcher()
        result = sync.run(self.root, fetcher=fetch, now=NOW)
        after = self.catalog()[0]
        self.assertEqual({key: value for key, value in after.items() if key not in sync.FIELDS}, before)
        self.assertEqual(after["koreaReleaseDate"], "2026-09-24")
        self.assertEqual(result["korea"]["confirmed"], 1)
        self.assertEqual(result["korea"]["unconfirmed"], 0)
        self.assertEqual(result["korea"]["lastSuccessAt"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(len(fetch.calls), 2)

    def test_discovery_dates_and_franchise_links_never_confirm_a_release(self):
        self.save([product("제일복권 드래곤볼 SPIRIT OF THE SAIYAN")])
        fetch = FakeFetcher()
        result = sync.run(self.root, fetcher=fetch, now=NOW)
        self.assertEqual(result["korea"]["confirmed"], 0)
        self.assertEqual(self.catalog()[0]["koreaReleaseDatePrecision"], "unknown")
        self.assertEqual(len(fetch.calls), 1)

    def test_unofficial_author_is_reported_without_response_body(self):
        self.save()
        embed = announcement()
        embed["author_url"] = "https://x.com/attacker"
        result = sync.run(self.root, fetcher=FakeFetcher(embeds={DB: embed}), now=NOW)
        self.assertEqual(result["korea"]["failed"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["korea"]["errors"][0]["code"], "unverified_official_author")
        self.assertNotIn("attacker", json.dumps(result))

    def test_discovery_and_official_failures_preserve_last_verified_country_values(self):
        before = product()
        before.update(sync.parse_announcement(announcement(), DB, before, now=NOW))
        prior = {"status": "ok", "failed": 0, "korea": {"status": "ok", "failed": 0, "lastSuccessAt": NOW.isoformat(timespec="seconds")}}
        self.save([before], prior)
        result = sync.run(self.root, fetcher=FakeFetcher(fail_index=True, embeds={DB: sync.KoreaReleaseError("http_503")}), now=NOW)
        self.assertEqual(self.catalog()[0], before)
        self.assertEqual(result["korea"]["lastSuccessAt"], prior["korea"]["lastSuccessAt"])
        self.assertEqual(result["korea"]["failed"], 2)
        self.assertEqual(result["status"], "partial")

    def test_unknown_new_product_and_existing_japan_failures_are_preserved(self):
        self.save([product("제일복권 미공개 회차")], {"status": "failed", "failed": 3, "errors": [{"message": "Japanese request failure"}]})
        result = sync.run(self.root, fetcher=FakeFetcher(), now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed"], 3)
        self.assertEqual(result["korea"]["unconfirmed"], 1)
        self.assertEqual({key: self.catalog()[0][key] for key in sync.FIELDS}, sync.UNKNOWN)

    def test_repeated_country_failures_do_not_accumulate_top_level_failure_count(self):
        self.save()
        for _ in range(2):
            result = sync.run(self.root, fetcher=FakeFetcher(embeds={DB: sync.KoreaReleaseError("http_503")}), now=NOW)
            self.assertEqual(result["failed"], 1)

    def test_identical_titles_across_reissues_are_not_matched_by_discovery(self):
        duplicate = copy.deepcopy(product())
        duplicate["code"] = "ANOTHER_EDITION"
        self.save([product(), duplicate])
        fetch = FakeFetcher()
        result = sync.run(self.root, fetcher=fetch, now=NOW)
        self.assertEqual(result["korea"]["confirmed"], 0)
        self.assertEqual(result["korea"]["unconfirmed"], 2)
        self.assertEqual(len(fetch.calls), 1)

    def test_invalid_saved_calendar_or_nonofficial_source_never_counts_as_confirmed(self):
        for change in ({"koreaReleaseDate": "2026-02-30"}, {"koreaReleaseSourceUrl": "https://x.com/unofficial/status/" + DB}, {"koreaReleaseCheckedAt": "2026-11-02"}):
            record = product()
            record.update(sync.parse_announcement(announcement(), DB, record, now=NOW))
            record.update(change)
            self.assertFalse(sync.has_verified_saved_fields(record))

    def test_one_verified_product_survives_another_failed_request_and_cached_ids_are_reused(self):
        second = product("제일복권 진격의 거인 ~심장을 바쳐라~", "TITAN", "shingeki16")
        duplicate = product("제일복권 드래곤볼 SPIRIT OF THE SAIYAN", "SAIYAN", "db_z8")
        duplicate["koreaReleaseSourceUrl"] = f"https://x.com/ICHIBAN_KUJI_/status/{DB}"
        self.save([product(), duplicate, second])
        fetch = FakeFetcher(rows=[(DB, "드래곤볼 GT"), (TITAN, "진격의 거인 심장을 바쳐라")], embeds={DB: announcement(), TITAN: sync.KoreaReleaseError("http_503")})
        result = sync.run(self.root, fetcher=fetch, now=NOW)
        self.assertEqual(result["korea"]["status"], "partial")
        self.assertEqual(result["korea"]["confirmed"], 1)
        self.assertEqual(result["korea"]["unconfirmed"], 2)
        self.assertEqual(len(fetch.calls), 3)


if __name__ == "__main__":
    unittest.main()
