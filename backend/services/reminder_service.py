"""Deterministic parsing and persistence for one-time chat reminders.

LLMs are deliberately not allowed to decide whether a reminder exists.  A
future notification is real only after this module has parsed it and persisted
one ``ScheduledReminder`` row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ScheduledReminder
from services.time_service import BEIJING_TZ, beijing_now, beijing_to_utc_naive


_NUMBER_TOKEN = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
_REMINDER_INTENT_RE = re.compile(
    r"(?:提醒(?:我(?:一下|下)?|一下我|下我)|(?:叫|喊|通知)我|到(?:时|点)(?:提醒|叫|喊)我)"
)
_PAST_COMPLAINT_RE = re.compile(
    r"(?:"
    r"(?:没|没有|并没|为啥没|为什么没|怎么没|你没|你没有|没收到).{0,12}提醒我"
    r"|(?:你\s*)?(?:不是\s*)?(?:之前|刚才|昨天|早先)?\s*"
    r"(?:说|说过|答应|答应过|承诺|承诺过|讲|讲过|说好).{0,24}提醒我"
    r"|提醒我(?:过|了)(?:没有|没|吗|么)?[?？]?\s*$"
    r"|你\s*(?:是不是|是否).{0,24}提醒我(?:过|了)?[?？]?\s*$"
    r"|(?:你\s*)?(?:有|到底).{0,24}提醒我(?:吗|没|没有)?[?？]?\s*$"
    r"|提醒我(?:没|没有)[?？]?\s*$"
    r")"
)
_NEGATED_REMINDER_RE = re.compile(
    r"(?:不要|别|不用|取消|不需要).{0,16}(?:提醒我|叫我|喊我|通知我)"
)
_RELATIVE_TIME_RE = re.compile(
    rf"(?P<number>{_NUMBER_TOKEN}|半)\s*(?P<unit>分钟|分|小时|钟头)\s*(?:以后|之后|后)"
)
_ABSOLUTE_TIME_RE = re.compile(
    rf"(?:(?P<day>今天|明天|后天)\s*)?"
    rf"(?:(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|今晚)\s*)?"
    rf"(?P<hour>{_NUMBER_TOKEN})\s*"
    rf"(?:(?:点|时)\s*(?:(?P<half>半)|(?P<minute>{_NUMBER_TOKEN})?\s*分?)?|"
    rf"[:：]\s*(?P<colon_minute>{_NUMBER_TOKEN}))"
)


@dataclass(frozen=True)
class ReminderParseResult:
    is_intent: bool
    scheduled_at: datetime | None = None
    content: str = ""
    error: str = ""


def _chinese_number(token: str) -> int:
    token = (token or "").strip()
    if not token:
        raise ValueError("empty number")
    if token.isdigit():
        return int(token)
    token = token.replace("两", "二").replace("〇", "零")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in token:
        left, right = token.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    # "零二" is common when dictating minutes.
    value = 0
    for char in token:
        if char not in digits:
            raise ValueError(f"unsupported number: {token}")
        value = value * 10 + digits[char]
    return value


def _extract_content(text: str, time_match: re.Match) -> str:
    cleaned = text
    cleaned = cleaned[:time_match.start()] + " " + cleaned[time_match.end():]
    cleaned = _REMINDER_INTENT_RE.sub(" ", cleaned)
    cleaned = re.sub(
        r"(?:你能不能|你能|能不能|可以不可以|可以|请|麻烦|帮忙|帮我|记得|到时候|到时)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"[，,。.!！?？:：;；]", " ", cleaned)
    cleaned = re.sub(r"^(?:我|要|去|该|需要)\s*", "", cleaned.strip())
    cleaned = re.sub(r"(?:可以吗|行吗|好吗|么|吗|吧)$", "", cleaned.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "回来看看今天的任务"


def _apply_period(hour: int, period: str) -> int:
    # Chinese 12-hour expressions need explicit 0/12 handling.  In
    # particular, "今晚12点" is midnight, never noon.
    if period in {"晚上", "今晚"} and hour in {0, 12}:
        return 0
    if period in {"下午", "傍晚", "晚上", "今晚"} and 0 < hour < 12:
        return hour + 12
    if period == "中午" and hour < 11:
        return hour + 12
    if period in {"凌晨", "早上", "上午"} and hour == 12:
        return 0
    return hour


def parse_reminder_request(
    text: str,
    now: datetime | None = None,
) -> ReminderParseResult:
    """Parse an explicit future reminder request in Beijing time.

    The parser intentionally returns ``is_intent=False`` for complaints such
    as "为什么没提醒我", so ordinary conversation can still answer them.
    """
    text = (text or "").strip()
    if not text or not _REMINDER_INTENT_RE.search(text) or _PAST_COMPLAINT_RE.search(text):
        return ReminderParseResult(is_intent=False)
    if _NEGATED_REMINDER_RE.search(text):
        return ReminderParseResult(is_intent=True, error="negated")

    time_matches = list(_RELATIVE_TIME_RE.finditer(text)) + list(_ABSOLUTE_TIME_RE.finditer(text))
    if len(time_matches) > 1:
        return ReminderParseResult(is_intent=True, error="multiple_times")

    now = now or beijing_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)
    else:
        now = now.astimezone(BEIJING_TZ)

    relative = _RELATIVE_TIME_RE.search(text)
    if relative:
        number_token = relative.group("number")
        unit = relative.group("unit")
        if number_token == "半":
            amount = 30 if unit in {"分钟", "分"} else 0.5
        else:
            try:
                amount = _chinese_number(number_token)
            except ValueError:
                return ReminderParseResult(is_intent=True, error="invalid_time")
        delta = timedelta(minutes=amount if unit in {"分钟", "分"} else amount * 60)
        if delta <= timedelta(0) or delta > timedelta(days=30):
            return ReminderParseResult(is_intent=True, error="invalid_time")
        scheduled = now + delta
        return ReminderParseResult(
            is_intent=True,
            scheduled_at=beijing_to_utc_naive(scheduled),
            content=_extract_content(text, relative),
        )

    absolute = _ABSOLUTE_TIME_RE.search(text)
    if not absolute:
        return ReminderParseResult(is_intent=True, error="missing_time")

    try:
        hour = _chinese_number(absolute.group("hour"))
        minute_token = absolute.group("minute") or absolute.group("colon_minute")
        minute = 30 if absolute.group("half") else (_chinese_number(minute_token) if minute_token else 0)
    except ValueError:
        return ReminderParseResult(is_intent=True, error="invalid_time")

    period = absolute.group("period") or ""
    hour = _apply_period(hour, period)
    if hour > 23 or minute > 59:
        return ReminderParseResult(is_intent=True, error="invalid_time")

    day_word = absolute.group("day")
    day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day_word, 0)
    target_date = (now + timedelta(days=day_offset)).date()
    raw_hour = _chinese_number(absolute.group("hour"))
    # "明天晚上12点" means the midnight at the end of tomorrow.  Without
    # an explicit day ("今晚12点"), the normal next-occurrence rule below
    # already advances to the coming midnight.
    if day_word and period in {"晚上", "今晚"} and raw_hour == 12:
        target_date += timedelta(days=1)
    scheduled = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=BEIJING_TZ,
    )

    if scheduled <= now:
        if day_word:
            return ReminderParseResult(is_intent=True, error="past_time")
        # Without an explicit date, use the next occurrence of that clock time.
        scheduled += timedelta(days=1)

    return ReminderParseResult(
        is_intent=True,
        scheduled_at=beijing_to_utc_naive(scheduled),
        content=_extract_content(text, absolute),
    )


def create_scheduled_reminder(
    db: Session,
    *,
    user_id: int,
    source_message_id: int,
    idempotency_key: str,
    content: str,
    scheduled_at: datetime,
    commit: bool = True,
) -> ScheduledReminder:
    """Persist exactly one reminder per source chat message."""
    idempotency_key = (idempotency_key or f"message:{source_message_id}").strip()[:128]
    existing = db.query(ScheduledReminder).filter(
        ScheduledReminder.user_id == user_id,
        ScheduledReminder.idempotency_key == idempotency_key,
    ).first()
    if existing:
        return existing

    reminder = ScheduledReminder(
        user_id=user_id,
        source_message_id=source_message_id,
        idempotency_key=idempotency_key,
        content=(content or "回来看看今天的任务").strip(),
        scheduled_at=scheduled_at,
        status="pending",
    )
    db.add(reminder)
    try:
        db.flush()
        if commit:
            db.commit()
            db.refresh(reminder)
        return reminder
    except IntegrityError:
        db.rollback()
        existing = db.query(ScheduledReminder).filter(
            ScheduledReminder.user_id == user_id,
            or_(
                ScheduledReminder.idempotency_key == idempotency_key,
                ScheduledReminder.source_message_id == source_message_id,
            ),
        ).order_by(ScheduledReminder.id.asc()).first()
        if existing is None:
            raise
        return existing
