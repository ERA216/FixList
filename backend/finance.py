"""Estimate the value of reducing recorded downtime without inventing expenses."""
from collections import defaultdict
from datetime import date, datetime, timedelta
import math
import re

from . import config, db, open_data
from .schemas import kzt, num


EXAMPLE_HOUR_RATE = 50_000
EXAMPLE_REDUCTION = 20
MAX_RATE = 1_000_000_000
REDUCTION_RE = re.compile(r"(?:сократ|уменьш|сниз)\w*[^%]{0,42}?(-?\d+(?:[.,]\d+)?)\s*%", re.I)


def parameters(question, hourly_rate=None, reduction_percent=None):
    """Use a rate or goal explicitly stated in a finance question, if present."""
    rate_match = re.search(r"(?<![\d.,])(-?\d{1,3}(?:[ \u00a0\u202f]\d{3})+|-?\d+)\s*"
                           r"(?:₸|тг\.?|тенге)\s*(?:/\s*ч(?:ас)?\b|(?:в|за)\s+час\b)", question, re.I)
    if rate_match:
        hourly_rate = int(re.sub(r"\s+", "", rate_match[1]))
    reduction_match = REDUCTION_RE.search(question)
    if reduction_match:
        reduction_percent = float(reduction_match[1].replace(",", "."))
    if hourly_rate is not None and not 0 <= hourly_rate <= MAX_RATE:
        raise ValueError("Укажите тариф от 0 до 1 000 000 000 ₸/ч.")
    if reduction_percent is not None and not 0 <= reduction_percent <= 100:
        raise ValueError("Сокращение простоя должно быть от 0 до 100%.")
    return hourly_rate, reduction_percent


def scenario(hours, *, unrated_hours=None, rated_cost=0.0, hourly_rate=None, reduction_percent=None):
    unrated = hours if unrated_hours is None else unrated_hours
    rate, reduction = parameters("", hourly_rate, reduction_percent)
    reduction = EXAMPLE_REDUCTION if reduction is None else reduction
    fallback_rate = EXAMPLE_HOUR_RATE if rate is None else rate
    cost = rated_cost + unrated * fallback_rate
    return {"hours": hours, "unrated_hours": unrated, "rated_cost_kzt": rated_cost,
            "rate_kzt": fallback_rate, "example_rate": rate is None, "reduction_percent": reduction,
            "cost_kzt": cost, "saved_hours": hours * reduction / 100, "savings_kzt": cost * reduction / 100}


def _event_start(row):
    if source := row.get("source_record"):
        return open_data._time(source["started_at"])
    at = row["occurred_at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    return at.astimezone().replace(tzinfo=None) if at.tzinfo is not None else at


def _event_end(row, start):
    if source := row.get("source_record"):
        return open_data._time(source["ended_at"])
    if row.get("log_mode") in {"manual", "csv"} and row["downtime_min"] > 0:
        return start + timedelta(minutes=row["downtime_min"])
    return None


def _clip_span(start, end, period):
    if not period:
        return start, end
    lo, hi = open_data.period_bounds(*period)
    if start.tzinfo is None:
        lo, hi = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
    return max(lo, start), min(hi, end)


def event_starts_in_period(row, period):
    return not period or period[0] <= _event_start(row).date() <= period[1]


def events_in_period(rows, period, *, include_downtime=True):
    """Select new records and, when calculating downtime, intervals continuing into the period."""
    if not period:
        return rows
    selected = []
    for row in rows:
        start = _event_start(row)
        if period[0] <= start.date() <= period[1]:
            selected.append(row)
        elif include_downtime and (end := _event_end(row, start)) is not None:
            lo, hi = _clip_span(start, end, period)
            if lo < hi:
                selected.append(row)
    return selected


def downtime_minutes_in_period(row, period):
    start = _event_start(row)
    end = _event_end(row, start)
    if end is None:
        return row["downtime_min"] if event_starts_in_period(row, period) else 0
    lo, hi = _clip_span(start, end, period)
    return max(0, (hi - lo).total_seconds()) / 60


def _recorded_hours(rows, period):
    intervals, unplaced_minutes = defaultdict(list), defaultdict(list)
    for row in rows:
        equipment, minutes = row["equipment_id"], row["downtime_min"]
        if not minutes:
            continue
        start = _event_start(row)
        end = _event_end(row, start)
        # Shift reports and inspections record submission time, not the start of downtime.
        if end is None:
            if not period or period[0] <= start.date() <= period[1]:
                unplaced_minutes[equipment].append(minutes)
            continue
        lo, hi = _clip_span(start, end, period)
        if lo < hi:
            intervals[equipment].append((lo, hi))
    return {equipment: open_data.seconds_in(open_data.merge_spans(intervals[equipment])) / 3600
            + math.fsum(unplaced_minutes[equipment]) / 60
            for equipment in intervals.keys() | unplaced_minutes.keys()}


def for_events(rows, period=None, *, hourly_rate=None, reduction_percent=None):
    """Preserve known object tariffs; apply an assumption only to unpriced downtime."""
    archived = [row["source_record"] for row in rows if row.get("source_record")]
    merged = open_data.merged_intervals(archived, *period) if period else open_data.merged_intervals(archived)
    archive_hours = math.fsum(open_data.seconds_in(spans) for spans in merged.values()) / 3600
    recorded = [row for row in rows if not row.get("source_record")]
    hours = _recorded_hours(recorded, period)
    rates = {row["equipment_id"]: row["hour_cost_kzt"] for row in recorded}
    # The same tariff applies to all records of an object. Omitted tariffs are stored as 0 or null.
    known = {equipment: rate for equipment, rate in rates.items() if rate and math.isfinite(rate)}
    missing = archive_hours + math.fsum(elapsed for equipment, elapsed in hours.items() if equipment not in known)
    rated = math.fsum(elapsed * known[equipment] for equipment, elapsed in hours.items() if equipment in known)
    return scenario(archive_hours + math.fsum(hours.values()), unrated_hours=missing, rated_cost=rated,
                    hourly_rate=hourly_rate, reduction_percent=reduction_percent)


def company_report(*, hourly_rate=None, reduction_percent=None):
    end = date.today()
    start = end - timedelta(days=config.ANALYSIS_WINDOW_DAYS - 1)
    period = (start, end)
    rows = events_in_period(db.incidents(), period)
    parts = sum(row["parts_cost_kzt"] or 0 for row in rows if event_starts_in_period(row, period))
    return {**for_events(rows, period, hourly_rate=hourly_rate, reduction_percent=reduction_percent),
            "days": config.ANALYSIS_WINDOW_DAYS, "parts_kzt": parts, "event_count": len(rows)}


def assumption_label(result):
    if not result["unrated_hours"]:
        return "По тарифам оборудования" if result["hours"] else "За выбранный период"
    rate = f"{kzt(result['rate_kzt'])} ₸/ч"
    basis = f"пример тарифа {rate}" if result["example_rate"] else f"введённый тариф {rate}"
    if result["unrated_hours"] < result["hours"]:
        return f"Тарифы оборудования и {basis} для {num(round(result['unrated_hours'], 2))} ч без тарифа"
    return basis.capitalize()


def amount(value):
    return f"≈ {kzt(value)} ₸"


def investment_effect(savings, months, investment):
    monthly = savings / months
    payback = investment / monthly if investment is not None and monthly > 0 else None
    return monthly, payback
