from datetime import date, timedelta

LOAN_DAYS = 14
DAILY_FINE = 0.25


def due_date(start: date) -> date:
    return start + timedelta(days=LOAN_DAYS)


def late_fee(due: date, returned: date) -> float:
    days_late = (returned - due).days
    if days_late <= 0:
        return 0.0
    return round(days_late * DAILY_FINE, 2)
