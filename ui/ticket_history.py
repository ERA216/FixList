"""Show earlier incidents in the current task card and open their source cards."""
import streamlit as st

from backend import service
from ui.open_data import duration, interval_label, timestamp


OPEN_REQUEST = "ticket-history-open"


def _open(incident_id, return_to=None):
    st.session_state[OPEN_REQUEST] = (incident_id, return_to)
    st.rerun()


def render(record, *, return_to=None):
    incident_id = record["id"]
    if return_to is not None and st.button(
        f"← Вернуться к карточке №{return_to}", key=f"ticket-history-back-{incident_id}", type="tertiary"
    ):
        _open(return_to)

    history = service.ticket_repair_history(incident_id)
    if not history["entries"]:
        st.caption("В журнале пока нет похожих предыдущих событий по этому оборудованию.")
        return history

    with st.container(border=True, key=f"ticket-history-{incident_id}"):
        st.subheader("Что делали в прошлый раз")
        count = history["occurrences_30d"]
        if source := record["source_record"]:
            st.caption(f"Тот же код {source['code']} · время UTC · событий за 30 дней к этой дате, включая текущее · {count}")
        else:
            st.caption(f"Событий за 30 дней к этой дате, включая текущее · {count}")

        if repeat := history["repeat_after_repair"]:
            amount, unit = interval_label(repeat["elapsed_seconds"] / 86400)
            st.markdown(f"**Повтор через {amount.removesuffix(',0')} {unit} после завершения заявки №{repeat['id']}**")

        for entry in history["entries"]:
            elapsed = duration(entry["downtime_min"] * 60)
            st.markdown(f"**{timestamp(entry['occurred_at'])} · простой {elapsed}**")
            if entry["resolution"]:
                st.caption(f"Работы записаны · {timestamp(entry['resolved_at'])}")
                st.text(entry["resolution"])
            else:
                st.caption("Результат работ на момент этого события не указан.")
            label = "Открыть событие" if source else "Открыть заявку"
            if st.button(
                f"{label} №{entry['id']} →",
                key=f"ticket-history-link-{incident_id}-{entry['id']}",
                type="tertiary",
            ):
                _open(entry["id"], return_to or incident_id)
    return history
