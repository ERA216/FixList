"""Money and savings in the existing tab, with assumptions shared by chat."""
import streamlit as st

from backend import service
from backend.schemas import kzt, num
from ui.theme import card


def settings():
    stored = st.session_state.get("money-settings", {})
    return {name: stored.get(name) for name in ("hourly_rate", "reduction_percent")}


def _save(name, widget_key):
    st.session_state.setdefault("money-settings", {})[name] = st.session_state[widget_key]


def show(archive_report=None):
    archive = archive_report is not None
    prefix = "archive" if archive else "company-money"
    saved = st.session_state.get("money-settings", {})
    summary = st.container(key="money-scenario-summary")
    with st.container(border=True):
        st.subheader("Параметры расчёта")
        st.caption("Стоимость часа применяется к оборудованию без своего тарифа. Если оставить поле пустым, используется пример.")
        tariff, goal, budget = st.columns(3)
        rate_key, reduction_key, budget_key = (f"{prefix}-{name}" for name in ("hour-rate", "reduction", "investment"))
        rate = tariff.number_input("Стоимость часа простоя, ₸", min_value=0, max_value=service.MAX_RATE, step=1000,
                                   value=saved.get("hourly_rate"), key=rate_key,
                                   placeholder=f"{kzt(service.EXAMPLE_HOUR_RATE)} ₸/ч · пример",
                                   on_change=_save, args=("hourly_rate", rate_key))
        reduction = goal.number_input("Сокращение простоя, %", min_value=0, max_value=100, step=1,
                                      value=saved.get("reduction_percent", service.EXAMPLE_REDUCTION), key=reduction_key,
                                      placeholder=str(service.EXAMPLE_REDUCTION), on_change=_save,
                                      args=("reduction_percent", reduction_key))
        investment = budget.number_input("Затраты на меру, ₸", min_value=0, max_value=1_000_000_000_000,
                                         step=1000, value=saved.get("investment"), key=budget_key,
                                         placeholder="Для расчёта окупаемости", on_change=_save,
                                         args=("investment", budget_key))
    if archive:
        stats = archive_report["stats"]
        result = service.downtime_scenario(stats["downtime_seconds"] / 3600, hourly_rate=rate, reduction_percent=reduction)
        events, months = stats["events"], 12
        period = "Архив Kelmarsh · 2016 год"
    else:
        result = service.company_money_scenario(hourly_rate=rate, reduction_percent=reduction)
        events, months = result["event_count"], result["days"] / 30
        period = f"Журнал компании · последние {result['days']} дн."

    with summary:
        st.caption(period)
        if not events:
            st.info("Добавьте в журнал событие с длительностью простоя, чтобы увидеть денежную оценку.")
        else:
            hours = num(round(result["hours"], 2))
            reduction_label = num(result["reduction_percent"])
            loss, saving = st.columns(2)
            with loss:
                card("Стоимость простоя", service.estimated_money(result["cost_kzt"]),
                     f"{hours} ч по журналу · {service.money_assumptions(result)}")
            with saving:
                card("Возможная экономия", service.estimated_money(result["savings_kzt"]),
                     f"Если сократить простой на {reduction_label}% ({num(round(result['saved_hours'], 2))} ч)", tone="success")
            st.caption("Экономия рассчитана по сценарию сокращения простоя. Затраты на ремонт в неё не входят.")

    if events:
        if not archive and result["parts_kzt"]:
            st.caption(f"Запчасти по журналу · {kzt(result['parts_kzt'])} ₸")
        monthly, payback = service.investment_effect(result["savings_kzt"], months, investment)
        monthly_label = "Среднее за месяц архива" if archive else "В пересчёте на 30 дней"
        st.write(f"{monthly_label} · **{service.estimated_money(monthly)}** возможной экономии.")
        if payback is not None:
            st.write(f"Расчётная окупаемость · **≈ {num(round(payback, 2))} мес.** при сохранении этого эффекта.")
        elif investment is not None:
            st.caption("Окупаемость появится при положительной месячной экономии.")
        with st.expander("Как получились суммы"):
            st.write("Если остановки одного объекта перекрываются по времени, общие минуты учитываются один раз.")
            st.write("Стоимость простоя — сумма часов по тарифам оборудования и по тарифу сценария для остального времени.")
            st.write("Возможная экономия — стоимость простоя, умноженная на выбранный процент сокращения.")
            st.write("Окупаемость — затраты на меру, делённые на расчётную месячную экономию.")

    st.subheader("Как FixList помогает снизить расходы")
    st.write("FixList выявляет повторяющиеся остановки, чтобы команда сначала занялась проблемами, которые отнимают больше всего времени. "
             "По ним можно поручить проверку, а после внедрения меры сверить изменения по журналу.")
