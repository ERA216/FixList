"""Verify reported causes, completed work, and replacements against the log."""
import re


_CLAUSE_SPLIT = re.compile(r"[;\n!?]|,(?!\d)|(?<!шт)(?<!ед)\.(?!\d)|(?<!\d),", re.I)
_CAUSE = re.compile(r"\b(?:из-за|вследствие|по причине|потому что|причина(?:\s+в)?|себебі)\b")
_REPLACEMENT = (
    r"(?:заменил[аио]?|поменял[аио]?|поставил[аио]?|установил[аио]?|"
    r"заменен[аоы]?|ауыстырды(?:қ)?|орнатты(?:қ)?)"
)
_REPLACED = re.compile(rf"\b{_REPLACEMENT}\b")
_COMPLETED = re.compile(
    rf"\b(?:{_REPLACEMENT}|(?:подтяну|отрегулирова|очисти|почисти|провери|"
    r"осмотре|восстанови|отремонтирова|налади|перезапусти|устрани|"
    r"смаза|протяну|запусти|проду)л[аио]?|тазалады(?:қ)?|тексерді(?:к)?)\b"
)
_NON_ASSERTED = re.compile(r"\b(?:не|нет|нужно|надо|можно|возможно|планируем|предлагали|предложили|якобы)\b(?:\s+\w+){0,3}\s*$")
_CONDITIONAL = re.compile(r"\s+(?:бы|б)\b")
_BROKEN = re.compile(r"\b(?:порвал(?:ся|ась|ось|ись)|сгорел[аио]?|сломал(?:ся|ась|ось|ись)|поврежден[аоы]?|вышел из строя)\b")
_PART_FORMS = {
    "ремень": r"ремень|ремня|ремни|ремней|белдік",
    "датчик": r"датчик(?:а|и|ов)?",
    "подшипник": r"подшипник(?:а|и|ов)?",
    "предохранитель": r"предохранител(?:ь|я|и|ей)",
    "контактор": r"контактор(?:а|ы|ов)?",
    "сальник": r"сальник(?:а|и|ов)?",
    "масло": r"масло",
    "смазка": r"смазк[ауи]",
}
_NUMBER_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одно": 1, "два": 2,
    "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
_NUMBER = r"(?:\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")"
_OBJECT_SPLIT = re.compile(r"\s+(?:и|а также)\s+")


def _normalize(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def _phrase(text):
    return _normalize(text).strip(" «»\"'.,;:!?–—-")


def clauses(texts):
    # A blank clause stops pronouns from referring to a different occurrence.
    return [piece for text in texts for piece in (*_CLAUSE_SPLIT.split(text), "")]


def confirmed_cause(cause, facts):
    phrase = _phrase(cause)
    if not phrase:
        return False
    match = re.compile(re.escape(phrase) + r"(?!\w)")
    for raw in facts:
        fact = _phrase(raw)
        for marker in _CAUSE.finditer(fact):
            if not _NON_ASSERTED.search(fact[:marker.start()]) and match.match(fact[marker.end():].lstrip(" :–—-")):
                return True
    return False


def _asserted(fact, verb):
    return not _NON_ASSERTED.search(fact[:verb.start()]) and not _CONDITIONAL.match(fact, verb.end())


def _completed(fact):
    return any(_asserted(fact, verb) for verb in _COMPLETED.finditer(fact))


def confirmed_action(action, facts):
    phrase = _phrase(action)
    if not _completed(phrase):
        return False
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
    for raw in facts:
        fact = _phrase(raw)
        if any(not _NON_ASSERTED.search(fact[:match.start()]) for match in pattern.finditer(fact)):
            return True
    return False


def first_action(facts):
    return next((fact.strip() for fact in facts if _completed(_phrase(fact))), None)


def _part_name(text):
    name = _phrase(text)
    name = re.sub(r"^нов(?:ый|ая|ое|ые|ую|ого|ых)\s+", "", name)
    return next((key for key, pattern in _PART_FORMS.items() if re.fullmatch(pattern, name)), name)


def _replacement_matches(name, facts):
    if not name:
        return
    term = _PART_FORMS.get(name, re.escape(name))
    object_match = re.compile(rf"(?:(?P<count>{_NUMBER})\s+(?:(?:шт|ед)\.?\s+)?)?"
                              rf"(?:нов\w*\s+)?(?P<name>{term})(?!\w)")
    for index, raw in enumerate(facts):
        fact = _phrase(raw)
        for verb in _REPLACED.finditer(fact):
            if not _asserted(fact, verb):
                continue
            tail = fact[verb.end():].lstrip(" :–—-")
            objects = _OBJECT_SPLIT.split(tail)
            if tail in {"", "его", "ее", "новый", "новую", "новое"} and name in _PART_FORMS:
                # Accept a single named part for shorthand like «порвался ремень. Поставили новый».
                before = fact[:verb.start()].rstrip()
                previous = _phrase(facts[index - 1]) if index else ""
                previous_names = {key for key, forms in _PART_FORMS.items()
                                  if re.search(rf"(?<!\w)(?:{forms})(?!\w)", previous)}
                if object_match.fullmatch(before):
                    objects = [before]
                elif not before and previous_names == {name} and _BROKEN.search(previous) and re.search(
                    rf"(?<!\w)(?:{re.escape(name)}|белдік)(?!\w)", previous
                ):
                    objects = [name]
            for position, item in enumerate(objects):
                match = object_match.match(item)
                if not match or re.match(r"\s*(?:обратно|на место)\b", item[match.end():]):
                    continue
                count = match["count"]
                if count:
                    quantity = int(count) if count.isdecimal() else _NUMBER_WORDS[count]
                elif name in _PART_FORMS and match["name"] in (name, "белдік", "смазку"):
                    quantity = 1
                else:
                    continue
                if 1 <= quantity <= 100:
                    yield (index, verb.start(), position), quantity


def confirmed_parts(parts, facts):
    confirmed, used, names = [], set(), set()
    for part in parts:
        name = _part_name(part["name"])
        if name in names:
            continue
        for event, quantity in _replacement_matches(name, facts):
            if event not in used and quantity == part["qty"]:
                confirmed.append({"name": name if name in _PART_FORMS else part["name"].strip(), "qty": quantity})
                used.add(event)
                names.add(name)
                break
    return confirmed


def suggested_parts(facts):
    parts, used = [], set()
    for name in _PART_FORMS:
        for event, quantity in _replacement_matches(name, facts):
            if event not in used:
                parts.append({"name": name, "qty": quantity})
                used.add(event)
                break
    return parts
