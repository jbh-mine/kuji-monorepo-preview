"""Offline factual fixtures from official Korean-account public embeds (2026-11-02).

Only edition titles, sale-date phrases and the footer date are retained. WAKU
dates intentionally differ in fixtures: they must never become official dates.
"""
import copy
import html
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


def announcement(title="드래곤볼 GT", sale="9월 24일(목) 판매 개시합니다!", footer="September 22, 2026", identifier=DB, account=sync.ACCOUNT):
    body = "[신상][이치방쿠지]<br><br>&lt; " + title + " &gt;<br><br>📌" + sale + "<br><br>이치방쿠지 직영점에서 만나요!"
    return {"author_url": "https://x.com/" + account, "author_name": "이치방쿠지",
            "html": '<blockquote class="twitter-tweet"><p lang="ko">' + body + '</p>&mdash;이치방쿠지 <a href="https://x.com/' + account + '/status/' + identifier + '?ref_src=twsrc">' + footer + '</a></blockquote>'}


def listing(rows=None):
    rows = rows or [(DB, "드래곤볼 GT")]
    return ("<main>" + "".join('<a class="kr-row" href="kr/' + identifier + '.html"><div class="kr-name"><div class="kr-title">' + title + '</div><div class="kr-sub">작품 전체</div></div><span class="kr-date">2027-01-01</span></a>' for identifier, title in rows) + "</main>").encode()


class FakeFetcher:
    def __init__(self, rows=None, embeds=None, fail_index=False, sources=None):
        self.rows = rows
        self.embeds = embeds if embeds is not None else {DB: announcement()}
        self.fail_index = fail_index
        self.calls = []
        self.accounts = sync.approved_accounts(sources)
        self.registered_urls = {entry["sourceUrl"] for entry in (sync.load_sources() if sources is None else sources)}

    def __call__(self, url):
        sync.checked_url(url, self.accounts, self.registered_urls)
        self.calls.append(url)
        if url == sync.INDEX_URL:
            if self.fail_index:
                raise sync.KoreaReleaseError("http_503")
            return listing(self.rows)
        if sync.instagram_source(url):
            result = self.embeds[url]
            if isinstance(result, Exception):
                raise result
            return result.encode("utf-8")
        identifier = sync.canonical_tweet(sync.urllib.parse.parse_qs(sync.urllib.parse.urlsplit(url).query)["url"][0], self.accounts).rsplit("/", 1)[1]
        result = self.embeds[identifier]
        if isinstance(result, Exception):
            raise result
        return json.dumps(result, ensure_ascii=False).encode()


def instagram_html(body, account="__morammoram", posted="October 28, 2026"):
    description = f'5 likes, 0 comments - {account} on {posted}: "{body}".'
    return '<html><head><meta property="og:description" content="' + html.escape(description, quote=True) + '"></head><body>public post</body></html>'


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
        result = sync.run(self.root, sources=[], fetcher=fetch, now=NOW)
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
        result = sync.run(self.root, sources=[], fetcher=fetch, now=NOW)
        self.assertEqual(result["korea"]["confirmed"], 0)
        self.assertEqual(self.catalog()[0]["koreaReleaseDatePrecision"], "unknown")
        self.assertEqual(len(fetch.calls), 1)

    def test_unofficial_author_is_reported_without_response_body(self):
        self.save()
        embed = announcement()
        embed["author_url"] = "https://x.com/attacker"
        result = sync.run(self.root, sources=[], fetcher=FakeFetcher(embeds={DB: embed}), now=NOW)
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
        result = sync.run(self.root, sources=[], fetcher=FakeFetcher(fail_index=True, embeds={DB: sync.KoreaReleaseError("http_503")}), now=NOW)
        self.assertEqual(self.catalog()[0], before)
        self.assertEqual(result["korea"]["lastSuccessAt"], prior["korea"]["lastSuccessAt"])
        self.assertEqual(result["korea"]["failed"], 2)
        self.assertEqual(result["status"], "partial")

    def test_unknown_new_product_and_existing_japan_failures_are_preserved(self):
        self.save([product("제일복권 미공개 회차")], {"status": "failed", "failed": 3, "errors": [{"message": "Japanese request failure"}]})
        result = sync.run(self.root, sources=[], fetcher=FakeFetcher(), now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed"], 3)
        self.assertEqual(result["korea"]["unconfirmed"], 1)
        self.assertEqual({key: self.catalog()[0][key] for key in sync.FIELDS}, sync.UNKNOWN)

    def test_repeated_country_failures_do_not_accumulate_top_level_failure_count(self):
        self.save()
        for _ in range(2):
            result = sync.run(self.root, sources=[], fetcher=FakeFetcher(embeds={DB: sync.KoreaReleaseError("http_503")}), now=NOW)
            self.assertEqual(result["failed"], 1)

    def test_identical_titles_across_reissues_are_not_matched_by_discovery(self):
        duplicate = copy.deepcopy(product())
        duplicate["code"] = "ANOTHER_EDITION"
        self.save([product(), duplicate])
        fetch = FakeFetcher()
        result = sync.run(self.root, sources=[], fetcher=fetch, now=NOW)
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
        result = sync.run(self.root, sources=[], fetcher=fetch, now=NOW)
        self.assertEqual(result["korea"]["status"], "partial")
        self.assertEqual(result["korea"]["confirmed"], 1)
        self.assertEqual(result["korea"]["unconfirmed"], 2)
        self.assertEqual(len(fetch.calls), 3)


    def test_curated_localized_aliases_are_bound_to_the_exact_japanese_edition(self):
        cases = [
            ("myhero47", "제일복권 나의 히어로 아카데미아 ~최고의 히어로가 되기까지의 이야기~", "2026-10-24", "October 22, 2026"),
            ("hxh11", "제일복권 헌터×헌터 GREED ISLAND ②", "2026-10-23", "October 21, 2026"),
            ("chainsawman3", "제일복권 극장판 체인소맨 레제편 제2탄", "2026-10-16", "October 14, 2026"),
            ("onep104", "제일복권 원피스 엘바프편 GIANT BASH!! Vol.2", "2026-08-29", "August 28, 2026"),
            ("kimetsu30", "제일복권 귀멸의 칼날 ~상현의 이~", "2026-08-16", "August 14, 2026"),
        ]
        registry = sync.load_sources()
        weekdays = {"2026-10-24": "토", "2026-10-23": "금", "2026-10-16": "금", "2026-08-29": "토", "2026-08-16": "일"}
        for slug, title, expected, footer in cases:
            with self.subTest(slug=slug):
                spec = next(entry for entry in registry if entry["productSourceUrl"].endswith("/" + slug))
                identifier = spec["sourceUrl"].rsplit("/", 1)[1]
                day = sync.date.fromisoformat(expected)
                embed = announcement(spec["aliases"][0], f"{day.month}월 {day.day}일({weekdays[expected]}) 판매 개시", footer, identifier)
                self.save([product(title, slug.upper(), slug)])
                result = sync.run(self.root, fetcher=FakeFetcher(embeds={identifier: embed}), now=NOW, sources=[spec])
                self.assertEqual(self.catalog()[0]["koreaReleaseDate"], expected)
                self.assertEqual(result["korea"]["officialConfirmed"], 1)
                self.assertEqual(result["korea"]["retailerConfirmed"], 0)
                self.assertEqual(result["korea"]["status"], "ok")

        hunter = next(entry for entry in registry if entry["productSourceUrl"].endswith("/hxh11"))
        self.save([product("제일복권 헌터×헌터 그리드 아일랜드 ② (재판매)", "RESALE", "hxh11-2")])
        result = sync.run(self.root, fetcher=FakeFetcher(), now=NOW, sources=[hunter])
        self.assertEqual(result["korea"]["confirmed"], 0)

    def test_registry_identity_and_unsafe_sources_are_rejected_before_requesting(self):
        valid = sync.load_sources()[0]
        for patch_values in [
            {"productSourceUrl": "https://1kuji.com/products/myhero47?redirect=evil"},
            {"sourceUrl": "https://x.com/attacker/status/2113235842068275240", "sourceType": "official"},
            {"sourceUrl": "https://x.com.evil.test/ICHIBAN_KUJI_/status/2113235842068275240"},
            {"sourceUrl": 123}, {"dateKind": "registered"}, {"aliases": [""]},
        ]:
            with self.subTest(values=patch_values), self.assertRaises(sync.KoreaReleaseError):
                sync.validate_sources([{**valid, **patch_values}])
        with self.assertRaises(sync.KoreaReleaseError):
            sync.validate_sources([valid, valid])

    def test_registry_title_or_author_drift_is_reported_while_saved_facts_survive(self):
        spec = next(entry for entry in sync.load_sources() if entry["productSourceUrl"].endswith("/myhero47"))
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        record = product("제일복권 나의 히어로 아카데미아 ~최고의 히어로가 되기까지의 이야기~", "MHA", "myhero47")
        correct = announcement(spec["aliases"][0], "10월 24일(토) 판매 개시", "October 22, 2026", identifier)
        record.update(sync.parse_announcement(correct, identifier, record, now=NOW, source_spec=spec))
        self.save([record])
        changed = announcement("나의 히어로 아카데미아 체육대회", "10월 24일(토) 판매 개시", "October 22, 2026", identifier)
        result = sync.run(self.root, fetcher=FakeFetcher(embeds={identifier: changed}), now=NOW, sources=[spec])
        self.assertEqual(self.catalog()[0], record)
        self.assertEqual(result["korea"]["status"], "failed")
        self.assertEqual(result["korea"]["errors"][0]["code"], "registered_release_fact_missing")

    def retailer_spec(self, *, kind="release", slug="hxh11", aliases=None):
        return {"productSourceUrl": "https://1kuji.com/products/" + slug,
                "sourceUrl": "https://x.com/otakuearlyedu/status/2113225828217790761",
                "aliases": aliases or ["헌터X헌터 GREED ISLAND 2"], "sourceType": "retailer",
                "sourceName": "오타쿠 조기교육", "dateKind": kind}

    def test_registered_retailer_opening_date_has_its_own_provenance(self):
        spec = self.retailer_spec()
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        embed = announcement(spec["aliases"][0], "내일 10월 23일 금요일 13시에 오픈합니다!", "October 22, 2026", identifier, "otakuearlyedu")
        self.save([product("제일복권 헌터×헌터 GREED ISLAND ②", "HUNTER", "hxh11")])
        result = sync.run(self.root, fetcher=FakeFetcher(embeds={identifier: embed}, sources=[spec]), now=NOW, sources=[spec])
        saved = self.catalog()[0]
        self.assertEqual(saved["koreaReleaseDate"], "2026-10-23")
        self.assertEqual(saved["koreaReleaseDateKind"], "release")
        self.assertEqual(saved["koreaReleaseSourceName"], "오타쿠 조기교육")
        self.assertEqual(saved["koreaReleaseSourceType"], "retailer")
        self.assertEqual(result["korea"]["retailerConfirmed"], 1)
        self.assertEqual(result["korea"]["officialConfirmed"], 0)

    def test_arrival_requires_an_explicit_arrival_date_and_never_uses_registration_or_restock(self):
        spec = self.retailer_spec(kind="arrival")
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        record = product("제일복권 헌터×헌터 GREED ISLAND ②", "HUNTER", "hxh11")
        for phrase in ["입고되었습니다!", "10월 23일 금요일 13시에 오픈합니다!", "10월 23일(금) 이벤트 안내"]:
            with self.subTest(phrase=phrase):
                embed = announcement(spec["aliases"][0], phrase, "October 22, 2026", identifier, "otakuearlyedu")
                self.assertIsNone(sync.parse_announcement(embed, identifier, record, now=NOW, source_spec=spec))
        embed = announcement(spec["aliases"][0], "10월 23일(금) 신규 입고 완료", "October 23, 2026", identifier, "otakuearlyedu")
        result = sync.parse_announcement(embed, identifier, record, now=NOW, source_spec=spec)
        self.assertEqual(result["koreaReleaseDate"], "2026-10-23")
        self.assertEqual(result["koreaReleaseDateKind"], "arrival")
        with self.assertRaisesRegex(sync.KoreaReleaseError, "restock"):
            sync.parse_announcement(announcement(spec["aliases"][0], "10월 23일(금) 재입고", "October 23, 2026", identifier, "otakuearlyedu"), identifier, record, now=NOW, source_spec=spec)

    def test_retailer_author_and_url_are_checked_against_the_curated_spec(self):
        spec = self.retailer_spec()
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        record = product("제일복권 헌터×헌터 GREED ISLAND ②", "HUNTER", "hxh11")
        wrong_author = announcement(spec["aliases"][0], "10월 23일(금) 판매 개시", "October 22, 2026", identifier)
        with self.assertRaisesRegex(sync.KoreaReleaseError, "author"):
            sync.parse_announcement(wrong_author, identifier, record, now=NOW, source_spec=spec)
        correct = announcement(spec["aliases"][0], "10월 23일(금) 판매 개시", "October 22, 2026", identifier, "otakuearlyedu")
        correct["url"] = "https://x.com/otakuearlyedu/status/2113225828217790762"
        with self.assertRaisesRegex(sync.KoreaReleaseError, "identifier"):
            sync.parse_announcement(correct, identifier, record, now=NOW, source_spec=spec)

    def test_a_retailer_cannot_downgrade_a_verified_official_date(self):
        spec = self.retailer_spec(slug="db_gt", aliases=["드래곤볼 GT"])
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        record = product()
        record.update(sync.parse_announcement(announcement(), DB, record, now=NOW))
        self.save([record])
        retail = announcement("드래곤볼 GT", "10월 23일(금) 판매 개시", "October 22, 2026", identifier, "otakuearlyedu")
        fetch = FakeFetcher(embeds={DB: sync.KoreaReleaseError("http_503"), identifier: retail}, sources=[spec])
        result = sync.run(self.root, fetcher=fetch, now=NOW, sources=[spec])
        self.assertEqual(self.catalog()[0], record)
        self.assertEqual(result["korea"]["officialConfirmed"], 1)
        self.assertEqual(result["korea"]["retailerConfirmed"], 0)
        self.assertEqual(result["korea"]["status"], "partial")

    def test_unregistered_saved_retailer_values_are_not_counted_or_retrieved(self):
        spec = self.retailer_spec()
        identifier = spec["sourceUrl"].rsplit("/", 1)[1]
        record = product("제일복권 헌터×헌터 GREED ISLAND ②", "HUNTER", "hxh11")
        record.update(sync.parse_announcement(announcement(spec["aliases"][0], "10월 23일(금) 판매 개시", "October 22, 2026", identifier, "otakuearlyedu"), identifier, record, now=NOW, source_spec=spec))
        self.save([record])
        fetch = FakeFetcher()
        result = sync.run(self.root, fetcher=fetch, now=NOW, sources=[])
        self.assertEqual(result["korea"]["confirmed"], 0)
        self.assertEqual(result["korea"]["status"], "failed")
        self.assertEqual(len(fetch.calls), 1)


    def instagram_specs(self):
        return [entry for entry in sync.load_sources() if sync.instagram_source(entry["sourceUrl"])]

    def test_instagram_calendar_dates_are_bound_to_the_target_product_line(self):
        specs = [entry for entry in self.instagram_specs() if entry["sourceAccount"] == "__morammoram"]
        body = "11월 모람모람 일정 캘린더\n☑️ 11월21일 나루토VS히단 카쿠즈편 제일복권 (정발판)\n☑️ 11월27일 원피스 -하늘에서 울려퍼지는 러브송- 제일복권 (정발판)\n모든 일정은 물류 사정으로 변경될 수 있습니다.\n11월06일 동물의숲 입고 보류"
        records = [product("제일복권 나루토 질풍전 히단·카쿠즈편", "NARUTO", "naruto11"),
                   product("제일복권 원피스 하늘에 울려퍼지는 섬의 노랫소리", "ONE_PIECE", "onep_o12")]
        self.save(records)
        fetcher = FakeFetcher(embeds={specs[0]["sourceUrl"]: instagram_html(body)}, sources=specs)
        result = sync.run(self.root, fetcher=fetcher, now=NOW, sources=specs)
        self.assertEqual([entry["koreaReleaseDate"] for entry in self.catalog()], ["2026-11-21", "2026-11-27"])
        self.assertTrue(all(entry["koreaReleaseDateKind"] == "arrival" for entry in self.catalog()))
        self.assertTrue(all("입고 예정" in entry["koreaReleaseDateText"] for entry in self.catalog()))
        self.assertEqual(result["korea"]["retailerConfirmed"], 2)
        self.assertEqual(result["korea"]["status"], "ok")
        self.assertEqual(len(fetcher.calls), 2)  # One index plus one shared calendar.

    def test_instagram_sale_time_day_uses_published_month_and_matching_weekday(self):
        for spec in (entry for entry in self.instagram_specs() if entry["sourceAccount"] == "thekuhouse"):
            with self.subTest(source=spec["sourceUrl"]):
                slug = spec["productSourceUrl"].rsplit("/", 1)[1]
                record = product("제일복권 " + spec["aliases"][0], slug.upper(), slug)
                body = spec["aliases"][0] + " 판매시작합니다\n\n[ 판매 시작시간 ]\n📅29일(목) ⏰[오후 2시부터]\n\n관심 부탁드립니다"
                fields = sync.parse_instagram(instagram_html(body, "thekuhouse", "October 27, 2026"), record, now=NOW, source_spec=spec)
                self.assertEqual(fields["koreaReleaseDate"], "2026-10-29")
                self.assertEqual(fields["koreaReleaseDateKind"], "release")
                self.assertEqual(fields["koreaReleaseSourceName"], "더쿠하우스 국전점")

    def test_instagram_registration_date_bare_arrival_and_unrelated_dates_never_confirm(self):
        spec = next(entry for entry in self.instagram_specs() if entry["productSourceUrl"].endswith("/db_z8"))
        record = product("제일복권 드래곤볼 SPIRIT OF THE SAIYAN", "SAIYAN", "db_z8")
        for body in ["드래곤볼 SPIRIT OF THE SAIYAN 입고되었습니다", "드래곤볼 SPIRIT OF THE SAIYAN 금일 6시 오픈할게요", "드래곤볼 SPIRIT OF THE SAIYAN\n10월29일(목) 이벤트 안내"]:
            self.assertIsNone(sync.parse_instagram(instagram_html(body, "thekuhouse"), record, now=NOW, source_spec=spec))
        calendar = next(entry for entry in self.instagram_specs() if entry["productSourceUrl"].endswith("/naruto11"))
        naruto = product("제일복권 나루토 히단 카쿠즈편", "NARUTO", "naruto11")
        body = "11월 일정 캘린더\n나루토VS히단 카쿠즈편 제일복권\n11월27일 다른상품 입고 예정"
        self.assertIsNone(sync.parse_instagram(instagram_html(body), naruto, now=NOW, source_spec=calendar))

    def test_instagram_owner_year_weekday_and_target_cancellations_are_validated(self):
        spec = next(entry for entry in self.instagram_specs() if entry["productSourceUrl"].endswith("/naruto11"))
        record = product("제일복권 나루토 히단 카쿠즈편", "NARUTO", "naruto11")
        body = "11월 일정 캘린더\n11월21일 나루토VS히단 카쿠즈편 제일복권"
        for document in [instagram_html(body, "unregistered_store"), instagram_html(body, posted="October 28, 2027"), instagram_html(body, posted="October 28")]:
            with self.assertRaises(sync.KoreaReleaseError):
                sync.parse_instagram(document, record, now=NOW, source_spec=spec)
        with self.assertRaisesRegex(sync.KoreaReleaseError, "schedule_changed"):
            sync.parse_instagram(instagram_html(body + " 입고 보류"), record, now=NOW, source_spec=spec)
        with self.assertRaisesRegex(sync.KoreaReleaseError, "ambiguous"):
            sync.parse_instagram(instagram_html(body + "\n11월22일 나루토VS히단 카쿠즈편"), record, now=NOW, source_spec=spec)
        sale = next(entry for entry in self.instagram_specs() if entry["productSourceUrl"].endswith("/db_z8"))
        with self.assertRaises(sync.KoreaReleaseError):
            sync.parse_instagram(instagram_html("드래곤볼 SPIRIT OF THE SAIYAN 판매시작\n[판매 시작시간]\n29일(금) 오후2시", "thekuhouse", "October 27, 2026"), product("드래곤볼 SPIRIT OF THE SAIYAN", "DB", "db_z8"), now=NOW, source_spec=sale)

    def test_only_curated_instagram_posts_can_be_requested_or_followed(self):
        spec = self.instagram_specs()[0]
        self.assertEqual(sync.checked_url(spec["sourceUrl"]), spec["sourceUrl"])
        for url in ["https://www.instagram.com/p/NOT_REGISTERED/", "https://www.instagram.com/accounts/login/", "https://instagram.com/p/DfB961xh-Kp/", spec["sourceUrl"] + "?session=secret"]:
            with self.assertRaises(sync.KoreaReleaseError):
                sync.checked_url(url)
        with self.assertRaises(sync.KoreaReleaseError):
            sync.SafeRedirects().redirect_request(sync.urllib.request.Request(spec["sourceUrl"]), None, 302, "", {}, "https://www.instagram.com/accounts/login/")
        with self.assertRaises(sync.KoreaReleaseError):
            sync.validate_sources([{**spec, "sourceAccount": "bad/author"}])

    def test_instagram_sale_changes_and_conflicting_time_blocks_preserve_prior_facts(self):
        spec = next(entry for entry in self.instagram_specs() if entry["productSourceUrl"].endswith("/db_z8"))
        record = product("제일복권 드래곤볼 SPIRIT OF THE SAIYAN", "DB", "db_z8")
        body = "드래곤볼 SPIRIT OF THE SAIYAN 판매시작합니다\n[판매 시작시간]\n29일(목) 오후2시\n\n"
        for change in ["판매 일정이 연기되었습니다", "판매취소 안내", "판매 일정이 변경되었습니다"]:
            with self.subTest(change=change), self.assertRaisesRegex(sync.KoreaReleaseError, "schedule_changed"):
                sync.parse_instagram(instagram_html(body + change, "thekuhouse", "October 27, 2026"), record, now=NOW, source_spec=spec)
        policy = sync.parse_instagram(instagram_html(body + "구매 후 판매취소불가. 판매 일정은 변경될 수 있습니다.", "thekuhouse", "October 27, 2026"), record, now=NOW, source_spec=spec)
        self.assertEqual(policy["koreaReleaseDate"], "2026-10-29")
        with self.assertRaisesRegex(sync.KoreaReleaseError, "ambiguous"):
            sync.parse_instagram(instagram_html("드래곤볼 SPIRIT OF THE SAIYAN 판매시작\n[판매 시작시간]\n29일(목), 30일(금)", "thekuhouse", "October 27, 2026"), record, now=NOW, source_spec=spec)


if __name__ == "__main__":
    unittest.main()
