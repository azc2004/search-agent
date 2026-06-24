import os
import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import streamlit as st
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
# LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
# GUIDE_MODEL = os.environ.get("GUIDE_MODEL", "gpt-4o-mini")
# DIRECT_BASE_URL = os.environ.get("LLM_DIRECT_BASE_URL", "https://api.openai.com/v1")
# DIRECT_API_KEY = os.environ.get("LLM_DIRECT_API_KEY", "")
LITELLM_BASE_URL = st.secrets["LITELLM_BASE_URL"]
LITELLM_API_KEY = st.secrets["LITELLM_API_KEY"]
GUIDE_MODEL = st.secrets["GUIDE_MODEL"]
DIRECT_BASE_URL = st.secrets["LLM_DIRECT_BASE_URL"]
DIRECT_API_KEY = st.secrets["LLM_DIRECT_API_KEY"]
HAPIX = "https://hapix.halfclub.com"

# LLM은 검색 실데이터를 근거로 트렌드 안내문 + 검색어(JSON) 출력. 에디토리얼 차원 포함.
SYSTEM_PROMPT = (
    "패션 에디터 겸 이커머스 큐레이터. 제공된 [현재 검색 실데이터]를 근거로, 검색어의 최신 트렌드·"
    "인기 브랜드/소재·가격대·성별 경향과 함께 컬러·실루엣·소재 무드 등 스타일링 관점을 담아 "
    "3~5문장 한국어 평서문으로 안내. "
    "규칙: 데이터의 인기 브랜드/소재는 트렌드 정보로 인용 가능하나 특정 브랜드를 최고/추천으로 "
    "단정 금지, 가격은 사실 인용만 허용, 과장/이모지/마크다운 금지, 데이터에 없는 사실 창작 금지. "
    'JSON 본체만 출력(코드펜스/설명 금지): '
    '{"guide":"트렌드 안내문(컬러/실루엣/소재 무드 중 데이터 근거 있는 것 포함)",'
    '"brand":"가이드가 언급했거나 검색어에 명시된 관련 브랜드명 1~3개를 공백 구분(예: 쉬즈미스 데코). 없으면 빈 문자열",'
    '"category":"핵심 카테고리 명사(예: 원피스) 또는 빈 문자열",'
    '"gender":"여성/남성/공용 중 하나 또는 빈 문자열",'
    '"keywords":"안내문에 언급된 소재/스타일/컬러/실루엣 단일 명사 1~3개 공백 구분(예: 쉬폸 플리츠). '
    '브랜드명/카테고리명/계절명/연관검색어 전체는 넣지 말 것"}'
)
GENDER = {"여성": "02", "남성": "01", "공용": "03"}

TIMEOUT_API = 10
TIMEOUT_LLM = 20


# ── STEP 1-준비. 검색 실데이터(트렌드 신호 + 필터 해상용 agg) ─────────────
@st.cache_data(ttl=600)
def get_search_signals(keyword: str) -> dict:
    r = requests.get(f"{HAPIX}/searches/prdList/",
                     params={"keyword": keyword, "device": "pc", "limit": "0,5", "sortSeq": "12"},
                     timeout=TIMEOUT_API)
    r.raise_for_status()
    d = r.json()["data"]
    return {"rel": [k["keyword"] for k in d.get("rel_keywords", [])[:10]],
            "agg": d["result"].get("aggregations", {})}


@st.cache_data(ttl=3600)
def get_naver_trends() -> list:
    """네이버 데이터랩 쇼핑인사이트 패션의류 인기검색어(어제 기준). 실패 시 []."""
    end = date.today() - timedelta(days=1)
    try:
        r = requests.post(
            "https://datalab.naver.com/shoppingInsight/getCategoryKeywordRank.naver",
            data={"cid": "50000000", "timeUnit": "date",
                  "startDate": end.strftime("%Y-%m-%d"), "endDate": end.strftime("%Y-%m-%d")},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                     "Referer": "https://datalab.naver.com/shoppingInsight/s.naver",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        return [it["keyword"] for it in r.json().get("ranks", []) if it.get("keyword")]
    except Exception:
        return []


@st.cache_data(ttl=3600)
def get_musinsa_trends() -> list:
    """무신사 실시간 인기검색어(남/녁 합산, 중복제거 순서유지). 실패 시 []."""
    out = []
    try:
        for gf in ("M", "F"):
            r = requests.get(
                "https://client.musinsa.com/api/display/v1/search/web/keyword/search-home",
                params={"popularCount": 10, "gf": gf},
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                         "Referer": "https://www.musinsa.com/"},
                timeout=10,
            )
            r.raise_for_status()
            for c in r.json().get("data", {}).get("componentList", []):
                if c.get("key") == "popular":
                    for it in c.get("items", []):
                        t = it.get("text", "").strip()
                        if t and t not in out:
                            out.append(t)
        return out
    except Exception:
        return []


@st.cache_data(ttl=3600)
def get_fashion_articles(keyword: str) -> list:
    """패션 기사 수집. Daum 뉴스 요약(본문 단서) 우선, Google News 제목으로 보강. [{title,source,body}]."""
    out = _daum_news_snippets(keyword)               # 요약(본문 풍부) 우선
    for a in _gnews_titles(keyword):                 # 제목 보강(중복 제목 제외)
        if not any(a["title"][:12] in o["title"] for o in out):
            out.append(a)
    return out[:8]


def _meta(text: str, prop: str) -> str:
    m = re.search(rf'<meta property="{prop}" content="([^"]+)"', text)
    return html.unescape(m.group(1)).strip() if m else ""


def _daum_news_snippets(keyword: str) -> list:
    """Daum 뉴스 검색 → v.daum.net 기사 fetch로 매체명(og:site_name)+제목+본문요약(og:description) 수집."""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        r = requests.get(
            "https://search.daum.net/search?w=news&nil_suggest=btn&m=article&DA=PGD"
            f"&enc=utf8&cluster=y&q={urllib.parse.quote(keyword + ' 패션')}",
            headers=headers, timeout=15)
        urls = re.findall(r'<a href="(http://v\.daum\.net/v/[^"]+)"', r.text)
        out, seen = [], set()
        for u in urls:                                # 기사별 fetch → 메타에서 매체/제목/본문요약
            if u in seen:
                continue
            seen.add(u)
            try:
                ar = requests.get(u, headers=headers, timeout=15).text
            except Exception:
                continue
            media = _meta(ar, "og:site_name").replace("Daum | ", "").strip() or "패션매체"
            title = _meta(ar, "og:title")
            body = _meta(ar, "og:description")
            if len(body) < 25:
                continue
            out.append({"title": title[:50] or body[:30], "source": media,
                        "body": body[:300], "link": u})
            if len(out) >= 6:
                break
        return out
    except Exception:
        return []


def _gnews_titles(keyword: str) -> list:
    """Google News RSS에서 제목+매체명 수집(본문 미접근)."""
    try:
        r = requests.get(
            f"https://news.google.com/rss/search?q={urllib.parse.quote(keyword + ' 패션')}"
            f"&hl=ko&gl=KR&ceid=KR:ko",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        out = []
        for it in ET.fromstring(r.content).findall(".//item")[:8]:
            src = (it.findtext("source") or "").strip()
            title = (it.findtext("title") or "").strip()
            if src and title.endswith(" - " + src):
                title = title[: -(len(src) + 3)].strip()
            out.append({"title": title, "source": src or "기사", "body": "",
                        "link": it.findtext("link") or ""})
        return out
    except Exception:
        return []


@st.cache_data(ttl=3600)
def get_exa_articles(keyword: str) -> list:
    """Exa AI로 최신 패션 매체 기사(본문) 수집. [{title,source,body,link}]. 실패/키 미설정 시 []."""
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return []
    try:
        r = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": key, "content-type": "application/json"},
            json={"query": f"{keyword} 패션 트렌드", "numResults": 5, "type": "auto",
                  "startPublishedDate": "2025-01-01",
                  "contents": {"text": {"maxCharacters": 600}}},
            timeout=25)
        out = []
        for it in r.json().get("results", []):
            title = (it.get("title") or "").strip()
            source = "패션매체"
            if "|" in title:                                   # "제목 | 매체명" → 매체명
                source = title.rsplit("|", 1)[1].split("(")[0].strip() or source
                title = title.rsplit("|", 1)[0].strip()
            elif it.get("url"):
                source = urllib.parse.urlparse(it["url"]).netloc.replace("www.", "")
            txt = it.get("text") or ""
            if "공유" in txt:                                # "- 복사\n- 공유" 배너 이후 본문
                txt = txt.split("공유", 1)[1]
            txt = re.sub(r"^\s*\* 본 기사[^.]*\.\s*", "", txt)  # 에디터 면책문구 제거
            txt = re.sub(r"\s+", " ", txt.replace("#", " ")).strip()
            if len(txt) < 25:
                continue
            out.append({"title": title[:50], "source": source, "body": txt[:300],
                        "link": it.get("url", "")})
        return out
    except Exception:
        return []


def build_trend_context(signals: dict, keyword: str, naver: list, musinsa: list,
                        articles: list) -> str:
    agg, rel = signals["agg"], signals["rel"]
    brands = [b["name"] for b in sorted(agg.get("brand", {}).get("buckets", []),
                                        key=lambda x: -x["doc_count"])
              if b.get("name") not in ("기타", "")][:6]
    pr = agg.get("price", {})
    gnd = {"02": "여성", "01": "남성", "03": "공용"}
    gender = ", ".join(f"{gnd.get(b['key'], b['key'])} {b['doc_count']:,}건"
                       for b in agg.get("gndCd", {}).get("buckets", [])[:3])
    # 외부 트렌드: 검색어 토큰과 겹치는 네이버 인기검색어 우선, 없으면 패션 전체 상위
    toks = keyword.split()
    related = [k for k in naver if any(t in k for t in toks)] or naver
    lines = [f"[현재 검색 실데이터 — 검색어: {keyword}]"]
    if rel:
        lines.append(f"인기 연관검색어(사내): {', '.join(rel)}")
    if related:
        lines.append(f"네이버 데이터랩 인기검색어: {', '.join(related[:10])}")
    if musinsa:
        lines.append(f"무신사 실시간 인기검색어(남녀): {', '.join(musinsa[:10])}")
    if brands:
        lines.append(f"인기 브랜드: {', '.join(brands)}")
    if pr:
        lines.append(f"가격대: 평균 {pr.get('avg', 0):,.0f}원 "
                     f"(최저 {pr.get('min', 0):,.0f}~최고 {pr.get('max', 0):,.0f}원)")
    if gender:
        lines.append(f"성별 검색 비중: {gender}")
    if articles:                                  # 에디토리얼 트렌드(기사 본문/요약 단서 포함)
        parts = [f"{a['source']}: {a['body'] or a['title']}" for a in articles[:6]]
        lines.append("최신 패션 기사(컬러/실루엣/무드·본문 요약 단서): " + " | ".join(parts))
    return "\n".join(lines)


# ── LLM 호출 (직접 OpenAI호환 / LiteLLM 프록시 토글) ──────────────────────
def call_llm(system_prompt: str, user_content: str, max_tokens: int, use_litellm: bool) -> str:
    if use_litellm:                                  # 사내 LiteLLM 프록시 (Anthropic 포맷)
        r = requests.post(
            f"{LITELLM_BASE_URL}/v1/messages",
            headers={"x-api-key": LITELLM_API_KEY, "content-type": "application/json"},
            json={"model": GUIDE_MODEL, "max_tokens": max_tokens, "system": system_prompt,
                  "messages": [{"role": "user", "content": user_content}]},
            timeout=TIMEOUT_LLM)
        r.raise_for_status()
        return "".join(b["text"] for b in r.json()["content"] if b.get("type") == "text")
    r = requests.post(                               # 공급자 직접 (OpenAI 호환)
        f"{DIRECT_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {DIRECT_API_KEY}", "content-type": "application/json"},
        json={"model": GUIDE_MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system_prompt},
                           {"role": "user", "content": user_content}]},
        timeout=TIMEOUT_LLM)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ── STEP 1. LLM 안내문 + 검색어 추출 ─────────────────────────────────────
def make_guide(keyword: str, context: str, use_litellm: bool) -> dict:
    try:
        t = call_llm(SYSTEM_PROMPT, f"{context}\n\n검색어: {keyword}", 700, use_litellm)
        t = t[t.find("{"):t.rfind("}") + 1]            # JSON 본체만 추출(방어)
        o = json.loads(t)
        brands = [b for b in o.get("brand", "").split() if b]
        return {"guide": o.get("guide", ""), "brands": brands,
                "category": o.get("category", "").strip(), "gender": o.get("gender", "").strip(),
                "keywords": o.get("keywords", "").strip()}
    except Exception:
        return {"guide": "", "brands": [], "category": "", "gender": "", "keywords": ""}


# ── STEP 2.5. AI 큐레이션 — 후보 중 목적에 맞는 상품 선정 + 상품별 추천 근거 ──
CURATE_SYS = (
    "패션 쇼핑 에이전트. 검색어·AI 안내문·후보상품 목록(각 항목은 idx 인덱스 보유)을 받아 "
    "고객 목적에 가장 적합한 상품을 큐레이션한다. "
    "JSON 본체만 출력(코드펜스/설명 금지): "
    '{"summary":"왜 이 상품들을 골랐는지 1문장 한국어","top":추천 1순위 idx(정수),'
    '"picks":[{"idx":후보의 idx 정수,"reason":"이 상품이 검색어/안내문에 왜 맞는지 30자 내외 한국어"}]}. '
    "picks는 최대 6개, top은 picks의 idx 중 하나여야 한다. idx는 후보에 없는 값 사용 금지."
)


@st.cache_data(ttl=600)
def curate_products(keyword: str, guide: str, cand_key: tuple, use_litellm: bool) -> dict:
    cands = [{"idx": i, "name": c[1], "brand": c[2], "price": c[3], "desc": c[4]}
             for i, c in enumerate(cand_key)]
    idx_to_prd = {i: c[0] for i, c in enumerate(cand_key)}
    n = len(cand_key)
    try:
        t = call_llm(CURATE_SYS,
                     f"검색어: {keyword}\nAI 안내문: {guide}\n후보 상품: {json.dumps(cands, ensure_ascii=False)}",
                     800, use_litellm)
        o = json.loads(t[t.find("{"):t.rfind("}") + 1])
        seen, picks = set(), []
        for p in o.get("picks", []):
            i = p.get("idx")
            if isinstance(i, int) and 0 <= i < n and i not in seen:
                seen.add(i)
                picks.append({"prdNo": idx_to_prd[i], "reason": p.get("reason", "")})
        pick_ids = [p["prdNo"] for p in picks]
        ti = o.get("top")
        top = idx_to_prd[ti] if isinstance(ti, int) and 0 <= ti < n and idx_to_prd[ti] in pick_ids \
            else (pick_ids[0] if pick_ids else None)
        return {"summary": o.get("summary", ""), "top": top, "picks": picks}
    except Exception:
        return {"summary": "", "top": None, "picks": []}


def _ctgr_candidates(agg):
    """ctgr aggregation 평탄화 → (이름, 코드, depth, doc_count). key 포맷: rn@code@name."""
    out = []
    for b1 in agg.get("ctgr", {}).get("ctgr1", {}).get("buckets", []):
        p = b1["key"].split("@")
        out.append((p[2] if len(p) > 2 else "", p[1] if len(p) > 1 else "", 1, b1.get("doc_count", 0)))
        for b2 in b1.get("ctgr2", {}).get("buckets", []):
            p2 = b2["key"].split("@")
            out.append((p2[2] if len(p2) > 2 else "", p2[1] if len(p2) > 1 else "", 2, b2.get("doc_count", 0)))
            for b3 in b2.get("ctgr3", {}).get("buckets", []):
                out.append((b3.get("ctgrNm3", ""), b3.get("ctgrNo3", ""), 3, b3.get("doc_count", 0)))
    return out


def lookup_brand_code(name: str):
    """브랜드명 → brandCd. agg 미포함 시 브랜드명 검색으로 해상. 실패 시 None."""
    try:
        r = requests.get(f"{HAPIX}/searches/prdList/",
                         params={"keyword": name, "device": "pc", "limit": "0,1", "sortSeq": "12"},
                         timeout=TIMEOUT_API)
        hits = r.json()["data"]["result"]["hits"]["hits"]
        return hits[0]["_source"].get("brandCd") if hits else None
    except Exception:
        return None


_DEMOG = [("남성", "M"), ("남아", "M"), ("남자", "M"),
          ("여성", "F"), ("여아", "F"), ("여자", "F")]


def _demo_gender(s: str):
    for kw, g in _DEMOG:
        if kw in s:
            return g
    return None


def _match_category(target: str, keyword: str, gender: str, cands):
    """카테고리 정교 매칭: 핵심 명사 추출 → 인구통계 정렬 → 더 깊은(구체적) 카테고리 우선."""
    core = target
    for pre in ("남성", "여성", "남아", "여아", "남자", "여자"):   # 인구통계 접두어 제거
        if core.startswith(pre):
            core = core[len(pre):]
    core = core.strip() or target
    hits = [(nm, code, depth, cnt) for nm, code, depth, cnt in cands if core in nm]
    if not hits:
        return None
    want = _demo_gender(keyword) or _demo_gender(gender) or _demo_gender(target)
    if want:                                                   # 인구통계 맞는 후보로 좁힘
        aligned = [h for h in hits if _demo_gender(h[0]) == want]
        if aligned:
            hits = aligned
    hits.sort(key=lambda x: -x[3])                             # 인기(doc_count) 큰 카테고리 우선
    return (hits[0][0], hits[0][1], hits[0][2])


def resolve_filters(ext: dict, agg: dict, keyword: str):
    """추출어 → 필터 해상 + 안내문 트렌드 명사로 키워드 풍부화. (filters, info, enriched, residual) 반환."""
    filters, info, kw_stop = {}, {}, set(GENDER) | {"남", "여"}
    season = next((s for s in SEASONS if s in keyword), None)   # 시즌 post-filter와 중복 방지
    if season:
        kw_stop.add(season)
    if ext["gender"] in GENDER:
        filters["gndCd"] = GENDER[ext["gender"]]
        info["성별"] = f"{ext['gender']} → gndCd={filters['gndCd']}"
    buckets = agg.get("brand", {}).get("buckets", [])
    resolved, codes = [], set()                       # 가이드 브랜드명 → brandCd(다중)
    for bn in ext.get("brands", [])[:3]:
        code = next((b["key"] for b in buckets if bn and bn in b.get("name", "")), None) \
            or lookup_brand_code(bn)                  # agg 미포함 시 브랜드명 검색 폴백
        if code and code not in codes:                # 코드 중복 제거
            codes.add(code)
            resolved.append((bn, code))
            kw_stop.add(bn)
    if resolved:
        filters["brandCd"] = ",".join(c for _, c in resolved)
        info["브랜드"] = f"{', '.join(n for n, _ in resolved)} → brandCd={filters['brandCd']}"
    if ext["category"]:
        m = _match_category(ext["category"], keyword, ext.get("gender", ""), _ctgr_candidates(agg))
        if m:
            nm, code, depth = m
            filters[f"dpCtgrNo{depth}"] = code
            kw_stop.add(ext["category"])
            info["카테고리"] = f"{ext['category']} → {nm} (dpCtgrNo{depth}={code})"
    residual = " ".join(t for t in keyword.split() if t not in kw_stop)
    # 안내문 트렌드 명사(소재/스타일) — 원본·필터 차원 토큰은 배제하고 단일 명사만
    drop = set(keyword.split()) | kw_stop
    trend = [t for t in ext.get("keywords", "").split() if t and t not in drop and len(t) <= 4]
    if trend:
        info["트렌드키워드(안내문)"] = ", ".join(trend)
    enriched = " ".join([residual] + trend).strip()
    return filters, info, enriched, residual, trend


# ── STEP 2. 검색 API → 풀(필터 파라미터 적용). 가격/시즌은 post-filter ─────
@st.cache_data(ttl=600)
def search_pool(keyword: str, filters_key: tuple, limit: int = 80) -> list[dict]:
    params = {"keyword": keyword, "device": "pc", "limit": f"0,{limit}", "sortSeq": "12",
              **dict(filters_key)}
    r = requests.get(f"{HAPIX}/searches/prdList/", params=params, timeout=TIMEOUT_API)
    r.raise_for_status()
    out = []
    for h in r.json()["data"]["result"]["hits"]["hits"]:
        s = h["_source"]
        out.append({"prdNo": s["prdNo"], "prdNm": s.get("prdNm", ""),
                    "brandNm": s.get("brandNm", ""), "selPrc": s.get("selPrc", ""),
                    "normPrc": s.get("normPrc", ""), "reviewStar": s.get("reviewStar"),
                    "reviewQty": s.get("reviewQty"), "imageUrl": s.get("appPrdImgUrl") or ""})
    return out


SEASONS = ["봄", "여름", "가을", "겨울"]


def apply_post_filters(pool: list, price_band, season: str) -> list:
    out = pool
    if season:                                   # 시즌어가 상품명에 포함된 상품만
        out = [p for p in out if season in p["prdNm"]]
    if price_band:                               # 평균 근사치 가격대
        lo, hi = price_band
        out = [p for p in out if isinstance(p["selPrc"], (int, float)) and lo <= p["selPrc"] <= hi]
    return out


# ── STEP 3. Zilliz → desc 맵 (이전 추천 API 단계는 제거됨) ────────────────


# ── STEP 4. Zilliz → desc 맵 ─────────────────────────────────────────────
@st.cache_resource
def get_zilliz_client():
    from pymilvus import MilvusClient
    return MilvusClient(uri=os.environ["ZILLIZ_URI"], token=os.environ["ZILLIZ_TOKEN"])


def get_desc_map(prd_nos: list) -> dict:
    if not prd_nos or not os.environ.get("ZILLIZ_URI"):
        return {}
    try:
        # 확정: 컬렉션 prd_desc_vec_128, 필드 prd_no(VarChar)/desc. 추천 prdNo(int)→str 필터.
        rows = get_zilliz_client().query(
            collection_name=os.environ["ZILLIZ_COLLECTION"],
            filter=f'prd_no in {[str(n) for n in prd_nos]}',
            output_fields=["prd_no", "desc"], limit=len(prd_nos))
        return {int(r["prd_no"]): (r.get("desc") or "") for r in rows}
    except Exception as e:
        st.warning(f"Zilliz 조회 실패: {e}")
        return {}


# ── STEP 5. Streamlit UI ─────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="검색어 기반 추천 PoC", layout="wide")
    st.title("검색어 기반 LLM 추천 안내")

    st.markdown(
        "<style>"
        "[data-testid='stTextInput'] input{border-radius:24px;background:#f5f6f8;"
        "border:1px solid #e3e6eb;padding:11px 18px;font-size:15px;height:auto}"
        "[data-testid='stTextInput'] input:focus{border-color:#1e88e5;background:#fff;"
        "box-shadow:0 0 0 3px rgba(30,136,229,.13)}"
        "</style>", unsafe_allow_html=True)
    # 폼으로 감싸: 체크박스/입력 변경은 재실행 안 함, 엔터/검색 버튼(폼 제출) 시만 실행
    with st.form("search_form", clear_on_submit=False):
        sbar = st.columns([7, 2])
        with sbar[0]:
            keyword = st.text_input("검색어", "여름 원피스",
                                    label_visibility="collapsed", placeholder="상품·트렌드를 검색하세요")
        with sbar[1]:
            submitted = st.form_submit_button("🔍  검색", use_container_width=True, type="primary")
        trend_src = st.radio("외부 패션 트렌드 수집", ["사용 안 함", "Daum·Google 뉴스", "Exa AI (매체 본문)"],
                             horizontal=True, index=1)
        llm_mode = st.radio("LLM 호출 방식", ["직접 (OpenAI 호환)", "LiteLLM 프록시"],
                            horizontal=True, index=0)
    use_trends = trend_src == "Daum·Google 뉴스"
    use_exa = trend_src == "Exa AI (매체 본문)"
    use_litellm = llm_mode == "LiteLLM 프록시"
    use_litellm = llm_mode == "LiteLLM 프록시"
    if not submitted or not keyword:
        st.caption("검색어 입력 후 엔터 또는 검색 버튼을 누르세요.")
        return

    # STEP 1 — 트렌드 수집(수요 소스는 조용히) → 패션 출처(매체) 스켈레톤→카운트업→LLM 안내문
    st.markdown(
        "<style>"
        "@keyframes hcsk{0%{background-position:-300px 0}100%{background-position:300px 0}}"
        ".hc-sk{display:inline-block;height:11px;border-radius:4px;width:62%;vertical-align:middle;"
        "background:linear-gradient(90deg,#e2e5ea 25%,#eef0f3 50%,#e2e5ea 75%);"
        "background-size:600px 100%;animation:hcsk 1.1s infinite linear}"
        "@keyframes hcpulse{0%,100%{opacity:.3;transform:scale(.7)}50%{opacity:1;transform:scale(1.15)}}"
        ".hc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#1e88e5;"
        "margin-right:7px;vertical-align:middle;animation:hcpulse 1s infinite}"
        "</style>", unsafe_allow_html=True)
    ph = st.empty()

    def steps_render(items):
        rows = []
        for i, (label, state) in enumerate(items, 1):
            num = f'<b style="color:#bbb;display:inline-block;width:18px">{i}</b>'
            if state == "done":
                mark = '<span style="color:#43a047;font-weight:700;margin-right:6px">✓</span>'
                body = f'<span style="font-weight:600;color:#222">{html.escape(label)}</span>'
            elif state == "active":                 # 상태 라인 = 펄스 닷 + 텍스트
                mark = '<span class="hc-dot"></span>'
                body = f'<span style="color:#1e88e5;font-weight:600">{html.escape(label)}</span>'
            else:                                   # 수집 슬롯 = 펄스 닷 + 스켈레톤 바
                mark = '<span class="hc-dot"></span>'
                body = '<span class="hc-sk"></span>'
            rows.append(f'<div style="margin:6px 0;font-size:14px">{num}{mark}{body}</div>')
        ph.markdown('<div style="background:#f6f7f9;border-radius:8px;padding:12px 14px">'
                    + "".join(rows) + "</div>", unsafe_allow_html=True)

    steps_render([("패션 트렌드 정보를 수집하고 있어요", "active"),
                  ("", "running"), ("", "running"), ("", "running")])
    # 수요 소스(Halfclub/Naver/무신사)는 필터·컨텍스트용으로만 수집 (UI 미표기)
    signals = get_search_signals(keyword)
    naver = get_naver_trends()
    musinsa = get_musinsa_trends()
    articles = []
    if use_trends:
        articles += get_fashion_articles(keyword)
    if use_exa:                                   # Exa AI 본문 기사 병합
        articles += get_exa_articles(keyword)
    if articles:                                  # 제목 기준 경량 중복 제거
        seen_t, uniq = set(), []
        for a in articles:
            k = a["title"][:14]
            if k not in seen_t:
                seen_t.add(k)
                uniq.append(a)
        articles = uniq
    if articles:
        batches = [articles[i:i + 3] for i in range(0, len(articles), 3)]
        items = [("패션 트렌드 정보를 수집하고 있어요", "active")]
        for batch in batches:                       # 3개씩 누적 노출(히든 없이 유지)
            start = len(items)
            for a in batch:
                items.append((a["source"], "running"))
            steps_render(items)
            for j, a in enumerate(batch):           # 천천히 스켈레톤→완료 채움
                time.sleep(0.3)
                items[start + j] = (f"{a['source']} — {a['title'][:30]}", "done")
                steps_render(items)
        for k in range(len(articles) + 1):          # 건수 카운트업
            items[0] = (f"패션 트렌드 정보 {k}건을 확인했어요", "done")
            steps_render(items)
            time.sleep(0.04)
        items[0] = ("AI 트렌드 가이드를 작성하고 있어요", "active")
        steps_render(items)
    else:
        with st.spinner("AI 트렌드 가이드를 작성하고 있어요"):
            pass
    ctx = build_trend_context(signals, keyword, naver, musinsa, articles)
    ext = make_guide(keyword, ctx, use_litellm)
    ph.empty()                                      # 가이드 노출 시점에 수집 UI 히든

    # 출처 표기 — 패션 트렌드 매체 (색 사각 아이콘 + 출처명 링크)
    palette = ["#1e88e5", "#03c75a", "#43a047", "#8e24aa", "#fb8c00",
               "#00897b", "#d81b60", "#3949ab", "#6d4c41", "#111"]
    seen, sources = set(), []
    for a in articles:
        if a["source"] not in seen:
            seen.add(a["source"])
            sources.append((a["source"], a.get("link", "")))
    if sources:
        chips = "".join(
            f'<span style="display:inline-flex;align-items:center;margin:3px 10px 3px 0">'
            f'<span style="display:inline-block;width:11px;height:11px;background:{palette[i % len(palette)]};'
            f'margin-right:6px;border-radius:2px"></span>'
            f'<a href="{html.escape(link)}" target="_blank" style="color:#1a73e8;text-decoration:none">'
            f'{html.escape(name)}</a></span>'
            for i, (name, link) in enumerate(sources) if link)
        st.markdown(f'<div style="margin:4px 0 10px"><b>출처</b><br>{chips}</div>',
                    unsafe_allow_html=True)

    # STEP 2 — 필터 해상 + 키워드 풍부화 + 가격/시즌 post-filter 준비
    filters, info, kw_enriched, kw_residual, trend = resolve_filters(ext, signals["agg"], keyword)
    avg = signals["agg"].get("price", {}).get("avg")
    price_band = (avg * 0.5, avg * 1.5) if avg else None
    season = next((s for s in SEASONS if s in keyword), None)
    if price_band:
        info["가격대"] = f"{int(price_band[0]):,}~{int(price_band[1]):,}원 (평균 {int(avg):,}원 기준)"
    if season:
        info["시즌"] = f"{season} (상품명 매칭)"

    # AI 쇼핑 가이드 카드 (네이버 AI 쇼핑가이드 스타일: 아이콘 헤더 + 서브타이틀 + 불릿 추천포인트)
    with st.container(border=True):
        st.markdown(f"### 🛍 쇼핑 · 「{keyword}」 가이드")
        st.caption("AI가 실시간 검색 트렌드로 큐레이션한 추천 가이드예요.")
        st.write_stream(_typewriter(ext["guide"] or "안내 메시지를 생성하지 못했습니다."))
        points = [("카테고리", ext["category"])] if ext["category"] else []
        if ext["gender"]:
            points.append(("성별", ext["gender"]))
        if trend:
            points.append(("소재·스타일", ", ".join(trend)))
        if price_band:
            points.append(("가격대", f"{int(price_band[0]):,}~{int(price_band[1]):,}원"))
        if season:
            points.append(("시즌", season))
        if points:
            st.markdown(f"**「{keyword}」 추천 포인트**")
            for label, val in points:
                st.markdown(f"- **{label}** — {val}")
        st.caption(f"검색어: `{kw_enriched or '(없음)'}`")

    with st.expander("참고한 데이터 (필터 코드·출처 기사 포함)"):
        st.markdown("**필터 적용**")
        for k, v in info.items():
            st.markdown(f"- {k}: {v}")
        if articles:
            st.markdown("**참고한 패션 기사 (본문 요약 포함)**")
            for a in articles:
                body = a.get("body", "")
                link = a.get("link", "")
                head = f"**{a['source']}** — {a['title']}"
                if link:
                    head = f'{head} [↗]({link})'
                st.markdown(f"- {head}" + (f"\n  - {body}" if body else ""))
        url_ph = st.empty()                        # 최종 검색 API URL (검색 후 채움)
        st.code(ctx, language="text")

    fk = tuple(sorted(filters.items()))
    fk_nobrand = tuple((k, v) for k, v in fk if k != "brandCd")
    fk_cat = tuple((k, v) for k, v in fk if k.startswith("dpCtgrNo"))   # 카테고리 필수 필터
    # 브랜드 필터 우선, post-filter(가격/시즌)를 점진 완화. 카테고리는 모든 티어에서 필수.
    attempts = [(kw_enriched, fk, price_band, season)]
    if kw_residual and kw_residual != kw_enriched:
        attempts.append((kw_residual, fk, price_band, season))
    attempts += [("", fk, price_band, season),
                 (kw_enriched, fk, price_band, None), ("", fk, price_band, None),
                 (kw_enriched, fk, None, season), ("", fk, None, season)]
    if "brandCd" in filters:                      # 브랜드까지 완화 불가 시 브랜드 제외(카테고리 유지)
        attempts += [(kw_enriched, fk_nobrand, price_band, season),
                     ("", fk_nobrand, price_band, season)]
    # 최종: 카테고리만 필수, 나머지(브랜드/성별/가격/시즌) 전부 완화
    attempts.append((keyword, fk_cat or (), None, None))
    def _search_url(q, ff):
        params = [("keyword", q), ("device", "pc"), ("limit", "0,80"), ("sortSeq", "12")] + list(ff)
        return f"{HAPIX}/searches/prdList/?" + urllib.parse.urlencode(params)

    products, fallback, adopted, fb_qff = [], None, None, None
    try:
        for q, ff, pb, se in attempts:
            pool = search_pool(q, ff)
            if fallback is None:
                fallback, fb_qff = pool, (q, ff)
            filt = apply_post_filters(pool, pb, se)
            if len(filt) >= 12:                   # 충분하면 채택 (브랜드 우선)
                products, adopted = filt, (q, ff)
                break
            if len(filt) > len(products):
                products, adopted = filt, (q, ff)
        if not products and fallback is not None:
            products, adopted = fallback, fb_qff
        products = products[:20]
    except Exception as e:
        st.error(f"검색 API 실패: {e}")
        return
    if not products:
        st.info("검색결과가 없습니다.")
        return
    final_url = _search_url(*adopted) if adopted else ""
    if final_url:
        url_ph.markdown(f"**검색 API URL**\n\n`{final_url}`")

    # STEP 3~5 — Zilliz desc, AI 큐레이션, 카드 노출
    st.subheader("검색 결과")

    desc_map = get_desc_map([p["prdNo"] for p in products])

    # AI 큐레이션 — 후보에서 목적에 맞는 상품 선정 + 추천 근거. 픽은 상단 정렬, 20개 모두 노출
    cand_key = tuple((p["prdNo"], p["prdNm"], p["brandNm"], p["selPrc"],
                      desc_map.get(p["prdNo"], "")) for p in products)
    curation = curate_products(keyword, ext["guide"], cand_key, use_litellm)
    reason_map = {p["prdNo"]: p["reason"] for p in curation["picks"]}
    if curation["picks"]:
        wanted = [p["prdNo"] for p in curation["picks"]]
        products.sort(key=lambda p: wanted.index(p["prdNo"]) if p["prdNo"] in wanted else 999)
    products = products[:20]

    st.markdown("""<style>
    [data-testid="stVerticalBlockBorderWrapper"]{border-radius:12px!important;
      box-shadow:0 1px 6px rgba(0,0,0,.08)!important;border:1px solid #eee!important;
      padding:14px!important;margin-bottom:14px!important;}
    </style>""", unsafe_allow_html=True)

    if curation["summary"]:
        st.info(f"**AI 큐레이션** — {curation['summary']}")
    elif not curation["picks"]:
        st.caption("AI 큐레이션 실패 — 검색 상품을 그대로 보여드립니다.")

    for p in products:
        sel, norm = p.get("selPrc"), p.get("normPrc")
        price = f'<span style="font-size:20px;font-weight:800">{sel:,}원</span>'
        if isinstance(norm, (int, float)) and isinstance(sel, (int, float)) and norm > sel > 0:
            dc = round((1 - sel / norm) * 100)
            price = (f'<span style="color:#fff;background:#e53935;padding:1px 6px;border-radius:5px;'
                     f'font-size:12px;font-weight:700;margin-right:6px">{dc}%</span>'
                     f'<span style="font-size:20px;font-weight:800">{sel:,}원</span>'
                     f'<span style="color:#999;text-decoration:line-through;font-size:13px;margin-left:6px">{norm:,}원</span>')
        meta = []
        if p.get("reviewStar"):
            meta.append(f"⭐ {p['reviewStar']}")
        if p.get("reviewQty"):
            meta.append(f"리뷰 {p['reviewQty']:,}")
        if p.get("brandNm"):
            meta.append(p["brandNm"])
        meta.append(f"상품번호 {p['prdNo']}")
        reason = reason_map.get(p["prdNo"], "")
        is_top = p["prdNo"] == curation["top"]
        desc = desc_map.get(p["prdNo"])
        with st.container(border=True):
            if is_top:
                st.markdown('<span style="color:#fff;background:#1e88e5;padding:2px 8px;'
                            'border-radius:5px;font-size:12px;font-weight:700">★ AI 추천픽</span>',
                            unsafe_allow_html=True)
            c0, c1 = st.columns([1, 7])
            with c0:
                prd_url = "https://www.halfclub.com/product/" + str(p["prdNo"])
                image_url = p["imageUrl"]
                html_code = ""
                if p["imageUrl"]:
                    # st.image(p["imageUrl"], width='stretch')
                    html_code = f"""
                    <a href="{prd_url}" target="_blank">
                        <img src="{image_url}" style="width: 100%;">
                    </a>
                    """
                st.markdown(html_code, unsafe_allow_html=True)
            with c1:
                st.markdown(f"**{p['prdNm']}**")
                st.markdown(price, unsafe_allow_html=True)
                st.caption(" · ".join(meta))
                if reason:
                    st.markdown(
                        f'<div style="color:#1565c0;font-size:13px;font-weight:600;margin-top:6px">'
                        f'💡 {html.escape(reason)}</div>',
                        unsafe_allow_html=True)
                if desc:
                    st.markdown(
                        f'<div style="color:#222;font-size:15px;line-height:1.7;margin-top:8px;'
                        f'padding:10px 12px;background:#f7f7f8;border-left:3px solid #d0d0d0;'
                        f'border-radius:6px">{html.escape(desc)}</div>',
                        unsafe_allow_html=True)
                else:
                    st.caption("_상세설명 없음_")


def _typewriter(s):
    for ch in s:                       # 가이드는 원래 속도(빠름). 출처 애니메이션만 느림.
        yield ch
        time.sleep(0.03)


if __name__ == "__main__":
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx() is not None:               # streamlit run
        main()
    else:                                               # python app.py → 파이프라인 셀프체크
        kw0 = "여름 원피스"
        sig = get_search_signals(kw0)
        ctx = build_trend_context(sig, kw0, get_naver_trends(), get_musinsa_trends(),
                                  get_fashion_articles(kw0))
        ext = make_guide(kw0, ctx, False)
        assert ext["guide"], ext
        f, info, qen, qres, trend = resolve_filters(ext, sig["agg"], kw0)
        fk = tuple(sorted(f.items()))
        fk_cat = tuple((k, v) for k, v in fk if k.startswith("dpCtgrNo"))
        tiers = [(qen, fk)] + ([("", fk)] if f else []) + [(kw0, fk_cat or ())]
        avg = sig["agg"].get("price", {}).get("avg")
        band = (avg * 0.5, avg * 1.5) if avg else None
        season = next((s for s in SEASONS if s in kw0), None)
        best, fb = [], []
        for q, ff in tiers:
            pool = search_pool(q, ff)
            fb = fb or pool
            filt = apply_post_filters(pool, band, season)
            if len(filt) > len(best):
                best = filt
            if len(best) >= 20:
                break
        prods = (best or fb)[:20]
        assert prods and "prdNo" in prods[0], prods
        ck = tuple((p["prdNo"], p["prdNm"], p["brandNm"], p["selPrc"], "") for p in prods)
        cur = curate_products(kw0, ext["guide"], ck, False)
        assert cur["picks"] and cur["top"] in [p["prdNo"] for p in cur["picks"]], cur
        print(f"OK filters={f} enriched={qen!r} season={season} search={len(prods)} cur_picks={len(cur['picks'])} top={cur['top']}")
        print("summary:", cur["summary"][:80])
        print("info:", info)
        print("guide:", ext["guide"][:120])
