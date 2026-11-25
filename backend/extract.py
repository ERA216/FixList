"""Шаг 1: свободный текст смены → проверенные черновики инцидентов.

ИИ извлекает поля, код проверяет их по справочнику и по исходному тексту.
Если ИИ недоступен, тот же контракт выполняет разбор по правилам (резервный режим).
"""
from collections import defaultdict
import json
import re
from datetime import date
from typing import Optional

from . import config, db, diagnose, llm, report_evidence
from .assistant_scope import CODE_TOKEN_RE
from .schemas import FAILURE_LABELS, MAX_DOWNTIME_MIN, ExtractionResult, FailureType

# порядок важен: более конкретные признаки проверяются раньше («подшипник греется» — это подшипник, а не перегрев)
FAILURE_KEYWORDS = {
    "belt_break": ["ремен", "ремн", "белдік"],
    "bearing": ["подшип", "мойынтірек", "гул", "гудит", "вибрац"],
    "sensor": ["датчик", "сенсор", "концевик"],
    "electrical": ["электр", "автомат", "предохран", "контакт", "щит", "проводк", "замыкан", "искр"],
    "lubrication": ["смаз", "масл", "майла", "теч", "потек", "подтек", "сальник"],
    "overheating": ["перегр", "греет", "горяч", "қызып", "қызды"],
    "material": ["сырь", "материал", "заготов", "шикізат"],
}
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6}

SYSTEM_PROMPT = """Ты извлекаешь инциденты оборудования из отчёта мастера смены небольшого производства.
Отчёт может быть на русском, казахском или смеси, с опечатками.

Правила:
1. equipment_id — ТОЛЬКО id из справочника ниже. Если станок не назван или не найден — null и вопрос в needs_clarification.
2. failure_type — одно значение из списка типов.
3. downtime_min — простой в минутах целым числом («1,5 часа» = 90, «полчаса» = 30). Если длительность не указана — null.
4. downtime_quote — ДОСЛОВНЫЙ фрагмент отчёта, подтверждающий длительность именно этого объекта. Если привязка неясна — null.
5. cause — дословно после явного объяснения причины («из-за пыли», «потому что…»). Иначе null.
   Вероятные причины отдельно подберёт модуль диагностики.
6. action — дословная фраза о выполненной работе на этом оборудовании. parts — явно заменённые детали.
   Не включай плановые или отрицаемые работы. Не додумывай количество деталей.
7. confidence — 0..1, насколько уверен в полях этого инцидента.
8. Ничего не выдумывай: числа, детали и причины — только из текста.

Верни JSON строго вида:
{"incidents":[{"equipment_id":"M3","failure_type":"belt_break","downtime_min":90,"downtime_quote":"простой 1,5 часа",
"parts":[{"name":"ремень","qty":1}],"cause":null,"action":"заменили ремень","confidence":0.9}],
"needs_clarification":["..."]}"""


# ---------- разбор длительностей (используется и для проверки ответа ИИ) ----------

_MINUTE_UNIT = r"(?:мин(?:ут[ауы]?)?|м)\b"
_DUR_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>"
    rf"(?:час(?:а|ов)?|ч|сағ(?:ат)?)(?:\b|(?=\d+\s*{_MINUTE_UNIT}))|{_MINUTE_UNIT})"
    r"|(?P<half>полчаса|жарты сағат)"
    r"|(?P<hourword>(?<![\d,.]\s)(?<!\d)час\b)",
    re.I,
)
_NO_DOWNTIME = re.compile(r"\b(?:простоя\s+не\s+было|без\s+простоя|нет\s+простоя|простоя\s+нет|не\s+простаива\w*)\b", re.I)
_DOWNTIME_CONTEXT = re.compile(r"\b(?:просто[йяеию]\w*|простаива\w*|стоял\w*|останов\w*|тұрды)\b", re.I)
_OTHER_ACTIVITY = re.compile(r"\b(?:осмотр\w*|обход\w*|проверк\w*|проверял\w*|проверил\w*|диагностик\w*|ремонт\w*|наладк\w*)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"[;\n!?]|,(?!\d)|\.(?!\d)|(?<!\d)[,.]")
_SIGNAL_CODE_PREFIX = re.compile(
    r"\b(?:ошибк\w*|авари\w*|сигнал\w*|предупреждени\w*)\s*(?:код\w*\s*)?[\s:=#№«\"'(–—-]*$",
    re.I,
)


def _mask_codes(text):
    def mask(match):
        token = match[0].rstrip(".")
        if any(char.isdecimal() for char in token):
            return " " * len(token) + match[0][len(token):]
        return match[0]

    return CODE_TOKEN_RE.sub(mask, text)


def parse_durations(text: str) -> list[tuple[int, str]]:
    explicit, fallback = [], []
    # Match against a length-preserving copy so identifiers cannot become durations.
    masked = _mask_codes(text)
    boundaries = [0, *(separator.end() for separator in _CLAUSE_SPLIT.finditer(masked)), len(text)]
    for left, right in zip(boundaries, boundaries[1:]):
        clause, normalized_clause = text[left:right], masked[left:right]
        if negated := _NO_DOWNTIME.search(clause):
            explicit.append((0, negated[0]))
            continue
        tokens = list(_DUR_RE.finditer(normalized_clause))
        index = 0
        while index < len(tokens):
            token = tokens[index]
            end = token.end()
            unit = (token["unit"] or "").lower()
            hours = bool(token["hourword"] or unit.startswith(("ч", "сағ")))
            if token["half"]:
                minutes = 30
            elif token["hourword"]:
                minutes = 60
            else:
                minutes = float(token["num"].replace(",", ".")) * (60 if hours else 1)
            if hours and index + 1 < len(tokens):
                next_token = tokens[index + 1]
                if (next_token["unit"] or "").lower().startswith("м") and re.fullmatch(r"\s*(?:и\s+)?", clause[end:next_token.start()], re.I):
                    minutes += float(next_token["num"].replace(",", "."))
                    end = next_token.end()
                    index += 1
            index += 1
            if not 0 <= minutes <= MAX_DOWNTIME_MIN:
                continue
            # The nearest preceding activity indicates what the duration describes.
            downtime = list(_DOWNTIME_CONTEXT.finditer(clause, 0, token.start()))
            other = list(_OTHER_ACTIVITY.finditer(clause, 0, token.start()))
            after = clause[end:].lstrip()
            downtime_suffix = _DOWNTIME_CONTEXT.match(after)
            if _OTHER_ACTIVITY.match(after) or (not downtime_suffix and other and (not downtime or other[-1].end() > downtime[-1].end())):
                continue
            candidate = (round(minutes), clause[token.start():end])
            (explicit if downtime or downtime_suffix else fallback).append(candidate)
    return explicit or fallback


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip()


def _equipment_matches(text: str, eq: dict) -> tuple[set[str], bool]:
    t = _norm(text)
    tokens = [m for m in CODE_TOKEN_RE.finditer(t) if any(char.isdecimal() for char in m[0])]
    signal_codes = {m.start() for m in tokens if _SIGNAL_CODE_PREFIX.search(t, 0, m.start())}
    mentions = set()
    for e in eq.values():
        code = re.escape(_norm(e.get("code") or e["id"]))
        mentions.update((m.start(), m.end(), True, e["id"])
                        for m in re.finditer(r"(?<![\w.-])" + code + r"(?![\w-]|\.\w)", t)
                        if m.start() not in signal_codes)
        for alias in map(_norm, e["aliases"]):
            if alias:
                suffix = r"(?!\w)" if len(alias) <= 2 or alias[-1].isdigit() else ""
                mentions.update((m.start(), m.end(), False, e["id"])
                                for m in re.finditer(r"(?<!\w)" + re.escape(alias) + suffix, t)
                                if m.start() not in signal_codes)
    patterns = [r"(?:станок|станка|станке|станку|станоктың|станокта|ст\.)\s*(?:№|n|#|номер)?\s*(\d+)",
                r"(?:№|#)\s*(\d+)\s*станок", r"(\d+)\s*[-–]?\s*(?:й|ий|ый|ой)?\s*станок"]
    for pat in patterns:
        mentions.update((m.start(), m.end(), True, f"M{m[1]}") for m in re.finditer(pat, t) if f"M{m[1]}" in eq)
    ordinal_pattern = r"(" + "|".join(ORDINALS) + r")\w*\s+станок"
    mentions.update((m.start(), m.end(), True, f"M{ORDINALS[m[1]]}")
                    for m in re.finditer(ordinal_pattern, t) if f"M{ORDINALS[m[1]]}" in eq)
    # Prefer the longer name or the exact code for the same position in the text.
    best = [(left, right, exact, equipment) for left, right, exact, equipment in mentions
            if not any(start <= left and end >= right and (start < left or end > right or (is_code and not exact))
                       for start, end, is_code, _ in mentions)]
    codes = [(left, right, equipment) for left, right, exact, equipment in best if exact]
    matches = {equipment for _, _, equipment in codes}
    aliases = defaultdict(set)
    for left, right, exact, equipment in best:
        if not exact:
            aliases[left, right].add(equipment)
    for (left, right), options in aliases.items():
        # A code next to an ambiguous name identifies which object was meant.
        nearby = {equipment for start, end, equipment in codes
                  if (end <= left and re.fullmatch(r"[\s()·—–]*", t[end:left]))
                  or (right <= start and re.fullmatch(r"[\s()·—–]*", t[right:start]))}
        matches.update(options & nearby or options)
    unmatched_code = False
    for match in tokens:
        if match.start() in signal_codes:
            continue
        token = match[0].rstrip(".")
        if not any(left <= match.start() and right >= match.start() + len(token) for left, right, _, _ in mentions):
            unmatched_code = True
            break
    return matches, unmatched_code or bool(unknown_machine_number(text, eq, mentions))


def unknown_machine_number(text: str, eq: dict, mentions=()) -> Optional[str]:
    """Номер станка, которого нет в справочнике (например «станок №9»), — чтобы честно об этом сказать."""
    m = re.search(r"(?:станок|станка|станке|ст\.)\s*(?:№|n|#)?\s*(\d+)|(\d+)\s*[-–]?\s*(?:й|ий|ый)?\s*станок", _norm(text))
    if m:
        num = m.group(1) or m.group(2)
        if f"M{num}" not in eq and not any(left <= m.start() and right >= m.end() for left, right, _, _ in mentions):
            return num
    return None


def detect_failure(text: str) -> str:
    t = _norm(text)
    for ftype, words in FAILURE_KEYWORDS.items():
        if any(w in t for w in words):
            return ftype
    return "other"


# ---------- резервный разбор по правилам ----------

def _incident_segments(raw, eq):
    segments = [s for s in re.split(r"[\n;]+|(?<=[.!?])\s+(?=[А-ЯA-ZӘҒҚҢӨҰҮҺІ])", raw) if s.strip()]
    incidents = []
    for seg in segments:
        matches, unknown = _equipment_matches(seg, eq)
        if matches or unknown or not incidents:
            incidents.append((matches, unknown, [seg]))
        else:
            incidents[-1][2].append(seg)
    return [(matches, unknown, " ".join(text)) for matches, unknown, text in incidents]


def rule_based(raw: str) -> ExtractionResult:
    out, questions = [], []
    for matches, unknown, t in _incident_segments(raw, db.equipment_map()):
        equipment_id = next(iter(matches)) if len(matches) == 1 and not unknown else None
        durs = parse_durations(t)
        if not equipment_id and not durs and detect_failure(t) == "other":
            continue  # нет ни станка, ни поломки, ни простоя — это не инцидент
        facts = report_evidence.clauses([t]) if equipment_id else []
        out.append({
            "equipment_id": equipment_id,
            "failure_type": detect_failure(t),
            "downtime_min": durs[0][0] if durs else None,
            "downtime_quote": durs[0][1] if durs else None,
            "parts": report_evidence.suggested_parts(facts),
            "cause": None,
            "action": report_evidence.first_action(facts),
            "confidence": 0.6,
        })
    if not out:
        questions.append("Не удалось найти инцидент. Опишите, какой станок остановился и на сколько.")
    return ExtractionResult.model_validate({"incidents": out, "needs_clarification": questions})


# ---------- проверка результата кодом ----------

def validate(raw: str, result: ExtractionResult) -> tuple[list[dict], list[str]]:
    eq_map = db.equipment_map()
    durations = defaultdict(list)
    evidence = defaultdict(list)
    identified = set()
    segments = _incident_segments(raw, eq_map)
    for matches, unknown, text in segments:
        identified.update(matches)
        if not matches or (len(matches) == 1 and not unknown):
            equipment_id = next(iter(matches), None)
            durations[equipment_id].extend(parse_durations(text))
            if equipment_id is not None:
                evidence[equipment_id].append(text)
    drafts, questions = [], list(result.needs_clarification)
    for inc in result.incidents:
        d = inc.model_dump(mode="json")
        issues = []
        # 1. Станок должен быть назван в записи и существовать в справочнике.
        if d["equipment_id"] not in identified:
            only = next(iter(identified)) if len(identified) == len(result.incidents) == 1 else None
            d["equipment_id"] = only if only in durations else None
        if d["equipment_id"] is None:
            issues.append("equipment")
            num = unknown_machine_number(raw, eq_map) if not identified else None
            questions.append(f"Станка №{num} нет в справочнике цеха. Выберите станок из списка или попросите администратора добавить его."
                             if num else "Какой станок остановился? Выберите из списка.")
        # 2. Подтверждаем длительность только по тексту, относящемуся к этому объекту.
        mins, quote = d.get("downtime_min"), d.get("downtime_quote")
        same = [snippet for value, snippet in durations[d["equipment_id"]] if value == mins]
        if same:
            d["downtime_quote"] = next((snippet for snippet in same if _norm(snippet) in _norm(quote)), same[0])
        else:
            if mins is not None:
                issues.append("downtime_unverified")
            d["downtime_min"] = d["downtime_quote"] = None
            issues.append("downtime")
            questions.append("Сколько минут длился простой? В тексте не нашлось подтверждения для этого оборудования.")
        facts = report_evidence.clauses(evidence[d["equipment_id"]])
        if d["cause"] and not report_evidence.confirmed_cause(d["cause"], facts):
            d["cause"] = None
            issues.append("cause_removed")
        if d["action"] and not report_evidence.confirmed_action(d["action"], facts):
            d["action"] = None
            issues.append("action_unverified")
        parts = report_evidence.confirmed_parts(d["parts"], facts)
        if len(parts) != len(d["parts"]):
            issues.append("parts_unverified")
        d["parts"] = parts
        # 3б. Тип поломки не назван — подставляем самый частый по истории станка (мастер подтверждает)
        if not db.source_profile() and d["failure_type"] == "other" and d["equipment_id"] and detect_failure(raw) == "other":
            today = date.today().isoformat()
            past = [r for r in db.incidents(d["equipment_id"]) if r["occurred_at"][:10] <= today]
            recent = [r["failure_type"] for r in past[-10:] if r["failure_type"] != "other"]
            if recent:
                d["failure_type"] = max(set(recent), key=recent.count)
                issues.append("type_from_history")
        # 4. Уверенность
        if d["confidence"] < config.CONFIDENCE_THRESHOLD:
            issues.append("low_confidence")
        d["issues"] = sorted(set(issues))
        d["needs_review"] = bool(d["issues"])
        d["failure_label"] = FAILURE_LABELS.get(d["failure_type"], d["failure_type"])
        d["equipment_name"] = eq_map[d["equipment_id"]]["name"] if d["equipment_id"] else None
        prices = [db.part_price(part["name"]) * part["qty"] for part in parts]
        d["parts_cost_kzt"] = None if db.source_profile() or "parts_unverified" in issues or 0 in prices else sum(prices)
        drafts.append(d)
    return drafts, list(dict.fromkeys(questions))


def extract(raw: str) -> dict:
    """Возвращает {drafts, questions, mode, warning}. Никогда не бросает исключение из-за ИИ."""
    raw = (raw or "").strip()
    if not raw:
            raise ValueError("Пустой текст смены. Опишите, что произошло с оборудованием.")
    if len(raw) > 4000:
        raise ValueError("Текст слишком длинный (больше 4000 символов). Разбейте отчёт на несколько записей.")
    warning, mode = None, "ai"
    try:
        eq_list = json.dumps([{k: e[k] for k in ("id", "name", "line", "aliases")} for e in db.load_equipment()], ensure_ascii=False)
        types = ", ".join(f"{t.value} ({FAILURE_LABELS[t.value]})" for t in FailureType)
        user = f"Справочник оборудования:\n{eq_list}\n\nТипы неисправностей: {types}\n\nОтчёт мастера:\n\"\"\"{raw}\"\"\""
        result = llm.call_json(SYSTEM_PROMPT, user, ExtractionResult, "extract")
    except llm.LLMError as e:
        result, mode = rule_based(raw), "rules"
        warning = None if not config.ai_enabled() else e.user_message
    drafts, questions = validate(raw, result)
    for d in drafts:  # шаг 1б: гипотезы о причине по симптомам и истории станка
        if d["equipment_id"] and not db.source_profile():
            dg = diagnose.diagnose(raw, d["equipment_id"], d["failure_type"])
            d["hypotheses"], d["diagnosis_mode"], d["history"] = dg["hypotheses"], dg["mode"], dg["history"]
        else:
            d["hypotheses"], d["diagnosis_mode"], d["history"] = [], None, None
    return {"drafts": drafts, "questions": questions, "mode": mode, "warning": warning}
