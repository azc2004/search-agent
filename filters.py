"""필터·카테고리 해상(순수 로직, requests 없음). 추출어 → 검색 API 필터 + 키워드 풍부화."""
from config import GENDER, SEASONS, _DEMOG, _CAT_CONCEPTS
from sources import lookup_brand_code, lookup_brand_by_name


def _ctgr_candidates(agg):
    """ctgr aggregation 평탄화 → (이름, 코드, depth, doc_count, 경로문자열). key 포맷: rn@code@name."""
    out = []
    for b1 in agg.get("ctgr", {}).get("ctgr1", {}).get("buckets", []):
        p = b1["key"].split("@")
        n1 = p[2] if len(p) > 2 else ""
        c1 = p[1] if len(p) > 1 else ""
        out.append((n1, c1, 1, b1.get("doc_count", 0), n1))
        for b2 in b1.get("ctgr2", {}).get("buckets", []):
            p2 = b2["key"].split("@")
            n2 = p2[2] if len(p2) > 2 else ""
            c2 = p2[1] if len(p2) > 1 else ""
            out.append((n2, c2, 2, b2.get("doc_count", 0), f"{n1} {n2}"))
            for b3 in b2.get("ctgr3", {}).get("buckets", []):
                n3 = b3.get("ctgrNm3", "")
                out.append((n3, b3.get("ctgrNo3", ""), 3, b3.get("doc_count", 0), f"{n1} {n2} {n3}"))
    return out


def _demo_gender(s: str):
    for kw, g in _DEMOG:
        if kw in s:
            return g
    return None


def _cat_concepts(s: str) -> set:
    return {c for pat, c in _CAT_CONCEPTS if pat in s}


def _match_category(target: str, keyword: str, gender: str, cands):
    """경로 기반 개념 중복 매칭. leaf 이름 매칭을 path 매칭보다 강하게 우선(과세분화 방지).
    인구통계 정렬 + leaf중복·path중복·인기 가중. (이름, 코드, depth) 반환."""
    want = _demo_gender(keyword) or _demo_gender(gender) or _demo_gender(target)
    tc = _cat_concepts(target)
    if not tc:
        return None
    best = None                                  # (score, name, code, depth)
    for nm, code, depth, cnt, path in cands:
        pov = len(tc & _cat_concepts(path))      # 경로(조상 포함) 개념 중복
        if pov == 0:
            continue
        cg = _demo_gender(path)                  # 성별 불일치 후보 제외(무성별은 유지)
        if want and cg and cg != want:
            continue
        lov = len(tc & _cat_concepts(nm))        # leaf 이름 매칭(강한 신호)
        score = lov * 100000 + pov * 1000 + min(cnt, 9999)
        if best is None or score > best[0]:
            best = (score, nm, code, depth)
    return (best[1], best[2], best[3]) if best else None


def _brand_code(bn: str, buckets) -> str | None:
    """브랜드명 → brandCd. 정확명 일치 우선, 부분일치는 doc_count 최대. agg 미포함 시 None.
    부분일치만 있으면 첫 일치(서브브랜드)가 잡혀 메인 브랜드가 누락되는 사례 방지."""
    for b in buckets:                              # 1) 정확명 일치(예: '베네통' → BL103488)
        if b.get("name", "").strip() == bn:
            return b["key"]
    subs = [b for b in buckets if bn in b.get("name", "")]
    if subs:                                       # 2) 부분일치 — doc_count 최대(서브브랜드 중 메인 근사)
        return max(subs, key=lambda b: b.get("doc_count", 0))["key"]
    return None                                    # 3) agg 미포함 → caller에서 lookup_brand_code 폴백


def _brand_from_keyword(keyword: str, buckets) -> tuple[str, str] | None:
    """키워드 자체가 브랜드를 특정하면 (name, code) 반환. LLM 가이드와 무관하게 사용자 명시 의도 최우선.
    우선순위: 1) agg 정확명(닥스→닥스) 2) agg 부분문자열(베네통여성→베네통) 3) 상품검색 폴백(씨→씨).
    '기타' bucket은 제외. 폴백은 첫 상품 brandNm이 keyword와 정확 일치할 때만 채택."""
    for b in buckets:                              # 1) agg 정확명 일치
        name = b.get("name", "").strip()
        if name and name not in ("기타",) and name == keyword:
            return (name, b["key"])
    for b in buckets:                              # 2) agg name이 keyword의 부분문자열
        name = b.get("name", "").strip()
        if name and name not in ("기타",) and name in keyword:
            return (name, b["key"])
    return lookup_brand_by_name(keyword)           # 3) 상품검색 폴백(brandNm 정확 일치 검증)


def resolve_filters(ext: dict, agg: dict, keyword: str):
    """추출어 → 필터 해상 + 안내문 트렌드 명사로 키워드 풍부화.
    (filters, info, enriched, residual, trend, brand_locked) 반환.
    brand_locked=True면 키워드가 브랜드를 특정한 것 — caller(ui.py)는 brandCd 완화 금지."""
    filters, info, kw_stop = {}, {}, set(GENDER) | {"남", "여"}
    brand_locked = False
    season = next((s for s in SEASONS if s in keyword), None)   # 시즌 post-filter와 중복 방지
    if season:
        kw_stop.add(season)
    if ext["gender"] in GENDER:
        filters["gndCd"] = GENDER[ext["gender"]]
        info["성별"] = f"{ext['gender']} → gndCd={filters['gndCd']}"
    buckets = agg.get("brand", {}).get("buckets", [])
    # 키워드 자체가 브랜드를 특정하면 그것만 적용 — LLM 가이드 응닡과 무관하게 강제 적용
    kw_brand = _brand_from_keyword(keyword, buckets)
    if kw_brand:
        resolved, codes = [kw_brand], {kw_brand[1]}
        kw_stop.add(kw_brand[0])
        brand_locked = True                         # ui.py에서 brandCd 완화 스킵 신호
    else:
        mentioned = ext.get("brands", [])[:3]
        # 검색어에 명시된 브랜드가 있으면 그것만 적용 — 가이드가 권장한 다른 브랜드는 제외
        target = [bn for bn in mentioned if bn and bn in keyword] or mentioned
        resolved, codes = [], set()                   # 가이드 브랜드명 → brandCd(다중)
        for bn in target:
            code = _brand_code(bn, buckets) or lookup_brand_code(bn)  # 정확명→부분일치→검색 폴백
            if code and code not in codes:            # 코드 중복 제거
                codes.add(code)
                resolved.append((bn, code))
                kw_stop.add(bn)
    if resolved:
        filters["brandCd"] = ",".join(c for _, c in resolved)
        info["브랜드"] = f"{', '.join(n for n, _ in resolved)} → brandCd={filters['brandCd']}"
        # 단일 브랜드 특정 시 잠금(키워드 명시 또는 가이드 단일 브랜드).
        # 다중 브랜드(가이드 추런)는 완화 가능하므로 잠금 제외.
        brand_locked = brand_locked or len(resolved) == 1
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
    trend = [t for t in ext.get("keywords", "").split() if t and t not in drop]
    if trend:
        info["트렌드키워드(안내문)"] = ", ".join(trend)
    enriched = " ".join([residual] + trend).strip()
    return filters, info, enriched, residual, trend, brand_locked


def apply_post_filters(pool: list, price_band, season: str) -> list:
    """가격대(평균 근사치)·시즌(상품명 매칭) post-filter."""
    out = pool
    if season:                                   # 시즌어가 상품명에 포함된 상품만
        out = [p for p in out if season in p["prdNm"]]
    if price_band:                               # 평균 근사치 가격대
        lo, hi = price_band
        out = [p for p in out if isinstance(p["selPrc"], (int, float)) and lo <= p["selPrc"] <= hi]
    return out
