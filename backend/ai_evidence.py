"""Check citations and numbers in optional AI explanations; never replace stored facts."""
import json
import re

from . import llm
from .advisor import _numbers
from .schemas import BaseModel, Field


class EvidenceQuote(BaseModel):
    source_id: int
    quote: str = Field(min_length=4, max_length=350, description="Дословный непрерывный фрагмент поля evidence_text, description или label записи с указанным source_id. Не склеивать разные поля и не менять числа.")


class GroundedReply(BaseModel):
    source_ids: list[int] = Field(min_length=1, max_length=5)
    commentary: str = Field(default="", max_length=1200)
    quotes: list[EvidenceQuote] = Field(default_factory=list, max_length=5)


def _normal(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def validate(reply, sources, facts):
    """Reject foreign IDs, invented quotes/numbers and unsupported completed-work claims."""
    by_id = {row["id"]: row for row in sources}
    if any(key not in by_id for key in reply.source_ids):
        raise llm.LLMBadOutput("unsupported citations")
    for evidence in reply.quotes:
        if evidence.source_id not in reply.source_ids:
            raise llm.LLMBadOutput("quote is not among cited records")
        record = by_id[evidence.source_id]
        if not any(_normal(evidence.quote) in _normal(value) for value in record.values() if isinstance(value, str)):
            raise llm.LLMBadOutput("unsupported source quote")
    text = reply.commentary.strip()
    if not text:
        return ""
    if not reply.quotes:
        raise llm.LLMBadOutput("explanation has no evidence")
    trusted = facts + "\n" + json.dumps(sources, ensure_ascii=False)
    if _numbers(text) - _numbers(trusted):
        raise llm.LLMBadOutput("unsupported numbers")
    if re.search(r"https?://|www\.|!\[|\]\(", text, re.I):
        raise llm.LLMBadOutput("external link in explanation")
    for reference in re.findall(r"\b(?:kelmarsh|келмарш)\s*\d+\b|\b[A-ZА-Я]{1,8}[-.]\d+(?:[-.]\d+)*\b", text, re.I):
        if _normal(reference) not in _normal(trusted):
            raise llm.LLMBadOutput("unknown equipment in explanation")
    completed = r"\b(?:заменили|заменён\w*|заменен\w*|отремонтир\w*|устранили|устранён\w*|устранен\w*|сэкономили|подтвержден\w*|подтверждён\w*)\b"
    for claim in re.finditer(completed, text, re.I):
        word = claim.group()
        if _normal(word) not in _normal(trusted):
            # A full adjective in «нет сведений о подтверждённых причинах»
            # describes missing evidence, not a confirmed repair. Keep rejecting
            # affirmative short forms and clauses following «но» or punctuation.
            if re.fullmatch(r"подтвержденн\w+", _normal(word)):
                clause = re.split(r"[.!?;,:]|\b(?:но|однако|зато)\b", _normal(text[:claim.start()]))[-1]
                if (re.search(r"\bнет\s+(?:сведений|данных|информации)\s+(?:о|об)\b", clause)
                        or re.search(r"\b(?:нет|отсутствие)\s*$", clause)):
                    continue
            raise llm.LLMBadOutput("unsupported completed work")
    return text
