"""
run.py  —  실행 진입점
========================
1) 구글 시트(PDP_Test 탭)에서 goods_no 목록을 읽고
2) 각 상품의 PDP에서 브랜드/상품명/썸네일/디테일대표이미지/디테일이미지개수를 수집
3) '상품명에서만' 속성을 추출
4) 결과를 구글 스프레드시트에 새 워크시트로 저장

"""

# google-auth 토큰 갱신 시 'self-signed certificate in chain' 오류 방지.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from datetime import datetime
import time

import pandas as pd

from 최종_단일속성레이블링 import attribute_filter_final as af


# ──────────────────────────────────────────────
# ✅ 설정 (여기만 바꾸면 됨) ✅
# ──────────────────────────────────────────────
SHEET_URL = "https://docs.google.com/spreadsheets/d/100hvoe3QQWRNsz8B9nD7NZmKfguYFpw-tQZM9YbG1o8/edit?usp=sharing"
TRUTH_SHEET = "PDP_Test"       # goods_no 목록이 있는 탭

START_OFFSET = 0               # 시작 위치(0-based). 101번째부터면 100
TOP_N = 100                     # 처리 개수
SLEEP_SEC = 0.6                # 요청 간 대기

# 인증/시트 로드 실패 시 사용할 샘플 목록
GOODS_LIST_FALLBACK = ["6111868", "6502732", "6000000", "6200000", "6300000"]


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def find_goods_no_column(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        lc = str(col).lower()
        if "goods" in lc and "no" in lc:
            return col
        if lc in ("goods_no", "goodsno", "goods id", "no."):
            return col
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]) or pd.api.types.is_float_dtype(df[col]):
            return col
    return None


def get_goods_list(spreadsheet) -> list[str]:
    """PDP_Test 탭에서 START_OFFSET 위치부터 TOP_N개 goods_no를 추출 (실패 시 fallback)."""
    try:
        try:
            df = af.load_sheet_to_df(spreadsheet, sheet_name=TRUTH_SHEET)
        except Exception as e:
            print(f"경고: '{TRUTH_SHEET}' 탭 로드 실패: {e}. 첫 번째 워크시트를 사용합니다.")
            df = af.load_sheet_to_df(spreadsheet, index=0)

        col = find_goods_no_column(df)
        if col is not None:
            goods_col = df[col]
            if isinstance(goods_col, pd.DataFrame):
                goods_col = goods_col.iloc[:, 0]
            goods_list = goods_col.dropna().astype(str).unique().tolist()
            if goods_list:
                return goods_list[START_OFFSET:START_OFFSET + TOP_N]
    except Exception as e:
        print(f"[WARN] 시트에서 goods_no를 읽지 못했습니다: {e}")
    return GOODS_LIST_FALLBACK[START_OFFSET:START_OFFSET + TOP_N]


def build_rows(results) -> list[dict]:
    """결과 → 시트 저장용 행. 요청 필드 + 상품명 기반 속성 컬럼."""
    rows = []
    for p in results:
        row = {
            "goods_no": p.goods_no,
            "brand": p.brand,
            "goods_nm": p.goods_nm,
            "thumbnail_url": p.thumbnail_url,
            "rep_detail_image": p.rep_detail_image if p.rep_detail_image else "",  # 대표 1장 (없으면 빈칸)
            "detail_image_count": p.detail_image_count,
            "detail_image_urls": "|".join(p.detail_image_urls),  # 전체 URL을 | 로 연결
        }
        # 상품명에서 추출한 속성 (값 / 매칭 키워드)
        for attr in af.ATTRIBUTE_RULES.keys():
            row[attr] = p.attribute_map.get(attr, "")
            row[f"{attr}_매칭키워드"] = p.attribute_keyword_map.get(attr, "")
        rows.append(row)
    return rows


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main():
    spreadsheet = af.connect_sheet(SHEET_URL)

    goods = get_goods_list(spreadsheet)
    print(f"테스트 대상: {START_OFFSET + 1}번째부터 {len(goods)}개 "
          f"(인덱스 {START_OFFSET}~{START_OFFSET + len(goods) - 1})")
    print(f"  {goods}\n")

    t0 = time.time()
    results = af.collect_batch(goods, sleep_sec=SLEEP_SEC)
    elapsed = time.time() - t0
    n_done = len(results)
    per_item = (elapsed / n_done) if n_done else 0.0
    mins, secs = divmod(int(elapsed), 60)
    elapsed_msg = (
        f"⏱ 소요시간: {mins}분 {secs}초 ({elapsed:.1f}s) | "
        f"상품 {n_done}개 | 평균 {per_item:.2f}s/건 | sleep={SLEEP_SEC}s 포함 | "
        f"측정시각 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"\n{elapsed_msg}")

    df_final = pd.DataFrame(build_rows(results))

    print("\n[결과 — 상품명 기반 단일 속성 레이블링]")
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(df_final.to_string(index=False))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = f"attr_final_results_{ts}"
    print(f"\n구글 스프레드시트에 저장합니다: {title}")
    ws = af.write_df_to_new_sheet(spreadsheet, df_final, sheet_title=title)
    print(f"✅ 저장 완료 → 워크시트: {getattr(ws, 'title', str(ws))}")
    try:
        print(f"   URL: {ws.url}")
    except Exception:
        pass

    # 실행 속도를 시트 맨 위(1행)에 기재 — 표는 한 칸 아래로 밀림
    try:
        ws.insert_row([elapsed_msg], index=1)
        print(f"⏱ 소요시간을 시트 A1에 기재했습니다: {elapsed_msg}")
    except Exception as ex:
        print(f"소요시간 기재 실패: {ex}")


if __name__ == "__main__":
    main()

