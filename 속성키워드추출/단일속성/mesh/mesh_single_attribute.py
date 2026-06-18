from datetime import datetime
import time
from 상품명PDP키워드매칭_0617 import mesh_single_attribute as msa
import pandas as pd

# ✅ 변경포인트: 테스트할 상위 N개 (숫자를 바꿀 때 이 줄을 수정하세요)
TOP_N = 1000

# 기본 동작: 스프레드시트의 `df_truth`에서 상위 TOP_N개 상품번호를 가져옵니다.
# ✅ 변경포인트: goods_list_fallback을 수정하면 인증이 없을 때 사용할 샘플 목록을 바꿀 수 있습니다.
goods_list_fallback = ["6111868", "6502732", "6000000", "6200000", "6300000"]

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

print(f"Running collect_batch for top {TOP_N} goods...\n")

# try to extract goods list from sheet-loaded df_truth
goods = None
try:
    df = msa.df_truth
    col = find_goods_no_column(df)
    if col is not None:
        goods_col = df[col]
        if isinstance(goods_col, pd.DataFrame):
            goods_col = goods_col.iloc[:, 0]
        goods_list = goods_col.dropna().astype(str).unique().tolist()
        goods = goods_list[:TOP_N]
except Exception:
    goods = None

if not goods:
    # fallback to the small sample list
    goods = goods_list_fallback[:TOP_N]

t0 = time.time()
results = msa.collect_batch(goods, sleep_sec=0.8)
elapsed = time.time() - t0
n_done = len(results)
mins, secs = divmod(int(elapsed), 60)
per_item = (elapsed / n_done) if n_done else 0.0
elapsed_msg = (
    f"소요시간: {mins}분 {secs}초 ({elapsed:.1f}s) | "
    f"상품 {n_done}개 | 평균 {per_item:.2f}s/건 | sleep=0.8s 포함 | "
    f"측정시각 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)
print(f"\n⏱ {elapsed_msg}")

# prepare results dataframe
out = []
for p in results:
    out.append({
        "goods_no": p.goods_no,
        "brand": getattr(p, "brand", ""),
        "goods_nm": getattr(p, "goods_nm_raw", p.goods_nm),
        "attribute_map": p.attribute_map,
    })

df_res = pd.json_normalize(out)
if "attribute_map" in df_res.columns:
    attr_df = pd.json_normalize(df_res["attribute_map"]).add_prefix("")
    df_final = pd.concat([df_res.drop(columns=["attribute_map"]), attr_df], axis=1)
else:
    df_final = df_res

# sheet title with timestamp
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
title = f"mapping_test_results_{ts}"

print(f"Saving results to Google Sheet as new worksheet: {title}")
ws = msa.save_results_example(df_final, title=title)
print(f"Saved to worksheet: {getattr(ws, 'title', str(ws))}")

# 소요시간을 시트 맨 위(A1)에 기재: 새 행을 1행에 삽입하여 표는 한 칸 아래로 밀림
try:
    ws.insert_row([elapsed_msg], index=1)
    print(f"⏱ 소요시간을 A1에 기재했습니다: {elapsed_msg}")
except Exception as ex:
    print(f"소요시간 기재 실패: {ex}")
