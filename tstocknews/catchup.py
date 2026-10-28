"""The single user-approved late financial snapshot exception, with an expiry."""
from datetime import datetime

from .official import SourceError, TAIPEI


APPROVED_DATES = {"2026-10-28": "2026-10-29"}


def now():
    return datetime.now(TAIPEI)


def validate(day, financial_date):
    if APPROVED_DATES.get(day) != financial_date:
        raise SourceError("No approved late-financial exception for these dates")
    stamp = now().astimezone(TAIPEI)
    deadline = datetime.fromisoformat(financial_date + "T09:00:00+08:00")
    if stamp.date().isoformat() != financial_date or stamp >= deadline:
        raise SourceError("Approved catch-up must finish before the next session opens")
    return {"approved_formal_catchup": True, "late_financial_snapshot": True,
            "generated_at": stamp.isoformat(), "financial_snapshot_date": financial_date,
            "catchup_deadline": deadline.isoformat()}
