import streamlit as st
import pandas as pd
import pdfplumber
import re
import io

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="GST Reconciliation Engine", page_icon="⚖️", layout="wide")
st.title("⚖️ Automated GST Reconciliation Engine")
st.caption("Modules: Outward Supplies, ITC Availment, GSTR-2B Matching, Invoice Forensic Report")

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
MONTH_ORDER = {
    "April": 1, "May": 2, "June": 3, "July": 4, "August": 5, "September": 6,
    "October": 7, "November": 8, "December": 9, "January": 10, "February": 11, "March": 12
}

# ── 1. CORE HELPERS & KEYWORD MAPPER ──────────────────────────────────────────
def standardize_columns(df):
    """Looks for messy Excel column headers and renames them to strict standards."""
    df.columns = df.columns.astype(str).str.lower().str.strip()
    
    mapping = {
        'sale': 'Sales', 'job work': 'Sales', 'sales': 'Sales',
        'export': 'Export', 'sez': 'SEZ',
        'igst': 'IGST', 'integrated tax': 'IGST', 'gst-integrated': 'IGST', 'gst integrated': 'IGST',
        'cgst': 'CGST', 'central tax': 'CGST', 'gst- central': 'CGST', 'gst central': 'CGST',
        'sgst': 'SGST', 'state tax': 'SGST', 'gst- state': 'SGST', 'gst state': 'SGST',
        'month': 'Month', 'period': 'Month', 'mth': 'Month',
        'date': 'Date', 'invoice date': 'Date', 'doc date': 'Date', 'transaction date': 'Date',
        'type': 'Type', 'transaction type': 'Type', 'dr/cr': 'Type' # For Credit Ledger
    }
    
    new_cols = {}
    for col in df.columns:
        for key, standard_name in mapping.items():
            if key in col:
                new_cols[col] = standard_name
                break
                
    return df.rename(columns=new_cols)

def ensure_month_column(df):
    """Automatically generates a 'Month' column if a 'Date' column exists."""
    if 'Month' not in df.columns:
        if 'Date' in df.columns:
            # Convert Date to datetime, then extract full Month name
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df['Month'] = df['Date'].dt.month_name()
        else:
            df['Month'] = "Unknown"
    
    # Clean up month strings
    df['Month'] = df['Month'].astype(str).str.strip().str.capitalize()
    return df

def parse_gstr1_summary(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        
    month = "Unknown"
    m_match = re.search(r"(?:Tax\s+[Pp]eriod|Period)\s+([A-Za-z]+)", text)
    if m_match: month = m_match.group(1).capitalize()
    
    igst = cgst = sgst = 0.0
    m = re.search(r"Total\s+Liability\s*\(Outward[^)]+\)\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if m:
        igst, cgst, sgst = float(m.group(1).replace(",","")), float(m.group(2).replace(",","")), float(m.group(3).replace(",",""))
        
    total_sales = 0.0
    sales_matches = re.findall(r"(?:4A|7|6A)\s*[-–].*?([\d,]+\.\d{2})\s*$", text, re.MULTILINE)
    for s in sales_matches:
        total_sales += float(s.replace(",",""))
        
    return {"Month": month, "Sales": total_sales, "IGST": igst, "CGST": cgst, "SGST": sgst}

# ── 2. MASTER UPLOAD DASHBOARD ────────────────────────────────────────────────
st.header("📂 Master File Upload")
st.info("Upload all relevant files below. The engine will automatically route them to the correct reconciliation modules.")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📚 Books: Outward")
    books_sales_file = st.file_uploader("Sales Register (Excel)", type=["xlsx", "xls"], help="Must contain Date/Month, Sales, IGST, CGST, SGST")
    books_cn_file = st.file_uploader("Credit Notes (Excel)", type=["xlsx", "xls"], help="Reduces Outward Liability")

with col2:
    st.subheader("📚 Books: Inward")
    books_purchase_file = st.file_uploader("Purchase/Journal Register (Excel)", type=["xlsx", "xls"], help="Must contain Date/Month, IGST, CGST, SGST")
    books_dn_file = st.file_uploader("Debit Notes (Excel)", type=["xlsx", "xls"], help="Reduces Input Tax Credit")

with col3:
    st.subheader("🌐 GST Portal Files")
    gstr1_files = st.file_uploader("GSTR-1 (PDF)", type=["pdf"], accept_multiple_files=True, help="Upload one or multiple months")
    credit_ledger_file = st.file_uploader("Electronic Credit Ledger (Excel)", type=["xlsx", "xls"], help="Portal ledger to track ITC Availed (Cr) and Utilized (Dr)")
    gstr2b_file = st.file_uploader("GSTR-2B (Excel)", type=["xlsx", "xls"], disabled=True, help="Coming in Phase 3")

st.divider()

# ── 3. RECONCILIATION ENGINE TRIGGER ──────────────────────────────────────────
if st.button("⚡ Run Reconciliation Engine", type="primary"):
    
    # ==========================================================================
    # === MODULE 1: OUTWARD SUPPLIES ===
    # ==========================================================================
    if books_sales_file and len(gstr1_files) > 0:
        st.header("📊 Module 1: Outward Supplies (Books vs GSTR-1)")
        try:
            # 1. Process Books Sales
            df_sales = pd.read_excel(books_sales_file)
            df_sales = standardize_columns(df_sales)
            df_sales = ensure_month_column(df_sales)
            
            if df_sales['Month'].iloc[0] == "Unknown" and 'Month' not in df_sales.columns:
                st.error("❌ Could not find 'Date' or 'Month' in Sales Register.")
                st.stop()
            
            for col in ['Sales', 'Export', 'SEZ', 'IGST', 'CGST', 'SGST']:
                if col not in df_sales.columns: df_sales[col] = 0.0
                
            book_sales_grouped = df_sales.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            
            # 2. Process Credit Notes
            if books_cn_file:
                df_cn = pd.read_excel(books_cn_file)
                df_cn = standardize_columns(df_cn)
                df_cn = ensure_month_column(df_cn)
                for col in ['Sales', 'IGST', 'CGST', 'SGST']:
                    if col not in df_cn.columns: df_cn[col] = 0.0
                book_cn_grouped = df_cn.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
                book_sales_grouped = book_sales_grouped.set_index('Month').subtract(book_cn_grouped.set_index('Month'), fill_value=0).reset_index()

            # 3. Process GSTR-1 PDFs
            gstr1_records = []
            for file in gstr1_files: gstr1_records.append(parse_gstr1_summary(file))
            df_gstr1 = pd.DataFrame(gstr1_records)
            if not df_gstr1.empty: df_gstr1 = df_gstr1.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            else: df_gstr1 = pd.DataFrame(columns=['Month', 'Sales', 'IGST', 'CGST', 'SGST'])

            # 4. Display UI
            all_unique_months = list(set(book_sales_grouped['Month'].tolist() + df_gstr1['Month'].tolist()))
            all_unique_months = [m for m in all_unique_months if m != "Unknown"]
            all_unique_months.sort(key=lambda m: MONTH_ORDER.get(m, 99))
            
            for month in all_unique_months:
                with st.expander(f"📅 Outward Summary - {month}", expanded=True):
                    b_match = book_sales_grouped[book_sales_grouped['Month'] == month]
                    b_data = b_match.iloc[0] if not b_match.empty else pd.Series({'Sales':0.0, 'IGST':0.0, 'CGST':0.0, 'SGST':0.0})
                    p_match = df_gstr1[df_gstr1['Month'] == month]
                    p_data = p_match.iloc[0] if not p_match.empty else pd.Series({'Sales':0.0, 'IGST':0.0, 'CGST':0.0, 'SGST':0.0})
                    
                    # Explicitly forcing float() to prevent the "unsupported format string" crash
                    comparison_df = pd.DataFrame({
                        "Metric": ["Total Value", "IGST", "CGST", "SGST"],
                        "Data as per Books (Net of CN)": [float(b_data['Sales']), float(b_data['IGST']), float(b_data['CGST']), float(b_data['SGST'])],
                        "Data as per GSTR-1": [float(p_data['Sales']), float(p_data['IGST']), float(p_data['CGST']), float(p_data['SGST'])],
                    })
                    
                    comparison_df["Difference (Books - Portal)"] = comparison_df["Data as per Books (Net of CN)"] - comparison_df["Data as per GSTR-1"]
                    st.dataframe(comparison_df.style.format({c: "₹{:,.2f}" for c in comparison_df.columns[1:]}), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Error processing Module 1: {e}")
            

    # ==========================================================================
    # === MODULE 2: ITC AVAILMENT (Books vs Electronic Credit Ledger) ===
    # ==========================================================================
    if books_purchase_file and credit_ledger_file:
        st.header("📊 Module 2: ITC Availment (Books vs Credit Ledger)")
        try:
            # 1. Process Book Input
            df_purch = pd.read_excel(books_purchase_file)
            df_purch = standardize_columns(df_purch)
            df_purch = ensure_month_column(df_purch)
            
            for col in ['IGST', 'CGST', 'SGST']:
                if col not in df_purch.columns: df_purch[col] = 0.0
                
            book_inward_grouped = df_purch.groupby('Month')[['IGST', 'CGST', 'SGST']].sum().reset_index()
            
            # 2. Subtract Debit Notes
            if books_dn_file:
                df_dn = pd.read_excel(books_dn_file)
                df_dn = standardize_columns(df_dn)
                df_dn = ensure_month_column(df_dn)
                for col in ['IGST', 'CGST', 'SGST']:
                    if col not in df_dn.columns: df_dn[col] = 0.0
                book_dn_grouped = df_dn.groupby('Month')[['IGST', 'CGST', 'SGST']].sum().reset_index()
                book_inward_grouped = book_inward_grouped.set_index('Month').subtract(book_dn_grouped.set_index('Month'), fill_value=0).reset_index()

            # 3. Process Ledger
            df_ledger = pd.read_excel(credit_ledger_file)
            df_ledger = standardize_columns(df_ledger)
            df_ledger = ensure_month_column(df_ledger)
            
            for col in ['IGST', 'CGST', 'SGST']:
                if col not in df_ledger.columns: df_ledger[col] = 0.0
            
            if 'Type' not in df_ledger.columns: df_ledger['Type'] = 'Unknown'
            
            ledger_credit = df_ledger[df_ledger['Type'].astype(str).str.lower().str.contains('cr|credit') == True]
            ledger_debit = df_ledger[df_ledger['Type'].astype(str).str.lower().str.contains('dr|debit') == True]
            
            credit_grouped = ledger_credit.groupby('Month')[['IGST', 'CGST', 'SGST']].sum().reset_index()
            debit_grouped = ledger_debit.groupby('Month')[['IGST', 'CGST', 'SGST']].sum().reset_index()

            # 4. Display UI
            all_in_months = list(set(book_inward_grouped['Month'].tolist() + credit_grouped['Month'].tolist()))
            all_in_months = [m for m in all_in_months if m != "Unknown"]
            all_in_months.sort(key=lambda m: MONTH_ORDER.get(m, 99))
            
            for month in all_in_months:
                with st.expander(f"📅 ITC Summary - {month}", expanded=True):
                    b_match = book_inward_grouped[book_inward_grouped['Month'] == month]
                    b_data = b_match.iloc[0] if not b_match.empty else pd.Series({'IGST':0.0, 'CGST':0.0, 'SGST':0.0})
                    
                    c_match = credit_grouped[credit_grouped['Month'] == month]
                    c_data = c_match.iloc[0] if not c_match.empty else pd.Series({'IGST':0.0, 'CGST':0.0, 'SGST':0.0})
                    
                    d_match = debit_grouped[debit_grouped['Month'] == month]
                    d_data = d_match.iloc[0] if not d_match.empty else pd.Series({'IGST':0.0, 'CGST':0.0, 'SGST':0.0})
                    
                    # Forcing float() to prevent string format crashes
                    itc_comparison_df = pd.DataFrame({
                        "Tax Head": ["IGST", "CGST", "SGST"],
                        "ITC as per Books (Net of DN)": [float(b_data['IGST']), float(b_data['CGST']), float(b_data['SGST'])],
                        "ITC Availed in Portal (Ledger Cr.)": [float(c_data['IGST']), float(c_data['CGST']), float(c_data['SGST'])],
                    })
                    itc_comparison_df["Difference"] = itc_comparison_df["ITC as per Books (Net of DN)"] - itc_comparison_df["ITC Availed in Portal (Ledger Cr.)"]
                    
                    st.dataframe(itc_comparison_df.style.format({c: "₹{:,.2f}" for c in itc_comparison_df.columns[1:]}), use_container_width=True, hide_index=True)
                    
                    st.caption("🔻 *Informational: ITC Utilized during this month (from Ledger Dr.)*")
                    util_df = pd.DataFrame({
                        "Tax Head": ["IGST", "CGST", "SGST"],
                        "ITC Utilized": [float(d_data['IGST']), float(d_data['CGST']), float(d_data['SGST'])]
                    })
                    st.dataframe(util_df.style.format({"ITC Utilized": "₹{:,.2f}"}), hide_index=True)

        except Exception as e:
            st.error(f"Error processing Module 2: {e}")

    if not books_sales_file and not books_purchase_file:
        st.info("Upload files and click 'Run Reconciliation' to start.")
