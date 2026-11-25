"""Show and start work on a solution from the task card."""
from uuid import uuid4

import streamlit as st

from backend import service
from ui import ticket_assignment


def _show_action(recommendation):
    if recommendation["title"]:
        st.markdown(f"**Если проверка подтвердит причину «{recommendation['title']}»**")
    st.write(recommendation["solution"])
    st.markdown("**Как проверить результат**")
    st.write(recommendation["verify"])


def _show_checks(checks, signs=()):
    if checks:
        with st.expander("Что проверить перед выполнением"):
            st.caption("Работы с доступом к узлам выполняет допущенный специалист по регламенту оборудования и процедуре изоляции энергии.")
            if signs:
                st.caption("Признаки в записи · " + ", ".join(signs))
            for position, step in enumerate(checks, 1):
                st.write(f"{position}. {step}")


def show_confirmed(solution):
    with st.container(border=True):
        st.subheader("Подтверждённое решение")
        _show_action(solution)
        _show_checks(solution["checks"])
        st.caption(f"Подтвердил администратор · {solution['confirmed_by']}")
    return [solution] if solution["title"] else []


def render(record, user, history, *, assignee_user_id=None):
    if record["confirmed_solution"]:
        return show_confirmed(record["confirmed_solution"])
    if not record["can_confirm_solution"] or user["role"] != "admin":
        return []

    incident_id = record["id"]
    solutions = st.session_state.setdefault("ticket-solution-previews", {})
    version = (record["status"], record["taken_at"], record["cause"], record["resolution"])
    history_version = tuple((entry["id"], entry["resolved_at"]) for entry in history["entries"])
    plan = solutions.get(incident_id)
    if plan is None or plan["version"] != version or plan.get("history_version") != history_version:
        with st.spinner("Подбираем решение по данным карточки…"):
            try:
                plan = service.ticket_recommendation(incident_id)
            except ValueError as error:
                st.error(str(error))
                return []
            # Bind the action to the proposal the user saw before the dialog reruns.
            plan["button_key"] = f"ticket-apply-{incident_id}-{uuid4().hex}"
            plan["history_version"] = history_version
            solutions[incident_id] = plan

    with st.container(border=True, key=f"ticket-solution-result-{incident_id}"):
        st.subheader("Решение ИИ")
        action = plan["proposed_solution"]
        _show_action(action)
        if plan["mode"] != "ai":
            st.caption("По справочнику FixList и данным карточки")
        for evidence in plan["evidence"]:
            st.text(evidence)
        signs = plan["hypotheses"][0]["signs"] if plan["hypotheses"] else []
        _show_checks(action["checks"], signs)
        st.caption("Задача перейдёт в раздел «В работе».")
    if st.button("Решить с ИИ", key=plan["button_key"], type="primary", use_container_width=True):
        try:
            service.confirm_ticket_solution(incident_id, user["id"], plan, assignee_user_id=assignee_user_id)
        except ValueError as error:
            st.error(str(error))
        else:
            solutions.pop(incident_id, None)
            ticket_assignment.open_work_queue(incident_id, message="Решение сохранено. Задача в работе.")
    return [action] if action["title"] else []
