"""진입점. streamlit run app.py → UI / python app.py → 파이프라인 셀프체크."""
from streamlit.runtime.scriptrunner import get_script_run_ctx

from config import SEASONS
from sources import (get_search_signals, get_naver_trends, get_musinsa_trends,
                     get_fashion_articles, build_trend_context, search_pool)
from llm import make_guide, curate_products
from filters import resolve_filters, apply_post_filters
from ui import main


def _selfcheck():
    """파이프라인 통합 셀프체크 (여름 원피스)."""
    kw0 = "여름 원피스"
    sig = get_search_signals(kw0)
    ctx = build_trend_context(sig, kw0, get_naver_trends(), get_musinsa_trends(),
                              get_fashion_articles(kw0))
    ext = make_guide(kw0, ctx, False)
    assert ext["guide"], ext
    f, info, qen, _, _ = resolve_filters(ext, sig["agg"], kw0)
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
    print(f"OK filters={f} enriched={qen!r} season={season} search={len(prods)} "
          f"cur_picks={len(cur['picks'])} top={cur['top']}")
    print("summary:", cur["summary"][:80])
    print("info:", info)
    print("guide:", ext["guide"][:120])


if __name__ == "__main__":
    if get_script_run_ctx() is not None:               # streamlit run
        main()
    else:                                               # python app.py → 셀프체크
        _selfcheck()
