"""
mpesa-bot/insights/stats.py

Spend summary: fetches and computes the numbers shown after each
transaction and on /stats.

Architecture — two functions, separate concerns:

  compute_summary(today, yesterday, week)
    Pure function. Takes integers, returns SpendSummary.
    No DB. No side effects. All delta logic lives here.
    Independently testable without any infrastructure.

  get_summary(user_id)
    Calls repository for the three spend totals, then calls
    compute_summary. The only function in this module that
    touches the DB.

stats.py does NOT format Telegram messages.
Formatting is handlers.py's responsibility. stats.py returns
typed data so the same numbers can be rendered differently
(Telegram, logs, future web UI) without touching this file.

DELTA CASES — all handled explicitly, none allowed to crash:

  today=0, yesterday=0  →  delta_pct=None, direction=None
                            No spend either day, delta is meaningless.

  today>0, yesterday=0  →  delta_pct=None, direction=None
                            First spend today. Division by zero.
                            Undefined, not infinite.

  today=0, yesterday>0  →  delta_pct=-100.0, direction="down"
                            Spent nothing today vs something yesterday.

  today==yesterday>0    →  delta_pct=0.0,    direction="flat"
  today>yesterday>0     →  delta_pct=+N.N,   direction="up"
  today<yesterday,      →  delta_pct=-N.N,   direction="down"
    both>0
"""

from dataclasses import dataclass
from typing import Optional

from db.repository import (
    get_spend_last_7_days,
    get_spend_today,
    get_spend_yesterday,
)


# ------------------------------------------------------------------ #
# Result type                                                         #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class SpendSummary:
    """
    Computed spend summary for one user.

    today:      KES spent today (Nairobi calendar day, EAT)
    yesterday:  KES spent yesterday
    week:       KES spent in the last 7 days (including today)
    delta_pct:  % change today vs yesterday, rounded to 1 decimal.
                None when yesterday=0 (undefined, not infinite).
    direction:  "up", "down", "flat", or None (when delta_pct is None).

    All amounts in whole KES (integers). Never negative.
    delta_pct can be negative (spend went down).
    """
    today:      int
    yesterday:  int
    week:       int
    delta_pct:  Optional[float]
    direction:  Optional[str]


# ------------------------------------------------------------------ #
# Pure compute (no DB)                                                #
# ------------------------------------------------------------------ #

def compute_summary(today: int, yesterday: int, week: int) -> SpendSummary:
    """
    Compute a SpendSummary from raw spend totals.

    Pure function — no DB, no side effects.
    All delta edge cases handled here.

    Parameters
    ----------
    today     : KES spent today (>= 0)
    yesterday : KES spent yesterday (>= 0)
    week      : KES spent in the last 7 days (>= 0)

    Returns
    -------
    SpendSummary with delta_pct and direction computed.
    """
    # Defensive: clamp negatives to 0. Repository guarantees non-negative
    # via COALESCE(SUM(...), 0) but we defend here as well.
    today     = max(0, today)
    yesterday = max(0, yesterday)
    week      = max(0, week)

    delta_pct: Optional[float]
    direction: Optional[str]

    if yesterday == 0:
        # Division by zero — and also: if yesterday=0, today=0, there
        # is no meaningful comparison. If today>0, it's "first spend"
        # which is not a percentage increase over nothing.
        delta_pct = None
        direction = None
    elif today == yesterday:
        delta_pct = 0.0
        direction = "flat"
    else:
        raw = (today - yesterday) / yesterday * 100
        delta_pct = round(raw, 1)
        direction = "up" if today > yesterday else "down"

    return SpendSummary(
        today=today,
        yesterday=yesterday,
        week=week,
        delta_pct=delta_pct,
        direction=direction,
    )


# ------------------------------------------------------------------ #
# DB-backed summary                                                   #
# ------------------------------------------------------------------ #

def get_summary(user_id: str) -> SpendSummary:
    """
    Fetch spend totals from the DB and compute summary.

    Timezone note: all repository spend queries apply a +3 hours
    EAT offset internally. "Today" and "yesterday" here are
    Nairobi calendar days, not UTC days.

    Parameters
    ----------
    user_id : str
        Telegram chat ID. Must match raw_events.user_id.

    Returns
    -------
    SpendSummary
    """
    today     = get_spend_today(user_id)
    yesterday = get_spend_yesterday(user_id)
    week      = get_spend_last_7_days(user_id)
    return compute_summary(today, yesterday, week)
