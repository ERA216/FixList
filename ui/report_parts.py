"""Edit the parts and actual expenses before saving a shift report."""
import pandas as pd
import streamlit as st


def edit(draft, key):
    suggested = draft.get("parts", [])
    if not st.checkbox("Указать запчасти и расходы", value=bool(suggested), key=f"report-parts-on-{key}"):
        return [], None, True

    table = pd.DataFrame(suggested, columns=["name", "qty"]).astype({"name": "string", "qty": "Int64"})
    edited = st.data_editor(
        table, hide_index=True, num_rows="dynamic", use_container_width=True, key=f"report-parts-{key}",
        column_config={"name": st.column_config.TextColumn("Запчасть", required=True),
                       "qty": st.column_config.NumberColumn("Количество", min_value=1, max_value=100, step=1, required=True)},
    )
    parts = []
    valid = True
    for row in edited.to_dict("records"):
        name = "" if pd.isna(row["name"]) else row["name"].strip()
        qty = row["qty"]
        if not name and pd.isna(qty):
            continue
        if not name or pd.isna(qty) or not 1 <= qty <= 100 or qty != int(qty):
            valid = False
            continue
        parts.append({"name": name, "qty": int(qty)})
    if not valid:
        st.warning("Укажите название и целое количество от 1 до 100 для каждой запчасти.")

    cost = st.number_input("Подтверждённая стоимость запчастей, ₸", min_value=0, max_value=1_000_000_000,
                           value=None, step=1, placeholder="Сумма неизвестна", key=f"report-parts-cost-{key}")
    return parts, cost, valid
