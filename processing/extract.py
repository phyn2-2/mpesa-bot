"""
mpesa-bot/processing/extract.py

Field extraction from raw M-Pesa SMS text.

Responsibility: pull structured fields out of raw text.
Does NOT classify transaction type — that is classify.py's job.
Does NOT score confidence — that is score.py's job.

Design decisions documented here because they are non-obvious:

AMOUNT FORMAT
  Real M-Pesa SMS uses "Ksh" not "KES".
  Amounts include comma separators: Ksh1,500.00
  We strip commas, parse as float, convert to int (whole shillings).
  M-Pesa does not issue fractional-shilling transactions.
  int(float("1500.00")) is used deliberately — not round() — because
  we want truncation, not rounding, if a decimal ever appears.

DATE/TIME FORMAT
  M-Pesa SMS uses D/M/YY format (Kenya locale).
  "6/5/25" = 6th May 2025, NOT June 5th.
  Times are East Africa Time (EAT, UTC+3).
  We convert to UTC before storing so repository date arithmetic works.
  If conversion fails we return None — a missing timestamp is better
  than a wrong one silently corrupting date-range queries.

TRANSACTION CODE
  10-character alphanumeric, uppercase, at the start of the message.
  Pattern: [A-Z0-9]{10} at position 0.
  Some SMS types (airtime) omit the code — None is a valid result.

COUNTERPARTY
  For send: name before the phone number ("JOHN DOE 0712345678" → "JOHN DOE")
  For receive: name after "from" keyword
  For paybill/buy_goods: business name after "paid to"
  For airtime: the phone number topped up
  None is valid — not all SMS types include a clear counterparty.

REGEX PHILOSOPHY
  Use re.search not re.match — SMS text may have unexpected prefixes.
  Use re.IGNORECASE where Safaricom has inconsistent casing.
  Each extractor is a standalone function, independently testable.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


# ------------------------------------------------------------------ #
# Constants                                                           #
# ------------------------------------------------------------------ #

EAT = timezone(timedelta(hours=3))  # East Africa Time

# Transaction code: 10 uppercase alphanumeric chars at start of SMS.
_CODE_RE = re.compile(r'^([A-Z0-9]{10})\b')

# Amount: "Ksh" (case-insensitive) then digits, optional commas, optional decimals.
# Matches: Ksh500.00  Ksh1,500.00  KSh200  ksh50.00
_AMOUNT_RE = re.compile(r'[Kk][Ss][Hh]\s*([\d,]+(?:\.\d+)?)')

# Date: D/M/YY  (Kenya locale — day first)
# Time: H:MM AM/PM
_DATETIME_RE = re.compile(
    r'on\s+(\d{1,2}/\d{1,2}/\d{2})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)',
    re.IGNORECASE,
)

# Send counterparty: name (and optional phone) between "sent to" and "on"
# Captures "JOHN DOE 0712345678" then we strip trailing phone
_SEND_COUNTERPARTY_RE = re.compile(
    r'sent\s+to\s+([A-Za-z][A-Za-z\s]+?)(?:\s+\d{9,12})?\s+on',
    re.IGNORECASE,
)

# Receive counterparty: name after "from" before "on"
_RECEIVE_COUNTERPARTY_RE = re.compile(
    r'from\s+([A-Za-z][A-Za-z\s]+?)(?:\s+\d{9,12})?\s+on',
    re.IGNORECASE,
)

# Paybill/buy_goods counterparty: business name after "paid to" before period or "on"
_PAID_TO_RE = re.compile(
    r'paid\s+to\s+([A-Z][A-Z\s]+?)(?:\.|\s+on|\s+Account)',
    re.IGNORECASE,
)

# Airtime counterparty: phone number being topped up
_AIRTIME_PHONE_RE = re.compile(r'for\s+(0[17]\d{8})\b')


# ------------------------------------------------------------------ #
# Return type                                                         #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class ExtractedFields:
    """
    All fields we attempt to pull from a raw SMS.
    Every field is Optional — a failed extraction returns None,
    not an exception. Downstream (classify, score) handle nulls.

    raw_amount_str is preserved for debugging: if amount parsing
    ever produces wrong values, this shows what the regex captured
    before numeric conversion.
    """
    code:            Optional[str]   # M-Pesa ref e.g. "QGL8X7Z2TR"
    amount:          Optional[int]   # whole KES e.g. 500
    counterparty:    Optional[str]   # cleaned name or number
    transaction_at:  Optional[str]   # ISO 8601 UTC or None
    raw_amount_str:  Optional[str]   # what the regex captured before conversion


# ------------------------------------------------------------------ #
# Individual extractors                                               #
# ------------------------------------------------------------------ #

def extract_code(text: str) -> Optional[str]:
    """
    Extract M-Pesa transaction code from start of SMS.

    Returns uppercase 10-char code or None.
    Airtime SMS and some older formats omit the code — None is expected.
    """
    m = _CODE_RE.match(text.strip())
    return m.group(1) if m else None


def extract_amount(text: str) -> tuple[Optional[int], Optional[str]]:
    """
    Extract first KES amount from SMS.

    Returns (int_amount, raw_str) or (None, None).

    We take the FIRST amount because M-Pesa SMS lists the transaction
    amount first, then the new balance, then transaction cost.
    e.g. "Ksh500.00 sent... New balance Ksh1,500.00. Cost Ksh11.00"
         ↑ this one

    Returns raw_str alongside amount so callers can log what was
    captured if the numeric value looks wrong.
    """
    m = _AMOUNT_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)           # e.g. "1,500.00"
    cleaned = raw.replace(",", "")  # "1500.00"
    try:
        amount = int(float(cleaned))
        if amount <= 0:
            return None, raw   # defensive: negative or zero amounts = extraction failure
        return amount, raw
    except ValueError:
        return None, raw


def extract_transaction_at(text: str) -> Optional[str]:
    """
    Extract transaction timestamp from SMS and convert EAT → UTC.

    M-Pesa format: "on 6/5/25 at 10:23 AM"
    Parsed as D/M/YY (Kenya locale — day first, NOT month first).
    Converted from EAT (UTC+3) to UTC before returning.

    Returns ISO 8601 UTC string e.g. "2025-05-06T07:23:00Z"
    or None if parsing fails — a missing timestamp is better than
    a wrong one silently corrupting date-range queries.
    """
    m = _DATETIME_RE.search(text)
    if not m:
        return None

    date_str = m.group(1)   # "6/5/25"
    time_str = m.group(2).strip()  # "10:23 AM"

    try:
        # strptime: %d/%m/%y = day/month/2-digit-year
        dt_eat = datetime.strptime(
            f"{date_str} {time_str}", "%d/%m/%y %I:%M %p"
        ).replace(tzinfo=EAT)
        dt_utc = dt_eat.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def extract_counterparty(text: str) -> Optional[str]:
    """
    Extract counterparty name or number from SMS.

    Tries patterns in order: send → receive → paid_to → airtime phone.
    Returns cleaned string (stripped, collapsed whitespace) or None.

    This is a best-effort extraction — the result is stored for
    future V2 search features. A None here does not affect
    classification or confidence scoring.
    """
    for pattern in (
        _SEND_COUNTERPARTY_RE,
        _RECEIVE_COUNTERPARTY_RE,
        _PAID_TO_RE,
        _AIRTIME_PHONE_RE,
    ):
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip()
            # Collapse multiple spaces (names like "JOHN  DOE")
            return " ".join(raw.split())
    return None


# ------------------------------------------------------------------ #
# Composite extractor                                                 #
# ------------------------------------------------------------------ #

def extract_all(text: str) -> ExtractedFields:
    """
    Run all extractors against the raw SMS text.

    Always returns an ExtractedFields — never raises.
    Downstream code handles None fields.

    Call order is fixed: code, amount, timestamp, counterparty.
    No extractor depends on another's output.
    """
    code = extract_code(text)
    amount, raw_amount_str = extract_amount(text)
    transaction_at = extract_transaction_at(text)
    counterparty = extract_counterparty(text)

    return ExtractedFields(
        code=code,
        amount=amount,
        counterparty=counterparty,
        transaction_at=transaction_at,
        raw_amount_str=raw_amount_str,
    )
