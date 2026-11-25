"""Shared chat history and evidence for the private and archive assistants."""
from datetime import date

import streamlit as st

from backend import service
from backend.schemas import num
from ui import money_scenario


def render(chat, *, can_open_finance):
    for message in chat:
        with st.chat_message("user"):
            st.text(message["q"])
        with st.chat_message("assistant"):
            answer = message.get("answer", message.get("a", ""))
            st.markdown(answer.partition("\n\n**Комментарий ИИ:** ")[0])
            if message.get("mode") == "ai":
                st.caption(f"ИИ · {message.get('provider', 'модель')} · расчёты выполнены по журналу")
            else:
                st.caption("Ответ по данным журнала · без обращения к ИИ")
            if commentary := message.get("ai_commentary"):
                st.markdown("**Пояснение ИИ**")
                st.write(commentary)
            if sources := message.get("sources"):
                with st.expander("Записи из журнала"):
                    for source in sources:
                        when = date.fromisoformat(source["date"]).strftime("%d.%m.%Y")
                        zone = " UTC" if source["utc"] else ""
                        st.caption(f"№{source['id']} · {source['equipment']} · {when} {source['time']}{zone} · {num(source['duration_min'])} мин")
                        st.text(source["description"] or source["label"])
                        if source["code"]:
                            st.caption(f"Код {source['code']} · {source['reference']}")
            if message.get("warning"):
                st.caption(message["warning"])
    if can_open_finance and chat and (chat[-1].get("context") or {}).get("topic") == "money":
        if st.button("Открыть «Деньги и экономия»", key="chat-money"):
            st.session_state["page"] = "План экономии"
            st.rerun()


def reply(question, chat):
    with st.spinner("Собираем данные и готовим ответ…"):
        chat.append({"q": question, **service.ask_ai(question, chat, **money_scenario.settings())})
    st.rerun()
