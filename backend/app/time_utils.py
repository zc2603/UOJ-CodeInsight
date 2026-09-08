from datetime import datetime, timezone


def ensure_utc(value: datetime) -> datetime:
    """Normalize DB datetimes; SQLite test storage drops timezone metadata."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
