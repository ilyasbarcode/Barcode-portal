import streamlit as st
import pandas as pd
import sqlite3
from pathlib import Path
from io import BytesIO

DB_FILE = Path("barcode_history.db")

st.set_page_config(
    page_title="Barcode Checking Portal",
    page_icon="📊",
    layout="wide"
)

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT NOT NULL,
                creation_date TEXT,
                resolve_date TEXT NOT NULL,
                UNIQUE(barcode, creation_date, resolve_date)
            )
        """)

def load_history():
    with sqlite3.connect(DB_FILE) as conn:
        return pd.read_sql_query(
            "SELECT barcode, creation_date, resolve_date FROM history",
            conn
        )

def save_history(barcode, creation, resolve):
    with sqlite3.connect(DB_FILE) as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO history
            (barcode, creation_date, resolve_date)
            VALUES (?, ?, ?)
        """, list(zip(barcode, creation, resolve)))

init_db()

st.title("Barcode Checking Portal")
st.write(
    "Upload an Excel file to check repeated barcodes "
    "against previously saved records."
)

st.info(
    "Required columns: Barcode, Creation Date, Resolve Date. "
    "The 30-day comparison uses Resolve Date."
)

uploaded_file = st.file_uploader(
    "Upload Excel File",
    type=["xlsx"]
)

if uploaded_file is not None:
    try:
        df = pd.read_excel(uploaded_file)
        df.columns = [str(c).strip() for c in df.columns]

        required = [
            "Barcode",
            "Creation Date",
            "Resolve Date"
        ]

        if not all(c in df.columns for c in required):
            st.error(
                "Please use these exact column names: "
                "Barcode, Creation Date, Resolve Date."
            )
            st.stop()

        df = df[required].copy()

        df["Barcode"] = df["Barcode"].astype("string").str.strip()
        df["Creation Date"] = pd.to_datetime(
            df["Creation Date"], errors="coerce"
        ).dt.normalize()

        df["Resolve Date"] = pd.to_datetime(
            df["Resolve Date"], errors="coerce"
        ).dt.normalize()

        if (
            df["Barcode"].isna().any()
            or df["Barcode"].eq("").any()
            or df["Creation Date"].isna().any()
            or df["Resolve Date"].isna().any()
        ):
            st.error(
                "Some rows have missing barcodes or invalid dates. "
                "Please correct them in Excel and upload again."
            )
            st.stop()

        if (df["Resolve Date"] < df["Creation Date"]).any():
            st.error(
                "Resolve Date cannot be earlier than Creation Date."
            )
            st.stop()

        df["_row_order"] = range(len(df))

        history = load_history()

        if not history.empty:
            history["resolve_date"] = pd.to_datetime(
                history["resolve_date"], errors="coerce"
            ).dt.normalize()

        results = []

        # Process rows in date order so earlier rows in this
        # upload can be compared with later rows.
        ordered = df.sort_values(
            ["Resolve Date", "_row_order"]
        )

        known = {}

        for _, row in history.iterrows():
            barcode = str(row["barcode"])
            date = row["resolve_date"]

            if pd.notna(date):
                known.setdefault(barcode, []).append(date)

        for _, row in ordered.iterrows():
            barcode = str(row["Barcode"])
            current_date = row["Resolve Date"]

            previous_dates = known.get(barcode, [])

            if not previous_dates:
                status = "First Entry"
            else:
                previous_date = max(
                    d for d in previous_dates if d < current_date
                ) if any(d < current_date for d in previous_dates) else None

                if previous_date is None:
                    status = "First Entry"
                elif (current_date - previous_date).days <= 30:
                    status = "Within 30 days"
                else:
                    status = "After 30 days"

            results.append((row["_row_order"], status))

            # Add this row to the in-memory history for subsequent
            # rows in the same upload.
            known.setdefault(barcode, []).append(current_date)

        result_map = dict(results)

        output = df.sort_values("_row_order").drop(
            columns=["_row_order"]
        ).copy()

        output["Result"] = [
            result_map[i] for i in range(len(output))
        ]

        st.subheader("Results")
        st.dataframe(output, use_container_width=True)

        excel_buffer = BytesIO()

        with pd.ExcelWriter(
            excel_buffer, engine="openpyxl"
        ) as writer:
            output.to_excel(
                writer, index=False, sheet_name="Results"
            )

        st.download_button(
            "Download Result Excel",
            data=excel_buffer.getvalue(),
            file_name="barcode_results.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        )

        if st.button("Save this file to history"):
            save_history(
                df["Barcode"].tolist(),
                df["Creation Date"].dt.strftime("%Y-%m-%d").tolist(),
                df["Resolve Date"].dt.strftime("%Y-%m-%d").tolist()
            )
            st.success("Records saved to the database.")

    except Exception as e:
        st.error(f"Unable to process this file: {e}")
