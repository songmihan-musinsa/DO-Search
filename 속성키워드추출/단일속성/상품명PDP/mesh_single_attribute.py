from pathlib import Path
import time
try:
    import pandas as pd
except Exception:
    pd = None
try:
    import gspread
except Exception:
    gspread = None

#https://www.musinsa.com/products/{goodsNo}
# https://goods-detail.musinsa.com/api2/goods/{goodsNo}/essential   : api 호출

"""
무신사 PDP 텍스트 & 이미지 수집 + 속성 매핑 스크립트
======================================================

데이터 소스 (문서 기반):
  1. https://www.musinsa.com/products/{goodsNo}
       └─ HTML 내 <script id="__NEXT_DATA__"> JSON
           └─ props.pageProps.meta.data
               ├─ goodsNm, goodsNmEng
               ├─ thumbnailImageUrl
               ├─ goodsImages[].imageUrl       ← 썸네일 목록
               ├─ goodsContents                ← 상세 설명 HTML (이미지+텍스트 혼합)
               └─ goodsMaterial
  2. https://goods-detail.musinsa.com/api2/goods/{goodsNo}/essential
       └─ 상품 고시 정보, 추가 메타
  3. https://goods-detail.musinsa.com/api2/goods/{goodsNo}/actual-size
       └─ 실측 사이즈 정보

속성 매핑 대상 (모자 카테고리 예시):
  - 패널 수    : 6패널, 5패널, ...
  - 모자 종류  : 캡, 버킷햇, 비니, ...
  - 소재       : 메시, 코튼, 울, ...
  - 가먼트 다잉: 가먼트 다잉 여부
  - 챙 모양    : 플랫캡, 커브드캡, ...
  - 로고 크기  : 스몰, 미들, 라지, ...
"""

import re
import json
import time
import html as html_lib
from dataclasses import dataclass, field
from typing import Optional
import requests
import os

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
# HTTP 요청 헤더
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.musinsa.com/",
}

GOODS_DETAIL_API = "https://goods-detail.musinsa.com/api2/goods/{goods_no}/essential"
ACTUAL_SIZE_API  = "https://goods-detail.musinsa.com/api2/goods/{goods_no}/actual-size"
PDP_URL          = "https://www.musinsa.com/products/{goods_no}"
CDN_BASE         = "https://image.msscdn.net"


# ──────────────────────────────────────────────
# 데이터 구조
# ──────────────────────────────────────────────

@dataclass
class ContentBlock:
    """상세 설명 내 텍스트 또는 이미지 블록"""
    type: str          # "text" | "image"
    content: str       # 텍스트 내용 or 이미지 URL


@dataclass
class ProductData:
    goods_no: str
    goods_nm: str = ""
    goods_nm_eng: str = ""
    thumbnail_url: str = ""
    goods_images: list[str] = field(default_factory=list)
    goods_material: str = ""
    detail_blocks: list[ContentBlock] = field(default_factory=list)
    notification_info: dict = field(default_factory=dict)   # 상품 고시 정보
    actual_size: dict = field(default_factory=dict)         # 실측 사이즈
    raw_text: str = ""                                       # 모든 텍스트 합산 (디버그용)
    title_text: str = ""                                     # 상품명 + 영문명 (브랜드 제거 후)
    detail_text: str = ""                                    # PDP 상세페이지 텍스트
    ocr_text: str = ""                                       # PDP 상세페이지 이미지 OCR 텍스트
    attribute_map: dict = field(default_factory=dict)        # 매핑 결과 {속성: 값} (우선순위 합산, 하위호환용)
    attribute_source_map: dict = field(default_factory=dict) # 매핑 출처 {속성: 'title'|'detail'|'ocr'}
    # ── 소스 분리 매핑 결과 (정확도 비교용) ──
    title_attribute_map: dict = field(default_factory=dict)   # 상품명에서 추출한 {속성: 값}
    title_keyword_map: dict = field(default_factory=dict)     # 상품명에서 매칭된 {속성: 키워드}
    detail_attribute_map: dict = field(default_factory=dict)  # PDP에서 추출한 {속성: 값}
    detail_keyword_map: dict = field(default_factory=dict)    # PDP에서 매칭된 {속성: 키워드}
    # meta.data 원본 필드 (가능하면 채워집니다)
    brand: str = ""
    goods_nm_raw: str = ""
    goods_nm_eng_raw: str = ""
    thumbnail_image_url_raw: str = ""
    goods_images_raw: list = field(default_factory=list)
    goods_contents_raw: str = ""
    goods_material_raw: str = ""


# ──────────────────────────────────────────────
# HTML 파싱 유틸
# ──────────────────────────────────────────────

def extract_next_data(html: str) -> Optional[dict]:
    """
    <script id="__NEXT_DATA__" type="application/json">...</script> 추출
    """
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def normalize_url(url: str) -> str:
    """
    //image... → https://image...
    /path/...  → https://image.msscdn.net/path/...
    data:      → "" (무시)
    """
    if not url:
        return ""
    if url.startswith("data:"):
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return CDN_BASE + url
    return url


def extract_image_src(img_tag: str) -> str:
    """<img src="..."> / <img src='...'> / <img src=...> 에서 src 추출"""
    m = re.search(r'src=["\']([^"\'>\s]+)["\']', img_tag)
    if not m:
        m = re.search(r"src=([^\s>]+)", img_tag)
    return normalize_url(m.group(1)) if m else ""


def normalize_text(raw_html: str) -> str:
    """상세 설명 HTML 조각 → 읽기 쉬운 텍스트"""
    text = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>',  '', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</(p|div|li|h[1-6])>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_lib.unescape(text)
    text = re.sub(r'\u00a0', ' ', text)           # &nbsp;
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def ensure_text(value) -> str:
    """값을 문자열로 안전하게 변환합니다. 리스트/튜플은 공백으로 결합, 딕셔너리는 값들을 결합합니다."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(map(ensure_text, value))
    if isinstance(value, dict):
        return " ".join(ensure_text(v) for v in value.values())
    return str(value)


def extract_detail_blocks(goods_contents: str) -> list[ContentBlock]:
    """
    goodsContents HTML → [ContentBlock(text|image), ...]
    이미지 태그 기준으로 텍스트 / 이미지 블록 분리
    """
    if not isinstance(goods_contents, str) or not goods_contents.strip():
        return []

    blocks: list[ContentBlock] = []
    # <img ...> 태그를 기준으로 분리
    img_pattern = re.compile(r'(<img[^>]*>)', re.IGNORECASE)
    parts = img_pattern.split(goods_contents)

    for part in parts:
        if img_pattern.match(part):
            url = extract_image_src(part)
            if url:
                blocks.append(ContentBlock(type="image", content=url))
        else:
            text = normalize_text(part)
            if text:
                blocks.append(ContentBlock(type="text", content=text))

    return blocks


# ──────────────────────────────────────────────
# 속성 매핑 (모자 카테고리)
# ──────────────────────────────────────────────

# 메시 동의어 키워드 (상품명/메타에서 메쉬 여부를 탐지할 때 사용)
MESH_KEYWORDS = [
    "메쉬", "메시", "매쉬", "매시", "mesh", "meshed", "mash",
    "메쉬캡", "메쉬볼캡", "메쉬 캡", "메쉬 볼캡", "메쉬 볼 캡",
    "매쉬캡", "매쉬볼캡", "매쉬 캡", "메시캡", "메시볼캡", "메시 캡",
]

ATTRIBUTE_RULES: dict[str, dict[str, list[str]]] = {
    "패널수": {
        "2패널": ["2패널", "2 패널", "two panel"],
        "5패널": ["5패널", "5 패널", "five panel"],
        "6패널": ["6패널", "6 panel", "six panel"],
        "기타":  []
    },
    "모자종류": {
        "볼캡":    ["볼캡", "ball cap", "baseball cap", "야구모자"],
        "버킷햇":  ["버킷햇", "bucket hat", "버킷 햇"],
        "비니":    ["비니", "beanie"],
        "스냅백":  ["스냅백", "snapback", "snap back"],
        "캐스킷":  ["캐스킷", "casquette"],
        "트러커캡":["트러커", "trucker"],
        "기타":    [],
    },
    "소재": {
        # '메시' 항목에 메시 관련 동의어를 추가합니다.
        "메시":    ["메시", "mesh"] + [kw for kw in MESH_KEYWORDS],
        "코튼":    ["코튼", "cotton", "면"],
        "울":      ["울", "wool"],
        "데님":    ["데님", "denim"],
        "나일론":  ["나일론", "nylon"],
        "기타":    [],
    }
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", (text or "")) if t]


def _all_attribute_keyword_tokens() -> set[str]:
    """ATTRIBUTE_RULES에 정의된 모든 속성 키워드를 토큰 단위로 모은 집합.

    브랜드 토큰 제거 시, 속성 키워드와 겹치는 브랜드 토큰(예: 브랜드명이 'Mesh')을
    보호(제거 금지)하는 데 사용합니다.
    """
    toks: set[str] = set()
    for candidates in ATTRIBUTE_RULES.values():
        for keywords in candidates.values():
            for kw in keywords:
                for t in _tokenize(kw):
                    toks.add(t.lower())
    return toks


_PROTECTED_TOKENS = _all_attribute_keyword_tokens()


def _remove_brand_tokens(text: str, brand: str) -> str:
    """토큰 단위로 브랜드명을 제거합니다 (단순 문자열 치환 아님).

    단, 브랜드 토큰이 속성 키워드와 겹치면(예: 브랜드명 자체가 'Mesh') 제거하지
    않습니다. 무조건 제거하면 정상 속성까지 오삭제되어 오탐이 생기기 때문입니다.
    """
    if not text or not brand:
        return text or ""
    tokens = [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", brand) if t]
    out = text
    for tk in tokens:
        if not tk:
            continue
        # 속성 키워드와 겹치는 브랜드 토큰은 보호 (브랜드 리스트에만 의존하지 않음)
        if tk.lower() in _PROTECTED_TOKENS:
            continue
        # word-boundary removal, case-insensitive
        out = re.sub(rf"\b{re.escape(tk)}\b", "", out, flags=re.IGNORECASE)
    # collapse extra spaces
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out


# 속성별 허용 소스 및 우선순위
# 소스: title(상품명/영문명), detail(상세페이지 텍스트), ocr(이미지 OCR 텍스트)
# 우선순위(요청 반영): 1순위 상품명/영문명 > 2순위 상세페이지 > 3순위 OCR
# ※ 브랜드명은 어떤 속성에도 소스로 쓰지 않습니다 (title_text에서 토큰 제거).
SOURCE_PRIORITY = ["title", "detail", "ocr"]
ATTRIBUTE_ALLOWED_SOURCES: dict[str, list[str]] = {
    # 소재(=메시 등): 상품명/영문명, 상세페이지, OCR 에서만 허용 (브랜드 제외)
    "소재": ["title", "detail", "ocr"],
    # 그 외 속성: 기본적으로 모든 허용 소스 사용
}


def _is_mesh_accepted(label_kw: str, source: str, text_lower: str) -> bool:
    """영문 'mesh'는 문맥 검사, 한글은 우선 인정."""
    if label_kw.lower() in ("메쉬", "메시", "매쉬", "매시"):
        return True
    if label_kw.lower() == "mesh":
        # 한정: 영어 'mesh'는 상세페이지 또는 상품명에서만 인정
        if source not in ("detail", "title"):
            return False
        # require at least one context word nearby (or anywhere)
        context_words = [
            "cap", "hat", "fabric", "material", "breathable", "panel", "lining", "body", "upper",
        ]
        for ctx in context_words:
            if ctx in text_lower:
                return True
        return False
    return False


def map_attributes_by_source(title_text: str, detail_text: str, ocr_text: str, brand: str = "") -> dict:
    """
    Source-separated attribute mapping.

    Returns a mapping where each attribute maps to an object with value and source,
    e.g. {"소재": {"value": "메시", "source": "title"}, ...}
    Priority order (which source to prefer) is: title -> detail -> ocr.
    The `brand` is excluded from `title_text` searches by token-removal before matching.
    """
    # prepare texts and lowercase variants
    title_clean = _remove_brand_tokens(title_text or "", brand)
    sources = {
        "detail": (detail_text or ""),
        "ocr":    (ocr_text or ""),
        "title":  (title_clean or ""),
    }

    # prepare result
    result: dict[str, dict[str, str]] = {}

    for attr, candidates in ATTRIBUTE_RULES.items():
        allowed = ATTRIBUTE_ALLOWED_SOURCES.get(attr, ["detail", "ocr", "title"]) or ["detail", "ocr", "title"]
        matched_label = None
        matched_source = None

        # iterate sources in priority order
        for src in SOURCE_PRIORITY:
            if src not in allowed:
                continue
            text = sources.get(src, "")
            if not text:
                continue
            text_lower = text.lower()

            for label, keywords in candidates.items():
                if label == "기타":
                    continue
                for kw in keywords:
                    if not kw:
                        continue
                    kw_lc = kw.lower()
                    if kw_lc in text_lower:
                        # special handling for 'mesh' english keyword
                        if kw_lc == "mesh":
                            if not _is_mesh_accepted(kw_lc, src, text_lower):
                                continue
                        matched_label = label
                        matched_source = src
                        break
                if matched_label:
                    break
            if matched_label:
                break

        if matched_label:
            result[attr] = {"value": matched_label, "source": matched_source}

    return result


def map_attributes_per_source(title_text: str, detail_text: str, ocr_text: str, brand: str = "") -> dict:
    """소스별로 '독립적으로' 속성을 추출합니다 (우선순위로 하나만 고르지 않음).

    상품명(title)과 PDP(detail)에서 각각 어떤 속성이 어떤 키워드로 매칭됐는지를
    따로 비교할 수 있도록, 소스별 결과를 분리해서 반환합니다.

    반환 형태:
        {
          "title":  {속성: {"value": 레이블, "keyword": 매칭키워드}, ...},
          "detail": {속성: {"value": 레이블, "keyword": 매칭키워드}, ...},
          "ocr":    {속성: {"value": 레이블, "keyword": 매칭키워드}, ...},
        }
    """
    # 브랜드명은 title에서 토큰 제거 후 매칭 (오탐 방지)
    title_clean = _remove_brand_tokens(title_text or "", brand)
    sources = {
        "title":  (title_clean or ""),
        "detail": (detail_text or ""),
        "ocr":    (ocr_text or ""),
    }

    result: dict[str, dict[str, dict[str, str]]] = {"title": {}, "detail": {}, "ocr": {}}

    for src, text in sources.items():
        if not text:
            continue
        text_lower = text.lower()
        for attr, candidates in ATTRIBUTE_RULES.items():
            allowed = ATTRIBUTE_ALLOWED_SOURCES.get(attr, ["detail", "ocr", "title"]) or ["detail", "ocr", "title"]
            if src not in allowed:
                continue
            matched_label = None
            matched_kw = None
            for label, keywords in candidates.items():
                if label == "기타":
                    continue
                for kw in keywords:
                    if not kw:
                        continue
                    kw_lc = kw.lower()
                    if kw_lc in text_lower:
                        # 영문 'mesh'는 문맥 검사
                        if kw_lc == "mesh" and not _is_mesh_accepted(kw_lc, src, text_lower):
                            continue
                        matched_label = label
                        matched_kw = kw
                        break
                if matched_label:
                    break
            if matched_label:
                result[src][attr] = {"value": matched_label, "keyword": matched_kw}

    return result


def legacy_map_attributes(text: str) -> dict[str, str]:
    """Legacy single-text mapping (pre-separation).

    This mimics the old behavior: scan a single text blob and for each attribute
    pick the first matching label. Used for A/B comparison.
    """
    if not text:
        return {}
    text_lower = text.lower()
    result: dict[str, str] = {}
    for attr, candidates in ATTRIBUTE_RULES.items():
        for label, keywords in candidates.items():
            if label == "기타":
                continue
            for kw in keywords:
                if not kw:
                    continue
                if kw.lower() in text_lower:
                    result[attr] = label
                    break
            if attr in result:
                break
    return result


def map_attributes(text: str) -> dict[str, str]:
    """
    Backwards-compatible wrapper for single-text mapping. Returns simple dict attr->value.
    """
    mapped = map_attributes_by_source(title_text=text, detail_text=text, ocr_text="", brand="")
    # flatten to simple dict label->value
    simple: dict[str, str] = {}
    for k, v in mapped.items():
        if isinstance(v, dict):
            simple[k] = v.get("value", "")
        else:
            simple[k] = v
    return simple


def map_material_single(material_text: str) -> str:
    """
    단일 소재 판단 함수

    현재 정책: `meta.data.goodsMaterial` (파일에서 채워지는 `goods_material_raw`)을
    단일 소스로 사용합니다. 향후 단일 속성 소스가 바뀌면 이 함수의 입력
    또는 내부 로직을 수정하면 됩니다.

    반환값: ATTRIBUTE_RULES['소재']에 정의된 레이블(예: '메시', '코튼', ...) 또는 '기타'
    """
    # Normalize various possible shapes of goods_material_raw
    # goods_material_raw may be: string, list of strings, dict like {"materials": [...]}, etc.
    if material_text is None:
        return "기타"

    # If dict with 'materials' key, prefer joining that
    if isinstance(material_text, dict):
        if "materials" in material_text and isinstance(material_text["materials"], (list, tuple)):
            txt = " ".join(map(ensure_text, material_text["materials"]))
        else:
            # join all values
            txt = " ".join(ensure_text(v) for v in material_text.values())
    elif isinstance(material_text, (list, tuple)):
        txt = " ".join(map(ensure_text, material_text))
    else:
        txt = ensure_text(material_text)

    txt = txt.lower()

    # Try exact keyword matches from ATTRIBUTE_RULES
    for label, keywords in ATTRIBUTE_RULES.get("소재", {}).items():
        if label == "기타":
            continue
        for kw in keywords:
            if kw.lower() in txt:
                return label

    # Fallback: check MESH_KEYWORDS explicitly
    for kw in MESH_KEYWORDS:
        if kw.lower().replace(" ", "") in txt.replace(" ", ""):
            return "메시"

    return "기타"


# ──────────────────────────────────────────────
# HTTP 요청
# ──────────────────────────────────────────────

def fetch(url: str, as_json: bool = False, timeout: int = 10) -> Optional[str | dict]:
    # Use a Session with retries and separate connect/read timeouts to avoid long hangs
    import certifi
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # timeout can be a tuple (connect_timeout, read_timeout)
    connect_timeout = 5
    read_timeout = timeout if isinstance(timeout, (int, float)) else 10
    try:
        resp = session.get(url, headers=HEADERS, timeout=(connect_timeout, read_timeout), verify=certifi.where())
        resp.raise_for_status()
        if as_json:
            return resp.json()
        return resp.text
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"  [ERROR] {url} → {e}")
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


# ──────────────────────────────────────────────
# 메인 수집 함수
# ──────────────────────────────────────────────

def collect_product(goods_no: str | int) -> ProductData:
    goods_no = str(goods_no)
    product = ProductData(goods_no=goods_no)

    # ── STEP 1: PDP HTML → __NEXT_DATA__ 파싱 ──
    print(f"[1/3] PDP HTML 수집: {PDP_URL.format(goods_no=goods_no)}")
    html = fetch(PDP_URL.format(goods_no=goods_no))
    if html:
        next_data = extract_next_data(html)
        if not next_data:
            print(f"  [WARN] goods_no={goods_no} __NEXT_DATA__ 추출 실패 (None)")
        else:
            try:
                props = next_data.get("props") or {}
                pageProps = props.get("pageProps") or {}
                meta = pageProps.get("meta") or {}
                data = meta.get("data") or {}

                product.goods_nm = data.get("goodsNm", "")
                product.goods_nm_raw = data.get("goodsNm", "")
                product.goods_nm_eng = data.get("goodsNmEng", "")
                product.goods_nm_eng_raw = data.get("goodsNmEng", "")
                product.thumbnail_url = normalize_url(data.get("thumbnailImageUrl", ""))
                product.thumbnail_image_url_raw = data.get("thumbnailImageUrl", "")
                product.goods_material = data.get("goodsMaterial", "")
                product.goods_material_raw = data.get("goodsMaterial", "")
                # 브랜드 정보가 meta.data 내에 있으면 가져오기 (필드명은 다양할 수 있음)
                product.brand = data.get("brandName") or data.get("brand") or data.get("maker") or ""

                # 썸네일 이미지 목록
                goods_images = data.get("goodsImages", [])
                product.goods_images = [
                    normalize_url(img.get("imageUrl", ""))
                    for img in goods_images
                    if isinstance(img, dict) and img.get("imageUrl")
                ]
                product.goods_images_raw = goods_images if isinstance(goods_images, list) else []

                # 상세 설명 HTML → 블록 분리
                goods_contents = data.get("goodsContents", "")
                product.goods_contents_raw = goods_contents
                product.detail_blocks = extract_detail_blocks(goods_contents)
            except Exception as e:
                print(f"  [ERROR] goods_no={goods_no} __NEXT_DATA__ 처리 중 예외: {e}")
                try:
                    # 디버그 정보 출력(추적용)
                    snippet = html[:1000].replace('\n', ' ') if isinstance(html, str) else ''
                    print(f"    [DEBUG] html_snippet={snippet[:500]}")
                    print(f"    [DEBUG] extract_next_data_repr={repr(next_data)[:500]}")
                except Exception:
                    pass
                # 안전하게 기본값 유지(빈 필드)
                product.detail_blocks = []

    # ── STEP 2: goods-detail essential API ──
    print(f"[2/3] Essential API 호출")
    essential = fetch(GOODS_DETAIL_API.format(goods_no=goods_no), as_json=True)
    if isinstance(essential, dict):
        # API 응답 구조에 따라 조정 필요 — 상품 고시 정보 추출
        data = essential.get("data", essential)
        product.notification_info = data.get("notificationInfo", {}) or data.get("goodsNotification", {})

    # ── STEP 3: actual-size API ──
    print(f"[3/3] Actual-size API 호출")
    actual = fetch(ACTUAL_SIZE_API.format(goods_no=goods_no), as_json=True)
    if isinstance(actual, dict):
        data = actual.get("data", actual)
        product.actual_size = data

    # ── STEP 4: 속성 매핑용 텍스트를 "소스별로" 분리 수집 ──
    # 속성은 오직 아래 소스에서만 추출합니다 (브랜드/goodsMaterial 등은 제외):
    #   - title  : 원 상품명(goodsNm) + 원 영문명(goodsNmEng)
    #   - detail : PDP 상세페이지 텍스트 (goodsContents의 text 블록 + 고시 정보)
    #   - ocr    : PDP 상세페이지 이미지 OCR 텍스트 (현재 미구현이면 빈 값)

    # title: 상품명 + 영문명
    title_parts = [
        ensure_text(product.goods_nm),
        ensure_text(product.goods_nm_eng),
    ]
    product.title_text = "\n".join(filter(None, title_parts))

    # detail: 상세 설명 텍스트 블록 + 상품 고시 정보
    detail_parts: list[str] = []
    for v in product.notification_info.values():
        detail_parts.append(ensure_text(v))
    for block in product.detail_blocks:
        if block.type == "text":
            detail_parts.append(ensure_text(block.content))
    product.detail_text = "\n".join(filter(None, detail_parts))

    # ocr: 이미지 OCR 결과 (외부에서 채워질 수 있음, 없으면 빈 문자열)
    product.ocr_text = ensure_text(product.ocr_text)

    # 디버그/하위호환용으로 합산본도 보관 (매핑에는 사용하지 않음)
    product.raw_text = "\n".join(
        filter(None, [product.title_text, product.detail_text, product.ocr_text])
    )

    # ── STEP 5: 소스 분리 속성 매핑 (브랜드는 title_text에서 토큰 제거 후 제외) ──
    mapped = map_attributes_by_source(
        title_text=product.title_text,
        detail_text=product.detail_text,
        ocr_text=product.ocr_text,
        brand=product.brand,
    )
    # {속성: 값} 과 {속성: 출처} 로 분리 저장
    product.attribute_map = {k: v["value"] for k, v in mapped.items()}
    product.attribute_source_map = {k: v["source"] for k, v in mapped.items()}

    # ── STEP 6: 소스별 독립 매핑 (상품명 vs PDP 정확도 비교용) ──
    per_source = map_attributes_per_source(
        title_text=product.title_text,
        detail_text=product.detail_text,
        ocr_text=product.ocr_text,
        brand=product.brand,
    )
    product.title_attribute_map = {k: v["value"] for k, v in per_source["title"].items()}
    product.title_keyword_map   = {k: v["keyword"] for k, v in per_source["title"].items()}
    product.detail_attribute_map = {k: v["value"] for k, v in per_source["detail"].items()}
    product.detail_keyword_map   = {k: v["keyword"] for k, v in per_source["detail"].items()}

    return product


# ──────────────────────────────────────────────
# 출력 헬퍼
# ──────────────────────────────────────────────

def print_product_summary(p: ProductData) -> None:
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"상품번호  : {p.goods_no}")
    print(f"상품명    : {p.goods_nm}")
    print(f"영문명    : {p.goods_nm_eng}")
    print(f"소재      : {p.goods_material}")
    print(f"썸네일    : {p.thumbnail_url}")
    print(f"이미지 수 : 썸네일 {len(p.goods_images)}개 / 상세블록 {len(p.detail_blocks)}개")
    print(f"\n[상세 블록 미리보기 (최대 5개)]")
    for i, block in enumerate(p.detail_blocks[:5]):
        if block.type == "text":
            preview = block.content[:80].replace("\n", " ")
            print(f"  [{i}] TEXT  : {preview}…")
        else:
            print(f"  [{i}] IMAGE : {block.content}")

    print(f"\n[속성 매핑 결과 — 소스 분리 (상품명 vs PDP)]")
    all_attrs = list(ATTRIBUTE_RULES.keys())
    print(f"  {'속성':12s} | {'상품명 추출':28s} | {'PDP 추출':28s}")
    for attr in all_attrs:
        t_val = p.title_attribute_map.get(attr, "")
        t_kw  = p.title_keyword_map.get(attr, "")
        d_val = p.detail_attribute_map.get(attr, "")
        d_kw  = p.detail_keyword_map.get(attr, "")
        t_cell = f"{t_val} ({t_kw})" if t_val else "-"
        d_cell = f"{d_val} ({d_kw})" if d_val else "-"
        print(f"  {attr:12s} | {t_cell:28s} | {d_cell:28s}")

    # 추가 원본 필드 출력
    print(f"브랜드      : {p.brand}")
    print(f"원상품명    : {p.goods_nm_raw}")
    print(f"원영문명    : {p.goods_nm_eng_raw}")
    print(f"원썸네일URL : {p.thumbnail_image_url_raw}")
    print(f"원소재      : {p.goods_material_raw}")

    if p.notification_info:
        print(f"\n[상품 고시 정보 (일부)]")
        for k, v in list(p.notification_info.items())[:5]:
            print(f"  {k}: {v}")
    print(sep)


# ──────────────────────────────────────────────
# 배치 실행 예시
# ──────────────────────────────────────────────

def collect_batch(goods_no_list: list[str], sleep_sec: float = 1.0) -> list[ProductData]:
    """
    여러 상품 순차 수집.
    sleep_sec: 요청 간 대기 (서버 부하 방지)
    """
    results = []
    for i, gno in enumerate(goods_no_list):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(goods_no_list)}] goods_no={gno}")
        p = collect_product(gno)
        results.append(p)
        print_product_summary(p)
        if i < len(goods_no_list) - 1:
            time.sleep(sleep_sec)
    return results


# ──────────────────────────────────────────────
# 실행 진입점
# ──────────────────────────────────────────────

if __name__ == "__main__":
    # 테스트용 상품번호 목록
    test_goods = [
        "6111868",   # 문서 예시 — 6패널 캡 (메시 등 속성 포함)
        "6502732",   # 추가 확인 상품
    ]

    products = collect_batch(test_goods, sleep_sec=1.5)

    # 결과를 JSON으로 저장
    output = []
    for p in products:
        output.append({
            "goods_no":        p.goods_no,
            "goods_nm":        p.goods_nm,
            "goods_nm_eng":    p.goods_nm_eng,
            "goods_material":  p.goods_material,
            "thumbnail_url":   p.thumbnail_url,
            "goods_images":    p.goods_images,
            "notification_info": p.notification_info,
            "detail_blocks":   [{"type": b.type, "content": b.content} for b in p.detail_blocks],
            "attribute_map":   p.attribute_map,
            "title_attribute_map":  p.title_attribute_map,
            "title_keyword_map":    p.title_keyword_map,
            "detail_attribute_map": p.detail_attribute_map,
            "detail_keyword_map":   p.detail_keyword_map,
        })

    out_path = "musinsa_collected.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장 완료 → {out_path}")
SKIP_GSPREAD = os.environ.get("SKIP_GSPREAD") == "1"

if not SKIP_GSPREAD:
    BASE_DIR = Path(__file__).resolve().parent
    PROJECT_ROOT = BASE_DIR.parent
    def find_service_account_file() -> Path:
        candidates = [
            BASE_DIR / "service_account.json",
            PROJECT_ROOT / "service_account.json",
            Path.cwd() / "service_account.json",
        ]
        for p in candidates:
            if p.exists():
                return p
        if "GOOGLE_APPLICATION_CREDENTIALS" in __import__("os").environ:
            env = Path(__import__("os").environ["GOOGLE_APPLICATION_CREDENTIALS"])
            if env.exists():
                return env
        raise FileNotFoundError(
            "service_account.json 파일을 찾을 수 없습니다. 프로젝트 폴더에 배치하거나 "
            "환경변수 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
        )

    # OAuth credentials 파일을 찾는 함수
    def find_credentials_file() -> Path | None:
        """프로젝트에서 OAuth `credentials.json` 파일을 찾습니다. 없으면 None 반환."""
        candidates = [
            BASE_DIR / "credentials.json",
            PROJECT_ROOT / "credentials.json",
            Path.cwd() / "credentials.json",
            PROJECT_ROOT / "키워드 매칭_0617" / "credentials.json",
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    # OAuth 인증 연결 함수
    def oauth_connect(credentials_path: Path):
        import os, pickle
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

        SCOPES = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        token_path = BASE_DIR / "token.pickle"
        creds = None
        if token_path.exists():
            with open(token_path, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)

        return gspread.authorize(creds)

    # gspread 서비스 계정 연결 함수
    def connect_gspread(service_account_path: Path):
        return gspread.service_account(filename=str(service_account_path))

    # gspread 워크시트 로드 및 저장 함수
    def load_sheet_to_df(spreadsheet, sheet_name: str = None, index: int = 0) -> pd.DataFrame:
        if sheet_name:
            ws = spreadsheet.worksheet(sheet_name)
        else:
            ws = spreadsheet.get_worksheet(index)
        rows = ws.get_all_values()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows[1:], columns=rows[0])

    # gspread 워크시트에 DataFrame 저장 함수
    def write_df_to_new_sheet(spreadsheet, df: pd.DataFrame, sheet_title: str):
        title = sheet_title
        existing_titles = {w.title for w in spreadsheet.worksheets()}
        if title in existing_titles:
            title = f"{title}_{int(time.time())}"
        rows = len(df) + 1
        cols = len(df.columns) if df.columns is not None else 1
        ws = spreadsheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))
        values = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
        ws.update(values)
        return ws


    # 사용할 스프레드시트 URL
    sheet_url = "https://docs.google.com/spreadsheets/d/100hvoe3QQWRNsz8B9nD7NZmKfguYFpw-tQZM9YbG1o8/edit?usp=sharing"

    cred_file = find_credentials_file()
    if cred_file:
        print(f"🔑 OAuth credentials found: {cred_file}")
        gc = oauth_connect(cred_file)
    else:
        print("🔒 서비스 계정 키 파일을 찾는 중...")
        sa_file = find_service_account_file()
        print(f"✅ 서비스 계정 키: {sa_file}")
        gc = connect_gspread(sa_file)

    print("🔗 gspread로 스프레드시트에 연결합니다...")
    spreadsheet = gc.open_by_url(sheet_url)
    print("✅ 스프레드시트 연결 완료")


    # 워크시트 읽기: truth는 명시된 탭 이름, target은 두번째 탭(없으면 첫 탭)을 사용합니다.
    try:
        df_truth = load_sheet_to_df(spreadsheet, sheet_name="PDP_Test")
    except Exception as e:
        print(f"경고: 'PDP_Test' 탭을 로드할 수 없습니다: {e}. 첫번째 워크시트를 사용합니다.")
        df_truth = load_sheet_to_df(spreadsheet, index=0)
    try:
        df_target = load_sheet_to_df(spreadsheet, index=1)
    except Exception:
        df_target = load_sheet_to_df(spreadsheet, index=0)

    print(f"로드된 truth 행: {len(df_truth)}")
    print(f"로드된 target 행: {len(df_target)}")


    # 예시: 처리 결과를 새로운 시트에 저장하는 사용 사례
    def save_results_example(results_df: pd.DataFrame, title: str = "results"):
        try:
            ws = write_df_to_new_sheet(spreadsheet, results_df, sheet_title=title)
        except Exception as e:
            print(f"결과 저장 중 오류: {e}")
            raise

        # 검증: ws가 워크시트 객체인지 확인
        if ws is None:
            raise RuntimeError("write_df_to_new_sheet가 워크시트를 반환하지 않았습니다.")

        # 반환 및 메시지
        try:
            print(f"결과가 새 워크시트에 저장되었습니다: {ws.url}")
        except Exception:
            print("결과가 새 워크시트에 저장되었습니다 (워크시트 URL을 가져올 수 없음)")
        return ws

else:
    print("🔒 SKIP_GSPREAD=1 set; skipping gspread setup")
    # provide safe fallbacks for importing/running in test mode
    df_truth = pd.DataFrame()
    df_target = pd.DataFrame()
    def save_results_example(results_df: pd.DataFrame, title: str = "results"):
        raise RuntimeError("gspread is skipped; cannot save results to sheet in test mode")

# 이후 기존 스크립트의 로직에서 df_truth, df_target을 사용하면 됩니다.

if __name__ == "__main__":
    # 테스트 모드: 시트에서 상위 N개 상품번호를 가져와 로컬의 collect_product로 매핑 테스트

    def find_goods_no_column(df: pd.DataFrame) -> str | None:
        for col in df.columns:
            lc = str(col).lower()
            if "goods" in lc and "no" in lc:
                return col
            if lc in ("goods_no", "goodsno", "goods id", "no."):
                return col
        # fallback: 숫자형 컬럼 중 첫 번째
        for col in df.columns:
            if pd.api.types.is_integer_dtype(df[col]) or pd.api.types.is_float_dtype(df[col]):
                return col
        return None

    try:
        col = find_goods_no_column(df_truth)
        if col is None:
            raise RuntimeError("goods_no 컬럼을 찾을 수 없습니다. df_truth 컬럼: " + ",".join(map(str, df_truth.columns)))

        goods_col = df_truth[col]
        if isinstance(goods_col, pd.DataFrame):
            goods_col = goods_col.iloc[:, 0]
        goods_list = goods_col.dropna().astype(str).unique().tolist()
        # 기본 동작: 상위 50개만 테스트합니다. 필요시 이 값을 늘리세요.
        top_n = goods_list[:50]
        if not top_n:
            raise RuntimeError("테스트할 상품번호가 없습니다.")

        print(f"테스트 대상 상품번호 상위 {len(top_n)}개: {top_n}")

        results = []
        for gno in top_n:
            print(f"\n--- 수집 & 매핑 테스트: goods_no={gno} ---")
            try:
                p = collect_product(gno)
                # 소스 분리 매핑 결과 출력
                print("매핑 결과 (상품명 | PDP):")
                for attr in ATTRIBUTE_RULES.keys():
                    t = p.title_attribute_map.get(attr, "")
                    tk = p.title_keyword_map.get(attr, "")
                    d = p.detail_attribute_map.get(attr, "")
                    dk = p.detail_keyword_map.get(attr, "")
                    print(f"  {attr:12} : 상품명={t}({tk}) | PDP={d}({dk})")

                # 결과 행 구성: 속성별로 상품명/ PDP 추출을 분리한 컬럼 생성
                row = {
                    "goods_no": gno,
                    "brand": getattr(p, "brand", ""),
                    "goods_nm": getattr(p, "goods_nm_raw", p.goods_nm),
                }
                for attr in ATTRIBUTE_RULES.keys():
                    t = p.title_attribute_map.get(attr, "")
                    tk = p.title_keyword_map.get(attr, "")
                    d = p.detail_attribute_map.get(attr, "")
                    dk = p.detail_keyword_map.get(attr, "")
                    # 값 + 매칭된 키워드를 함께 기록 (정확도 비교용)
                    row[f"{attr}_상품명"] = f"{t} ({tk})" if t else ""
                    row[f"{attr}_PDP"] = f"{d} ({dk})" if d else ""
                results.append(row)
            except Exception as ex:
                print(f"상품 {gno} 처리 중 오류: {ex}")

        # 성공한 항목이 있으면 시트에 저장
        if results:
            # results는 이미 평탄화된 행 (속성별 상품명/PDP 컬럼 포함)
            df_final = pd.DataFrame(results)

            # 브랜드, 상품명 컬럼 추가 및 정렬
            if "brand" not in df_final.columns:
                df_final["brand"] = ""
            if "goods_nm" not in df_final.columns:
                df_final["goods_nm"] = ""
            try:
                df_final = df_final.sort_values(by=["brand", "goods_no"])
            except Exception:
                pass

            print("\n성공 항목을 스프레드시트에 저장합니다...")
            try:
                ws = save_results_example(df_final, title="mapping_test_results")
                print(f"시트에 저장된 결과 워크시트: {ws.title}")
            except Exception as ex:
                print("결과 저장 실패:", ex)
        else:
            print("저장할 성공 항목이 없습니다.")

        print("\n테스트 완료")
    except Exception as e:
        print("테스트 실패:", e)





