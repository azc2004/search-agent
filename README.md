# 검색어 기반 LLM 추천 안내 & 추천상품 상세 (Streamlit PoC)

검색어 1건 → 검색 실데이터 수집 → LLM 트렌드 안내문+검색어추출 → 필터 해상→검색API 상위5 → 추천API → Zilliz desc → 1행 1상품(좌:정보/우:desc) 노출.

## 실행
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 값 채우기
.venv/bin/streamlit run app.py
```

CLI 파이프라인 셀프체크: `.venv/bin/python app.py`

## 처리 흐름
1. **검색 실데이터 수집** (`get_search_signals`) — 검색 API 1회 호출로 연관검색어 + aggregations 동시 확보.
2. **외부 트렌드(수요)** — 네이버 데이터랩(`get_naver_trends`) + 무신사 실시간 인기검색어(`get_musinsa_trends`, 남/녁). ttl 1h. 수요 합의(Halfclub+Naver+Musinsa)로 단일 소스 편향 감소.
3. **외부 트렌드(에디토리얼)** — `get_fashion_articles`(Daum 뉴스 본문요약 + Google News 제목)와 `get_exa_articles`(Exa AI, 매체 본문)를 옵션 체크박스로 선택. Vogue·ELLE·W Korea·Harper's Bazaar·패션비즈 등. 컬러/실루엣/무드 트렌드 단서. ttl 1h.
4. **트렌드 컨텍스트** (`build_trend_context`) — 사내 연관검색어 + Naver + 무신사 + 패션 기사 + 인기 브랜드·가격대·성별 비중. 가이드 프롬프트는 에디토리얼 차원(컬러/실루엣/소재 무드) 명시. **출처 표기**(색 사각 아이콘+출처명) UI로 정보 근거 투명 노출.
4. **LLM 안내문+추출** (`make_guide`) — 컨텍스트를 근거로 트렌드 안내문(3~5문장) + JSON(`brand`/`category`/`gender`) 생성. 타자 효과로 노출.
5. **필터 해상+키워드 풍부화** (`resolve_filters`) — `gender→gndCd`, `category→dpCtgrNo{1,2,3}`, **`brand→brandCd`(가이드 브랜드 1~3, 콤마 다중값)**. 브랜드명→코드는 agg 우선, 미포함 시 브랜드명 검색 폴백(`lookup_brand_code`). 검색 키워드 = 잔여 토큰 + 안내문 트렌드 명사.
6. **검색** (`search_pool` + `apply_post_filters`) — **브랜드 필터 우선 + post-filter(가격/시즌) 점진 완화** 선택(≥12건 채택): 브랜드+가격+시즌 → 브랜드+가격(시즌완화) → … → 브랜드 제외 → 원본. 가이드 브랜드가 결과에 실제 반영되도록 우선. 풀 80 → 상위 **20건**.
7. **AI 큐레이션** (`curate_products`) — 후보 20개(상품명/브랜드/가격/desc)를 LLM에 넣어 목적에 맞는 ~6개 선정 + 상품별 추천 근거 + 1순위 추천픽. **인덱스 기준**(LLM은 idx 반환→prdNo 매핑)으로 큰 prdNo 복사 오류 차단. 선택 과부하 감소.
8. **Zilliz desc** (`get_desc_map`) → UI 카드(추천픽 배지 + 💡근거 + 상세설명).

> 추천 API(`/recommend/home`) 확장 조회 기능은 제거됨. 최종 결과 = 검색 결과 20건.

> 참고: 가격/시즌은 검색 API가 전용 필터 파라미터를 제공하지 않아 post-fetch Python 필터로 적용. 카테고리/성별/브랜드는 API 필터 파라미터(`dpCtgrNo*`/`gndCd`/`brandCd`) 사용.

## .env
| 변수 | 비고 |
|------|------|
| LITELLM_BASE_URL | 사내 LiteLLM 프록시 (`/v1/messages`) |
| LITELLM_API_KEY | 프록시 키 |
| GUIDE_MODEL | 기본 `gpt-4o-mini` (현재 `gpt-4o-mini`) |
| LLM_DIRECT_BASE_URL | 직접호출 기본 `https://api.openai.com/v1` |
| LLM_DIRECT_API_KEY | 직접호출(OpenAI 호환) 키 |
| ZILLIZ_URI | Zilliz Cloud endpoint |
| ZILLIZ_TOKEN | API key |
| ZILLIZ_COLLECTION | `prd_desc_vec_128` |
| EXA_API_KEY | Exa AI 키 (옵션, 미설정 시 Exa 수집 스킵) |

## Phase 0 확정 결과 (전부 실호출 — 가정 항목 없음)
- **검색 API** `GET /searches/prdList/` → 상품 `data.result.hits.hits[]._source.prdNo`(int). aggregations: `gndCd`/`brand`(key=brandCd,name)/`ctgr`(ctgr1→ctgr2→ctgr3, key=`rn@code@name`)/`price`. `rel_keywords` 상위.
- **필터 파라미터** `gndCd`(성별 01남/02여/03공용)·`brandCd`·`dpCtgrNo{1,2,3}`(카테고리 depth별 코드) 실동작 확인. `limit=0,1`은 total 이상 → `0,5` 사용.
- **추천 API** `GET /recommend/home` → `result[]` 필드 `prdNo`(int)/`prdNm`/`brandNm`/`selPrc`/`appPrdImgUrl`.
- **LLM** 호출 2모드(폼 라디오, 기본=직접): 직접 = OpenAI 호환 `POST {LLM_DIRECT_BASE_URL}/chat/completions`(`Authorization: Bearer`, `choices[0].message.content`). LiteLLM = 사내 프록시 `/v1/messages`(Anthropic 포맷, `content[].text`). `call_llm`가 토글. 모델 `gpt-4o-mini`.
- **외부 트렌드(수요)** 네이버 데이터랩 `getCategoryKeywordRank.naver`(POST, cid=50000000 패션의류, 어제) → `ranks[].keyword`. 무신사 `client.musinsa.com/.../keyword/search-home`(GET, popularCount+gf=M/F) → `data.componentList[key=popular].items[].text`.
- **외부 트렌드(에디토리얼)** Daum 뉴스 검색 → `v.daum.net` 기사 fetch → `og:site_name`(매체)/`og:title`/`og:description`(본문요약). Google News RSS(제목+매체). Exa AI `api.exa.ai/search`(POST, `x-api-key`, `startPublishedDate` 최신 필터, `contents.text` 본문) → `results[].{title,url,text,publishedDate}`.
- **Zilliz** 컬렉션 `prd_desc_vec_128`, 필드 `prd_no`(VarChar, **문자열**)/`desc`(VarChar)/`vector`. 추천 `prdNo`(int)→str 변환해 `prd_no in [...]` 스칼라 필터.

## 예외 처리
LLM 실패 → 빈 안내(흐름 유지) / 검색 0건 → 3단계 폴백 / 각 API·Zilliz 타임아웃·에러 → 해당 영역 폴백 메시지 / Zilliz 미적재 → "상세설명 없음".
