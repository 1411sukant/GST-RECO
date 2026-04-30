import streamlit as st
import pandas as pd
import pdfplumber
import re
import io

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="GST Reconciliation Engine", page_icon="⚖️", layout="wide")
st.title("⚖️ Automated GST Reconciliation Engine")
st.caption("Modules: Outward Supplies, ITC Availment, GSTR-2B Matching, Invoice Forensic Report")

# ── CONSTANTS & TEMPLATES ─────────────────────────────────────────────────────
MONTHS_FY = ["Opening", "April", "May", "June", "July", "August", "September", 
             "October", "November", "December", "January", "February", "March"]

MODULE_1_COLS = ['Month', 'B2B', 'B2C', 'Amendment', 'Debit Note', 'Credit Note', 'Export', 
                 'Advances Adjusted', 'Outward Supply (Net)', 'IGST', 'CGST', 'SGST']

def create_fy_template():
    df = pd.DataFrame(columns=MODULE_1_COLS)
    df['Month'] = MONTHS_FY
    for col in MODULE_1_COLS[1:]:
        df[col] = 0.0
    return df.set_index('Month')

# ── 1. CORE HELPERS & KEYWORD MAPPER ──────────────────────────────────────────
def standardize_columns(df):
    """Hunts for true headers and maps them to our strict internal columns."""
    
    # --- UPGRADE: TRUE HEADER HUNTER (BULLETPROOFED) ---
    keywords = ['b2b', 'b2c', 'sale', 'igst', 'cgst', 'sgst', 'credit note', 'month', 'date', 'value']
    
    # FIX: Deep cast every column to str to prevent "float found" join crashes
    cols_as_str = [str(c) for c in df.columns]
    max_score = sum(1 for k in keywords if k in " ".join(cols_as_str).lower())
    best_idx = -1
    
    for i in range(min(5, len(df))):
        # FIX: Deep cast every cell to str to prevent "float found" join crashes
        row_as_str = [str(x) for x in df.iloc[i]]
        row_str = " ".join(row_as_str).lower()
        score = sum(1 for k in keywords if k in row_str)
        if score > max_score:
            max_score = score
            best_idx = i
            
    if best_idx != -1:
        new_headers = []
        for col_idx in range(len(df.columns)):
            val1 = str(df.columns[col_idx]).replace("\n", " ")
            if val1.lower().startswith("unnamed"): val1 = ""
            
            val2 = str(df.iloc[best_idx, col_idx]).replace("\n", " ")
            if val2 == "nan" or val2 == "None": val2 = ""
            
            new_headers.append(f"{val1} {val2}".strip())
        df.columns = new_headers
        df = df.iloc[best_idx+1:].reset_index(drop=True)
    # ------------------------------------

    # Convert all columns to strings for mapping
    df.columns = [str(c).lower().strip() for c in df.columns]
    
    mapping = {
        'b2b': 'B2B', 'b2c': 'B2C',
        'sale': 'B2B', 'job work': 'B2B', 'sales': 'B2B', 'taxable value': 'B2B', 'value': 'B2B',
        'amendment': 'Amendment', 'amd': 'Amendment', 
        'debit note': 'Debit Note', 'credit note': 'Credit Note', 'cn': 'Credit Note', 'dn': 'Debit Note',
        'return': 'Credit Note', 'sales return': 'Credit Note', 
        'export': 'Export', 'sez': 'Export',
        'advance': 'Advances Adjusted', 'adj': 'Advances Adjusted',
        'igst': 'IGST', 'integrated tax': 'IGST', 'gst-integrated': 'IGST', 'gst integrated': 'IGST',
        'cgst': 'CGST', 'central tax': 'CGST', 'gst- central': 'CGST', 'gst central': 'CGST',
        'sgst': 'SGST', 'state tax': 'SGST', 'gst- state': 'SGST', 'gst state': 'SGST',
        'month': 'Month', 'period': 'Month', 'mth': 'Month',
        'date': 'Date', 'invoice date': 'Date', 'doc date': 'Date', 'transaction date': 'Date'
    }
    
    new_cols = {}
    for col in df.columns:
        for key, standard_name in mapping.items():
            if key in col:
                new_cols[col] = standard_name
                break
                
    df = df.rename(columns=new_cols)
    
    target_numeric = ['B2B', 'B2C', 'Amendment', 'Debit Note', 'Credit Note', 'Export', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']
    for col in target_numeric:
        if col not in df.columns:
            df[col] = 0.0
        elif isinstance(df[col], pd.DataFrame):
            summed = df[col].apply(pd.to_numeric, errors='coerce').fillna(0).sum(axis=1)
            df = df.drop(columns=[col])
            df[col] = summed
        else:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            
    df = df.loc[:, ~df.columns.duplicated()]
    
    # --- UPGRADE: NEGATIVE NUMBER AUTO-SHIFTER ---
    # If the user put Credit Notes as negative numbers in the B2B/B2C columns, move them!
    for col in ['B2B', 'B2C', 'Export']:
        neg_mask = df[col] < 0
        if neg_mask.any():
            df.loc[neg_mask, 'Credit Note'] += df.loc[neg_mask, col].abs()
            df.loc[neg_mask, col] = 0.0 
            
            for tax in ['IGST', 'CGST', 'SGST']:
                tax_mask = (df[col] == 0) & (df[tax] < 0) 
                if tax_mask.any():
                    df.loc[tax_mask, tax] = df.loc[tax_mask, tax].abs()

    if 'Credit Note' in df.columns: df['Credit Note'] = df['Credit Note'].abs()
    if 'Debit Note' in df.columns: df['Debit Note'] = df['Debit Note'].abs()
        
    return df

def ensure_month_column(df):
    if 'Month' not in df.columns:
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
            df['Month'] = df['Date'].dt.strftime('%B').fillna('Unknown')
        else:
            df['Month'] = "Unknown"
    
    df['Month'] = df['Month'].astype(str).str.strip().str.capitalize()
    
    month_map = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "Jun": "June", 
                 "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
    df['Month'] = df['Month'].replace(month_map)
    
    return df

def brute_force_assign(template_df, data_df, subtract=False):
    if 'Month' in data_df.columns:
        data_df = data_df.set_index('Month')
        
    for idx in data_df.index:
        idx_str = str(idx).strip().capitalize()
        if idx_str in template_df.index:
            for col in data_df.columns:
                if col in template_df.columns:
                    raw_val = data_df.loc[idx, col]
                    if isinstance(raw_val, pd.Series): raw_val = raw_val.sum()
                    try: val = float(raw_val)
                    except: val = 0.0
                        
                    if subtract: template_df.at[idx_str, col] -= val
                    else: template_df.at[idx_str, col] += val
                        
    return template_df

# ── 2. YOUR EXACT GSTR-1 PDF PARSER ───────────────────────────────────────────
def get_section_total(text, header_pattern, stop_pattern=None, target_word="total", window=1500):
    start_match = re.search(header_pattern, text, re.IGNORECASE | re.DOTALL)
    if not start_match: return 0.0
    start = start_match.start()
    end = start + window
    if stop_pattern:
        stop_match = re.search(stop_pattern, text[start + 10:], re.IGNORECASE)
        if stop_match: end = start + 10 + stop_match.start()
    section = text[start:end]
    target_match = re.search(target_word, section, re.IGNORECASE)
    if not target_match: return 0.0
    amounts = re.findall(r'-?[\d,]+\.\d{2}', section[target_match.start():])
    if amounts: return float(amounts[0].replace(',', ''))
    return 0.0

def extract_month_from_pdf(text):
    match = re.search(r'Tax\s+[Pp]eriod\s+([A-Za-z]+)', text)
    if match: return match.group(1).capitalize()
    for m in MONTHS_FY:
        if m != "Opening" and re.search(m, text[:500], re.IGNORECASE):
            return m
    return "Unknown"

def extract_liability(text):
    igst = cgst = sgst = 0.0
    match = re.search(r'Total\s+Liability\s*\(Outward[^)]+\)\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', text, re.IGNORECASE)
    if match:
        igst = float(match.group(2).replace(',', ''))
        cgst = float(match.group(3).replace(',', ''))
        sgst = float(match.group(4).replace(',', ''))
    else:
        m2 = re.search(r'Total\s+Liability', text, re.IGNORECASE)
        if m2:
            chunk = text[m2.start(): m2.start() + 400]
            amounts = re.findall(r'-?[\d,]+\.\d{2}', chunk)
            if len(amounts) >= 4:
                igst = float(amounts[1].replace(',', ''))
                cgst = float(amounts[2].replace(',', ''))
                sgst = float(amounts[3].replace(',', ''))
    return igst, cgst, sgst

def parse_gstr1_detailed(file):
    with pdfplumber.open(file) as pdf:
        # FIX: Deep cast to str to prevent join crashes here too
        full_text = "\n".join([str(page.extract_text() or "") for page in pdf.pages])

    month_name = extract_month_from_pdf(full_text)

    b2b = get_section_total(full_text, r'4A\s*[-–]?\s*Taxable\s+outward\s+supplies\s+made\s+to\s+registered', r'4B\s*[-–]?\s*Taxable')
    b2cs = get_section_total(full_text, r'7\s*[-–]?\s*Taxable\s+supplies.*?unregistered', r'8\s*[-–]?\s*Nil')
    
    exp_6a = get_section_total(full_text, r'6A\s*[–-]?\s*Exports?\s*\(', r'6B\s*[-–]?\s*Supplies')
    sez_6b = get_section_total(full_text, r'6B\s*[-–]?\s*Supplies\s+made\s+to\s+SEZ', r'6C\s*[-–]?\s*Deemed')
    deemed_6c = get_section_total(full_text, r'6C\s*[-–]?\s*Deemed\s+Exports', r'7\s*[-–]?\s*Taxable')
    total_exports = exp_6a + sez_6b + deemed_6c

    cdn_reg = get_section_total(full_text, r'9B\s*[-–]?\s*Credit/Debit\s+Notes?\s*\(Registered\)', r'9B\s*[-–]?\s*Credit/Debit\s+Notes?\s*\(Unregistered\)', target_word=r'Total\s*[-–]?\s*Net\s+off')
    cdn_unreg = get_section_total(full_text, r'9B\s*[-–]?\s*Credit/Debit\s+Notes?\s*\(Unregistered\)', r'9C\s*[-–]?\s*Amended', target_word=r'Total\s*[-–]?\s*Net\s+off')
    total_cdn = cdn_reg + cdn_unreg

    amendment_9a = 0.0
    sec_9a = re.search(r'9A\s*[-–]?\s*Amendment', full_text, re.IGNORECASE)
    sec_9b = re.search(r'9B\s*[-–]?\s*Credit', full_text, re.IGNORECASE)
    if sec_9a:
        chunk_9a = full_text[sec_9a.start(): sec_9b.start() if sec_9b else sec_9a.start() + 5000]
        for m in re.finditer(r'Amended\s+amount\s*[-–]?\s*Total', chunk_9a, re.IGNORECASE):
            snippet = chunk_9a[m.start(): m.start() + 300]
            amounts = re.findall(r'-?[\d,]+\.\d{2}', snippet)
            if amounts:
                val = float(amounts[0].replace(',', ''))
                if val != 0.0:
                    amendment_9a += val

    advances = get_section_total(full_text, r'11A\(1\).*?Advances', r'11B\(1\).*?Advance', target_word=r'Total')
    igst, cgst, sgst = extract_liability(full_text)

    return {
        "Month": month_name, "B2B": b2b, "B2C": b2cs, "Amendment": amendment_9a,
        "Debit Note": 0.0, "Credit Note": abs(total_cdn), 
        "Export": total_exports, "Advances Adjusted": advances,
        "IGST": igst, "CGST": cgst, "SGST": sgst
    }

# ── 3. MULTI-USER SESSION STATE INITIALIZATION ────────────────────────────────
if 'books_sales_data' not in st.session_state:
    st.session_state.books_sales_data = None
if 'books_cn_data' not in st.session_state:
    st.session_state.books_cn_data = None
if 'gstr1_data_list' not in st.session_state:
    st.session_state.gstr1_data_list = []

# ── 4. MASTER UPLOAD DASHBOARD ────────────────────────────────────────────────
st.header("📂 Master File Upload")
st.info("Upload files to generate the Executive Reconciliation layout.")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📚 Books: Outward")
    
    books_sales_file = st.file_uploader("Sales Register (Excel)", type=["xlsx", "xls"], key="sales_upload")
    if books_sales_file:
        # Load and store in session state to protect against reruns
        st.session_state.books_sales_data = pd.read_excel(books_sales_file)
        
    books_cn_file = st.file_uploader("Credit Notes (Excel)", type=["xlsx", "xls"], key="cn_upload")
    if books_cn_file:
        st.session_state.books_cn_data = pd.read_excel(books_cn_file)

with col2:
    st.subheader("📚 Books: Inward")
    books_purchase_file = st.file_uploader("Purchase Register (Excel)", type=["xlsx", "xls"], disabled=False)
    books_dn_file = st.file_uploader("Debit Notes (Excel)", type=["xlsx", "xls"], disabled=False)

with col3:
    st.subheader("🌐 GST Portal Files")
    gstr1_files = st.file_uploader("GSTR-1 (PDF)", type=["pdf"], accept_multiple_files=True, key="gstr1_upload")
    # Only process PDFs if they haven't been processed yet in this session
    if gstr1_files and len(gstr1_files) > len(st.session_state.gstr1_data_list):
        with st.spinner("Processing GSTR-1 PDFs..."):
            st.session_state.gstr1_data_list = [parse_gstr1_detailed(f) for f in gstr1_files]
            
    credit_ledger_file = st.file_uploader("Electronic Credit Ledger (Excel)", type=["xlsx", "xls"], disabled=False)

st.divider()

# ── 5. RECONCILIATION ENGINE TRIGGER ──────────────────────────────────────────
if st.button("⚡ Run Reconciliation Engine", type="primary"):
    
    if st.session_state.books_sales_data is not None and len(st.session_state.gstr1_data_list) > 0:
        st.header("📊 Module 1: Outward Supplies")
        try:
            df_books_final = create_fy_template()
            df_gstr1_final = create_fy_template()
            
            book_cn_grouped = pd.DataFrame()

            # --- PROCESS BOOKS (Main Sales File) ---
            # Create a copy so we don't alter the raw session state data
            df_sales = standardize_columns(st.session_state.books_sales_data.copy())
            df_sales = ensure_month_column(df_sales)
            
            book_sales_grouped = df_sales.groupby('Month')[['B2B', 'B2C', 'Amendment', 'Export', 'Debit Note', 'Credit Note', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            df_books_final = brute_force_assign(df_books_final, book_sales_grouped)

            # --- PROCESS BOOKS (Dedicated Credit Notes File) ---
            if st.session_state.books_cn_data is not None:
                df_cn = standardize_columns(st.session_state.books_cn_data.copy())
                df_cn = ensure_month_column(df_cn)
                
                df_cn['Credit Note'] = df_cn[['Credit Note', 'B2B', 'B2C', 'Export']].sum(axis=1)
                
                book_cn_grouped = df_cn.groupby('Month')[['Credit Note', 'IGST', 'CGST', 'SGST']].sum().reset_index()
                
                df_books_final = brute_force_assign(df_books_final, book_cn_grouped[['Month', 'Credit Note']])
                df_books_final = brute_force_assign(df_books_final, book_cn_grouped[['Month', 'IGST', 'CGST', 'SGST']], subtract=True)

            df_books_final['Outward Supply (Net)'] = (
                df_books_final['B2B'] + df_books_final['B2C'] + 
                df_books_final['Amendment'] + df_books_final['Export'] + 
                df_books_final['Debit Note'] + df_books_final['Advances Adjusted'] - df_books_final['Credit Note']
            )

            # --- PROCESS GSTR-1 ---
            df_gstr1_raw = pd.DataFrame(st.session_state.gstr1_data_list)
            if not df_gstr1_raw.empty:
                gstr1_grouped = df_gstr1_raw.groupby('Month').sum().reset_index()
                df_gstr1_final = brute_force_assign(df_gstr1_final, gstr1_grouped)
                
            df_gstr1_final['Outward Supply (Net)'] = (
                df_gstr1_final['B2B'] + df_gstr1_final['B2C'] + 
                df_gstr1_final['Amendment'] + df_gstr1_final['Export'] + 
                df_gstr1_final['Debit Note'] + df_gstr1_final['Advances Adjusted'] - df_gstr1_final['Credit Note']
            )

            # --- CALCULATE DIFFERENCE ---
            df_diff_final = df_books_final - df_gstr1_final

            # --- FORMATTING & DISPLAY ---
            def format_df(df):
                df = df.reset_index()
                total_row = df.sum(numeric_only=True)
                total_row['Month'] = 'Total'
                df.loc[len(df)] = total_row
                
                style = {col: "{:,.2f}" for col in df.columns if col != 'Month'}
                def hide_zeros(val):
                    if isinstance(val, (int, float)) and val == 0:
                        return "color: transparent"
                    return ""
                
                return df.style.format(style).map(hide_zeros)
            
            st.markdown("### 📘 GST AS PER BOOKS")
            st.dataframe(format_df(df_books_final), use_container_width=True, hide_index=True)
            
            # --- THE X-RAY DEBUGGER ---
            with st.expander("🔍 🚨 DIAGNOSTICS: Check how the engine read your files", expanded=False):
                colA, colB = st.columns(2)
                with colA:
                    st.write("**Data from Sales File:**")
                    st.dataframe(book_sales_grouped[['Month', 'B2B', 'Credit Note']], use_container_width=True)
                
                with colB:
                    st.write("**Data from Credit Note File:**")
                    if not book_cn_grouped.empty:
                        st.dataframe(book_cn_grouped[['Month', 'Credit Note', 'IGST', 'CGST', 'SGST']], use_container_width=True)
                    else:
                        st.write("*No dedicated Credit Note file was uploaded.*")

            st.markdown("### 🌐 GST AS PER GSTR 1")
            st.dataframe(format_df(df_gstr1_final), use_container_width=True, hide_index=True)

            st.markdown("### ⚖️ DIFFERENCE")
            st.dataframe(format_df(df_diff_final), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Error processing Module 1: {e}")
    else:
        st.warning("Upload 'Sales Register' and at least one 'GSTR-1' PDF to view the summary.")
