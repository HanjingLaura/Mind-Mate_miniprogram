"""Time helpers for the product's China-facing business rules.

Database timestamps remain naive UTC for compatibility with the existing schema.
Only business-day decisions and API presentation use Asia/Shanghai.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return the current time in Asia/Shanghai."""
    return datetime.now(BEIJING_TZ)


def utc_now_naive() -> datetime:
    """Return UTC in the legacy naive format used by the current MySQL tables."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def beijing_to_utc_naive(value: datetime) -> datetime:
    """Convert a Beijing/local aware datetime to the DB's naive UTC format."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_beijing(value: datetime | None) -> datetime | None:
    """Convert a legacy naive UTC database value to an aware Beijing timestamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ)
