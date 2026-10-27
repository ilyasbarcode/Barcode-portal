import streamlit as st
import pandas as pd
import sqlite3
import hmac
import re
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
            remarks TEXT NOT NULL DEFAULT '',
            UNIQUE(barcode, creation_date, resolve_date)
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(barcode_history)").fetchall()
    }
    if "order_number" not in existing_columns:
        conn.execute("ALTER TABLE barcode_history ADD COLUMN order_number TEXT NOT NULL DEFAULT ''")
    if "remarks" not in existing_columns:
        conn.execute("ALTER TABLE barcode_history ADD COLUMN remarks TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


# Recognized terms from the user's material list. Longer names are checked first
# so "Door Gasket 400SAX Chest" is not confused with "Door Gasket 400SAX".
MATERIAL_TERMS = [
    ("Compressor", ["COMPRESSOR REFURBISHED R134 1/3 HP", "COMPRESSOR REPLACED", "COMPRESSOR"]),
    ("Gas", ["GAS CHARGED", "GAS CHARGING", "GAS CHARGE", "GAS"]),
    ("CFS Basic Service", ["CFS BASIC SERVICE"]),
    ("Door Gasket 400SAX Chest", ["DOOR GASKET 400SAX CHEST"]),
    ("Door Gasket 400SAX", ["DOOR GASKET 400SAX"]),
    ("Compressor Condensor 1/3", ["CONDENSOR 1/3", "CONDENSER 1/3"]),
    ("Capacitor", ["CAPACITOR"]),
    ("Caster Wheel", ["CASTER WHEEL"]),
    ("Fan Motor 10W", ["FAN MOTOR 10W"]),
    ("Fan Motor 5W", ["FAN MOTOR 5W"]),
    ("LED", ["LED REPLACED", "LED"]),
    ("Power Cord Plug-VISI & Chest", ["POWER CORD PLUG-VISI & CHEST", "POWER CORD PLUG"]),
    ("Relay", ["RELAY"]),
    ("Overload", ["OVERLOAD"]),
    ("Thermostat - Chest", ["THERMOSTAT - CHEST", "THERMOSTAT CHEST"]),
    ("Door Complete Chest", ["DOOR COMPLETE CHEST"]),
    ("Door Repaired", ["DOOR REPAIRED", "DOORREPAIRED"]),
]


def detect_materials(remarks):
    """Return canonical materials found in a single Remarks cell."""
    text = str(remarks or "").upper().replace("–", "-").replace("—", "-")
    found = []
    for canonical, terms in MATERIAL_TERMS:
        # Match complete terms/phrases so "GAS" does not accidentally match "GASKET".
        matched = any(
            re.search(r"(?<![A-Z0-9])" + re.escape(term) + r"(?![A-Z0-9])", text)
            for term in terms
        )
        if matched and canonical not in found:
            found.append(canonical)
    return found


def is_part(material):
    return material not in {"Compressor", "Gas", "CFS Basic Service"}


def gap_result(current_creation, previous_resolve):
    current_day = pd.Timestamp(current_creation).normalize()
    previous_day = pd.Timestamp(previous_resolve).normalize()
    if current_day < previous_day:
        return "Created Before Previous Resolve", (current_day - previous_day).days
    days = (current_day - previous_day).days
    if days <= 30:
        return "Within 30 Days", days
    if days <= 90:
        return "Within 90 Days", days
    return "After 90 Days", days


def load_history():
    conn = get_connection()
    try:
        history = pd.read_sql_query(
            "SELECT order_number, barcode, creation_date, resolve_date, remarks FROM barcode_history",
            conn
        )
    finally:
        conn.close()

    records = []
    for _, row in history.iterrows():
        creation = pd.to_datetime(row["creation_date"], errors="coerce")
        resolve = pd.to_datetime(row["resolve_date"], errors="coerce")
        if pd.notna(creation) and pd.notna(resolve):
            records.append({
                "Order Number": str(row.get("order_number", "") or ""),
                "Barcode": str(row["barcode"]),
                "Creation Date": creation,
                "Resolve Date": resolve,
                "Remarks": str(row.get("remarks", "") or ""),
                "Materials": detect_materials(row.get("remarks", "")),
            })
    return records


def save_history(rows):
    """Save barcode/date records and Remarks for future material warranty checks."""
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO barcode_history
                (order_number, barcode, creation_date, resolve_date, remarks)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(barcode, creation_date, resolve_date) DO UPDATE SET
                order_number = CASE WHEN excluded.order_number != '' THEN excluded.order_number ELSE barcode_history.order_number END,
                remarks = CASE WHEN excluded.remarks != '' THEN excluded.remarks ELSE barcode_history.remarks END
            """,
            [
                (
                    str(row.get("Order Number", "") or ""),
                    str(row["Barcode"]),
                    pd.Timestamp(row["Creation Date"]).strftime("%Y-%m-%d %H:%M:%S"),
                    pd.Timestamp(row["Resolve Date"]).strftime("%Y-%m-%d %H:%M:%S"),
                    str(row.get("Remarks", "") or ""),
                )
                for row in rows
            ]
        )
        conn.commit()
    finally:
        conn.close()


def check_barcodes(input_df, saved_history):
    """Check each warranty category independently and show separate result columns."""
    working = input_df.copy()
    working["__original_order"] = range(len(working))
    working = working.sort_values(["Creation Date", "__original_order"], kind="stable")

    previous_records = list(saved_history)
    output_rows = []

    for _, row in working.iterrows():
        barcode = str(row["Barcode"]).strip()
        creation = pd.Timestamp(row["Creation Date"])
        resolve = pd.Timestamp(row["Resolve Date"])
        remarks = str(row.get("Remarks", "") or "")
        materials = detect_materials(remarks)

        current_parts = [m for m in materials if is_part(m)]
        current_has_compressor = "Compressor" in materials
        current_has_gas = "Gas" in materials
        current_is_service = not materials or materials == ["CFS Basic Service"]

        earlier = [
            rec for rec in previous_records
            if str(rec.get("Barcode", "")).strip() == barcode
            and pd.Timestamp(rec["Creation Date"]) < creation
        ]
        earlier.sort(key=lambda rec: pd.Timestamp(rec["Creation Date"]), reverse=True)

        compressor_result = ""
        gas_result = ""
        parts_result = ""
        service_result = ""

        # Compressor warranty: same barcode + compressor seen before; 90-day period.
        if current_has_compressor:
            previous_compressors = [
                rec for rec in earlier
                if "Compressor" in rec.get("Materials", detect_materials(rec.get("Remarks", "")))
            ]
            if previous_compressors:
                prev = max(previous_compressors, key=lambda rec: pd.Timestamp(rec["Creation Date"]))
                label, days = gap_result(creation, prev["Resolve Date"])
                if label == "Created Before Previous Resolve":
                    compressor_result = label
                elif days <= 90:
                    compressor_result = "Within 90 Days"
                else:
                    compressor_result = "After 90 Days"
            else:
                compressor_result = "No Previous Compressor"

        # Gas warranty: same barcode + gas seen before; 90-day period.
        if current_has_gas:
            previous_gas = [
                rec for rec in earlier
                if "Gas" in rec.get("Materials", detect_materials(rec.get("Remarks", "")))
            ]
            if previous_gas:
                prev = max(previous_gas, key=lambda rec: pd.Timestamp(rec["Creation Date"]))
                label, days = gap_result(creation, prev["Resolve Date"])
                if label == "Created Before Previous Resolve":
                    gas_result = label
                elif days <= 90:
                    gas_result = "Within 90 Days"
                else:
                    gas_result = "After 90 Days"
            else:
                gas_result = "No Previous Gas"

        # Parts warranty: any same part repeats for the same barcode; 30-day period.
        if current_parts:
            part_matches = []
            for material in current_parts:
                previous_parts = [
                    rec for rec in earlier
                    if material in rec.get("Materials", detect_materials(rec.get("Remarks", "")))
                ]
                if previous_parts:
                    prev = max(previous_parts, key=lambda rec: pd.Timestamp(rec["Creation Date"]))
                    label, days = gap_result(creation, prev["Resolve Date"])
                    part_matches.append((label, days))
            if part_matches:
                # A repeated part is under warranty if any exact part match is within 30 days.
                if any(label == "Created Before Previous Resolve" for label, _ in part_matches):
                    parts_result = "Created Before Previous Resolve"
                elif any(days <= 30 for _, days in part_matches):
                    parts_result = "Within 30 Days"
                else:
                    parts_result = "No Warranty Part"
            else:
                parts_result = "No Warranty Part"

        # Service warranty is checked only when no actual part, compressor, or gas
        # is recorded in the current complaint (CFS Basic Service counts as service).
        if current_is_service:
            previous_service = [
                rec for rec in earlier
                if not rec.get("Materials", detect_materials(rec.get("Remarks", "")))
                or rec.get("Materials", detect_materials(rec.get("Remarks", ""))) == ["CFS Basic Service"]
            ]
            if previous_service:
                prev = max(previous_service, key=lambda rec: pd.Timestamp(rec["Creation Date"]))
                label, days = gap_result(creation, prev["Resolve Date"])
                if label == "Created Before Previous Resolve":
                    service_result = label
                elif days <= 30:
                    service_result = "Within 30 Days"
                else:
                    service_result = "After 30 Days"
            else:
                service_result = "First Service"

        out = row.drop(labels=["__original_order"]).to_dict()
        out["Detected Materials"] = ", ".join(materials) if materials else "Service"
        out["Compressor Warranty"] = compressor_result
        out["Gas Warranty"] = gas_result
        out["Parts Warranty"] = parts_result
        out["Service Warranty"] = service_result

        # Keep general previous-complaint details for traceability.
        if earlier:
            previous = earlier[0]
            out["Previous Order Number"] = str(previous.get("Order Number", "") or "")
            out["Previous Resolve Date"] = pd.Timestamp(previous["Resolve Date"])
            out["Days Between Complaints"] = (
                creation.normalize() - pd.Timestamp(previous["Resolve Date"]).normalize()
            ).days
        else:
            out["Previous Order Number"] = ""
            out["Previous Resolve Date"] = None
            out["Days Between Complaints"] = None

        output_rows.append(out)

        # Include this complaint in the working history for later rows in this upload.
        previous_records.append({
            "Order Number": str(row.get("Order Number", "") or ""),
            "Barcode": barcode,
            "Creation Date": creation,
            "Resolve Date": resolve,
            "Remarks": remarks,
            "Materials": materials,
        })

    result_df = pd.DataFrame(output_rows)
    preferred = [
        "Order Number", "Barcode", "Creation Date", "Resolve Date", "Remarks",
        "Detected Materials", "Previous Order Number", "Previous Resolve Date",
        "Days Between Complaints", "Compressor Warranty", "Gas Warranty",
        "Parts Warranty", "Service Warranty"
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
    "**Order Number, Barcode, Creation Date, Resolve Date, Remarks**."
)

with st.expander("Rules / Checking Logic"):
    st.markdown(
        """
        - Remarks ke ek cell mein multiple materials ho sakte hain; portal unhein detect karega.
        - Warranty results alag columns mein aayenge: Compressor, Gas, Parts aur Service.
        - Compressor Warranty: same barcode aur compressor repeat; warranty period 90 days.
        - Gas Warranty: same barcode aur gas repeat; warranty period 90 days.
        - Parts Warranty: same barcode aur same part repeat; warranty period 30 days. Repeat part 30 din se baad aaye to No Warranty Part.
        - Service Warranty: jab current remarks mein koi part/compressor/gas na ho; service history ka warranty period 30 days hai.
        - Gap current Creation Date minus previous matching complaint Resolve Date se calculate hota hai.
        - Order Number sirf output/tracking ke liye hai; calculation par asar nahi karta.
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
        "Order Number", "Barcode", "Creation Date", "Resolve Date", "Remarks"
    ]
    missing = [col for col in required_columns if col not in input_df.columns]

    if missing:
        st.error("Excel mein yeh required columns missing hain: " + ", ".join(missing))
        st.info("Column headings bilkul is tarah hon: Order Number, Barcode, Creation Date, Resolve Date, Remarks.")
        st.stop()

    # Clean identifiers and dates.
    for col in ["Order Number", "Barcode", "Remarks"]:
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
    warranty_cols = [
        "Compressor Warranty", "Gas Warranty", "Parts Warranty", "Service Warranty"
    ]
    c2.metric(
        "Warranty Matches",
        int(result_df[warranty_cols].apply(
            lambda col: col.astype(str).str.startswith("Within ")
        ).sum().sum())
    )
    c3.metric(
        "No Warranty / First",
        int(result_df[warranty_cols].apply(
            lambda col: col.astype(str).str.contains("No Warranty|First ", case=False, regex=True)
        ).sum().sum())
    )

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
