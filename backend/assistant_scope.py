"""Resolve what the chat question refers to before reading the incident log."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
import re

from .finance import REDUCTION_RE


ARCHIVE_PERIOD = (date(2016, 1, 1), date(2016, 12, 31))
MONTHS = "январ феврал март апрел ма[йеяю] июн июл август сентябр октябр ноябр декабр".split()
MONTH_RE = re.compile(r"\b(" + "|".join(f"({stem})" for stem in MONTHS) + r")\w*\b")
MONTH_WORD = r"(?:" + "|".join(MONTHS) + r")\w*"
DAY_RANGE_RE = re.compile(
    rf"(?<![\w.-])(?:с\s+)?(?P<first>\d{{1,2}})(?:\s+(?P<first_month>{MONTH_WORD})(?:\s+(?P<first_year>\d{{4}})(?:\s+г(?:ода|\.))?)?)?"
    rf"\s*(?:по\b|до\b|[–—-])\s*(?P<last>\d{{1,2}})\s+(?P<last_month>{MONTH_WORD})(?:\s+(?P<last_year>\d{{4}})\b)?"
)
DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\b")
ARCHIVE_EQUIPMENT_RE = re.compile(r"\b(?:kelmarsh|келмарш|кэлмарш|kel|турбин\w*)\s*(?:№|#)?\s*(\d+)\b(?![-.]\w)")
CODE_TOKEN_RE = re.compile(r"(?<![\w.-])[^\W\d_][\w.-]{0,39}(?![\w-]|\.\w)")
EXPLICIT_CODE_RE = re.compile(r'\bкод\w*\s+оборудовани\w*(?:\s*[:=]\s*[«"]?|\s+[«"])([\w.-]{1,40})\b')
FOLLOW_UP_RE = re.compile(r"^(?:а|и|если)\b|\b(?:это|эти|там|у него|у нее|по нему|по ней)\b")


@dataclass(frozen=True)
class Scope:
    question: str
    equipment: tuple[str, ...]
    period: tuple[date, date] | None
    topic: str
    event_id: int | None
    code: str | None
    comparison: bool
    follow_up: bool

    def context(self):
        return {"equipment": list(self.equipment), "period": [d.isoformat() for d in self.period] if self.period else None,
                "topic": self.topic, "code": self.code, "event_id": self.event_id}


def _normalize(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def _date(value):
    if "." not in value:
        return date.fromisoformat(value)
    day, month, year = map(int, value.split("."))
    return date(year, month, day)


def _month_number(value):
    return next(i for i, group in enumerate(MONTH_RE.fullmatch(value).groups()[1:], 1) if group)


def _period(q, today, is_archive):
    values = DATE_RE.findall(q)
    year_match = re.search(r"\b(\d{4})\s*(?:год\w*|г\.)|\bв\s+(\d{4})\b", q)
    if values:
        if len(values) > 2:
            raise ValueError("Укажите одну дату или диапазон из двух дат.")
        dates = [_date(value) for value in values]
        return dates[0], dates[-1]
    if match := DAY_RANGE_RE.search(q):
        last_month = _month_number(match["last_month"])
        first_month = _month_number(match["first_month"]) if match["first_month"] else last_month
        default_year = 2016 if is_archive else today.year
        first_year, last_year = match["first_year"], match["last_year"]
        if first_year:
            start_year = int(first_year)
            end_year = int(last_year) if last_year else start_year + (first_month > last_month)
        else:
            end_year = int(last_year) if last_year else default_year
            start_year = end_year - (first_month > last_month)
        return date(start_year, first_month, int(match["first"])), date(end_year, last_month, int(match["last"]))
    if re.search(r"\bпозавчера\b", q):
        return (today - timedelta(days=2),) * 2
    if re.search(r"\bвчера\b", q):
        return (today - timedelta(days=1),) * 2
    if re.search(r"\bсегодня\b", q):
        return today, today
    if match := re.search(r"\b(?:последн\w*\s+)?(\d{1,4})\s+(день|дня|дней|дн\.?|недел\w*)\b", q):
        if not re.search(r"\b(?:за|последн\w*)\s+", q[:match.start(1)]):
            return None
        days = int(match[1]) * (7 if match[2].startswith("недел") else 1)
        if not 1 <= days <= 3660:
            raise ValueError("Укажите период от 1 до 3 660 дней.")
        return today - timedelta(days=days - 1), today
    if re.search(r"\b(?:за неделю|за последнюю неделю)\b", q):
        return today - timedelta(days=6), today
    year = int(year_match[1] or year_match[2]) if year_match else (2016 if is_archive else today.year)
    if match := MONTH_RE.search(q):
        month = _month_number(match[0])
        if year_after_month := re.match(r"\s+(\d{4})\b", q[match.end():]):
            year = int(year_after_month[1])
        if day := re.search(r"\b(\d{1,2})\s+$", q[:match.start()]):
            return (date(year, month, int(day[1])),) * 2
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    if year_match:
        return date(year, 1, 1), date(year, 12, 31)
    return None


def _check_equipment_codes(q, matches, is_archive):
    references = [(match[1], *match.span(1)) for match in EXPLICIT_CODE_RE.finditer(q)]
    for match in CODE_TOKEN_RE.finditer(q):
        code = match[0].rstrip(".")
        if not any(char.isdecimal() for char in code):
            continue
        if is_archive and re.fullmatch(r"kel2016-[1-6]-\d{4}", code):
            continue
        references.append((code, match.start(), match.start() + len(code)))
    for code, start, end in references:
        if not any(left <= start and right >= end for left, right, _, _ in matches):
            raise ValueError(f"Оборудование с кодом «{code.upper()}» не найдено. Проверьте код в справочнике.")


def _equipment(q, catalog, is_archive):
    candidates = []
    if is_archive:
        for match in ARCHIVE_EQUIPMENT_RE.finditer(q):
            number = int(match[1])
            if number not in range(1, 7):
                raise ValueError("В архиве есть Kelmarsh 1–6. Укажите один из этих номеров.")
            candidates.append((match.start(), match.end(), True, f"KEL{number:02d}"))
    for item in catalog:
        code = item.get("code") or item["id"]
        aliases = [(code, True), *((alias, False) for alias in {item["name"], *item["aliases"]})]
        for label, is_code in aliases:
            label = _normalize(label)
            if not label:
                continue
            boundary = r"(?<![\w.-])" if is_code else r"(?<!\w)"
            suffix = r"(?![\w-]|\.\w)" if is_code else r"(?!\w)"
            # For Russian names such as «насос подачи», also recognize «насосе подачи».
            pattern = re.escape(label) if is_code else r"\s+".join(
                re.escape(word) + (r"(?:а|у|е|ом|ы|и|ой|ью)?" if word.isalpha() else "") for word in label.split())
            for match in re.finditer(boundary + pattern + suffix, q):
                candidates.append((match.start(), match.end(), is_code, item["id"]))
    matches = set(candidates)
    _check_equipment_codes(q, matches, is_archive)
    best = [row for row in matches if not any(other[0] <= row[0] and other[1] >= row[1]
                                             and (other[0] < row[0] or other[1] > row[1] or (other[2] and not row[2]))
                                             for other in matches)]
    explicit = {row[3] for row in best if row[2]}
    chosen = []
    for start, end in sorted({row[:2] for row in best}):
        ids = {row[3] for row in best if row[:2] == (start, end)}
        if len(ids) > 1 and not (ids & explicit):
            codes = ", ".join(item.get("code", item["id"]) for item in catalog if item["id"] in ids)
            raise ValueError(f"Под это название подходит несколько объектов. Уточните код оборудования: {codes}.")
        chosen.extend(sorted(ids & explicit if len(ids) > 1 else ids))
    return tuple(dict.fromkeys(chosen))


def _topic(q):
    if re.search(r"сколько\s+(?:час\w*|минут\w*)", q) and not re.search(r"₸|тенге|стоимост", q):
        return "downtime"
    for topic, pattern in (
        ("money", r"убыт|потер|дороже|стоимост|сколько стоит|эконом|избеж\w*\s+расход|₸|тенге"),
        ("risk", r"сломается|прогноз|здоровь|текущ\w+ состояни|риск"),
        ("cause", r"почему|причин|что сделали|починили|результат ремонт"),
        ("actions", r"что провер|что делать|приоритет|первую очередь"),
        ("tickets", r"заяв|задач|в работе|одобрен"),
        ("repeats", r"повтор|сигнал.*чаще|чаще.*сигнал"),
        ("downtime", r"сколько\W+(?:час\w*|минут\w*)|просто[ийея]|простаива|остановок.*час|суммарн.*останов"),
        ("latest", r"последн\w+\s+(?:событи|запис|полом|останов)|самая свежая|когда (?:был|останов)"),
        ("count", r"сколько.*(?:событи|запис|полом|останов)"),
    ):
        if re.search(pattern, q):
            return topic
    return "overview"


def resolve(question, catalog, history, *, is_archive, today=None):
    question = (question or "").strip()
    if not question:
        raise ValueError("Напишите вопрос.")
    if len(question) > 2000:
        raise ValueError("Сократите вопрос до 2 000 символов.")
    q = _normalize(question)
    equipment = _equipment(q, catalog, is_archive)
    try:
        period = _period(q, today or date.today(), is_archive)
    except (OverflowError, ValueError) as exc:
        if exc.args and str(exc).startswith("Укажите"):
            raise
        raise ValueError("Проверьте дату. Укажите её в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.") from None
    if period and period[0] > period[1]:
        raise ValueError("Дата начала должна быть раньше даты окончания.")
    topic = _topic(q)
    event = re.search(r"(?:\b(?:заявк|запис|событи)\w*\s*[№#]?\s*|(?<!\w)#)(\d{1,12})\b", q)
    if is_archive and event and re.search(r"\b(?:kelmarsh|келмарш|кэлмарш|турбин\w*)\s*$", q[:event.start()]):
        event = None
    code = re.search(r"\bкод\w*\s*[№#:]?\s*(\d+)\b", q) if is_archive else None
    if code and event and code.start() <= event.start() < code.end():
        event = None
    event_id = int(event[1]) if event else None
    signal_code = code[1] if code else None
    follow_up = bool(event is None and history and FOLLOW_UP_RE.search(q))
    if follow_up:
        previous = history[-1].get("context") or {}
        known = {item["id"] for item in catalog}
        if not equipment and signal_code is None:
            event_id = previous.get("event_id")
        equipment = equipment or tuple(key for key in previous.get("equipment", ()) if key in known)
        signal_code = signal_code or previous.get("code")
        topic = previous.get("topic", topic) if topic == "overview" else topic
        if previous.get("topic") == "money" and topic == "downtime" and REDUCTION_RE.search(q):
            topic = "money"
        if not period and previous.get("period"):
            period = tuple(map(date.fromisoformat, previous["period"]))
    comparison = len(equipment) > 1 or bool(re.search(r"сравн|дольше|дороже|больше всего|сам\w+ длинн", q))
    return Scope(question, equipment, period, topic, event_id, signal_code, comparison, follow_up)
