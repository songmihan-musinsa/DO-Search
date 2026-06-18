  
## 코드리뷰 및 개발 문서

**대상:**

[상품명PDP키워드매칭_0617/mesh_single_attribute.py](상품명PDP키워드매칭_0617/mesh_single_attribute.py) and [상품명PDP키워드매칭_0617/test_run_5.py](상품명PDP키워드매칭_0617/test_run_5.py)




**개발 명세**
- **Python 환경**: 프로젝트는 가상환경(예: `.venv`)에서 실행을 전제로 합니다. 필수 패키지: `requests`, `pandas`, `gspread`, `google-auth-oauthlib`, `google-auth` 등(환경에 따라 추가). 의존성은 `requirements.txt`에 정리 권장.
- **환경 변수**:
  - **SKIP_GSPREAD**: `1`로 설정하면 Google Sheets 연동을 건너뜁니다 (테스트용).
  - **GOOGLE_APPLICATION_CREDENTIALS**: 서비스 계정 키 경로 지정 시 사용.
- **파일/토큰**:
  - `credentials.json` (OAuth client) 또는 `service_account.json` (서비스 계정)을 프로젝트 경로에서 검색합니다.
  - OAuth 실행시 `token.pickle`을 `BASE_DIR`에 저장합니다.



**1) 환경 변수 설정 (요약)**
- 로컬 테스트에서 Google 연동을 끄려면:

```powershell
$env:SKIP_GSPREAD = "1"
& .venv\Scripts\python.exe 상품명PDP키워드매칭_0617/test_run_5.py
```

- 실제 시트에 저장하려면 `credentials.json`이나 `service_account.json`을 프로젝트에 배치하거나 `GOOGLE_APPLICATION_CREDENTIALS`를 설정하세요.

  

**2) 데이터 소스 및 처리 흐름**
- PDP HTML: `https://www.musinsa.com/products/{goods_no}`의 페이지에서 `<script id="__NEXT_DATA__">` JSON을 파싱해 `meta.data`로부터 주요 필드 추출.
- API 호출: `goods-detail`의 엔드포인트 사용
  - `GOODS_DETAIL_API = https://goods-detail.musinsa.com/api2/goods/{goods_no}/essential`
  - `ACTUAL_SIZE_API  = https://goods-detail.musinsa.com/api2/goods/{goods_no}/actual-size`
- 수집 항목: `goodsNm`, `goodsNmEng`, `thumbnailImageUrl`, `goodsImages[]`, `goodsContents` (상세 HTML), `goodsMaterial`, `notificationInfo` 등.
- 상세 설명(`goodsContents`)은 `extract_detail_blocks`로 텍스트/이미지 블록으로 분리.



**3) 구글 시트 연동 (OAuth / 로컬)**
- `find_credentials_file()`와 `find_service_account_file()`로 키 파일 탐색.
- OAuth: `oauth_connect()`는 `InstalledAppFlow`를 사용해 `token.pickle`을 생성합니다.
- 서비스 계정: `connect_gspread(service_account_path)` 호출로 `gspread` 인증.
- 시트 읽기/쓰기 헬퍼:
  - `load_sheet_to_df(spreadsheet, sheet_name=None, index=0)` → DataFrame
  - `write_df_to_new_sheet(spreadsheet, df, sheet_title)` → 새 워크시트 생성 후 `ws.update(values)`로 전체 업데이트
- 주의: 현재 모듈은 import 시점에 (환경에 따라) 즉시 시트 연결 시도를 합니다. (`SKIP_GSPREAD`로 건너뛸 수 있음)



**4) 함수별 기능 요약 (핵심)**
- `extract_next_data(html) -> Optional[dict]` : `<script id="__NEXT_DATA__">` 내부 JSON 파싱.
- `normalize_url(url) -> str` : CDN 접두사 보정, data: URL 무시 등.
- `extract_image_src(img_tag) -> str` : `<img>` 태그에서 src 추출 및 정규화.
- `normalize_text(raw_html) -> str` : HTML → 텍스트 정리 (스크립트/스타일 제거, 엔티티 언패킹).
- `extract_detail_blocks(goods_contents) -> list[ContentBlock]` : 이미지/텍스트 블록 분리.
- `map_attributes_by_source(title_text, detail_text, ocr_text, brand) -> dict` : 소스별( `title`, `detail`, `ocr`)로 키워드 매칭 후 우선순위에 따라 라벨 결정. 반환은 `{속성: {"value": 라벨, "source": 소스}}`.
- `legacy_map_attributes(text)`, `map_attributes(text)` : 단일 text 기반의 하위호환 매핑.
- `map_material_single(material_text) -> str` : `goodsMaterial` 필드 기반 단일 소재 라벨 판정.
- `fetch(url, as_json=False)` : `requests.Session` + retry를 사용하는 HTTP 헬퍼(현재는 호출할 때마다 새 Session 생성).
- `collect_product(goods_no)` : PDP HTML → API → 텍스트 결합 → `map_attributes_by_source` 호출하여 `ProductData` 생성.
- `collect_batch(goods_no_list, sleep_sec)` : 순차 처리, `sleep_sec`으로 서버 부하 완화.
- `print_product_summary(p)` : 콘솔 출력 포맷.
- Google 관련: `oauth_connect`, `connect_gspread`, `load_sheet_to_df`, `write_df_to_new_sheet`, `save_results_example`.

참고 소스 파일: [상품명PDP키워드매칭_0617/mesh_single_attribute.py](상품명PDP키워드매칭_0617/mesh_single_attribute.py)



**test_run_5.py (실행 스크립트)**
- `TOP_N` 상수로 상위 N개 처리 (파일 최상단에 `TOP_N = 1000` 등으로 표기; 변경시 이 위치만 수정하면 됩니다).
- `find_goods_no_column(df)`로 `df_truth`에서 goods_no 컬럼 감지.
- `goods` 리스트가 비어 있으면 `goods_list_fallback` 사용.
- `collect_batch(goods, sleep_sec=0.8)` 호출로 매핑 수행.
- 결과를 `pandas.json_normalize`로 평탄화하여 `save_results_example(df_final, title=...)`로 Google Sheet에 저장.
- 실행시간 요약(소요시간 메시지)을 시트 맨 위(A1)에 `ws.insert_row`로 삽입.

파일 위치: [상품명PDP키워드매칭_0617/test_run_5.py](상품명PDP키워드매칭_0617/test_run_5.py)



**속성 매칭 로직 상세**
- 규칙 데이터: `ATTRIBUTE_RULES` 딕셔너리로 속성별(예: `패널수`, `모자종류`, `소재`) 라벨과 키워드 집합 정의.
- 메시 감지: `MESH_KEYWORDS` + 영어 `mesh`의 문맥 검사 (`_is_mesh_accepted`).
- 브랜드 제거: `title_text`에서 `_remove_brand_tokens`로 브랜드 토큰을 제거하되, 속성 키워드와 겹치는 토큰은 보호합니다( `_PROTECTED_TOKENS`).
- 소스 별 우선순위: `SOURCE_PRIORITY = ["title", "detail", "ocr"]` (현재 설정).
- 매칭절차: 각 속성별로 SOURCE_PRIORITY 순서로 소스 검사 → 키워드 포함 여부로 라벨 결정 → special-case 처리 후 결과 기록.



**5) 실행 결과 / 성능 (사용자 측정)**
- **1000개 시행 결과**: 총 소요 50분 52초 (50:52) — 평균 **3.05s/건** ( `sleep = 0.8s` 포함).
  - 단순 계산: 평균 처리 시간 3.05s = sleep(0.8s) + 네트워크/파싱 오버헤드 약 **2.25s/건**.



**성능 개선 권장(우선순위 높은 것부터)**
1. **Session 재사용 (저난이도, 큰 효과)**
   - 현재 `fetch()`가 호출할 때마다 `requests.Session()`을 새로 생성합니다. 모듈 레벨에 세션을 하나 만들고 재사용하면 TCP 연결 재사용으로 오버헤드가 크게 줄어듭니다.
   - 예: 모듈 초기화 시 `SESSION = requests.Session()` 및 `SESSION.mount(...)` 설정 후 `fetch()`에서 재사용.

2. **병렬화 (중간 난이도, 큰 효과)**
   - IO-bound 작업이므로 `ThreadPoolExecutor`(권장: 8~16 worker) 또는 `asyncio + aiohttp`로 동시 요청을 수행하면 처리량이 증가합니다.
   - 단, 대상 사이트의 rate limit/ToS를 준수해야 하며 적절한 릴레이/백오프 필요.

3. **sleep 전략 개선 (즉시 적용 가능)**
   - 고정 `sleep_sec` 대신 동적 throttling 사용 (동시성 기반, 실패율에 따라 증가).
   - 테스트 환경에서는 `sleep_sec`을 낮춰도 좋음(예: 0.2).

4. **Google Sheets 쓰기 최적화**
   - 한 건씩 쓰지 말고 메모리에서 완성한 표를 한 번에 `ws.update`로 업로드.
   - 대량 업데이트 시 `gspread`의 `batch_update` 또는 Google Sheets API `values_batch_update` 사용 권장.
   - 중간 결과(체크포인트)는 로컬 JSON으로 주기 저장하고, 시트에는 배치 단위(예: 100건)로 업로드.

5. **API 우선 사용**
   - PDP HTML 파싱 대신 `goods-detail` API에서 필요한 정보를 우선 추출하면 파싱 비용 절감.

6. **Checkpoint & Resume (중요)**
   - 실행 중단 대비: N건마다 로컬에 JSON 저장 및(선택) Google Sheet에 중간 업로드.
   - `test_run_5.py`에 chunked save 로직을 추가 권장.

7. **프로파일링/모니터링**
   - `collect_product` 내부 각 단계(HTML fetch, essential API, actual-size API, parsing, mapping)의 시간을 측정해 병목 식별.


**추가 권장 작업 (우선순위 정리)**
- (A) 세션 재사용 적용 + `fetch()` 리팩터링. (빠른 이득)
- (B) 로컬 체크포인트(예: 50~100건 마다 JSON 저장) 구현. (안전성)
- (C) Google Sheets 배치 업로드 도입(100건 단위). (속도/비용 개선)
- (D) 병렬화(Pool) 시범 도입 및 부하/에러 모니터링. (고성능)
- (E) 유닛 테스트 추가: `map_attributes_by_source`, `map_material_single`, `extract_next_data` 등 핵심 로직.

---

**리스크 / 주의사항**
- 타깃 사이트의 접근 정책(robots.txt, ToS)을 확인하고 수집 빈도 제한을 준수하세요.
- Google API 호출 제한 및 인증 만료 케이스(토큰 refresh) 예외 처리를 추가해야 합니다.
- 민감한 인증 파일(`service_account.json`, `credentials.json`)은 절대 저장소에 커밋하지 마세요.

---

**결론 및 다음 단계 제안**
- 우선 `fetch()`의 `Session` 재사용과 로컬 체크포인트 저장을 먼저 적용하면 안정성과 속도 모두 개선됩니다.
- 적용 원하시면 제가 (1) `fetch()` 리팩터링, (2) `test_run_5.py`에 체크포인트 저장 추가, (3) Google Sheet 배치 업로드 예제 코드를 차례로 만들어 드리겠습니다.


---

*문서 생성 위치:* [CODE_REVIEW.md](CODE_REVIEW.md)


<img width="510" height="421" alt="image" src="https://github.com/user-attachments/assets/c1a1325a-cac7-4f35-a450-8f585897865e" />
