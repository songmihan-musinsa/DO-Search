"""
attribute_filter_final.py  —  함수 정리 모듈
================================================
무신사 PDP 수집 + '상품명(goodsNm) 기반' 단일 속성 레이블링 함수 모음.

이 파일의 역할(함수만 모음):
  - PDP HTML(__NEXT_DATA__) 파싱  ← 기존 로직 동일
  - JSON에서 수집: 브랜드명 / 상품명 / 썸네일이미지 / 디테일 대표이미지 1장 / 디테일이미지 개수
  - 상품명에서만 속성 키워드 추출 (PDP 상세/OCR 사용 안 함)
  - 구글시트 연동/저장 헬퍼  ← 기존 로직 동일

속성 정의(키워드)는 attributes.json 으로 분리해서 정리
실행은 run.py 
"""

from __future__ import annotations

import re
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import requests

try:
    import pandas as pd
except Exception:
    pd = None
try:
    import gspread
except Exception:
    gspread = None


# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

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

PDP_URL  = "https://www.musinsa.com/products/{goods_no}"
CDN_BASE = "https://image.msscdn.net"


# ──────────────────────────────────────────────
# 속성 정의 로드 (attributes.json)
# ──────────────────────────────────────────────

def load_attribute_config(path: Optional[Path] = None) -> dict:
    """attributes.json 을 읽어 속성 정의를 반환합니다."""
    cfg_path = path or (BASE_DIR / "attributes.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {
        "ATTRIBUTE_RULES": cfg.get("ATTRIBUTE_RULES", {}),
        "MESH_KEYWORDS": cfg.get("MESH_KEYWORDS", []),
        "MESH_CONTEXT_WORDS": cfg.get("MESH_CONTEXT_WORDS", []),
    }


_CONFIG = load_attribute_config()
ATTRIBUTE_RULES: dict = _CONFIG["ATTRIBUTE_RULES"]
MESH_KEYWORDS: list = _CONFIG["MESH_KEYWORDS"]
MESH_CONTEXT_WORDS: list = _CONFIG["MESH_CONTEXT_WORDS"]


# ──────────────────────────────────────────────
# 데이터 구조
# ──────────────────────────────────────────────

@dataclass
class ProductData:
    goods_no: str
    brand: str = ""                          # 브랜드명
    goods_nm: str = ""                        # 상품명
    thumbnail_url: str = ""                   # 썸네일 이미지
    rep_detail_image: Optional[str] = None    # 디테일 이미지 대표 1장 (없으면 None)
    detail_image_count: int = 0               # 디테일 이미지 개수
    detail_image_urls: list[str] = field(default_factory=list)  # 디테일 이미지 전체 URL 목록
    title_text: str = ""                      # 속성 추출에 사용한 상품명 텍스트
    attribute_map: dict = field(default_factory=dict)          # {속성: 값}
    attribute_keyword_map: dict = field(default_factory=dict)  # {속성: 매칭 키워드}


# ──────────────────────────────────────────────
# HTML 파싱 유틸  (기존 로직 동일)
# ──────────────────────────────────────────────

def extract_next_data(html: str) -> Optional[dict]:
    """<script id="__NEXT_DATA__" type="application/json">...</script> 추출"""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("data:"):
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return CDN_BASE + url
    return url


def to_thumb_url(path: str) -> str:
    """msscdn 이미지 경로를 PDP 노출용 thumbnails URL로 변환합니다.

    예) '/images/prd_img/.../detail_xxx.jpg'
        → 'https://image.msscdn.net/thumbnails/images/prd_img/.../detail_xxx.jpg'
    """
    if not path:
        return ""
    if path.startswith("data:"):
        return ""
    if path.startswith("//"):
        return "https:" + path
    if path.startswith("/"):
        return CDN_BASE + "/thumbnails" + path
    return path


def extract_detail_image_paths(goods_images) -> list[str]:
    """meta.data.goodsImages([{imageUrl: ...}]) 에서 imageUrl 경로 목록을 추출."""
    if not isinstance(goods_images, list):
        return []
    paths: list[str] = []
    for img in goods_images:
        if isinstance(img, dict) and img.get("imageUrl"):
            paths.append(img["imageUrl"])
    return paths


def ensure_text(value) -> str:
    """값을 문자열로 안전 변환."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(map(ensure_text, value))
    if isinstance(value, dict):
        return " ".join(ensure_text(v) for v in value.values())
    return str(value)


# ──────────────────────────────────────────────
# 상품명 기반 속성 추출
# ──────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", (text or "")) if t]


def _all_attribute_keyword_tokens() -> set[str]:
    """속성 키워드 토큰 집합 (브랜드 토큰 제거 시 보호용)."""
    toks: set[str] = set()
    for candidates in ATTRIBUTE_RULES.values():
        for keywords in candidates.values():
            for kw in keywords:
                for t in _tokenize(kw):
                    toks.add(t.lower())
    return toks


_PROTECTED_TOKENS = _all_attribute_keyword_tokens()


def remove_brand_tokens(text: str, brand: str) -> str:
    """토큰 단위로 브랜드명을 제거. 단, 속성 키워드와 겹치는 토큰은 보호."""
    if not text or not brand:
        return text or ""
    tokens = [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", brand) if t]
    out = text
    for tk in tokens:
        if not tk or tk.lower() in _PROTECTED_TOKENS:
            continue
        out = re.sub(rf"\b{re.escape(tk)}\b", "", out, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", out).strip()


def _is_mesh_english_accepted(text_lower: str) -> bool:
    """영문 'mesh'는 모자/소재 맥락 단어가 함께 있을 때만 인정 (오탐 방지)."""
    return any(ctx in text_lower for ctx in MESH_CONTEXT_WORDS)


def extract_attributes_from_title(title_text: str, brand: str = "") -> dict:
    """
    상품명(title_text)에서만 속성을 추출합니다. (PDP 상세/OCR 사용 안 함)

    반환: {속성: {"value": 레이블, "keyword": 매칭키워드}, ...}
    브랜드명은 토큰 제거 후 매칭에서 제외합니다.
    """
    title_clean = remove_brand_tokens(title_text or "", brand)
    text_lower = title_clean.lower()
    result: dict[str, dict[str, str]] = {}

    for attr, candidates in ATTRIBUTE_RULES.items():
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
                    # 영문 'mesh'만 맥락 검사, 그 외(한글 포함)는 그대로 인정
                    if kw_lc == "mesh" and not _is_mesh_english_accepted(text_lower):
                        continue
                    matched_label = label
                    matched_kw = kw
                    break
            if matched_label:
                break
        if matched_label:
            result[attr] = {"value": matched_label, "keyword": matched_kw}

    return result


# ──────────────────────────────────────────────
# HTTP 요청  (기존 로직 동일)
# ──────────────────────────────────────────────

def fetch(url: str, as_json: bool = False, timeout: int = 10):
    import certifi
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    connect_timeout = 5
    read_timeout = timeout if isinstance(timeout, (int, float)) else 10
    try:
        resp = session.get(url, headers=HEADERS, timeout=(connect_timeout, read_timeout), verify=certifi.where())
        resp.raise_for_status()
        return resp.json() if as_json else resp.text
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
# 메인 수집: PDP → 필요한 필드 + 상품명 속성
# ──────────────────────────────────────────────

def collect_product(goods_no: str | int) -> ProductData:
    goods_no = str(goods_no)
    product = ProductData(goods_no=goods_no)

    print(f"[PDP] {PDP_URL.format(goods_no=goods_no)}")
    html = fetch(PDP_URL.format(goods_no=goods_no))
    if not html:
        return product

    next_data = extract_next_data(html)
    if not next_data:
        print(f"  [WARN] goods_no={goods_no} __NEXT_DATA__ 추출 실패")
        return product

    try:
        props = next_data.get("props") or {}
        page_props = props.get("pageProps") or {}
        meta = page_props.get("meta") or {}
        data = meta.get("data") or {}

        # 브랜드명 / 상품명 / 썸네일
        product.brand = data.get("brandName") or data.get("brand") or data.get("maker") or ""
        product.goods_nm = data.get("goodsNm", "") or ""
        product.thumbnail_url = normalize_url(data.get("thumbnailImageUrl", ""))

        # 디테일 이미지: meta.data.goodsImages(=상세 이미지, msscdn) 기반
        #  - detail_image_count : 디테일 이미지 개수 (goodsImages)
        #  - rep_detail_image   : 대표 1장 (첫 디테일 이미지, 없으면 None)
        #  - detail_image_urls  : 디테일 이미지 + 썸네일 (PDP 노출용 thumbnails URL)
        detail_paths = extract_detail_image_paths(data.get("goodsImages", []))
        detail_urls = [u for u in (to_thumb_url(p) for p in detail_paths) if u]

        product.detail_image_count = len(detail_urls)
        product.rep_detail_image = detail_urls[0] if detail_urls else None

        thumb_url = to_thumb_url(data.get("thumbnailImageUrl", ""))
        product.detail_image_urls = detail_urls + ([thumb_url] if thumb_url else [])

        # 속성 추출용 텍스트 = 상품명만
        product.title_text = ensure_text(product.goods_nm)

        # 상품명에서만 속성 추출
        mapped = extract_attributes_from_title(product.title_text, brand=product.brand)
        product.attribute_map = {k: v["value"] for k, v in mapped.items()}
        product.attribute_keyword_map = {k: v["keyword"] for k, v in mapped.items()}
    except Exception as e:
        print(f"  [ERROR] goods_no={goods_no} 처리 중 예외: {e}")

    return product


def collect_batch(goods_no_list: list[str], sleep_sec: float = 0.8) -> list[ProductData]:
    results = []
    n = len(goods_no_list)
    for i, gno in enumerate(goods_no_list):
        print(f"[{i+1}/{n}] goods_no={gno}")
        results.append(collect_product(gno))
        if i < n - 1:
            time.sleep(sleep_sec)
    return results


# ──────────────────────────────────────────────
# 구글시트 연동/저장 헬퍼  (기존 로직 동일)
# ──────────────────────────────────────────────

def find_service_account_file() -> Path:
    candidates = [
        BASE_DIR / "service_account.json",
        PROJECT_ROOT / "service_account.json",
        Path.cwd() / "service_account.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    import os
    if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
        env = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        if env.exists():
            return env
    raise FileNotFoundError(
        "service_account.json 파일을 찾을 수 없습니다. 프로젝트 폴더에 배치하거나 "
        "환경변수 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
    )


def find_credentials_file() -> Optional[Path]:
    """OAuth credentials.json 탐색 (없으면 None)."""
    candidates = [
        BASE_DIR / "credentials.json",
        PROJECT_ROOT / "credentials.json",
        Path.cwd() / "credentials.json",
        PROJECT_ROOT / "키워드 매칭_0617" / "credentials.json",
        PROJECT_ROOT / "상품명PDP키워드매칭_0617" / "credentials.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_existing_token() -> Optional[Path]:
    """이미 인증된 token.pickle 을 재사용하기 위해 후보 위치를 탐색."""
    candidates = [
        BASE_DIR / "token.pickle",
        PROJECT_ROOT / "상품명PDP키워드매칭_0617" / "token.pickle",
        PROJECT_ROOT / "키워드 매칭_0617" / "token.pickle",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def oauth_connect(credentials_path: Path):
    import pickle
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    token_path = BASE_DIR / "token.pickle"
    creds = None
    existing = _find_existing_token()
    if existing:
        with open(existing, "rb") as f:
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


def connect_gspread(service_account_path: Path):
    return gspread.service_account(filename=str(service_account_path))


def connect_sheet(sheet_url: str):
    """credentials(OAuth) 우선, 없으면 서비스 계정으로 연결 후 스프레드시트를 엽니다."""
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
    return spreadsheet


def load_sheet_to_df(spreadsheet, sheet_name: str = None, index: int = 0):
    if sheet_name:
        ws = spreadsheet.worksheet(sheet_name)
    else:
        ws = spreadsheet.get_worksheet(index)
    rows = ws.get_all_values()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows[1:], columns=rows[0])


def write_df_to_new_sheet(spreadsheet, df, sheet_title: str):
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
