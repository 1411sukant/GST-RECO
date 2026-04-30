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

MODULE_1_COLS = ['Month', 'B2B', 'B2C', 'Debit Note', 'Credit Note', 'Export', 
                 'Advances Adjusted', 'Outward Supply (Net)', 'IGST', 'CGST', 'SGST']

def create_fy_template():
    """Creates a blank FY dataframe in the exact format of the requested image."""
    df = pd.DataFrame(columns=MODULE_1_COLS)
    df['Month'] = MONTHS_FY
    for col in MODULE_1_COLS[1:]:
        df[col] = 0.0
    return df

# ── 1. CORE HELPERS & KEYWORD MAPPER ──────────────────────────────────────────
def standardize_columns(df):
    """Maps messy Excel headers to our strict internal columns."""
    df.columns = df.columns.astype(str).str.lower().str.strip()
    
    mapping = {
        'b2b': 'B2B', 'b2c': 'B2C',
        'sale': 'B2B', 'job work': 'B2B', 'sales': 'B2B', # Default generic sales to B2B
        'debit note': 'Debit Note', 'credit note': 'Credit Note', 'cn': 'Credit Note', 'dn': 'Debit Note',
        'export': 'Export', 'sez': 'Export',
        'advance': 'Advances Adjusted', 'adj': 'Advances Adjusted',
        'igst': 'IGST', 'integrated tax': 'IGST', 'gst-integrated': 'IGST', 'gst integrated': 'IGST',
        'cgst': 'CGST', 'central tax': 'CGST', 'gst- central': 'CGST', 'gst central': 'CGST',
        'sgst': 'SGST', 'state tax': 'SGST', 'gst- state': 'SGST', 'gst state': 'SGST',
        'month': 'Month', 'period': 'Month', 'mth': 'Month',
        'date': 'Date', 'invoice date': 'Date', 'doc date': 'Date', 'transaction date': 'Date',
        'type': 'Type', 'transaction type': 'Type', 'dr/cr': 'Type' 
    }
    
    new_cols = {}
    for col in df.columns:
        for key, standard_name in mapping.items():
            if key in col:
                new_cols[col] = standard_name
                break
                
    df = df.rename(columns=new_cols)
    
    # --- SAFEGUARD: Deduplicate duplicate columns & force numeric math ---
    target_numeric = ['B2B', 'B2C', 'Debit Note', 'Credit Note', 'Export', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']
    for col in target_numeric:
        if col not in df.columns:
            df[col] = 0.0
        elif isinstance(df[col], pd.DataFrame):
            # If two columns got named the same thing, drop them and replace with a summed single column
            summed = df[col].apply(pd.to_numeric, errors='coerce').fillna(0).sum(axis=1)
            df = df.drop(columns=[col])
            df[col] = summed
        else:
            # Force text to zero
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                
    return df

def ensure_month_column(df):
    if 'Month' not in df.columns:
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df['Month'] = df['Date'].dt.month_name()
        else:
            df['Month'] = "Unknown"
    df['Month'] = df['Month'].astype(str).str.strip().str.capitalize()
    return df

# --- ADVANCED GSTR-1 PARSER ---
def find_amounts(text: str, n: int = 1) -> list:
    vals = re.findall(r"-?[\d,]+\.\d{2}", text)
    result = []
    for v in vals:
        result.append(float(v.replace(",", "")))
        if len(result) == n: break
    return result

def section_total(text, header_re, stop_re=None, target_word="total", window=1500) -> float:
    m = re.search(header_re, text, re.IGNORECASE | re.DOTALL)
    if not m: return 0.0
    start = m.start()
    end = start + window
    if stop_re:
        s = re.search(stop_re, text[start + 10:], re.IGNORECASE)
        if s: end = start + 10 + s.start()
    chunk = text[start:end]
    tm = re.search(target_word, chunk, re.IGNORECASE)
    if not tm: return 0.0
    vals = find_amounts(chunk[tm.start():], 1)
    return vals[0] if vals else 0.0

def parse_gstr1_detailed(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        
    text = re.sub(r'(\d[\d,]*\.\d+)\n(\d+)', r'\1\2', text) 
        
    month = "Unknown"
    m_match = re.search(r"(?:Tax\s+[Pp]eriod|Period)\s+([A-Za-z]+)", text)
    if m_match: month = m_match.group(1).capitalize()
    
    b2b = section_total(text, r"4A\s*[-–]?\s*Taxable\s+outward\s+supplies\s+made\s+to\s+registered", r"4B\s*[-–]?\s*Taxable")
    b2cs = section_total(text, r"7\s*[-–]?\s*Taxable\s+supplies.*?unregistered", r"8\s*[-–]?\s*Nil")
    exp_6a = section_total(text, r"6A\s*[–-]?\s*Exports?\s*\(", r"6B\s*[-–]?\s*Supplies")
    sez_6b = section_total(text, r"6B\s*[-–]?\s*Supplies.*?SEZ",  r"6C\s*[-–]?\s*Deemed")
    
    cdn_reg = section_total(text, r"9B\s*[-–]?\s*Credit/Debit\s+Notes?\s*\(Registered\)", r"9B\s*[-–]?\s*Credit/Debit\s+Notes?\s*\(Unregistered\)", target_word=r"Total\s*[-–]?\s*Net\s+off")
    advances = section_total(text, r"11A\(1\).*?Advances", r"11B\(1\).*?Advance", target_word=r"Total")

    igst = cgst = sgst = 0.0
    m = re.search(r"Total\s+Liability\s*\(Outward[^)]+\)\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if m:
        igst, cgst, sgst = float(m.group(1).replace(",","")), float(m.group(2).replace(",","")), float(m.group(3).replace(",",""))
        
    return {
        "Month": month, "B2B": b2b, "B2C": b2cs, "Debit Note": 0.0, "Credit Note": cdn_reg, 
        "Export": exp_6a + sez_6b, "Advances Adjusted": advances,
        "IGST": igst, "CGST": cgst, "SGST": sgst
    }

# ── 2. MASTER UPLOAD DASHBOARD ────────────────────────────────────────────────
st.header("📂 Master File Upload")
st.info("Upload files to generate the Executive Reconciliation layout.")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📚 Books: Outward")
    books_sales_file = st.file_uploader("Sales Register (Excel)", type=["xlsx", "xls"], help="Ideally contains B2B, B2C, Export columns")
    books_cn_file = st.file_uploader("Credit Notes (Excel)", type=["xlsx", "xls"], help="Reduces Outward Liability")

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
            # --- 1. PREPARE TEMPLATES ---
            df_books_final = create_fy_template().set_index('Month')
            df_gstr1_final = create_fy_template().set_index('Month')

            # --- 2. PROCESS BOOKS ---
            df_sales = standardize_columns(pd.read_excel(books_sales_file))
            df_sales = ensure_month_column(df_sales)
            
            book_sales_grouped = df_sales.groupby('Month')[['B2B', 'B2C', 'Export', 'Debit Note', 'Advances Adjusted', 'IGST', 'CGST', 'SGST']].sum()
            df_books_final.update(book_sales_grouped)

            # Process Books Credit Notes
            if books_cn_file:
                df_cn = standardize_columns(pd.read_excel(books_cn_file))
                df_cn = ensure_month_column(df_cn)
                book_cn_grouped = df_cn.groupby('Month')[['Credit Note', 'IGST', 'CGST', 'SGST']].sum()
                
                # SAFELY Add CN value to Credit Note column, Subtract Tax from liability using fill_value
                if 'Credit Note' in book_cn_grouped.columns:
                    df_books_final['Credit Note'] = df_books_final['Credit Note'].add(book_cn_grouped['Credit Note'], fill_value=0)
                if 'IGST' in book_cn_grouped.columns:
                    df_books_final['IGST'] = df_books_final['IGST'].sub(book_cn_grouped['IGST'], fill_value=0)
                if 'CGST' in book_cn_grouped.columns:
                    df_books_final['CGST'] = df_books_final['CGST'].sub(book_cn_grouped['CGST'], fill_value=0)
                if 'SGST' in book_cn_grouped.columns:
                    df_books_final['SGST'] = df_books_final['SGST'].sub(book_cn_grouped['SGST'], fill_value=0)

            # Calculate Books Net Supply Safely
            df_books_final['Outward Supply (Net)'] = (
                df_books_final['B2B'].fillna(0) + df_books_final['B2C'].fillna(0) + 
                df_books_final['Export'].fillna(0) + df_books_final['Debit Note'].fillna(0) + 
                df_books_final['Advances Adjusted'].fillna(0) - df_books_final['Credit Note'].fillna(0)
            )

            # --- 3. PROCESS GSTR-1 ---
            gstr1_records = [parse_gstr1_detailed(f) for f in gstr1_files]
            df_gstr1_raw = pd.DataFrame(gstr1_records)
            if not df_gstr1_raw.empty:
                gstr1_grouped = df_gstr1_raw.groupby('Month').sum()
                df_gstr1_final.update(gstr1_grouped)
                
            df_gstr1_final['Outward Supply (Net)'] = (
                df_gstr1_final['B2B'].fillna(0) + df_gstr1_final['B2C'].fillna(0) + 
                df_gstr1_final['Export'].fillna(0) + df_gstr1_final['Debit Note'].fillna(0) + 
                df_gstr1_final['Advances Adjusted'].fillna(0) - df_gstr1_final['Credit Note'].fillna(0)
            )

            # --- 4. CALCULATE DIFFERENCE ---
            df_diff_final = df_books_final - df_gstr1_final

            # --- 5. FORMATTING & DISPLAY ---
            def format_df(df):
                df = df.reset_index()
                # Add Total Row safely
                total_row = df.sum(numeric_only=True)
                total_row['Month'] = 'Total'
                df.loc[len(df)] = total_row
                
                style = {col: "{:,.2f}" for col in df.columns if col != 'Month'}
                
                # Failsafe style mapper to blank out pure zeros
                def hide_zeros(val):
                    if isinstance(val, (int, float)) and val == 0:
                        return "color: transparent"
                    return ""
                
                return df.style.format(style).map(hide_zeros)

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
