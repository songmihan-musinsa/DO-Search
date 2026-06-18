"""
mesh_single_attribute 전용 테스트 
=======================================================
- 시트(df_truth)에서 상위 TOP_N개 goods_no를 가져와 크롤링/매핑
- 상품명(title) vs PDP(detail) 추출 속성을 '분리 컬럼'으로 만들어
  콘솔에 표로 출력하고 구글 스프레드시트에 새 워크시트로 저장

"""
from datetime import datetime
import time

from 상품명PDP키워드매칭_0617 import mesh_single_attribute_v2 as msa
import pandas as pd

# ✅ 시작 위치(0-based)와 개수. START_OFFSET=100 → 101번째부터, TOP_N=100 → 100개
# ✅  201~300번을 돌리려면 START_OFFSET만 200으로 바꾸고 TOP_N은 100으로 유지
START_OFFSET = 100
TOP_N = 100

# 인증/시트 로드 실패 시 사용할 샘플 목록
goods_list_fallback = ["6111868", "6502732", "6000000", "6200000", "6300000"]


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


def get_goods_list() -> list[str]:
    """시트의 df_truth에서 START_OFFSET 위치부터 TOP_N개 goods_no를 추출 (실패 시 fallback)."""
    try:
        df = msa.df_truth
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
    return goods_list_fallback[START_OFFSET:START_OFFSET + TOP_N]


def build_rows(results) -> list[dict]:
    """상품명/PDP 추출을 분리한 행 목록 생성 (값 + 매칭 키워드)."""
    rows = []
    for p in results:
        row = {
            "goods_no": p.goods_no,
            "brand": getattr(p, "brand", ""),
            "goods_nm": getattr(p, "goods_nm_raw", p.goods_nm),
        }
        for attr in msa.ATTRIBUTE_RULES.keys():
            t = p.title_attribute_map.get(attr, "")
            tk = p.title_keyword_map.get(attr, "")
            d = p.detail_attribute_map.get(attr, "")
            dk = p.detail_keyword_map.get(attr, "")
            row[f"{attr}_상품명"] = f"{t} ({tk})" if t else ""
            row[f"{attr}_PDP"] = f"{d} ({dk})" if d else ""
        rows.append(row)
    return rows


def main():
    goods = get_goods_list()
    print(f"테스트 대상: {START_OFFSET + 1}번째부터 {len(goods)}개 (인덱스 {START_OFFSET}~{START_OFFSET + len(goods) - 1})")
    print(f"  {goods}\n")

    t0 = time.time()
    results = msa.collect_batch(goods, sleep_sec=0.8)
    elapsed = time.time() - t0
    n_done = len(results)
    per_item = (elapsed / n_done) if n_done else 0.0
    print(
        f"\n⏱ 소요시간: {elapsed:.1f}s | 상품 {n_done}개 | "
        f"평균 {per_item:.2f}s/건 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    rows = build_rows(results)
    df_final = pd.DataFrame(rows)

    # 콘솔 출력 (전체 컬럼 표시)
    print("\n[결과 — 상품명 vs PDP 분리 추출]")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(df_final.to_string(index=False))

    # 구글 스프레드시트에 새 워크시트로 저장
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = f"mapping_test_results_v2_{ts}"
    print(f"\n구글 스프레드시트에 저장합니다: {title}")
    ws = msa.save_results_example(df_final, title=title)
    print(f"✅ 저장 완료 → 워크시트: {getattr(ws, 'title', str(ws))}")
    try:
        print(f"   URL: {ws.url}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
