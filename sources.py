"""외부 데이터 수집 (Halfclub/Naver/무신사/Daum/Google/Exa) + Halfclub 검색 호출군 + 트렌드 컨텍스트 조립."""
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import streamlit as st
from datetime import date, timedelta

from config import HAPIX, TIMEOUT_API, FASHION_MEDIA


# ── Halfclub 인기검색어 ───────────────────────────────────────────────────
@st.cache_data(ttl=600)
def get_popular_keywords() -> list:
    """하프클럽 실시간 인기검색어(range=3 최근). [{keyword, change}]. 실패 시 []."""
    try:
        r = requests.get(f"{HAPIX}/searches/popularKeyword/",
                         params={"range": 3, "limit": 20, "countryCd": "001",
                                 "langCd": "001", "siteCd": 1, "deviceCd": "001", "device": "mc"},
                         timeout=TIMEOUT_API)
        r.raise_for_status()
        return [{"keyword": d["keyword"], "change": d.get("change", 0)}
                for d in r.json().get("data", []) if d.get("keyword")]
    except Exception:
        return []


# ── Halfclub 인기브랜드 ───────────────────────────────────────────────────
@st.cache_data(ttl=600)
def get_popular_brands() -> list:
    """하프클럽 실시간 인기브랜드(range=3 최근). [{brand_name, brand_cd}]. 실패 시 []."""
    try:
        r = requests.get(f"{HAPIX}/searches/popularBrand/",
                         params={"range": 3, "limit": 30, "countryCd": "001",
                                 "langCd": "001", "siteCd": 1, "deviceCd": "001", "device": "mc"},
                         timeout=TIMEOUT_API)
        r.raise_for_status()
        return [{"brand_name": d["brand_name"], "brand_cd": d["brand_cd"]}
                for d in r.json().get("data", []) if d.get("brand_name")]
    except Exception:
        return []


# ── Halfclub 검색 신호 (연관검색어 + aggregations) ────────────────────────
@st.cache_data(ttl=600)
def get_search_signals(keyword: str) -> dict:
    r = requests.get(f"{HAPIX}/searches/prdList/",
                     params={"keyword": keyword, "device": "pc", "limit": "0,5", "sortSeq": "12"},
                     timeout=TIMEOUT_API)
    r.raise_for_status()
    d = r.json()["data"]
    return {"rel": [k["keyword"] for k in d.get("rel_keywords", [])[:10]],
            "agg": d["result"].get("aggregations", {})}


# ── 외부 수요 트렌드 ──────────────────────────────────────────────────────
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


# ── 패션 에디토리얼 기사 (Daum 뉴스 + Google News) ────────────────────────
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


# ── Exa AI (패션매체 필수 + 전체 보강) ─────────────────────────────────────
def _parse_exa(results: list) -> list:
    out = []
    for it in results:
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


@st.cache_data(ttl=1800)
def get_exa_articles(keyword: str, use_livecrawl: bool = False) -> list:
    """Exa AI로 최신 패션 기사 수집. 패션매체(필수) + 전체(보강) 병합. [{title,source,body,link}]."""
    key = st.secrets.get("EXA_API_KEY")
    if not key:
        return []
    start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")   # 최신 30일 윈도우
    base = {"query": f"{keyword} 패션 트렌드", "numResults": 5, "type": "auto",
            "startPublishedDate": start, "contents": {"text": {"maxCharacters": 600}}}
    if use_livecrawl:                              # 실시간 크롤(최신 본문, 지연↑)
        base["livecrawl"] = "always"
    timeout = 40 if use_livecrawl else 25

    def _search(extra):                            # Exa 1회 호출 + 파싱
        try:
            r = requests.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": key, "content-type": "application/json"},
                json={**base, **extra}, timeout=timeout)
            return _parse_exa(r.json().get("results", []))
        except Exception:
            return []

    media = _search({"includeDomains": FASHION_MEDIA})    # 패션매체 필수
    general = _search({})                                  # 전 도메인 보강
    seen, out = set(), []
    for i in range(max(len(media), len(general))):        # 교차 병합(타 매체도 노출 보장)
        for src in (media, general):
            if i < len(src) and src[i]["link"] and src[i]["link"] not in seen:
                seen.add(src[i]["link"])
                out.append(src[i])
    return out[:8]


# ── Halfclub 검색 API 호출군 (브랜드 해상 / 상품 풀) ──────────────────────
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


def lookup_brand_by_name(name: str):
    """브랜드명 검색 → 첫 상품의 brandNm이 name과 정확 일치하면 (brandNm, brandCd).
    짧은/단일 브랜드 검색어(예: '씨')가 agg 상위 버킷에 누락된 경우의 브랜드 특정 폴백.
    일치하지 않으면 None(카테고리명 등의 오검출 방지용 검증)."""
    try:
        r = requests.get(f"{HAPIX}/searches/prdList/",
                         params={"keyword": name, "device": "pc", "limit": "0,1", "sortSeq": "12"},
                         timeout=TIMEOUT_API)
        hits = r.json()["data"]["result"]["hits"]["hits"]
        if hits:
            s = hits[0]["_source"]
            bnm = (s.get("brandNm") or "").strip()
            if bnm == name:
                return (bnm, s.get("brandCd"))
    except Exception:
        pass
    return None


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


# ── 트렌드 컨텍스트 조립 (수집 데이터 → LLM 입력용) ────────────────────────
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
