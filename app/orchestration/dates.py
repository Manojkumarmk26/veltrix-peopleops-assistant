"""Deterministic date understanding for chat messages (Section 8.4).

Rules implemented:
* Today is ``APP_TODAY``; a date without a year means the configured default
  year (2026 per the brief).
* Weeks start on Monday. "next week" = Monday to Friday of the following week.
* "next Friday" (and a bare "Friday") = the first Friday after today.
* "this Friday" = the Friday of the current week.
* Phrases that are ambiguous (e.g. 03/04/2026, "this week", "next month" for a
  leave request) or that point to a day that has already passed (e.g.
  "yesterday", "last Monday", "15 March" when today is later) are flagged so
  the assistant asks instead of guessing.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

_MONTHS = {
    name: idx
    for idx, names in enumerate(
        [
            (),
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for name in names
}
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DAY_RE = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR_RE = r"(?:,?\s+(\d{4}))?"
_WEEKDAY_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)"
_RANGE_SEP = r"\s*(?:-|–|—|to|until|till|through|thru)\s*"

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DAY_RANGE_MONTH = re.compile(rf"\b{_DAY_RE}{_RANGE_SEP}{_DAY_RE}\s+(?:of\s+)?{_MONTH_RE}\b\.?{_YEAR_RE}", re.I)
_MONTH_DAY_RANGE = re.compile(rf"\b{_MONTH_RE}\.?\s+{_DAY_RE}{_RANGE_SEP}{_DAY_RE}\b{_YEAR_RE}", re.I)
_DAY_MONTH = re.compile(rf"\b{_DAY_RE}\s+(?:of\s+)?{_MONTH_RE}\b\.?{_YEAR_RE}", re.I)
_MONTH_DAY = re.compile(rf"\b{_MONTH_RE}\.?\s+{_DAY_RE}\b(?!\s*(?:am|pm|hours?|days?))" + _YEAR_RE, re.I)
_SLASH = re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b")
_RELATIVE = re.compile(r"\b(day after tomorrow|tomorrow|today|yesterday)\b", re.I)
_NEXT_WEEK = re.compile(r"\bnext\s+week\b", re.I)
_THIS_WEEK = re.compile(r"\b(this|current)\s+week\b", re.I)
_WEEKDAY = re.compile(rf"\b(?:(next|this|coming|last|on|until|till|to|from|through)\s+)?{_WEEKDAY_RE}\b", re.I)
_MONTH_ONLY = re.compile(rf"\b(?:in|during|for|of|throughout)\s+{_MONTH_RE}\b(?:\s+(\d{{4}}))?", re.I)
_NEXT_MONTH = re.compile(r"\b(next|this)\s+month\b", re.I)
_YEAR_ONLY = re.compile(r"\b(?:in|during|for|as of|back in)\s+(20\d{2})\b", re.I)


@dataclass
class DateMention:
    """A resolved date mention found in a message."""

    start: date
    end: date
    pos: int
    explicit_year: bool
    kind: str  # "date", "range", "relative", "weekday", "week", "month"


@dataclass
class DateParseResult:
    """Outcome of parsing the dates in a message."""

    start: Optional[date] = None
    end: Optional[date] = None
    ambiguous: bool = False
    past_phrase: bool = False
    month_only: bool = False
    invalid: bool = False
    notes: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.start is not None


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_num(token: str) -> int:
    return _MONTHS.get(token.lower().rstrip("."), 0)


def _next_weekday_after(anchor: date, weekday: int, inclusive: bool = False) -> date:
    delta = (weekday - anchor.weekday()) % 7
    if delta == 0 and not inclusive:
        delta = 7
    return anchor + timedelta(days=delta)


def find_date_mentions(
    text: str,
    today: date,
    default_year: int = 2026,
    anchor: Optional[date] = None,
) -> tuple[list[DateMention], DateParseResult]:
    """Find every date mention in ``text`` (in order of appearance)."""
    result = DateParseResult()
    mentions: list[DateMention] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    def add(span, start, end, explicit_year, kind):
        if start is None or end is None:
            result.invalid = True
            taken.append(span)
            return
        taken.append(span)
        mentions.append(DateMention(start, end, span[0], explicit_year, kind))

    for m in _ISO.finditer(text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        add(m.span(), d, d, True, "date")

    for m in _DAY_RANGE_MONTH.finditer(text):
        if not free(m.span()):
            continue
        year = int(m.group(4)) if m.group(4) else default_year
        month = _month_num(m.group(3))
        add(m.span(), _safe_date(year, month, int(m.group(1))),
            _safe_date(year, month, int(m.group(2))), bool(m.group(4)), "range")

    for m in _MONTH_DAY_RANGE.finditer(text):
        if not free(m.span()):
            continue
        year = int(m.group(4)) if m.group(4) else default_year
        month = _month_num(m.group(1))
        add(m.span(), _safe_date(year, month, int(m.group(2))),
            _safe_date(year, month, int(m.group(3))), bool(m.group(4)), "range")

    for pattern, day_group, month_group in ((_DAY_MONTH, 1, 2), (_MONTH_DAY, 2, 1)):
        for m in pattern.finditer(text):
            if not free(m.span()):
                continue
            year = int(m.group(3)) if m.group(3) else default_year
            d = _safe_date(year, _month_num(m.group(month_group)), int(m.group(day_group)))
            add(m.span(), d, d, bool(m.group(3)), "date")

    for m in _SLASH.finditer(text):
        if not free(m.span()):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year = default_year if not year_raw else (int(year_raw) + 2000 if len(year_raw) == 2 else int(year_raw))
        if a > 12 and b <= 12:
            d = _safe_date(year, b, a)
        elif b > 12 and a <= 12:
            d = _safe_date(year, a, b)
        elif a == b:
            d = _safe_date(year, a, b)
        else:
            result.ambiguous = True
            result.notes.append(f"'{m.group(0)}' could be day/month or month/day")
            taken.append(m.span())
            continue
        add(m.span(), d, d, bool(year_raw), "date")

    for m in _RELATIVE.finditer(text):
        if not free(m.span()):
            continue
        word = m.group(1).lower()
        offset = {"today": 0, "tomorrow": 1, "day after tomorrow": 2, "yesterday": -1}[word]
        d = today + timedelta(days=offset)
        if offset < 0:
            result.past_phrase = True
        add(m.span(), d, d, False, "relative")

    for m in _NEXT_WEEK.finditer(text):
        if not free(m.span()):
            continue
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        add(m.span(), monday, monday + timedelta(days=4), False, "week")

    for m in _THIS_WEEK.finditer(text):
        if free(m.span()):
            result.ambiguous = True
            result.notes.append("'this week' does not say which days")
            taken.append(m.span())

    weekday_mentions = []
    for m in _WEEKDAY.finditer(text):
        if not free(m.span()):
            continue
        qualifier = (m.group(1) or "").lower()
        weekday = _WEEKDAYS[m.group(2).lower()]
        weekday_mentions.append((m, qualifier, weekday))

    for m, qualifier, weekday in weekday_mentions:
        if qualifier == "last":
            result.past_phrase = True
            d = today - timedelta(days=((today.weekday() - weekday) % 7) or 7)
        elif qualifier == "this":
            d = today - timedelta(days=today.weekday()) + timedelta(days=weekday)
            if d < today:
                result.past_phrase = True
        elif anchor is not None and qualifier in ("until", "till", "to", "through"):
            # "make it until Friday" refers to the Friday on/after the start.
            d = _next_weekday_after(anchor, weekday, inclusive=True)
        elif mentions and mentions[-1].pos < m.start() and qualifier in ("until", "till", "to", "through"):
            d = _next_weekday_after(mentions[-1].end, weekday, inclusive=True)
        else:
            d = _next_weekday_after(today, weekday)
        add(m.span(), d, d, False, "weekday")

    for m in _MONTH_ONLY.finditer(text):
        if not free(m.span()):
            continue
        year = int(m.group(2)) if m.group(2) else default_year
        month = _month_num(m.group(1))
        if not month:
            continue
        last = calendar.monthrange(year, month)[1]
        result.month_only = True
        add(m.span(), date(year, month, 1), date(year, month, last), bool(m.group(2)), "month")

    for m in _NEXT_MONTH.finditer(text):
        if not free(m.span()):
            continue
        if m.group(1).lower() == "next":
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        else:
            year, month = today.year, today.month
        result.month_only = True
        last = calendar.monthrange(year, month)[1]
        add(m.span(), date(year, month, 1), date(year, month, last), False, "month")

    mentions.sort(key=lambda mm: mm.pos)
    return mentions, result


def parse_date_range(
    text: str,
    today: date,
    default_year: int = 2026,
    anchor: Optional[date] = None,
) -> DateParseResult:
    """Resolve the leave/date range a message refers to.

    The first mention gives the start, the last mention gives the end. A
    single mention gives a one-day (or one-week/one-month) range.
    """
    mentions, result = find_date_mentions(text, today, default_year, anchor)
    if not mentions:
        return result

    result.start = mentions[0].start
    result.end = mentions[-1].end if len(mentions) > 1 else mentions[0].end
    if len(mentions) > 1:
        result.month_only = all(m.kind == "month" for m in mentions)
    else:
        result.month_only = mentions[0].kind == "month"

    for m in mentions:
        if not m.explicit_year and m.kind in ("date", "range", "weekday", "month") and m.end < today:
            result.past_phrase = True
    return result


def extract_reference_date(text: str, today: date, default_year: int = 2026) -> Optional[date]:
    """Reference date for policy questions about a specific past date (Section 5).

    "What was the sick leave allowance in 2025?" -> 2025-12-31.
    "What applied on 1 August 2025?" -> 2025-08-01.
    Returns None when the question is about today.
    """
    mentions, _ = find_date_mentions(text, today, default_year)
    past = [m for m in mentions if m.end < today and m.kind in ("date", "range", "month")]
    if past:
        return past[0].end if past[0].kind == "month" else past[0].start

    for m in _YEAR_ONLY.finditer(text):
        year = int(m.group(1))
        if year < today.year:
            return date(year, 12, 31)
    return None
