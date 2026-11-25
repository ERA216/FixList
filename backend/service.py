"""Фасад для интерфейса: всё, что нужно экранам, в одном месте. UI вызывает только эти функции."""
from datetime import datetime
import json
from typing import Optional

from . import advisor, analytics, auth, briefing, config, data_import, db, extract, finance, notifications, seed, workspace_data
from .finance import EXAMPLE_HOUR_RATE, EXAMPLE_REDUCTION, MAX_RATE
from .finance import amount as estimated_money, assumption_label as money_assumptions, investment_effect
from .schemas import FAILURE_LABELS, MAX_DOWNTIME_MIN, FailureType, event_type_label


def bootstrap() -> None:
    """Initialize accounts, then replace the sample workspace with real source events."""
    db.init_db()
    from . import archive
    archive.install(seed.ensure_demo_company())
    db.set_meta("seed_version", seed.SEED_VERSION)


def status() -> dict:
    return {"ai_enabled": config.ai_enabled(), "mode_label": config.mode_label(), **db.llm_stats()}


def equipment_options() -> list[dict]:
    return db.load_equipment()


def import_company_data(user_id: int, equipment_csv: bytes | None = None, events_csv: bytes | None = None) -> dict:
    return data_import.import_company_data(user_id, equipment_csv, events_csv)


def data_import_templates() -> tuple[bytes, bytes]:
    return data_import.templates()


def start_workspace_with_import(workspace_name, equipment_csv, events_csv=None):
    return data_import.start_with_import(workspace_name, equipment_csv, events_csv)


def start_workspace(workspace_name, name, code="", line="", hour_cost_kzt=0):
    return workspace_data.start_workspace(workspace_name, name, code, line, hour_cost_kzt)


def add_workspace_equipment(user_id, name, code="", line="", hour_cost_kzt=0):
    return workspace_data.add_equipment(user_id, name, code, line, hour_cost_kzt)


def add_workspace_event(user_id, equipment_id, description, occurred_at, failure_type, downtime_min=0, parts_cost_kzt=0,
                        *, equipment_name=None, custom_event_type=None):
    return workspace_data.add_event(user_id, equipment_id, description, occurred_at, failure_type, downtime_min, parts_cost_kzt,
                                    equipment_name=equipment_name, custom_event_type=custom_event_type)


def failure_options(*, include_archive: bool = True) -> dict:
    if include_archive and db.source_profile():
        from . import open_data
        return {"kelmarsh:" + row["code"]: open_data.message_ru(row) for row in open_data.events()} | {t.value: FAILURE_LABELS[t.value] for t in FailureType}
    return {t.value: FAILURE_LABELS[t.value] for t in FailureType}


# ---------- экран 1: новая запись ----------

def analyze_text(raw: str) -> dict:
    """Текст смены → черновики для проверки мастером. ValueError — понятная ошибка ввода."""
    return extract.extract(raw)


def save_record(raw: str, drafts: list[dict], mode: str = "ai", author: Optional[str] = None, *, author_user_id=None) -> dict:
    """Сохранить подтверждённые мастером инциденты. Возвращает id и сигналы по затронутым станкам."""
    if not (raw or "").strip():
        raise ValueError("Пустой текст смены.")
    if not drafts:
        raise ValueError("Нет инцидентов для сохранения.")
    eq = db.equipment_map()
    for i, d in enumerate(drafts, 1):
        if d.get("equipment_id") not in eq:
            raise ValueError(f"Инцидент {i}: выберите станок из списка.")
        if d.get("failure_type") not in FAILURE_LABELS:
            raise ValueError(f"Инцидент {i}: выберите тип неисправности.")
        if d.get("downtime_min") is None or not 0 <= int(d["downtime_min"]) <= MAX_DOWNTIME_MIN:
            raise ValueError(f"Инцидент {i}: укажите простой от 0 до {MAX_DOWNTIME_MIN} мин.")
        if "parts_cost_known" in d:
            parts = d.get("parts", [])
            if not isinstance(parts, list) or any(
                not isinstance(part, dict) or not isinstance(part.get("name"), str) or not part["name"].strip()
                or type(part.get("qty")) is not int or not 1 <= part["qty"] <= 100 for part in parts
            ):
                raise ValueError(f"Инцидент {i}: укажите название и целое количество от 1 до 100 для каждой запчасти.")
            cost = d.get("parts_cost_kzt")
            cost_known = d["parts_cost_known"]
            if not isinstance(cost_known, bool) or (cost_known and (type(cost) is not int or not 0 <= cost <= 1_000_000_000)):
                raise ValueError(f"Инцидент {i}: укажите целую стоимость запчастей от 0 до 1 000 000 000 ₸.")
            if not cost_known and cost is not None:
                raise ValueError(f"Инцидент {i}: подтвердите стоимость запчастей или оставьте её пустой.")
    with db.connect() as c:
        log_id = db.add_shift_log(raw.strip(), "processed", mode, conn=c, author=author, author_user_id=author_user_id)
        ids = [db.add_incident(log_id, {**d, "parts_cost_kzt": d.get("parts_cost_kzt")}, conn=c) for d in drafts]
        notifications.queue_incidents(ids, conn=c)
    touched = {d["equipment_id"] for d in drafts}
    signals = [] if db.source_profile() else [s for s in analytics.anomalies() if s["equipment_id"] in touched]
    return {"log_id": log_id, "incident_ids": ids, "signals": signals}


# ---------- экран 2: аномалии ----------

def dashboard() -> dict:
    if db.source_profile():
        from . import archive
        return archive.dashboard()
    summary = analytics.equipment_summary()
    return {
        "window_days": config.ANALYSIS_WINDOW_DAYS,
        "total_loss_kzt": analytics.sum_losses(r["loss_kzt"] for r in summary),
        "parts_kzt": sum(r["parts_kzt"] for r in summary),
        "total_downtime_h": round(sum(r["downtime_h"] for r in summary), 1),
        "by_equipment": summary,
        "anomalies": analytics.anomalies(),
    }


def incident_evidence(incident_ids: list[int]) -> list[dict]:
    rows = [r for r in db.incidents() if r["id"] in set(incident_ids)]
    return [{"id": r["id"], "date": r["occurred_at"][:10], "downtime_min": r["downtime_min"], "raw_text": r["raw_text"], "is_demo": bool(r["is_demo"])} for r in rows]


# ---------- экран 3: план экономии ----------

def savings_plan(equipment_id: str, failure_type: str, custom_event_type=None) -> dict:
    return advisor.plan(equipment_id, failure_type, custom_event_type=custom_event_type)


def mark_applied(equipment_id: str, measure_id: str) -> None:
    db.apply_measure(equipment_id, measure_id)


def unmark_applied(equipment_id: str, measure_id: str) -> None:
    db.unapply_measure(equipment_id, measure_id)


def applied_set() -> set[tuple[str, str]]:
    return {(a["equipment_id"], a["measure_id"]) for a in db.applied_measures()}


# ---------- экран 4: история и эффект ----------

def history(query: str = "", limit: int = 300) -> list[dict]:
    q = (query or "").casefold().strip()
    rows = []
    for r in reversed(db.incidents()):
        row = {"Запись": f"#{r['id']}", "Дата": r["occurred_at"][:16].replace("T", " "), "Станок": r["equipment_name"],
               "Тип": event_type_label(r["failure_type"], r["custom_event_type"]), "Простой, мин": r["downtime_min"],
               "Запчасти, ₸": r["parts_cost_kzt"], "Кто записал": r.get("author_role") or "", "Исходный текст": r["raw_text"]}
        if not q or q in " ".join(map(str, row.values())).casefold():
            rows.append(row)
    return rows[:limit]


def effects() -> list[dict]:
    if db.source_profile():
        return []
    measures = {m["id"]: m for m in db.load_measures()}
    eq = db.equipment_map()
    out = []
    for a in db.applied_measures():
        m = measures.get(a["measure_id"])
        if not m:
            continue
        eff = analytics.measure_effect(a["equipment_id"], m["applies_to"], a["applied_at"])
        out.append({**eff, "measure_id": m["id"], "measure_title": m["title"], "equipment_name": eq[a["equipment_id"]]["name"], "is_demo": bool(a["is_demo"])})
    return out


# ---------- сводка дня (руководитель) ----------

def today_summary() -> dict:
    return briefing.today_summary()


def checklist() -> list[dict]:
    return briefing.checklist()


def daily_brief() -> dict:
    return briefing.daily_brief()


def reset_demo() -> None:
    seed.reset_demo()


def ask_ai(question: str, history=None, *, hourly_rate=None, reduction_percent=None) -> dict:
    from . import assistant
    return assistant.ask(question, history, hourly_rate=hourly_rate, reduction_percent=reduction_percent)


def downtime_scenario(hours: float, *, hourly_rate=None, reduction_percent=None) -> dict:
    return finance.scenario(hours, hourly_rate=hourly_rate, reduction_percent=reduction_percent)


def company_money_scenario(*, hourly_rate=None, reduction_percent=None) -> dict:
    return finance.company_report(hourly_rate=hourly_rate, reduction_percent=reduction_percent)


# ---------- обход оборудования (сотрудник-аналитик) ----------

def save_inspection(rows: list[dict], author: str, *, author_user_id=None) -> dict:
    """rows: [{equipment_id, state: ok|remark|problem, note, downtime_min}]. Замечания и проблемы становятся карточками."""
    eq = db.equipment_map()
    rows = [r for r in rows if r.get("equipment_id") in eq]
    if not rows:
        raise ValueError("Отчёт пустой.")
    for r in rows:
        if r["state"] not in ("ok", "remark", "problem"):
            raise ValueError("Неизвестное состояние.")
        if not 0 <= int(r.get("downtime_min") or 0) <= MAX_DOWNTIME_MIN:
            raise ValueError(f"Укажите простой от 0 до {MAX_DOWNTIME_MIN} мин.")
        if r["state"] != "ok" and not (r.get("note") or "").strip():
            raise ValueError(f"{eq[r['equipment_id']]['name']}: опишите замечание или проблему в комментарии.")
    cards = []
    for r in rows:
        incident_id = None
        if r["state"] != "ok":
            note = r["note"].strip()
            label = "Проблема" if r["state"] == "problem" else "Замечание"
            text = f"Обход · {eq[r['equipment_id']]['name']} · {label}: {note}"
            with db.connect() as c:
                log_id = db.add_shift_log(text, "processed", "inspection", conn=c, author=author, author_user_id=author_user_id)
                incident_id = db.add_incident(log_id, {"equipment_id": r["equipment_id"], "failure_type": extract.detect_failure(note),
                                                       "downtime_min": int(r.get("downtime_min") or 0), "parts": [], "cause": None,
                                                       "action": None, "confidence": 1.0}, conn=c)
                notifications.queue_incidents([incident_id], conn=c)
            cards.append(incident_id)
        db.add_inspection(r["equipment_id"], r["state"], (r.get("note") or "").strip() or None, author, incident_id)
    return {"checked": len(rows), "cards": cards}


def inspections_today() -> list[dict]:
    from datetime import date
    return [r for r in db.inspections() if r["created_at"][:10] == date.today().isoformat()]


def equipment_health() -> list[dict]:
    return analytics.equipment_health()


# ---------- заявки: новая → в работе → решена ----------

def _ticket(r: dict) -> dict:
    status = r.get("status") or "new"
    confirmed = r.get("confirmed_solution_json")
    return {"id": r["id"], "time": r["occurred_at"][:16].replace("T", " "), "equipment_id": r["equipment_id"], "equipment_name": r["equipment_name"],
            "occurred_at": r["occurred_at"], "hour_cost_kzt": r["hour_cost_kzt"], "parts_cost_kzt": r["parts_cost_kzt"],
            "failure_type": r["failure_type"], "custom_event_type": r["custom_event_type"],
            "failure_label": event_type_label(r["failure_type"], r["custom_event_type"]),
            "downtime_min": r["downtime_min"], "raw_text": r["raw_text"], "author": r.get("author_role") or "", "cause": r["cause"],
            "status": status, "status_label": db.STATUS_LABELS.get(status), "assignee": r.get("assignee"),
            "assignee_user_id": r["assignee_user_id"], "assignee_name": r["assignee_name"], "assignee_login": r["assignee_login"],
            "can_confirm_solution": status in db.SOLUTION_CONFIRMATION_STATUSES and not confirmed,
            "confirmed_solution": json.loads(confirmed) if confirmed else None,
            "resolution": r.get("resolution"), "resolved_at": r.get("resolved_at"), "taken_at": r.get("taken_at"),
            "approved_by": r.get("approved_by"),
            "escalation_note": r.get("escalation_note"), "is_demo": bool(r["is_demo"]),
            "source_id": r.get("source_id"), "source_dataset": r.get("source_dataset"), "source_record": r.get("source_record")}


def ticket(incident_id: int) -> dict | None:
    rows = db.incidents(incident_id=incident_id)
    return _ticket(rows[0]) if rows else None


def tickets(statuses: Optional[list[str]] = None, author: Optional[str] = None, limit: int | None = 100, *,
            author_user_id: Optional[int] = None, worker_user_id: Optional[int] = None) -> list[dict]:
    rows = [_ticket(r) for r in reversed(db.incidents(author_user_id=author_user_id, worker_user_id=worker_user_id))]
    if statuses:
        rows = [t for t in rows if t["status"] in statuses]
    if author:
        rows = [t for t in rows if t["author"] == author]
    return rows[:limit]


def ticket_counts() -> dict:
    from datetime import date
    all_ = [_ticket(r) for r in db.incidents()]
    today = date.today().isoformat()
    out = {k: sum(t["status"] == k for t in all_) for k in ("new", "approved", "escalated")}
    out["archived"] = sum(t["status"] == "archived" for t in all_)
    out["resolved_today"] = sum(t["status"] == "resolved" and (t["resolved_at"] or "")[:10] == today for t in all_)
    return out


def ticket_hypotheses(t: dict) -> list[dict]:
    """Быстрые гипотезы причины для механика (по правилам, без вызова ИИ)."""
    if db.source_profile():
        return []
    from . import diagnose
    hist = diagnose.history(t["equipment_id"], t["failure_type"], custom_event_type=t.get("custom_event_type"))
    return diagnose._present(diagnose.rule_rank(t["raw_text"], t["failure_type"], hist), "rules")


def ticket_repair_history(incident_id: int) -> dict:
    from . import ticket_plan

    rows = db.incidents(incident_id=incident_id)
    if not rows:
        raise ValueError("Запись не найдена в текущем кабинете.")
    return ticket_plan.repair_history(rows[0])


def ticket_recommendation(incident_id: int) -> dict:
    from . import ticket_plan

    rows = db.incidents(incident_id=incident_id)
    if not rows:
        raise ValueError("Запись не найдена в текущем кабинете.")
    row = rows[0]
    plan = ticket_plan.for_incident(row)
    primary = plan["hypotheses"][0] if plan["hypotheses"] else plan
    solution = primary["solution"]
    if row["status"] != "resolved":
        history = ticket_plan.repair_history(row)
        if previous := history["repeat_after_repair"]:
            date = datetime.fromisoformat(previous["resolved_at"]).strftime("%d.%m.%Y")
            solution = (f"Сравните условия с заявкой №{previous['id']} и проверьте результат работ, "
                        f"записанных {date}. {solution}")
        elif history["entries"]:
            previous = history["entries"][0]
            date = datetime.fromisoformat(previous["occurred_at"]).strftime("%d.%m.%Y")
            solution = f"Сравните обстоятельства события с записью №{previous['id']} от {date}. {solution}"
    return {**plan, "incident_id": incident_id,
            "version": (row["status"], row["taken_at"], row["cause"], row["resolution"]),
            "proposed_solution": {"title": primary.get("title"), "solution": solution,
                                  "verify": primary["verify"], "checks": primary.get("checks", plan["steps"]),
                                  "mode": plan["mode"]}}


def confirm_ticket_solution(incident_id: int, user_id: int, proposal: dict, *,
                            assignee_user_id: Optional[int] = None) -> None:
    """Confirm the server-generated proposal displayed for this task."""
    if proposal["incident_id"] != incident_id:
        raise ValueError("Это решение подготовлено для другой задачи. Откройте карточку заново.")
    db.confirm_solution(incident_id, user_id, proposal["version"], proposal["proposed_solution"],
                        assignee_user_id=assignee_user_id)


def approve_ticket(incident_id: int, admin: str, *, expected_status=None,
                   assignee_user_id: Optional[int] = None, actor_user_id: Optional[int] = None) -> None:
    """Администратор одобряет: заявку решают сотрудники с подсказками ИИ."""
    db.set_status(incident_id, "approved", who=admin, expected_status=expected_status,
                  assignee_user_id=assignee_user_id, actor_user_id=actor_user_id)


def assign_ticket(incident_id: int, actor_user_id: int, assignee_user_id: int, *,
                  expected_assignee_user_id: Optional[int]) -> None:
    """Assign an existing task that is already in work."""
    db.assign_ticket(incident_id, actor_user_id, assignee_user_id, expected_assignee_user_id=expected_assignee_user_id)


def resume_ticket(incident_id: int, actor_user_id: int, *, expected_status: str = "escalated") -> None:
    """Resume work after an administrator handles the request for help."""
    db.resume_ticket(incident_id, actor_user_id, expected_status=expected_status)


def escalate_ticket(incident_id: int, who: str, note: str, *, expected_status=None,
                    actor_user_id: Optional[int] = None) -> None:
    """Сотрудникам не хватает сил — передать администратору (запчасти, деньги, подрядчик)."""
    if not (note or "").strip():
        raise ValueError("Напишите, чего не хватает: запчасти, инструмента, знаний — администратору будет проще решить.")
    db.set_status(incident_id, "escalated", who=who, note=note.strip(), expected_status=expected_status,
                  actor_user_id=actor_user_id)


def resolve_ticket(incident_id: int, who: str, resolution: str, cause: Optional[str] = None, *,
                   expected_status=None, actor_user_id: Optional[int] = None) -> None:
    if not (resolution or "").strip():
        raise ValueError("Опишите, что сделали, — это попадёт в историю станка.")
    db.set_status(incident_id, "resolved", who=who, resolution=resolution.strip(), cause=(cause or None),
                  expected_status=expected_status, actor_user_id=actor_user_id)


def reopen_ticket(incident_id: int, *, expected_status=None, actor_user_id: Optional[int] = None) -> None:
    db.set_status(incident_id, "new", expected_status=expected_status, actor_user_id=actor_user_id)


# ---------- компании и сотрудники ----------

def use_company(company_id: int) -> None:
    db.set_company(company_id)
    if db.source_profile():
        from . import open_data
        FAILURE_LABELS.update({"kelmarsh:" + row["code"]: open_data.message_ru(row) for row in open_data.events()})


def archive_context():
    return db.source_profile()


def archive_report(equipment_id=None, start=None, end=None):
    from . import archive
    return archive.report(equipment_id, start, end)


def archive_equipment():
    from . import archive
    return archive.equipment()


def archive_daily(day):
    from . import archive
    return archive.daily(day)


def archive_record(incident_id):
    rows = db.incidents(incident_id=incident_id)
    return rows[0] if rows else None


def login(company_code: str, login_: str, pin: str):
    return auth.login(company_code, login_, pin)


def register_company(name: str, city: str = "", note: str = "") -> dict:
    return auth.register_company(name, city, note)


def add_workers(company_id: int, names_text: str, role: str = "worker") -> list[dict]:
    return auth.add_workers(company_id, names_text, role)


def list_users(company_id: int) -> list[dict]:
    return auth.list_users(company_id)


def demo_credentials() -> dict:
    return {"code": seed.DEMO_CODE, "users": [{"name": n, "role": r, "login": l, "pin": p} for n, r, l, p in seed.DEMO_USERS]}


def notification_settings(user_id: int) -> dict:
    return notifications.settings(user_id)


def save_notification_settings(user_id: int, recipient: str, enabled: bool) -> None:
    notifications.save_settings(user_id, recipient, enabled)


def test_notification(user_id: int) -> None:
    notifications.queue_test(user_id)


def retry_notifications(user_id: int) -> None:
    notifications.retry_failed(user_id)


def start_notifications() -> None:
    notifications.start_worker()
