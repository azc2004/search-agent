"""Streamlit UI — main + UI 헬퍼."""
import html
import time
import urllib.parse
import streamlit as st

from config import HAPIX, SEASONS
from sources import (get_search_signals, get_naver_trends, get_musinsa_trends,
                     get_fashion_articles, get_exa_articles, build_trend_context,
                     search_pool, get_popular_keywords, get_popular_brands)
from llm import make_guide, curate_products
from filters import resolve_filters, apply_post_filters
from zilliz import get_desc_map


def _popular_row(items: list[tuple[str, str]], title: str, key_prefix: str):
    """컴팩트 pill 버튼 라인 — items=[(label, value), ...]. 클릭 시 value를 kw_input으로.
    키워드 길이 비례 가로 나열(문자합 ~40 기준 줄바꿈). 빈 값이면 노출 생략."""
    if not items:
        return
    st.markdown(
        f'<div style="font-weight:700;font-size:13px;color:#666;margin:8px 0 4px">{title}</div>',
        unsafe_allow_html=True)
    rows, cur, cur_len = [], [], 0
    for it in items:                                # 문자합 ~40 기준 행 분할
        n = len(it[0])
        if cur and cur_len + n > 40:
            rows.append(cur); cur, cur_len = [], 0
        cur.append(it); cur_len += n
    if cur:
        rows.append(cur)
    for ri, row in enumerate(rows):
        cols = st.columns([len(it[0]) for it in row], gap="small")  # 비례 폭 + 좁은 갭
        for ci, (label, value) in enumerate(row):
            with cols[ci]:
                if st.button(label, key=f"{key_prefix}_{ri}_{ci}", use_container_width=True):
                    st.session_state._pending_kw = value
                    st.rerun()


def main():
    st.set_page_config(page_title="검색어 기반 추천 PoC", layout="wide")
    st.title("검색어 기반 LLM 추천 안내")

    st.markdown(
        "<style>"
        "[data-testid='stTextInput'] input{border-radius:24px;background:#f5f6f8;"
        "border:1px solid #e3e6eb;padding:11px 18px;font-size:15px;height:auto}"
        "[data-testid='stTextInput'] input:focus{border-color:#1e88e5;background:#fff;"
        "box-shadow:0 0 0 3px rgba(30,136,229,.13)}"
        "[data-testid='stButton'] button{padding:4px 8px!important;font-size:13px!important;"
        "line-height:1.2!important;border-radius:14px!important}"
        "</style>", unsafe_allow_html=True)
    # 인기검색어 클릭 보류값 — 위젯 렌더 전에 소개(reset)해야 kw_input 수정 RuntimeError 회피
    if "_pending_kw" in st.session_state:
        st.session_state.kw_input = st.session_state.pop("_pending_kw")
        st.session_state._pop_triggered = True
    # 검색바(폼) — 엔터/버튼(폼 제출) 시만 검색 실행. 입력창은 기본 빈 값.
    if "kw_input" not in st.session_state:
        st.session_state.kw_input = ""
    with st.form("search_form", clear_on_submit=False):
        sbar = st.columns([7, 2])
        with sbar[0]:
            keyword = st.text_input("검색어", key="kw_input",
                                    label_visibility="collapsed", placeholder="상품·트렌드를 검색하세요")
        with sbar[1]:
            submitted = st.form_submit_button("🔍  검색", use_container_width=True, type="primary")
        llm_mode = st.radio("LLM 호출 방식", ["직접 (OpenAI 호환)", "LiteLLM 프록시"],
                            horizontal=True, index=0)
    # 인기검색어/인기브랜드 — 탭 분리(첫 탭=인기검색어 기본 노출). 항상 활성.
    tab_kw, tab_br = st.tabs(["실시간 인기검색어", "실시간 인기브랜드"])
    with tab_kw:
        _popular_row([(p["keyword"], p["keyword"]) for p in get_popular_keywords()],
                     "클릭 시 검색", "pop")
    with tab_br:
        _popular_row([(b["brand_name"], b["brand_name"]) for b in get_popular_brands()],
                     "클릭 시 검색", "pob")
    # 트렌드 소스/실시간크롤 — 폼 밖(반응형): Daum·Google 또는 사용안함 시 실시간크롤 옵션 즉시 히든
    trend_src = st.radio("외부 패션 트렌드 수집", ["사용 안 함", "Daum·Google 뉴스", "Exa AI (매체 본문)"],
                         horizontal=True, index=2)
    use_livecrawl = trend_src == "Exa AI (매체 본문)" and \
        st.checkbox("Exa 실시간 크롤(livecrawl, 본문 신선도↑·지연↑)", value=True)
    use_trends = trend_src == "Daum·Google 뉴스"
    use_exa = trend_src == "Exa AI (매체 본문)"
    use_litellm = llm_mode == "LiteLLM 프록시"
    # 검색 트리거: 폼 제출 OR 인기검색어 클릭
    run = submitted or st.session_state.pop("_pop_triggered", False)
    if not run:
        if keyword:
            st.caption("검색 버튼을 누르거나 엔터로 실행하세요.")
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
        articles += get_exa_articles(keyword, use_livecrawl)
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
        guide_text = ext["guide"] or "안내 메시지를 생성하지 못했습니다."
        st.markdown(
            f'<div style="background:linear-gradient(90deg,#eef6ff,#f8fbff);'
            f'border-left:4px solid #1e88e5;border-radius:8px;padding:14px 16px;margin:6px 0;'
            f'font-size:16px;line-height:1.7;color:#1a3a5c;font-weight:500">'
            f'{html.escape(guide_text)}</div>',
            unsafe_allow_html=True)
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
