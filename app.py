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
    """Creates a blank FY dataframe with explicitly float types to prevent crashes."""
    df = pd.DataFrame(columns=MODULE_1_COLS)
    df['Month'] = MONTHS_FY
    for col in MODULE_1_COLS[1:]:
        df[col] = 0.0
    return df.set_index('Month')

# ── 1. CORE HELPERS & KEYWORD MAPPER ──────────────────────────────────────────
def standardize_columns(df):
    """Maps messy Excel headers to our strict internal columns."""
    df.columns = df.columns.astype(str).str.lower().str.strip()
    
    mapping = {
        'b2b': 'B2B', 'b2c': 'B2C',
        'sale': 'B2B', 'job work': 'B2B', 'sales': 'B2B', 
        'amendment': 'Amendment', 'amd': 'Amendment', 
        'debit note': 'Debit Note', 'credit note': 'Credit Note', 'cn': 'Credit Note', 'dn': 'Debit Note',
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
    
    # Brute-force convert to numeric and combine duplicate columns
    target_numeric = ['B2B', 'B2C', 'Amendment', 'Debit Note', 'Credit Note', 'Export', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']
    for col in target_numeric:
        if col not in df.columns:
            df[col] = 0.0
        elif isinstance(df[col], pd.DataFrame):
            df[col] = df[col].apply(pd.to_numeric, errors='coerce').fillna(0).sum(axis=1)
        else:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                
    return df

def ensure_month_column(df):
    """Bulletproof date parser."""
    if 'Month' not in df.columns:
        if 'Date' in df.columns:
            # dayfirst=True handles Indian DD/MM/YYYY formats perfectly
            df['Date'] = pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
            df['Month'] = df['Date'].dt.strftime('%B') # Extracts full month name (e.g., "April")
            df['Month'] = df['Month'].fillna('Unknown')
        else:
            df['Month'] = "Unknown"
    
    df['Month'] = df['Month'].astype(str).str.strip().str.capitalize()
    return df

def brute_force_assign(template_df, data_df, subtract=False):
    """Safely adds (or subtracts) data into the exact Month row without relying on Pandas .update()"""
    if 'Month' in data_df.columns:
        data_df = data_df.set_index('Month')
        
    for idx in data_df.index:
        idx_str = str(idx).strip().capitalize()
        if idx_str in template_df.index:
            for col in data_df.columns:
                if col in template_df.columns:
                    val = float(data_df.at[idx, col])
                    if subtract:
                        template_df.at[idx_str, col] -= val
                    else:
                        template_df.at[idx_str, col] += val
    return template_df

# --- ADVANCED GSTR-1 PARSER ---
def fix_broken_numbers(text):
    text = re.sub(r'(\d[\d,]*\.\d+)\n(\d+)', r'\1\2', text)
    return re.sub(r'(\d+)\n(\d{2})\b', r'\1\2', text)

def parse_gstr1_detailed(file):
    with pdfplumber.open(file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        
    text = fix_broken_numbers(text)
        
    # Month
    month = "Unknown"
    m_match = re.search(r"(?:Tax\s+[Pp]eriod|Period)\s+([A-Za-z]+)", text)
    if m_match: month = m_match.group(1).capitalize()
    
    # Helper for generic tables
    def extract_total(start_regex, stop_regex):
        m = re.search(start_regex, text, re.IGNORECASE | re.DOTALL)
        if not m: return 0.0
        chunk = text[m.start():]
        stop_m = re.search(stop_regex, chunk[10:], re.IGNORECASE)
        if stop_m: chunk = chunk[:stop_m.start() + 10]
        
        tm = re.search(r"Total[^\d-]*?(-?[\d,]+\.\d{2})", chunk, re.IGNORECASE)
        if tm: return float(tm.group(1).replace(",", ""))
        return 0.0

    b2b = extract_total(r"4A.*?Taxable", r"4B")
    b2cs = extract_total(r"7.*?Taxable", r"8")
    exp = extract_total(r"6A.*?Export", r"6B")
    sez = extract_total(r"6B.*?SEZ", r"6C")
    advances = extract_total(r"11A\(1\).*?Advance", r"11B")

    # Ultra-Aggressive Hunt for Amendments & Credit Notes
    amendments = sum(float(m.group(1).replace(",", "")) for m in re.finditer(r"Net\s+differential\s+amount[^\d-]*?(-?[\d,]+\.\d{2})", text, re.IGNORECASE))
    credit_notes = sum(float(m.group(1).replace(",", "")) for m in re.finditer(r"Net\s+off[^\d-]*?(-?[\d,]+\.\d{2})", text, re.IGNORECASE))

    # Ultra-Aggressive Hunt for Taxes (Last Page only!)
    igst = cgst = sgst = 0.0
    last_page_chunk = text[-3000:] # Jump to the end of the document
    
    # Look for the exact headers you mentioned
    header_match = re.search(r"(Integrated\s*[Tt]ax|Central\s*[Tt]ax|State)", last_page_chunk, re.IGNORECASE)
    if header_match:
        target_chunk = last_page_chunk[header_match.start():]
        # Find the Total Liability row right under those headers
        tax_row = re.search(r"Total\s+Liability[^\d]*?(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})", target_chunk, re.IGNORECASE)
        if tax_row:
            igst = float(tax_row.group(1).replace(",",""))
            cgst = float(tax_row.group(2).replace(",",""))
            sgst = float(tax_row.group(3).replace(",",""))
            
    # Absolute Fallback if headers are weirdly formatted
    if igst == 0.0 and cgst == 0.0:
        fallback = re.search(r"Total\s+Liability\s*\(Outward[^)]*\)[^\d]*?(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})", text, re.IGNORECASE)
        if fallback:
            igst, cgst, sgst = float(fallback.group(1).replace(",","")), float(fallback.group(2).replace(",","")), float(fallback.group(3).replace(",",""))

    return {
        "Month": month, "B2B": b2b, "B2C": b2cs, "Amendment": amendments,
        "Debit Note": 0.0, "Credit Note": abs(credit_notes), 
        "Export": exp + sez, "Advances Adjusted": advances,
        "IGST": igst, "CGST": cgst, "SGST": sgst
    }

# ── 2. MASTER UPLOAD DASHBOARD ────────────────────────────────────────────────
st.header("📂 Master File Upload")
st.info("Upload files to generate the Executive Reconciliation layout.")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📚 Books: Outward")
    books_sales_file = st.file_uploader("Sales Register (Excel)", type=["xlsx", "xls"])
    books_cn_file = st.file_uploader("Credit Notes (Excel)", type=["xlsx", "xls"])

with col2:
    st.subheader("📚 Books: Inward")
    books_purchase_file = st.file_uploader("Purchase Register (Excel)", type=["xlsx", "xls"], disabled=False)
    books_dn_file = st.file_uploader("Debit Notes (Excel)", type=["xlsx", "xls"], disabled=False)

with col3:
    st.subheader("🌐 GST Portal Files")
    gstr1_files = st.file_uploader("GSTR-1 (PDF)", type=["pdf"], accept_multiple_files=True)
    credit_ledger_file = st.file_uploader("Electronic Credit Ledger (Excel)", type=["xlsx", "xls"], disabled=False)

st.divider()

# ── 3. RECONCILIATION ENGINE TRIGGER ──────────────────────────────────────────
if st.button("⚡ Run Reconciliation Engine", type="primary"):
    
    # ==========================================================================
    # === MODULE 1: OUTWARD SUPPLIES (EXECUTIVE LAYOUT) ===
    # ==========================================================================
    if books_sales_file and len(gstr1_files) > 0:
        st.header("📊 Module 1: Outward Supplies")
        try:
            df_books_final = create_fy_template()
            df_gstr1_final = create_fy_template()

            # --- PROCESS BOOKS ---
            df_sales = standardize_columns(pd.read_excel(books_sales_file))
            df_sales = ensure_month_column(df_sales)
            book_sales_grouped = df_sales.groupby('Month')[['B2B', 'B2C', 'Amendment', 'Export', 'Debit Note', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            
            df_books_final = brute_force_assign(df_books_final, book_sales_grouped)

            # Process Books Credit Notes
            if books_cn_file:
                df_cn = standardize_columns(pd.read_excel(books_cn_file))
                df_cn = ensure_month_column(df_cn)
                book_cn_grouped = df_cn.groupby('Month')[['Credit Note', 'IGST', 'CGST', 'SGST']].sum().reset_index()
                
                # Add CN value, but Subtract Taxes
                df_books_final = brute_force_assign(df_books_final, book_cn_grouped[['Month', 'Credit Note']])
                df_books_final = brute_force_assign(df_books_final, book_cn_grouped[['Month', 'IGST', 'CGST', 'SGST']], subtract=True)

            # Calculate Books Net Supply
            df_books_final['Outward Supply (Net)'] = (
                df_books_final['B2B'] + df_books_final['B2C'] + 
                df_books_final['Amendment'] + df_books_final['Export'] + 
                df_books_final['Debit Note'] + df_books_final['Advances Adjusted'] - df_books_final['Credit Note']
            )

            # --- PROCESS GSTR-1 ---
            gstr1_records = [parse_gstr1_detailed(f) for f in gstr1_files]
            df_gstr1_raw = pd.DataFrame(gstr1_records)
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

            st.success("✅ Reconciliation Data Extracted Successfully!")
            
            st.markdown("### 📘 GST AS PER BOOKS")
            st.dataframe(format_df(df_books_final), use_container_width=True, hide_index=True)

            st.markdown("### 🌐 GST AS PER GSTR 1")
            st.dataframe(format_df(df_gstr1_final), use_container_width=True, hide_index=True)

            st.markdown("### ⚖️ DIFFERENCE")
            st.dataframe(format_df(df_diff_final), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Error processing Module 1: {e}")
    else:
        st.warning("Upload 'Sales Register' and at least one 'GSTR-1' PDF to view the summary.")
