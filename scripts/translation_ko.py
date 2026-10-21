#!/usr/bin/env python3
"""Translate public Japanese Kuji facts into Korean; retain Japanese originals.

No account, API key, user data, or paid API is used. The public Google translation
endpoint is best effort, so a bounded persistent cache and explicit pending state
are required. Human-written Korean takes precedence over machine translation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

CACHE_VERSION = 1
GLOSSARY_VERSION = "2026-10-ko-v1"
MAX_TEXT = 1200
MAX_RESPONSE = 256 * 1024
MAX_CACHE_RECORDS = 20000
MAX_ERROR_RECORDS = 10
JAPANESE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff]")
HANGUL = re.compile(r"[가-힣]")

# Official franchise/character spellings, familiar product types, and edition
# phrases are public product terminology, not translations of promotional prose.
GLOSSARY = {
    "The Gathering of RIVALS": "라이벌들의 집결", "20th ANNIVERSARY": "20주년",
    "ディオ・ブランドー": "디오 브란도", "カーズ": "카즈", "DIO": "디오",
    "川尻浩作": "카와지리 코사쿠", "ディアボロ": "디아볼로", "エンリコ・P": "엔리코 P",
    "ディエゴ・ブランドー": "디에고 브란도",
    "碇シンジ": "이카리 신지", "綾波レイ": "아야나미 레이",
    "式波・アスカ・ラングレー": "시키나미 아스카 랑그레이", "渚カヲル": "나기사 카오루",
    "新劇場版：破": "신극장판: 파", "亡妃の秘宝": "망비의 비보",
    "猫猫": "마오마오", "壬氏": "진시", "きららいずマスコット": "키라라이즈 마스코트",
    "ナツ・ドラグニル": "나츠 드래그닐", "ルーシィ・ハートフィリア": "루시 하트필리아",
    "ハッピー": "해피", "エルザ・スカーレット": "엘자 스칼렛",
    "星霊達のデスクトップマスコット": "성령들의 데스크톱 마스코트",
    "一番くじ": "제일복권", "鬼滅の刃": "귀멸의 칼날", "進撃の巨人": "진격의 거인",
    "チェンソーマン": "체인소맨", "ドラゴンボール": "드래곤볼", "ワンピース": "원피스",
    "ONE PIECE": "원피스", "NARUTO-ナルト-": "나루토", "ナルト": "나루토",
    "呪術廻戦": "주술회전", "HUNTER×HUNTER": "헌터×헌터", "HUNTERxHUNTER": "헌터×헌터",
    "僕のヒーローアカデミア": "나의 히어로 아카데미아", "DEATH NOTE": "데스노트",
    "BLEACH": "블리치", "ハイキュー!!": "하이큐!!", "ハイキュー！！": "하이큐!!",
    "ジョジョの奇妙な冒険": "죠죠의 기묘한 모험", "エヴァンゲリオン": "에반게리온",
    "ヱヴァンゲリヲン新劇場版": "에반게리온 신극장판", "薬屋のひとりごと": "약사의 혼잣말",
    "FAIRY TAIL": "페어리테일", "FAIRYTAIL": "페어리테일",
    "兄妹の鬼": "남매의 오니", "廻想録": "회상록", "虎杖悠仁": "이타도리 유지",
    "あにまる大集合！": "동물 대집합!", "あにまる大集合!": "동물 대집합!",
    "彼女たちの物語": "그녀들의 이야기", "うなれ体育祭": "포효하라, 체육대회",
    "神と神の闘い": "신과 신의 싸움", "孫悟空少年期編": "손오공 소년기편",
    "メモリーオブヒロインズ": "메모리 오브 히로인즈", "CHMERA ANT": "키메라 앤트",
    "CHIMERA ANT": "키메라 앤트", "GREED ISLAND": "그리드 아일랜드",
    "Fantastic Adventure": "판타스틱 어드벤처", "ASSEMBLE COLLECTION": "어셈블 컬렉션",
    "DRAGON OMNIBUS": "드래곤 옴니버스", "IMPACT LOG": "임팩트 로그",
    "Royal Princess": "로열 프린세스", "再販売": "재판매", "劇場版": "극장판",
    "TO BE CONTINUED THE GIGANT NAME": "투 비 컨티뉴드 대형 로고 조형물",
    "牛魔王の車フィギュア": "우마왕의 자동차 피규어",
    "超サイヤ人ゴッド孫悟空": "초사이어인 갓 손오공", "超サイヤ人3ゴテンクス": "초사이어인 3 오천크스",
    "アルティメット悟飯": "얼티밋 손오반", "破壊神ビルス": "파괴신 비루스",
    "親子かめはめ波": "부자 에네르기파", "孫悟飯": "손오반", "孫悟空": "손오공",
    "ブルマ": "부르마", "ウーロン": "오룡", "牛魔王": "우마왕", "ウイス": "우이스",
    "メルエム": "메르엠", "ネテロ": "네테로", "ネフェルピトー": "네페르피트",
    "シャウアプフ": "샤우아푸후", "モントゥトゥユピー": "몽투투유피", "キルア": "키르아",
    "ゴン": "곤", "成長ver.": "성장 버전", "麗日お茶子": "우라라카 오챠코",
    "蛙吹梅雨": "아스이 츠유", "葉隠透": "하가쿠레 토오루", "波動ねじれ": "하도 네지레",
    "トガヒミコ": "토가 히미코", "爆豪勝己": "바쿠고 카츠키", "常闇踏陰": "토코야미 후미카게",
    "緑谷出久": "미도리야 이즈쿠", "エンデヴァー": "엔데버", "轟焦凍": "토도로키 쇼토",
    "しょうと：オリジン": "쇼토: 오리진", "しょうと:オリジン": "쇼토: 오리진",
    "モンキー・D・ルフィ": "몽키 D. 루피", "チャルロス聖": "천룡인 차를로스",
    "ポートガス・D・エース": "포트거스 D. 에이스", "ロロノア・ゾロ": "롤로노아 조로",
    "ウソップ": "우솝", "ペローナ": "페로나", "ネフェルタリ・ビビ": "네펠타리 비비",
    "ヴィンスモーク・レイジュ": "빈스모크 레이주", "ジュエリー・ボニー": "쥬얼리 보니",
    "レベッカ": "레베카", "しらほし": "시라호시", "チョッパー": "쵸파",
    "炎帝": "염제", "名刀・鼻嵐": "명도 코폭풍", "マルチスタンド": "멀티 스탠드",
    "ヘッドマグネット": "헤드 마그넷", "ラバーコレクション": "러버 컬렉션",
    "あにまるブランケット": "동물 담요", "あにまるマグカップ": "동물 머그컵",
    "あにまるステッカーコレクション": "동물 스티커 컬렉션", "あにまるラバーコレクション": "동물 러버 컬렉션",
    "タオルコレクション": "타월 컬렉션", "カルー ビッグぬいぐるみ": "카루 대형 봉제인형",
    "あにまる": "동물", "ブランケット": "담요", "マグカップ": "머그컵", "タオル": "타월",
    "コレクション": "컬렉션", "超カルガモ部隊": "초 카루가모 부대", "アクリルスタンド": "아크릴 스탠드",
    "カルー": "카루", "ビッグ": "대형", "ぬいぐるみ": "봉제인형",
    "ステーショナリーアソート": "문구 모음", "ストラップ付きフォンタブ": "스트랩 포함 폰 탭",
    "ネックストラップ": "목걸이 스트랩", "ステッカーアソート": "스티커 모음",
    "ラバートレイ": "러버 트레이", "シリコンカバー": "실리콘 커버", "ラバータイ": "러버 타이",
    "ラバースマホグリップ": "러버 스마트폰 그립", "ラバーマーカーチャーム": "러버 마커 참",
    "ラバーヘアクリップ": "러버 헤어클립",
    "ラバーコースター": "러버 코스터", "クリアファイル": "클리어 파일", "ノート": "노트",
    "メモ": "메모", "フォンタブ": "폰 탭", "ストラップ": "스트랩", "ステッカー": "스티커",
    "フィギュア": "피규어", "頭頂まで": "머리 꼭대기까지", "高さ": "높이", "直径": "지름",
    "幅": "폭", "全長": "전체 길이", "約": "약 ", "枚セット": "장 세트", "個セット": "개 세트",
    "才以上": "세 이상", "歳以上": "세 이상", "コンビニエンスストア": "편의점",
    "書店": "서점", "ホビーショップ": "하비숍", "一番くじ公式ショップ": "제일복권 공식 매장",
    "一番くじONLINE": "제일복권 온라인", "ジャンプショップ": "점프 숍", "など": "등",
    "販売予定": "판매 예정", "発売予定": "발매 예정", "順次発売予定": "순차 발매 예정",
    "発売日未定": "발매일 미정", "発売日": "발매일", "末日": "말일", "より": "부터 ",
    "販売開始予定": "판매 시작 예정", "台座込み": "받침대 포함", "ロープ": "로프",
    "ローソン": "로손", "ファミリーマート": "패밀리마트", "ミニストップ": "미니스톱",
    "ファミリマート": "패밀리마트", "デイリーヤマザキ": "데일리 야마자키", "麦わらストア": "무기와라 스토어",
    "セブン‐イレブン": "세븐일레븐", "セブン-イレブン": "세븐일레븐", "セブンイレブン": "세븐일레븐",
    "デンジ": "덴지", "レゼ": "레제", "早川アキ": "하야카와 아키", "天使の悪魔": "천사의 악마",
    "夜神月": "야가미 라이토", "弥海砂": "아마네 미사", "リューク": "류크", "レム": "렘",
    "ニア": "니아", "メロ": "멜로", "ビーム": "빔", "スペシャルカラーver.": "스페셜 컬러 버전",
    "クリアポスター": "클리어 포스터", "ホログラムステッカーセット": "홀로그램 스티커 세트",
    "きゅんキャラアクリルスタンド": "큔캐러 아크릴 스탠드", "ちょこのっこフィギュアセット": "쵸코놋코 피규어 세트",
    "描きおろしビジュアルボード": "신규 일러스트 비주얼 보드",
}
FRANCHISE_KO = {
    "DEMON_SLAYER": "귀멸의 칼날", "ATTACK_ON_TITAN": "진격의 거인", "CHAINSAW_MAN": "체인소맨",
    "DRAGON_BALL": "드래곤볼", "ONE_PIECE": "원피스", "NARUTO": "나루토", "JUJUTSU_KAISEN": "주술회전",
    "HUNTER_HUNTER": "헌터×헌터", "MY_HERO_ACADEMIA": "나의 히어로 아카데미아",
    "DEATH_NOTE": "데스노트", "BLEACH": "블리치", "HAIKYU": "하이큐!!",
    "JOJO": "죠죠의 기묘한 모험", "EVANGELION": "에반게리온",
    "APOTHECARY_DIARIES": "약사의 혼잣말", "FAIRY_TAIL": "페어리테일",
}
# Legacy cache rows have no preparedSource. Invalidate only rows whose newly
# corrected character reading disagrees; all unrelated translations stay valid.
CACHE_TERM_CORRECTIONS = {name: GLOSSARY[name] for name in ("夜神月", "弥海砂", "リューク", "レム")}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u200b-\u200f\ufeff]", "", value)).strip()


def korean(value) -> bool:
    return isinstance(value, str) and bool(HANGUL.search(value)) and not JAPANESE.search(value)


def glossary(value: str) -> str:
    # A single regex avoids replacing Japanese substrings inside a substituted term.
    pattern = "|".join(re.escape(key) for key in sorted(GLOSSARY, key=len, reverse=True))
    result = re.sub(pattern, lambda match: GLOSSARY[match.group()], value)
    result = re.sub(r"(\d{4})年", r"\1년 ", result)
    result = re.sub(r"(\d{1,2})月", r"\1월 ", result)
    result = re.sub(r"(\d{1,2})日", r"\1일", result)
    weekdays = {"月": "월", "火": "화", "水": "수", "木": "목", "金": "금", "土": "토", "日": "일"}
    result = re.sub(r"[（(]([月火水木金土日])[）)]", lambda match: "(" + weekdays[match[1]] + ")", result)
    return result


def _safe_translation(value: str, source: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_TEXT * 3
            and not re.search(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f]", value)
            and not JAPANESE.search(value)
            and (bool(HANGUL.search(value)) or not JAPANESE.search(source)))


class TranslationError(ValueError):
    pass


def sanitized_error(error: Exception) -> dict:
    """Keep only controlled diagnostic codes; never retain request or response text."""
    if isinstance(error, urllib.error.HTTPError):
        status = error.code
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            known = {400, 401, 403, 404, 408, 429, 500, 502, 503, 504}
            code = f"http_{status}" if status in known else "http_error"
            return {"code": code, "httpStatus": status}
        return {"code": "request_failed"}
    if isinstance(error, TimeoutError) or (isinstance(error, urllib.error.URLError)
                                         and isinstance(error.reason, TimeoutError)):
        return {"code": "timeout"}
    if isinstance(error, (TranslationError, json.JSONDecodeError, UnicodeError)):
        return {"code": "invalid_response"}
    return {"code": "request_failed"}


class TranslationRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if url.scheme != "https" or url.hostname != "translate.googleapis.com" or url.port not in {None, 443}:
            raise TranslationError("Unexpected translation redirect")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def google_translate(source: str, timeout: float = 8) -> str:
    query = urllib.parse.urlencode({"client": "gtx", "sl": "ja", "tl": "ko", "dt": "t", "q": source})
    request = urllib.request.Request("https://translate.googleapis.com/translate_a/single?" + query,
                                     headers={"User-Agent": "KujiPublicFactsTranslation/1.0", "Accept": "application/json"})
    with urllib.request.build_opener(TranslationRedirects()).open(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise TranslationError("Translation response exceeds its size limit")
        payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise TranslationError("Unexpected translation response")
    return clean("".join(row[0] for row in payload[0] if isinstance(row, list) and row and isinstance(row[0], str)))


class KoreanTranslator:
    def __init__(self, cache: dict | None = None, *, request: Callable[[str], str] | None = None,
                 max_requests: int = 120, max_seconds: float = 180):
        self.cache = cache if isinstance(cache, dict) and cache.get("version") == CACHE_VERSION else {"version": CACHE_VERSION, "translations": {}}
        if not isinstance(self.cache.get("translations"), dict): self.cache["translations"] = {}
        self.request = request or google_translate
        self.max_requests = max(0, min(max_requests, 1000))
        # Official discovery and image fetches can take several minutes. Only
        # actual translation requests consume this independent time budget.
        self.remaining_seconds = max(0, min(max_seconds, 600))
        self.requests = 0
        self.hits = 0
        self.failed = 0
        self.errors = []
        self.network_available = True

    def translate(self, source: str, fallback: str, *, require_korean: bool = False) -> tuple[str, bool]:
        source = clean(source or "")
        if not source: return "", False
        if len(source) > MAX_TEXT or re.search(r"[<>\x00-\x08]", source):
            return fallback, True
        candidate = clean(glossary(source))
        key = hashlib.sha256((GLOSSARY_VERSION + "\0" + source).encode()).hexdigest()
        if require_korean and re.fullmatch(r"(?:MASTERLISE(?: EXTRA| PLUS| EXPIECE)?|MEGAIMPACT|Revible Moment)", candidate):
            candidate += " 경품"
        if _safe_translation(candidate, candidate) and (not require_korean or HANGUL.search(candidate)):
            # A new glossary entry can supersede a poor earlier machine reading
            # with no external request. Repair only an already-cached source.
            saved = self.cache["translations"].get(key)
            if key in self.cache["translations"] and (not isinstance(saved, dict) or saved.get("translation") != candidate):
                self.cache["translations"][key] = {"source": source, "translation": candidate,
                                                     "engine": "ko-glossary", "preparedSource": candidate}
            return candidate, False
        cached = self.cache["translations"].get(key)
        if (isinstance(cached, dict) and cached.get("source") == source
                and _safe_translation(cached.get("translation"), candidate)
                and (not require_korean or HANGUL.search(cached["translation"]))
                and ("preparedSource" not in cached or cached["preparedSource"] == candidate)
                and all(original not in source or corrected in cached["translation"]
                        for original, corrected in CACHE_TERM_CORRECTIONS.items())):
            self.hits += 1
            return cached["translation"], False
        if not self.network_available or self.requests >= self.max_requests or self.remaining_seconds <= 0:
            return fallback, True
        self.requests += 1
        started = time.monotonic()
        try:
            translated = clean(self.request(candidate))
            if not _safe_translation(translated, candidate) or (require_korean and not HANGUL.search(translated)):
                raise TranslationError("Translation did not produce validated Korean")
            if len(self.cache["translations"]) < MAX_CACHE_RECORDS:
                self.cache["translations"][key] = {"source": source, "translation": translated,
                                                     "engine": "google-public-ja-ko+glossary", "preparedSource": candidate}
            return translated, False
        except Exception as error:
            self.failed += 1
            if len(self.errors) < MAX_ERROR_RECORDS:
                self.errors.append(sanitized_error(error))
            # Rate limiting/outage must not add 120 repeated failed requests to a run.
            if not isinstance(error, TranslationError):
                self.network_available = False
            return fallback, True
        finally:
            self.remaining_seconds = max(0, self.remaining_seconds - max(0, time.monotonic() - started))


def translate_catalog(products: list[dict], translator: KoreanTranslator) -> tuple[list[dict], dict]:
    translated = []
    for original in products:
        product = copy.deepcopy(original)
        pending = False
        generated = set(product.get("translationGenerated", []))
        franchise = product.get("franchiseKo") or FRANCHISE_KO.get(product.get("franchiseCode"), "애니메이션")
        title_jp = product.get("productTitle") or product.get("nameJp") or ""
        title_ko = product.get("productTitleKo") or product.get("nameKo") or ""
        if not korean(title_ko) or "title" in generated:
            title_ko, failed = translator.translate(title_jp, f"제일복권 {franchise} 신상품", require_korean=True)
            pending |= failed
            generated.add("title")
        product.update(nameKo=title_ko, productTitleKo=title_ko, nameJp=title_jp)
        product.setdefault("franchiseKo", franchise)
        for key in ("size", "age", "retailers", "releaseDateText"):
            # Keep the published Japanese text for exact reference, including dates.
            source = product.get(key + "Jp", product.get(key, ""))
            if source:
                product[key + "Jp"] = source
                if JAPANESE.search(source):
                    fallback = {"age": "공식 연령 안내 확인", "retailers": "공식 판매처 안내 확인", "releaseDateText": "발매 일정 확인 중", "size": "크기 확인 중"}[key]
                    product[key], failed = translator.translate(source, fallback)
                    pending |= failed
        for collection in ("prizes", "previewPrizes"):
            for prize in product.get(collection, []):
                prize_generated = set(prize.get("translationGenerated", []))
                original_name = prize.get("name", "")
                label = "라스트 원" if prize.get("letter") == "LAST" else str(prize.get("letter", "")) + "상"
                if not korean(prize.get("nameKo")) or "name" in prize_generated:
                    prize["nameKo"], failed = translator.translate(original_name, label + " 공개 경품", require_korean=True)
                    pending |= failed
                    prize_generated.add("name")
                size_jp = prize.get("sizeJp", prize.get("size", ""))
                if size_jp:
                    prize["sizeJp"] = size_jp
                    if JAPANESE.search(size_jp):
                        prize["size"], failed = translator.translate(size_jp, "크기 정보 번역 확인 중")
                        pending |= failed
                if JAPANESE.search(prize.get("variants", "")):
                    prize["variants"], failed = translator.translate(prize["variants"], "종류 정보 번역 확인 중")
                    pending |= failed
                desc = prize.get("description", "")
                if not korean(desc) or "description" in prize_generated:
                    facts = [prize["nameKo"], prize.get("variants", ""), prize.get("size", "")]
                    prize["description"] = " · ".join(str(fact) for fact in facts if fact) + ". 공식 공개 사양입니다."
                    prize_generated.add("description")
                prize["translationGenerated"] = sorted(prize_generated)
        campaign = product.get("doubleChance")
        if isinstance(campaign, dict):
            if not korean(campaign.get("nameKo")) or campaign.get("translationGenerated"):
                campaign["nameKo"], failed = translator.translate(campaign.get("name", ""), "더블 찬스 캠페인 경품", require_korean=True)
                pending |= failed
                campaign["translationGenerated"] = True
            deadline_jp = campaign.get("deadlineJp", campaign.get("deadline", ""))
            if deadline_jp:
                campaign["deadlineJp"] = deadline_jp
                if JAPANESE.search(deadline_jp):
                    campaign["deadline"], failed = translator.translate(deadline_jp, "공식 캠페인 마감 안내 확인")
                    pending |= failed
        summary = product.get("summary", "")
        automatic_summary = summary == "공식 상품 페이지에 공개된 일본어 정식 상품명과 경품 정보를 표시합니다."
        if not korean(summary) or automatic_summary or "summary" in generated:
            prize_names = [prize["nameKo"] for prize in product.get("prizes", [])[:3]]
            if not prize_names:
                prize_names = [prize["nameKo"] for prize in product.get("previewPrizes", [])[:3]]
            summary = title_ko + ". "
            summary += "공개 경품: " + ", ".join(prize_names) + "." if prize_names else "경품 구성은 공식 발표 후 업데이트됩니다."
            if product.get("prizeDetailsPending") and prize_names: summary += " 전체 경품 구성은 아직 공개되지 않았습니다."
            product["summary"] = summary
            generated.add("summary")
        if "summary" in generated:
            product["descriptionHtml"] = "<p>" + html.escape(summary) + "</p><p>일본 공식 정보를 한국어로 안내하며 일본어 정식 명칭을 함께 표시합니다. 원화 가격과 티켓 수량은 체험용 예시입니다.</p>"
        product["translationGenerated"] = sorted(generated)
        product["translationPending"] = pending
        product["translationLanguage"] = "ko"
        translated.append(product)
    return translated, {"pending": sum(bool(item.get("translationPending")) for item in translated),
                        "requests": translator.requests, "cacheHits": translator.hits, "failedRequests": translator.failed,
                        "errors": copy.deepcopy(translator.errors)}


def load_cache(path: Path) -> dict:
    try:
        if path.stat().st_size > 16 * 1024 * 1024: return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--max-seconds", type=float, default=180,
                        help="Cumulative translation request seconds; official fetch and cache time are excluded")
    args = parser.parse_args(argv)
    translator = KoreanTranslator(load_cache(args.cache), max_requests=args.max_requests, max_seconds=args.max_seconds)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if not isinstance(catalog, list) or not catalog or len(catalog) > 200:
        raise TranslationError("A nonempty bounded public catalog is required")
    products, status = translate_catalog(catalog, translator)
    save_json(args.catalog, products)
    save_json(args.cache, translator.cache)
    print(json.dumps(status, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
