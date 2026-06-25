"""Zilliz Cloud → 상품 desc 맵. 컬렉션 prd_desc_vec_128, 필드 prd_no(VarChar)/desc."""
import streamlit as st


@st.cache_resource
def get_zilliz_client():
    from pymilvus import MilvusClient
    return MilvusClient(uri=st.secrets["ZILLIZ_URI"], token=st.secrets["ZILLIZ_TOKEN"])


def get_desc_map(prd_nos: list) -> dict:
    """추천 상품번호로 스칼라 필터 query → {prdNo(int): desc}. 미설정/실패 시 {}."""
    if not prd_nos or not st.secrets.get("ZILLIZ_URI"):
        return {}
    try:
        rows = get_zilliz_client().query(
            collection_name=st.secrets["ZILLIZ_COLLECTION"],
            filter=f'prd_no in {[str(n) for n in prd_nos]}',
            output_fields=["prd_no", "desc"], limit=len(prd_nos))
        return {int(r["prd_no"]): (r.get("desc") or "") for r in rows}
    except Exception as e:
        st.warning(f"Zilliz 조회 실패: {e}")
        return {}
