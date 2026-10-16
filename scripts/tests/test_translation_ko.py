"""Deterministic translation tests; never send user data or use a live network."""
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translation_ko import (KoreanTranslator, TranslationError, TranslationRedirects,
                            JAPANESE, korean, translate_catalog, load_cache, save_json, GLOSSARY_VERSION)


def product():
    return {"code": "AUTO_KIMETSU31", "franchiseCode": "DEMON_SLAYER",
            "productTitle": "一番くじ 鬼滅の刃 ～兄妹の鬼～", "nameKo": "一番くじ 鬼滅の刃 ～兄妹の鬼～",
            "productTitleKo": "一番くじ 鬼滅の刃 ～兄妹の鬼～", "nameJp": "一番くじ 鬼滅の刃 ～兄妹の鬼～",
            "summary": "공식 상품 페이지에 공개된 일본어 정식 상품명과 경품 정보를 표시합니다.",
            "prizeDetailsPending": True, "releaseDateText": "2027年01月16日(土)より順次発売予定",
            "age": "15才以上", "prizes": [{"letter": "A", "name": "新しい形のフィギュア", "nameKo": "新しい形のフィギュア",
                "size": "高さ 約10cm", "variants": "총 1종", "description": "공식 공개 경품. 높이 約10cm", "sampleCount": 0}]}


class TranslationTests(unittest.TestCase):
    def test_public_logo_award_and_vehicle_figure_have_korean_names_without_network(self):
        translator = KoreanTranslator(request=lambda text: self.fail("Known official product terms need no request"))
        for source, expected in [
            ("TO BE CONTINUED THE GIGANT NAME", "투 비 컨티뉴드 대형 로고 조형물"),
            ("孫悟空&ブルマ&ウーロン 牛魔王の車フィギュア", "손오공&부르마&오룡 우마왕의 자동차 피규어"),
        ]:
            value, pending = translator.translate(source, "공개 경품", require_korean=True)
            self.assertEqual(value, expected)
            self.assertFalse(pending)
        self.assertEqual(translator.requests, 0)

    def test_glossary_preserves_familiar_names_and_real_originals(self):
        source = product()
        translator = KoreanTranslator(request=lambda text: "새로운 형태의 피규어")
        result, status = translate_catalog([source], translator)
        item = result[0]
        self.assertEqual(item["nameKo"], "제일복권 귀멸의 칼날 ～남매의 오니～")
        self.assertEqual(item["nameJp"], source["productTitle"])
        self.assertEqual(item["prizes"][0]["name"], "新しい形のフィギュア")
        self.assertEqual(item["prizes"][0]["nameKo"], "새로운 형태의 피규어")
        self.assertEqual(item["prizes"][0]["size"], "높이 약 10cm")
        self.assertEqual(item["prizes"][0]["sizeJp"], "高さ 約10cm")
        self.assertEqual(item["age"], "15세 이상")
        self.assertEqual(item["releaseDateText"], "2027년 01월 16일(토)부터 순차 발매 예정")
        self.assertFalse(item["translationPending"])
        self.assertEqual(status["requests"], 1)
        self.assertNotIn("translationGenerated", source)  # Inputs are never mutated.
        self.assertFalse(JAPANESE.search(item["prizes"][0]["description"]))

    def test_human_korean_titles_and_descriptions_are_preserved(self):
        source = product()
        source.update(productTitleKo="제일복권 귀멸의 칼날 남매", nameKo="제일복권 귀멸의 칼날 남매", summary="직접 작성한 한국어 상품 소개입니다.", descriptionHtml="<p>직접 작성한 소개</p>")
        source["prizes"][0].update(nameKo="직접 번역한 피규어", description="사람이 검수한 상세 설명입니다.")
        result, status = translate_catalog([source], KoreanTranslator(request=lambda text: self.fail("Human Korean must not be submitted")))
        self.assertEqual(result[0]["nameKo"], source["nameKo"])
        self.assertEqual(result[0]["summary"], source["summary"])
        self.assertEqual(result[0]["descriptionHtml"], source["descriptionHtml"])
        self.assertEqual(result[0]["prizes"][0]["description"], source["prizes"][0]["description"])
        self.assertEqual(status["requests"], 0)

    def test_cache_reuses_public_translation_and_invalidates_changed_source(self):
        calls = []
        translator = KoreanTranslator(request=lambda text: calls.append(text) or "새로운 형태의 피규어")
        first, _ = translate_catalog([product()], translator)
        second = KoreanTranslator(translator.cache, request=lambda text: self.fail("Cache was ignored"))
        again, status = translate_catalog(first, second)
        self.assertEqual(status["cacheHits"], 1)
        self.assertEqual(status["requests"], 0)
        self.assertEqual(again[0]["prizes"][0]["nameKo"], "새로운 형태의 피규어")
        changed = copy.deepcopy(first)
        changed[0]["prizes"][0]["name"] = "別の形のフィギュア"
        result, status = translate_catalog(changed, KoreanTranslator(translator.cache, request=lambda text: "다른 형태의 피규어"))
        self.assertEqual(status["requests"], 1)
        self.assertEqual(result[0]["prizes"][0]["nameKo"], "다른 형태의 피규어")

    def test_outage_uses_honest_korean_fallback_then_retries_next_run(self):
        calls = []
        def unavailable(text):
            calls.append(text)
            raise OSError("Fixture translation unavailable")
        source = product()
        source["prizes"].append({"letter": "B", "name": "未知の景品", "nameKo": "未知の景品", "size": "", "description": ""})
        first, status = translate_catalog([source], KoreanTranslator(request=unavailable))
        self.assertTrue(first[0]["translationPending"])
        self.assertEqual(first[0]["prizes"][0]["nameKo"], "A상 공개 경품")
        self.assertEqual(len(calls), 1)  # Stop hammering an unavailable public endpoint.
        self.assertEqual(status["failedRequests"], 1)
        again, status = translate_catalog(first, KoreanTranslator(request=lambda text: "한국어 경품명"))
        self.assertFalse(again[0]["translationPending"])
        self.assertEqual(again[0]["prizes"][0]["nameKo"], "한국어 경품명")

    def test_bad_responses_cannot_publish_html_or_japanese_as_korean(self):
        for bad in ("<script>alert(1)</script>", "日本語のまま", "", "a" * 4000):
            result, status = translate_catalog([product()], KoreanTranslator(request=lambda text: bad))
            self.assertTrue(result[0]["translationPending"])
            self.assertTrue(korean(result[0]["prizes"][0]["nameKo"]))
            self.assertNotIn("<script>", json.dumps(result, ensure_ascii=False))

    def test_unknown_lineup_never_invents_prizes_or_allocations(self):
        source = product()
        source["prizes"] = []
        result, _ = translate_catalog([source], KoreanTranslator(max_requests=0))
        self.assertEqual(result[0]["prizes"], [])
        self.assertIn("공식 발표 후", result[0]["summary"])
        self.assertTrue(result[0]["prizeDetailsPending"])

    def test_untranslated_english_name_is_not_marked_as_complete_korean(self):
        source = product()
        source["prizes"][0].update(name="NEW HERO FIGURE", nameKo="NEW HERO FIGURE")
        first, status = translate_catalog([source], KoreanTranslator(request=lambda text: text))
        self.assertTrue(first[0]["translationPending"])
        self.assertEqual(first[0]["prizes"][0]["nameKo"], "A상 공개 경품")
        second, status = translate_catalog(first, KoreanTranslator(request=lambda text: "새로운 히어로 피규어"))
        self.assertFalse(second[0]["translationPending"])
        self.assertEqual(second[0]["prizes"][0]["nameKo"], "새로운 히어로 피규어")
        brand, status = KoreanTranslator(max_requests=0).translate("MASTERLISE", "경품", require_korean=True)
        self.assertEqual(brand, "MASTERLISE 경품")
        self.assertFalse(status)

    def test_request_and_time_budgets_leave_explicit_pending_state(self):
        for kwargs in ({"max_requests": 0}, {"max_seconds": 0}):
            result, status = translate_catalog([product()], KoreanTranslator(request=lambda text: self.fail("Budget exceeded"), **kwargs))
            self.assertEqual(status["requests"], 0)
            self.assertTrue(result[0]["translationPending"])

    def test_slow_official_fetches_do_not_consume_translation_request_time_budget(self):
        clock = [0.0]
        calls = []
        def request(text):
            calls.append(text)
            clock[0] += 5.0  # Five seconds actually spent translating.
            return "새로운 형태의 피규어"
        with patch("translation_ko.time.monotonic", side_effect=lambda: clock[0]):
            translator = KoreanTranslator(request=request, max_seconds=5)
            clock[0] = 10000.0  # Unrelated official discovery/fetch work.
            translated, pending = translator.translate("新しい形のフィギュア", "A상 공개 경품", require_korean=True)
            self.assertEqual(translated, "새로운 형태의 피규어")
            self.assertFalse(pending)
            self.assertEqual(translator.remaining_seconds, 0)
            # Cache/glossary hits remain usable after the request budget ends.
            self.assertEqual(translator.translate("新しい形のフィギュア", "경품", require_korean=True), (translated, False))
            second, pending = translator.translate("別の形のフィギュア", "B상 공개 경품", require_korean=True)
            self.assertEqual(second, "B상 공개 경품")
            self.assertTrue(pending)
            self.assertEqual(len(calls), 1)

    def test_saved_cache_is_only_public_terms_not_credentials(self):
        translator = KoreanTranslator(request=lambda text: "새로운 형태의 피규어")
        translate_catalog([product()], translator)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translation-cache.json"
            save_json(path, translator.cache)
            loaded = load_cache(path)
            self.assertEqual(loaded, translator.cache)
            record = next(iter(loaded["translations"].values()))
            self.assertEqual(set(record), {"source", "translation", "engine", "preparedSource"})
            self.assertEqual(len(next(iter(loaded["translations"]))), 64)

    def test_death_note_character_glossary_repairs_only_the_incorrect_cached_reading(self):
        def cache_key(source):
            return hashlib.sha256((GLOSSARY_VERSION + "\0" + source).encode()).hexdigest()
        source = product()
        source["doubleChance"] = {"name": "夜神月 フィギュア", "nameKo": "야가미즈키 피규어", "translationGenerated": True}
        source["prizes"][0].update(name="夜神月 フィギュア", nameKo="야가미 라이토 피규어", description="직접 검수한 한국어 설명입니다.")
        unrelated = {"source": "未知の景品", "translation": "새로운 경품", "engine": "google-public-ja-ko+glossary"}
        key = cache_key("夜神月 フィギュア")
        cache = {"version": 1, "translations": {key: {"source": "夜神月 フィギュア", "translation": "야가미즈키 피규어", "engine": "google-public-ja-ko+glossary"}, cache_key("未知の景品"): unrelated.copy()}}
        translator = KoreanTranslator(cache, request=lambda text: self.fail("Known character names need no network"))
        result, status = translate_catalog([source], translator)
        self.assertEqual(result[0]["doubleChance"]["nameKo"], "야가미 라이토 피규어")
        self.assertEqual(result[0]["doubleChance"]["name"], "夜神月 フィギュア")
        self.assertEqual(result[0]["prizes"][0]["nameKo"], "야가미 라이토 피규어")
        self.assertEqual(result[0]["prizes"][0]["description"], "직접 검수한 한국어 설명입니다.")
        self.assertEqual(cache["translations"][key]["translation"], "야가미 라이토 피규어")
        self.assertEqual(cache["translations"][cache_key("未知の景品")], unrelated)
        self.assertEqual(status["requests"], 0)
        self.assertFalse(result[0]["translationPending"])
        for original, expected in (("弥海砂", "아마네 미사"), ("リューク", "류크"), ("レム", "렘")):
            self.assertEqual(translator.translate(original + " フィギュア", "경품", require_korean=True), (expected + " 피규어", False))

    def test_character_reading_correction_invalidates_only_matching_legacy_composite_cache(self):
        source = "夜神月が見ているフィギュア"
        key = hashlib.sha256((GLOSSARY_VERSION + "\0" + source).encode()).hexdigest()
        cache = {"version": 1, "translations": {key: {"source": source, "translation": "야가미즈키가 보고 있는 피규어", "engine": "google-public-ja-ko+glossary"}}}
        calls = []
        translator = KoreanTranslator(cache, request=lambda text: calls.append(text) or "야가미 라이토가 보고 있는 피규어")
        value, pending = translator.translate(source, "경품", require_korean=True)
        self.assertEqual(value, "야가미 라이토가 보고 있는 피규어")
        self.assertFalse(pending)
        self.assertEqual(len(calls), 1)
        self.assertIn("야가미 라이토", calls[0])
        self.assertEqual(cache["translations"][key]["preparedSource"], calls[0])

    def test_translation_redirect_cannot_send_public_text_to_another_host(self):
        with self.assertRaises(TranslationError):
            TranslationRedirects().redirect_request(None, None, 302, "redirect", {}, "https://unapproved.invalid/")


if __name__ == "__main__":
    unittest.main()
