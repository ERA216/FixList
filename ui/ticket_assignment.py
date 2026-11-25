"""Show, choose, and update the worker responsible for a task."""
import streamlit as st

from backend import service


WORK_QUEUE_REQUEST = "ticket-work-queue"


def label(record):
    name, login = record.get("assignee_name"), record.get("assignee_login")
    if name:
        return f"{name} · {login}" if login else name
    if record["status"] in {"approved", "escalated"}:
        return "Не назначен"
    return None


def _workers(user):
    return {item["id"]: f"{item['display_name']} · {item['login']}"
            for item in service.list_users(user["company_id"]) if item["role"] == "worker"}


def select(record, user):
    if (user["role"] != "admin" or record["status"] == "approved"
            or not (record["status"] == "new" or record["can_confirm_solution"])):
        return None

    workers = _workers(user)
    if not workers:
        st.caption("Добавьте сотрудника в разделе «Сотрудники», чтобы назначать задачи.")
        return None

    assigned = record["assignee_user_id"]
    default = assigned if assigned in workers else 0
    options = list(workers) if default else [0, *workers]
    selected = st.selectbox("Исполнитель", options, index=options.index(default),
                            format_func=lambda value: workers.get(value, "Без назначения · общая очередь"),
                            key=f"ticket-assignee-{record['id']}")
    return selected or None


def edit_active(record, user):
    if user["role"] != "admin" or record["status"] != "approved":
        return

    assigned = record["assignee_user_id"]
    incident_id = record["id"]
    workers = _workers(user)
    title = "Изменить исполнителя" if assigned is not None else "Назначить исполнителя"
    with st.expander(title, expanded=assigned is None):
        if not workers:
            st.caption("В кабинете пока нет сотрудников.")
            if st.button("Добавить сотрудника →", key=f"ticket-add-worker-{incident_id}"):
                st.session_state["page"] = "Сотрудники"
                st.rerun()
            return

        options = list(workers)
        index = options.index(assigned) if assigned in workers else None
        # A change in the current assignee invalidates an older selection in another tab.
        revision = f"{incident_id}-{assigned or 0}"
        with st.form(key=f"ticket-assignee-form-{revision}", border=False):
            selected = st.selectbox("Исполнитель", options, index=index, placeholder="Выберите сотрудника",
                                    format_func=workers.get, key=f"ticket-edit-assignee-{revision}")
            submitted = st.form_submit_button("Сохранить исполнителя", type="primary")
        if not submitted:
            return
        if selected is None:
            st.warning("Выберите сотрудника.")
            return
        try:
            service.assign_ticket(incident_id, user["id"], selected, expected_assignee_user_id=assigned)
        except ValueError as error:
            st.error(str(error))
        else:
            open_work_queue(incident_id, message="Исполнитель сохранён.")


def resume(record, user):
    if user["role"] != "admin" or record["status"] != "escalated":
        return

    if note := record["escalation_note"]:
        st.warning(f"Запрос от {record['assignee'] or 'сотрудника'} · {note}")
    incident_id = record["id"]
    if st.button("Вернуть в работу", type="primary", use_container_width=True, key=f"ticket-resume-{incident_id}"):
        try:
            service.resume_ticket(incident_id, user["id"], expected_status="escalated")
        except ValueError as error:
            st.error(str(error))
        else:
            open_work_queue(incident_id, message="Задача возвращена в работу.")


def open_work_queue(incident_id, *, message="Задача переведена в работу."):
    st.session_state[WORK_QUEUE_REQUEST] = (incident_id, message)
    st.session_state["page"] = "Панель задач"
    st.rerun()
