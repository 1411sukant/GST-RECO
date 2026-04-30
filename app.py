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
# Used to sort the UI tabs chronologically from April to March
MONTH_ORDER = {
    "April": 1, "May": 2, "June": 3, "July": 4, "August": 5, "September": 6,
    "October": 7, "November": 8, "December": 9, "January": 10, "February": 11, "March": 12
}

# ── 1. CORE HELPERS & KEYWORD MAPPER ──────────────────────────────────────────
def standardize_columns(df):
    """
    Looks for messy Excel column headers and renames them to our strict internal standard.
    """
    # Ensure all column names are strings before applying string methods
    df.columns = df.columns.astype(str).str.lower().str.strip()
    
    mapping = {
        'sale': 'Sales', 'job work': 'Sales', 'sales': 'Sales',
        'export': 'Export', 'sez': 'SEZ',
        'igst': 'IGST', 'integrated tax': 'IGST', 'gst-integrated': 'IGST', 'gst integrated': 'IGST',
        'cgst': 'CGST', 'central tax': 'CGST', 'gst- central': 'CGST', 'gst central': 'CGST',
        'sgst': 'SGST', 'state tax': 'SGST', 'gst- state': 'SGST', 'gst state': 'SGST',
        'month': 'Month', 'period': 'Month', 'date': 'Month', 'mth': 'Month' # Added extended month keywords
    }
    
    new_cols = {}
    for col in df.columns:
        for key, standard_name in mapping.items():
            if key in col:
                new_cols[col] = standard_name
                break
                
    return df.rename(columns=new_cols)

# Extract basic data from GSTR-1 PDF
def parse_gstr1_summary(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        
    # Month
    month = "Unknown"
    m_match = re.search(r"(?:Tax\s+[Pp]eriod|Period)\s+([A-Za-z]+)", text)
    if m_match: month = m_match.group(1).capitalize()
    
    # Liability (IGST, CGST, SGST)
    igst = cgst = sgst = 0.0
    m = re.search(r"Total\s+Liability\s*\(Outward[^)]+\)\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if m:
        igst, cgst, sgst = float(m.group(1).replace(",","")), float(m.group(2).replace(",","")), float(m.group(3).replace(",",""))
        
    # Total Sales (Simplified for Module 1 summary: 4A + 7 + Exports)
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
    st.subheader("📚 Books: Outward (Sales)")
    books_sales_file = st.file_uploader("Sales Register (Excel)", type=["xlsx", "xls"], help="Must contain Month, Sales, IGST, CGST, SGST")
    books_cn_file = st.file_uploader("Credit Notes (Excel)", type=["xlsx", "xls"], help="Reduces Outward Liability")

with col2:
    st.subheader("📚 Books: Inward (Purchases)")
    books_purchase_file = st.file_uploader("Purchase/Journal Register (Excel)", type=["xlsx", "xls"], disabled=True, help="Coming in Phase 2")
    books_dn_file = st.file_uploader("Debit Notes (Excel)", type=["xlsx", "xls"], disabled=True, help="Coming in Phase 2")

with col3:
    st.subheader("🌐 GST Portal Files")
    gstr1_files = st.file_uploader("GSTR-1 (PDF)", type=["pdf"], accept_multiple_files=True, help="Upload one or multiple months")
    gstr3b_file = st.file_uploader("GSTR-3B / Credit Ledger (PDF/Excel)", type=["pdf", "xlsx", "xls"], disabled=True, help="Coming in Phase 2")
    gstr2b_file = st.file_uploader("GSTR-2B (Excel)", type=["xlsx", "xls"], disabled=True, help="Coming in Phase 3")

st.divider()

# ── 3. RECONCILIATION ENGINE TRIGGER ──────────────────────────────────────────
if st.button("⚡ Run Reconciliation Engine", type="primary"):
    
    # === MODULE 1: OUTWARD SUPPLIES ===
    if books_sales_file and len(gstr1_files) > 0:
        st.header("📊 Module 1: Outward Supplies (Books vs GSTR-1)")
        
        try:
            # 1. Process Books Sales
            df_sales = pd.read_excel(books_sales_file)
            df_sales = standardize_columns(df_sales)
            
            # FAILSAFE: Ensure Month column exists
            if 'Month' not in df_sales.columns:
                st.error("❌ Could not find a 'Month' column in your Sales Register Excel file. Please ensure one column header is named 'Month', 'Period', or 'Date'.")
                st.stop()
            
            for col in ['Sales', 'Export', 'SEZ', 'IGST', 'CGST', 'SGST']:
                if col not in df_sales.columns: df_sales[col] = 0.0
                
            df_sales['Month'] = df_sales['Month'].astype(str).str.strip().str.capitalize()
            book_sales_grouped = df_sales.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            
            # 2. Process Credit Notes (if provided)
            if books_cn_file:
                df_cn = pd.read_excel(books_cn_file)
                df_cn = standardize_columns(df_cn)
                
                # Failsafe for Credit Notes file
                if 'Month' not in df_cn.columns:
                     st.error("❌ Could not find a 'Month' column in your Credit Notes Excel file.")
                     st.stop()

                for col in ['Sales', 'IGST', 'CGST', 'SGST']:
                    if col not in df_cn.columns: df_cn[col] = 0.0
                
                df_cn['Month'] = df_cn['Month'].astype(str).str.strip().str.capitalize()
                book_cn_grouped = df_cn.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
                
                book_sales_grouped = book_sales_grouped.set_index('Month').subtract(book_cn_grouped.set_index('Month'), fill_value=0).reset_index()

            # 3. Process GSTR-1 PDFs
            gstr1_records = []
            for file in gstr1_files:
                gstr1_records.append(parse_gstr1_summary(file))
                
            df_gstr1 = pd.DataFrame(gstr1_records)
            
            if not df_gstr1.empty:
                df_gstr1 = df_gstr1.groupby('Month')[['Sales', 'IGST', 'CGST', 'SGST']].sum().reset_index()
            else:
                df_gstr1 = pd.DataFrame(columns=['Month', 'Sales', 'IGST', 'CGST', 'SGST'])

            # 4. Display Vertical Month-Wise UI
            book_months = book_sales_grouped['Month'].unique().tolist() if not book_sales_grouped.empty else []
            portal_months = df_gstr1['Month'].unique().tolist() if not df_gstr1.empty else []
            all_unique_months = list(set(book_months + portal_months))
            all_unique_months.sort(key=lambda m: MONTH_ORDER.get(m, 99))
            
            for month in all_unique_months:
                with st.expander(f"📅 Month: {month}", expanded=True):
                    
                    # Get Books Data
                    b_match = book_sales_grouped[book_sales_grouped['Month'] == month]
                    b_data = b_match.iloc[0] if not b_match.empty else pd.Series({'Sales':0, 'IGST':0, 'CGST':0, 'SGST':0})
                    
                    # Get Portal Data
                    p_match = df_gstr1[df_gstr1['Month'] == month]
                    p_data = p_match.iloc[0] if not p_match.empty else pd.Series({'Sales':0, 'IGST':0, 'CGST':0, 'SGST':0})
                    
                    # Create the comparison table
                    comparison_df = pd.DataFrame({
                        "Metric": ["Total Value", "IGST", "CGST", "SGST"],
                        "Data as per Books (Net of CN)": [b_data['Sales'], b_data['IGST'], b_data['CGST'], b_data['SGST']],
                        "Data as per GSTR-1": [p_data['Sales'], p_data['IGST'], p_data['CGST'], p_data['SGST']],
                    })
                    
                    # Calculate Difference
                    comparison_df["Difference (Books - Portal)"] = comparison_df["Data as per Books (Net of CN)"] - comparison_df["Data as per GSTR-1"]
                    
                    st.dataframe(comparison_df.style.format({
                        "Data as per Books (Net of CN)": "₹{:,.2f}",
                        "Data as per GSTR-1": "₹{:,.2f}",
                        "Difference (Books - Portal)": "₹{:,.2f}"
                    }), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Error processing Module 1: {e}")
    else:
        st.warning("Upload 'Sales Register' and at least one 'GSTR-1' PDF to run Module 1 (Outward Supplies).")
