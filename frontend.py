import streamlit as st
import requests
import pandas as pd

# =====================================================
# CONFIG
# =====================================================
API_BASE_URL = "http://localhost:5612"
PROCESS_ENDPOINT = f"{API_BASE_URL}/api/v1/process-document"

# =====================================================
# PAGE SETUP
# =====================================================
st.set_page_config(
    page_title="INVOICE PROCESSING",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

# =====================================================
# CUSTOM CSS
# =====================================================
st.markdown("""
<style>
.main { background-color: #f8f9fa; }
.stButton>button {
    width: 100%;
    background-color: #4CAF50;
    color: white;
    font-weight: bold;
    border-radius: 8px;
    padding: 0.5rem 1rem;
    border: none;
}
.stButton>button:hover { background-color: #45a049; }
h4 { color: #2c3e50; margin-top: 20px; border-bottom: 2px solid #eee; padding-bottom: 5px; }
.stCaption { font-size: 0.9rem; color: #7f8c8d; }
</style>
""", unsafe_allow_html=True)

# =====================================================
# HELPERS
# =====================================================
def recursive_display(data):
    """
    Recursively displays dictionary data. 
    Simple fields are shown in columns.
    Nested dictionaries are shown as subsections.
    """
    if not isinstance(data, dict):
        return

    # 1. Separate Simple Fields vs Nested Objects
    simple_fields = {k: v for k, v in data.items() if not isinstance(v, (dict, list)) and v is not None}
    nested_objects = {k: v for k, v in data.items() if isinstance(v, dict) and v is not None}
    
    # 2. Display Simple Fields in Grid
    if simple_fields:
        cols = st.columns(3)
        keys = list(simple_fields.keys())
        for i, key in enumerate(keys):
            val = simple_fields[key]
            with cols[i % 3]:
                st.caption(key.replace('_', ' ').title())
                if isinstance(val, float):
                    st.markdown(f"**{val:,.2f}**")
                else:
                    st.markdown(f"**{val}**")
    
    # 3. Display Nested Objects (Recursion)
    if nested_objects:
        for key, val in nested_objects.items():
            st.markdown(f"#### {key.replace('_', ' ').title()}")
            recursive_display(val)

# =====================================================
# MAIN
# =====================================================
def main():
    # ---------------- Sidebar ----------------
    with st.sidebar:
        st.title("📦 Invoice AI")
        st.markdown("Universal Document Processing")

        st.markdown("### Supported Docs")
        st.success("📄 Invoices")
        st.info("📝 Purchase Orders")
        st.warning("🚚 GRNs")

        st.markdown("### Backend Status")
        try:
            requests.get(f"{API_BASE_URL}/docs", timeout=2)
            st.success("Backend Online")
        except:
            st.error("Backend Offline")
            
        if st.button("🔄 Reset / Clear Cache"):
            if 'extraction_results' in st.session_state:
                del st.session_state['extraction_results']
            st.rerun()

    # ---------------- Main UI ----------------
    st.title("📄 Invoice Document Processor")
    st.markdown("Upload a PDF containing Invoices, Purchase Orders, or GRNs.")
    
    # Use a specific key for the uploader to help manage state
    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"], key="pdf_uploader")

    if uploaded_file:
        st.write(f"**File:** {uploaded_file.name}")
        st.write(f"**Size:** {round(uploaded_file.size / 1024, 2)} KB")

        # Logic to detect new file and clear old results
        if 'last_uploaded_file' not in st.session_state or st.session_state.last_uploaded_file != uploaded_file.name:
            st.session_state.last_uploaded_file = uploaded_file.name
            if 'extraction_results' in st.session_state:
                del st.session_state['extraction_results']

        # Start Processing Button
        if st.button("🚀 Start Processing"):
            with st.spinner("Processing document... (Classification -> Splitting -> Parallel Extraction)"):
                try:
                    # Reset pointer to 0 just in case
                    uploaded_file.seek(0)
                    files = {"file": (uploaded_file.name, uploaded_file, "application/pdf")}
                    
                    response = requests.post(PROCESS_ENDPOINT, files=files, timeout=600)

                    if response.status_code == 200:
                        # STORE RESULT IN SESSION STATE
                        st.session_state['extraction_results'] = response.json()
                    else:
                        st.error(f"Error {response.status_code}: {response.text}")

                except Exception as e:
                    st.error(f"Connection Error: {str(e)}")

    # ---------------- Display Results from Session State ----------------
    # This block runs even after reruns (e.g., clicking checkboxes)
    if 'extraction_results' in st.session_state:
        display_results(st.session_state['extraction_results'])


# =====================================================
# RESULTS
# =====================================================
def display_results(result):
    # ---------- Classification ----------
    if result.get("classification"):
        st.markdown("### 📑 Classification Map")
        rows = []
        class_data = result["classification"]
        for dtype, ranges in class_data.items():
            if not ranges: continue
            for r in ranges:
                rows.append({
                    "Document Type": dtype.upper(),
                    "Pages": f"{r[0]}-{r[1]}",
                    "Page Count": r[1] - r[0] + 1
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No pages classified into known categories.")

    st.markdown("---")
    st.markdown("### 📝 Extracted Data")

    results_list = result.get("results", [])
    if not results_list:
        st.warning("No extraction results returned.")
        return

    # ---------- GROUPING ----------
    invoices, pos, grns, others = [], [], [], []

    for r in results_list:
        dtype = (r.get("document_type") or "").lower()
        if "invoice" in dtype:
            invoices.append(r)
        elif "po" in dtype or "purchase" in dtype:
            pos.append(r)
        elif "grn" in dtype or "goods" in dtype:
            grns.append(r)
        else:
            others.append(r)

    tab1, tab2, tab3, tab4 = st.tabs([
        f"Invoices ({len(invoices)})",
        f"Purchase Orders ({len(pos)})",
        f"GRNs ({len(grns)})",
        f"Others ({len(others)})"
    ])

    with tab1: render_docs(invoices, "Invoice")
    with tab2: render_docs(pos, "PO")
    with tab3: render_docs(grns, "GRN")
    with tab4: render_docs(others, "Document")

# =====================================================
# DOC RENDERER
# =====================================================
def render_docs(docs, label):
    if not docs:
        st.caption(f"No {label}s found in this batch.")
        return

    for i, doc in enumerate(docs):
        data = doc.get("data") or {}
        error = doc.get("error")
        pages = doc.get("page_range", [0, 0])

        # Smart Reference ID
        ref = "Unknown Ref"
        if isinstance(data, dict):
            ref = (
                data.get("invoice_no") or      
                data.get("po_number") or       
                data.get("grn_number") or      
                data.get("invoice_number") or  
                "Unknown Ref"
            )

        # Expander Header
        with st.expander(f"📄 {label} #{i+1} — {ref} (Pages {pages[0]}-{pages[1]})", expanded=(i == 0)):
            if error:
                st.error(f"Extraction Failed: {error}")
                continue

            # 1. Header Details
            st.markdown("#### Header Details")
            header_data = {k: v for k, v in data.items() if k != 'items'}
            recursive_display(header_data)

            # 2. Line Items
            items = data.get("items", [])
            st.markdown(f"#### Line Items ({len(items)})")

            if items:
                df_items = pd.DataFrame(items)
                st.dataframe(df_items, use_container_width=True, hide_index=True)
            else:
                st.info("No line items found.")

            # 3. Raw JSON View (Persisted)
            st.divider()
            # This checkbox triggers a rerun, but since data is in session_state,
            # display_results is called again, and the checkbox state is maintained.
            if st.checkbox("Show Raw JSON", key=f"raw_{label}_{i}_{ref}"):
                st.markdown("```json")
                st.json(doc)
                st.markdown("```")

if __name__ == "__main__":
    main()