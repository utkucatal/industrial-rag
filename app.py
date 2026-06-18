"""Streamlit chat that also exposes the retrieval and the prompt, so the RAG is visible."""
import anthropic
import streamlit as st
from dotenv import load_dotenv

import rag

load_dotenv(override=True)

st.set_page_config(page_title="Industrial Product RAG", page_icon="🔧")
st.title("🔧 Industrial Product RAG")
st.caption("Answers only from the 411 products in the catalog.")


@st.cache_resource
def get_conn():
    return rag.connect()


@st.cache_resource
def get_client():
    return anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment


@st.cache_resource
def warm_embedder():
    rag.embedder()   # load bge-m3 once at startup
    return True


warm_embedder()
conn = get_conn()
client = get_client()

if not rag.index_ready(conn):
    st.warning("Index not built yet. Run:  `docker compose exec app python rag.py`")
    st.stop()

q = st.chat_input("e.g. vacuum pumps over 300 kg / Siemens motors under €2000")
if q:
    st.chat_message("user").write(q)
    with st.chat_message("assistant"):
        with st.spinner("Searching..."):
            filters, results = rag.retrieve(conn, client, q)
            ans = rag.answer(client, q, results)
        st.write(ans)

        with st.expander(f"🔎 Retrieval details — {len(results)} products"):
            st.write("**Extracted filters:**")
            st.json(filters)
            for r in results:
                st.write(f"- **{r['id']}** · score `{r['sim']:.2f}` · {r['title']}")

        with st.expander("📝 Prompt sent to Claude"):
            st.code(rag.build_prompt(results) if results else "(no catalog match)")