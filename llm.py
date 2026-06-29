"""LLM 호출 (직접 OpenAI 호환 / LiteLLM 프록시 토글) — 가이드 생성·큐레이션."""
import json
import requests
import streamlit as st

from config import (LITELLM_BASE_URL, LITELLM_API_KEY, GUIDE_MODEL,
                    DIRECT_BASE_URL, DIRECT_API_KEY, TIMEOUT_LLM,
                    SYSTEM_PROMPT, CURATE_SYS)


def call_llm(system_prompt: str, user_content: str, max_tokens: int, use_litellm: bool) -> str:
    """직접(OpenAI 호환) / LiteLLM 프록시(Anthropic 포맷) 토글. 본문 텍스트 반환."""
    if use_litellm:                                  # 사내 LiteLLM 프록시
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


def make_guide(keyword: str, context: str, use_litellm: bool) -> dict:
    """컨텍스트를 근거로 트렌드 안내문 + 검색어(JSON) 추출. 실패 시 폴백 dict."""
    try:
        t = call_llm(SYSTEM_PROMPT, f"{context}\n\n검색어: {keyword}", 700, use_litellm)
        t = t[t.find("{"):t.rfind("}") + 1]            # JSON 본체만 추출(방어)
        o = json.loads(t)
        brands = [b for b in o.get("brand", "").split() if b]
        return {"guide": o.get("guide", ""), "brands": brands,
                "category": o.get("category", "").strip(), "gender": o.get("gender", "").strip(),
                "keywords": o.get("keywords", "").strip(),
                "season": o.get("season", "").strip()}
    except Exception:
        return {"guide": "", "brands": [], "category": "", "gender": "", "keywords": "", "season": ""}


@st.cache_data(ttl=600)
def curate_products(keyword: str, guide: str, cand_key: tuple, use_litellm: bool) -> dict:
    """후보 상품 중 목적에 맞는 ~6개 선정 + 추천 근거 + 1순위. idx 기준(prdNo 매핑)."""
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
