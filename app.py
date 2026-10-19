import streamlit as st
import pandas as pd
import sqlite3
import hmac
from io import BytesIO
from pathlib import Path

# ---------------------------------------------------------
# PAGE SETTINGS
# ---------------------------------------------------------
st.set_page_config(
    page_title="Barcode Checking Portal",
    page_icon="🔎",
    layout="wide"
)

DB_PATH = Path("barcode_history.db")

# ---------------------------------------------------------
# LOGIN SECURITY
# Set PORTAL_USERNAME and PORTAL_PASSWORD in Streamlit Cloud
# Settings -> Secrets. Never put your real password in this file.
# ---------------------------------------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("Barcode Checking Portal")
    st.subheader("Secure Login")

    if "PORTAL_USERNAME" not in st.secrets or "PORTAL_PASSWORD" not in st.secrets:
        st.error("Login setup is incomplete. Please add PORTAL_USERNAME and PORTAL_PASSWORD in Streamlit Cloud → App settings → Secrets.")
        st.code(
            'PORTAL_USERNAME = "your_username"\n'
            'PORTAL_PASSWORD = "your_strong_password"',
            language="toml"
        )
        st.stop()

    with st.form("portal_login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        correct_username = str(st.secrets["PORTAL_USERNAME"])
        correct_password = str(st.secrets["PORTAL_PASSWORD"])

        username_ok = hmac.compare_digest(username, correct_username)
        password_ok = hmac.compare_digest(password, correct_password)

        if username_ok and password_ok:
            st.session_state.logged_in = True
            st.rerun()
        else:
            st.error("Username ya password ghalat hai.")

    st.stop()

# Keep login active during normal page interactions; logout is explicit.
with st.sidebar:
    st.success("Login successful")
    if st.button("Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.rerun()

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL,
            creation_date TEXT NOT NULL,
            resolve_date TEXT NOT NULL,
            UNIQUE(barcode, creation_date, resolve_date)
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(barcode_history)").fetchall()
    }
    if "order_number" not in existing_columns:
        conn.execute(
            "ALTER TABLE barcode_history ADD COLUMN order_number TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()
    return conn


def load_history():
    conn = get_connection()
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(barcode_history)").fetchall()
        }
        if "order_number" in columns:
            history = pd.read_sql_query(
                "SELECT order_number, barcode, creation_date, resolve_date FROM barcode_history",
                conn
            )
        else:
            history = pd.read_sql_query(
                "SELECT barcode, creation_date, resolve_date FROM barcode_history",
                conn
            )
            history["order_number"] = ""
    finally:
        conn.close()

    if history.empty:
        return []

    records = []
    for _, row in history.iterrows():
        creation = pd.to_datetime(row["creation_date"], errors="coerce")
        resolve = pd.to_datetime(row["resolve_date"], errors="coerce")
        if pd.notna(creation) and pd.notna(resolve):
            records.append({
                "Order Number": str(row.get("order_number", "") or ""),
                "Barcode": str(row["barcode"]),
                "Creation Date": creation,
                "Resolve Date": resolve
            })
    return records


def save_history(rows):
    """Save only new unique barcode/date combinations to avoid re-upload duplicates."""
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO barcode_history
                (order_number, barcode, creation_date, resolve_date)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    str(row.get("Order Number", "") or ""),
                    str(row["Barcode"]),
                    pd.Timestamp(row["Creation Date"]).strftime("%Y-%m-%d %H:%M:%S"),
                    pd.Timestamp(row["Resolve Date"]).strftime("%Y-%m-%d %H:%M:%S")
                )
                for row in rows
            ]
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------
# BARCODE CHECKING LOGIC
# Gap = current complaint Creation Date - previous complaint Resolve Date
# ---------------------------------------------------------
def check_barcodes(input_df, saved_history):
    working = input_df.copy()
    working["__original_order"] = range(len(working))
    working = working.sort_values(
        ["Creation Date", "__original_order"], kind="stable"
    )

    previous_records = list(saved_history)
    output_rows = []

    for _, row in working.iterrows():
        barcode = str(row["Barcode"]).strip()
        creation = pd.Timestamp(row["Creation Date"])
        resolve = pd.Timestamp(row["Resolve Date"])

        earlier = [
            rec for rec in previous_records
            if str(rec["Barcode"]).strip() == barcode
            and pd.Timestamp(rec["Creation Date"]) < creation
        ]

        previous_resolve = None
        previous_order_number = ""
        days_between = None

        if not earlier:
            result = "First Complaint"
        else:
            previous = max(earlier, key=lambda rec: pd.Timestamp(rec["Creation Date"]))
            previous_resolve = pd.Timestamp(previous["Resolve Date"])
            previous_order_number = str(previous.get("Order Number", "") or "")
            days_between = (creation.normalize() - previous_resolve.normalize()).days

            if creation.normalize() < previous_resolve.normalize():
                result = "Created Before Previous Resolve"
            elif days_between <= 30:
                result = "Within 30 Days"
            elif days_between <= 90:
                result = "Within 90 Days"
            else:
                result = "After 90 Days"

        out = row.drop(labels=["__original_order"]).to_dict()
        out["Previous Order Number"] = previous_order_number
        out["Previous Resolve Date"] = previous_resolve
        out["Days Between Complaints"] = days_between
        out["Result"] = result
        output_rows.append(out)

        # Include this complaint when checking later rows in the same Excel upload.
        previous_records.append({
            "Order Number": str(row.get("Order Number", "") or ""),
            "Barcode": barcode,
            "Creation Date": creation,
            "Resolve Date": resolve
        })

    result_df = pd.DataFrame(output_rows)

    # Keep a friendly column order while preserving any extra input columns.
    preferred = [
        "Order Number", "Barcode", "Creation Date", "Resolve Date",
        "Previous Order Number", "Previous Resolve Date",
        "Days Between Complaints", "Result"
    ]
    ordered = [col for col in preferred if col in result_df.columns]
    ordered += [col for col in result_df.columns if col not in ordered]
    return result_df[ordered]


def to_excel(df):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Barcode Results")
    output.seek(0)
    return output.getvalue()


# ---------------------------------------------------------
# MAIN PORTAL
# ---------------------------------------------------------
st.title("Barcode Checking Portal")
st.write(
    "Excel file upload karein. Required columns: "
    "**Order Number, Barcode, Creation Date, Resolve Date**."
)

with st.expander("Rules / Checking Logic"):
    st.markdown(
        """
        - **First Complaint:** Is barcode ki pehle koi history nahi mili.
        - **Within 30 Days:** Previous complaint ki Resolve Date se current Creation Date tak 0–30 din.
        - **Within 90 Days:** Gap 31–90 din.
        - **After 90 Days:** Gap 90 din se zyada.
        - **Created Before Previous Resolve:** Nayi complaint, previous complaint resolve hone se pehle create hui.
        - **Order Number:** Output mein preserve hota hai; calculation par asar nahi karta.
        """
    )

uploaded_file = st.file_uploader(
    "Excel file upload karein",
    type=["xlsx", "xls"]
)

if uploaded_file is not None:
    try:
        # Read barcode/order identifiers as text to preserve leading zeros.
        input_df = pd.read_excel(
            uploaded_file,
            dtype={"Order Number": str, "Barcode": str}
        )
    except Exception as exc:
        st.error(f"Excel file read nahi ho saki: {exc}")
        st.stop()

    input_df.columns = [str(col).strip() for col in input_df.columns]
    required_columns = [
        "Order Number", "Barcode", "Creation Date", "Resolve Date"
    ]
    missing = [col for col in required_columns if col not in input_df.columns]

    if missing:
        st.error("Excel mein yeh required columns missing hain: " + ", ".join(missing))
        st.info("Column headings bilkul is tarah hon: Order Number, Barcode, Creation Date, Resolve Date.")
        st.stop()

    # Clean identifiers and dates.
    for col in ["Order Number", "Barcode"]:
        input_df[col] = input_df[col].fillna("").astype(str).str.strip()

    input_df["Creation Date"] = pd.to_datetime(
        input_df["Creation Date"], errors="coerce"
    )
    input_df["Resolve Date"] = pd.to_datetime(
        input_df["Resolve Date"], errors="coerce"
    )

    invalid_rows = (
        input_df["Barcode"].eq("")
        | input_df["Creation Date"].isna()
        | input_df["Resolve Date"].isna()
    )

    if invalid_rows.any():
        st.error(
            f"{int(invalid_rows.sum())} row(s) mein Barcode blank hai ya date ghalat/blank hai. "
            "Excel theek karke dobara upload karein."
        )
        st.dataframe(input_df.loc[invalid_rows], use_container_width=True)
        st.stop()

    if input_df.empty:
        st.warning("Excel file mein koi data nahi hai.")
        st.stop()

    if (input_df["Resolve Date"] < input_df["Creation Date"]).any():
        st.warning(
            "Kuch rows mein Resolve Date, Creation Date se pehle hai. "
            "Dates verify kar lein; checking phir bhi calculate hogi."
        )

    history = load_history()
    result_df = check_barcodes(input_df, history)

    st.subheader("Checking Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Rows", len(result_df))
    c2.metric("First Complaints", int((result_df["Result"] == "First Complaint").sum()))
    c3.metric("Within 90 Days", int(result_df["Result"].isin(
        ["Within 30 Days", "Within 90 Days"]
    ).sum()))

    st.dataframe(result_df, use_container_width=True, hide_index=True)

    excel_bytes = to_excel(result_df)
    st.download_button(
        label="Download Results Excel",
        data=excel_bytes,
        file_name="barcode_checking_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

    if st.button("Save This File to Barcode History", type="primary"):
        save_history(input_df.to_dict(orient="records"))
        st.success("Is file ke barcode records history mein save ho gaye hain.")

st.caption(
    "Note: Streamlit Cloud par local SQLite history restarts/redeployments ke baad "
    "hamesha persist nahi karti. Important history ke liye regular backup rakhein."
)
