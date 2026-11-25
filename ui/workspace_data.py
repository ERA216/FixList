"""Forms for entering equipment and events directly in FixList."""
from datetime import date, datetime

import streamlit as st

from backend import service
from backend.schemas import MAX_DOWNTIME_MIN, MAX_EVENT_TYPE_LENGTH, FailureType
from ui import data_import


def _equipment_fields():
    name = st.text_input("Название оборудования", placeholder="Например, насос подачи", max_chars=120)
    with st.expander("Участок, код и стоимость простоя"):
        line = st.text_input("Участок", placeholder="Например, цех № 2", max_chars=120)
        code = st.text_input("Код оборудования", placeholder="Присвоим автоматически, если оставить пустым", max_chars=40)
        tariff = st.number_input("Стоимость часа простоя, ₸", min_value=0, max_value=1_000_000_000, step=1000,
                                 help="Оставьте 0, если хотите учитывать только длительность простоя.")
    return name, code, line, tariff


def _start_form():
    st.caption("Введите первое оборудование и сразу откройте свой кабинет.")
    with st.form("workspace-start"):
        workspace_name = st.text_input("Название кабинета", placeholder="Например, Мастерская Сарыарка", max_chars=120)
        fields = _equipment_fields()
        submitted = st.form_submit_button("Сохранить и открыть кабинет", type="primary", use_container_width=True)
    if submitted:
        try:
            return service.start_workspace(workspace_name, *fields)
        except ValueError as error:
            st.error(str(error))
    return None


def _add_equipment(user):
    st.subheader("Добавить оборудование")
    revision = st.session_state.get("equipment-form-revision", 0)
    with st.form(f"workspace-equipment-{revision}"):
        fields = _equipment_fields()
        submitted = st.form_submit_button("Сохранить оборудование", use_container_width=True)
    if submitted:
        try:
            service.add_workspace_equipment(user["id"], *fields)
        except ValueError as error:
            st.error(str(error))
        else:
            st.session_state["equipment-form-revision"] = revision + 1
            st.session_state["workspace-message"] = f"Оборудование «{fields[0].strip()}» добавлено. Можно записать событие."
            st.rerun()


def _add_event(user, equipment, open_event):
    st.subheader("Записать событие")
    labels = {item["id"]: f"{item['name']} · {item['code']}" for item in equipment}
    failures = {None: "Определить по описанию", **service.failure_options()}
    revision = st.session_state.get("event-form-revision", 0)
    form_key = f"workspace-event-{revision}"
    with st.container(border=True, key=form_key):
        selected = st.selectbox("Оборудование", list(labels), index=None, format_func=lambda value: labels.get(value, value),
                                placeholder="Выберите или напишите название", accept_new_options=True, key=f"{form_key}-equipment")
        st.caption("Чтобы добавить новое оборудование, напишите название и нажмите Enter.")
        description = st.text_area("Что произошло?", placeholder="Например, насос остановился после падения давления",
                                   max_chars=4000, key=f"{form_key}-description")
        left, right = st.columns(2)
        failure_type = left.selectbox("Тип события", list(failures), format_func=failures.get, key=f"{form_key}-type")
        downtime = right.number_input("Простой, мин", min_value=0, max_value=MAX_DOWNTIME_MIN, step=1, key=f"{form_key}-downtime")
        custom_event_type = None
        if failure_type == FailureType.other.value:
            custom_event_type = st.text_input("Название события", placeholder="Например, утечка сжатого воздуха",
                                              max_chars=MAX_EVENT_TYPE_LENGTH, key=f"{form_key}-custom-type")
        with st.expander("Дата, время и запчасти"):
            left, right = st.columns(2)
            day = left.date_input("Дата события", value=date.today(), min_value=date(1900, 1, 1), max_value=date.today(),
                                  format="DD.MM.YYYY", key=f"{form_key}-date")
            time = right.time_input("Время события", value=None, key=f"{form_key}-time",
                                    help="Если оставить пустым, FixList подставит текущее время для сегодняшней даты или 00:00 для прошедшей.")
            cost = st.number_input("Стоимость запчастей, ₸", min_value=0, max_value=1_000_000_000, step=1, key=f"{form_key}-parts")
        submitted = st.button("Сохранить событие", type="primary", use_container_width=True, key=f"{form_key}-save")
    if submitted:
        try:
            if day is None:
                raise ValueError("Укажите дату события.")
            occurred_at = datetime.combine(day, time if time is not None else (datetime.now().time() if day == date.today() else datetime.min.time()))
            equipment_id = selected if selected in labels else None
            event_id = service.add_workspace_event(user["id"], equipment_id, description, occurred_at, failure_type, downtime, cost,
                                                   equipment_name=selected if equipment_id is None else None,
                                                   custom_event_type=custom_event_type)
        except ValueError as error:
            st.error(str(error))
        else:
            st.session_state["event-form-revision"] = revision + 1
            st.session_state["workspace-event-id"] = event_id
            st.session_state["company-board-status"] = "new"
            st.session_state["board-page-new"] = 1
            st.session_state.pop("plans", None)
            st.session_state.pop("brief_key", None)
            st.rerun()
    if event_id := st.session_state.get("workspace-event-id"):
        st.success(f"Событие №{event_id} добавлено в задачи и историю.")
        if st.button("Открыть задачу →"):
            open_event(event_id)


def start(enter_workspace):
    st.subheader("Добавить свои данные", anchor="add-own-data")
    manual, files = st.tabs(["Ввести вручную", "Загрузить CSV"])
    with manual:
        if account := _start_form():
            enter_workspace(account)
    with files:
        st.caption("Загрузите справочник оборудования и при необходимости журнал событий. Кабинет откроется после загрузки.")
        if account := data_import.render():
            enter_workspace(account)


def board_controls(user, open_event, enter_workspace, public_archive):
    if credentials := st.session_state.get("new-workspace-access"):
        with st.expander("Сохраните данные для следующего входа", expanded=True):
            st.code(f"Код компании: {credentials['code']}\nЛогин: {credentials['login']}\nPIN: {credentials['pin']}", language=None)
            if st.button("Данные сохранены"):
                st.session_state.pop("new-workspace-access", None)
                st.rerun()
    if message := st.session_state.pop("workspace-message", None):
        st.success(message)
    data_import.show_result()

    ss = st.session_state
    showing = ss.get("show-workspace-forms", False)
    if st.button("Скрыть формы" if showing else "Добавить свои данные", type="secondary" if showing else "primary"):
        ss["show-workspace-forms"] = not showing
        st.rerun()
    if not showing:
        return
    with st.container(border=True):
        if public_archive:
            st.caption("Ваши записи будут в отдельном кабинете. Архив Kelmarsh остаётся в демо FixList.")
            start(enter_workspace)
            return

        manual, files = st.tabs(["Ввести вручную", "Загрузить CSV"])
        with manual:
            catalog = [item for item in service.equipment_options() if item.get("code")]
            events, equipment = st.columns([1.5, 1], gap="large")
            with equipment:
                _add_equipment(user)
            with events:
                _add_event(user, catalog, open_event)
        with files:
            data_import.render(user)
