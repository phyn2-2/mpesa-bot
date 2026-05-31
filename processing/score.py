"""
mpesa-bot/processing/score.py

Confidence scoring for extracted + classified transactions.

Rules (defined in V1 scope, implemented here verbatim):

  HIGH    code + amount + type all present and type != 'unclassified'
  MEDIUM  amount + type present, no code, type != 'unclassified'
  LOW     amount present, type is 'unclassified' or missing
  FAILED  no amount extracted

These are the only four values. No fuzzy scores, no floats.
The string values match the DB CHECK constraint in schema.sql.

Why strings not an Enum:
  The DB stores 'HIGH'/'MEDIUM'/'LOW'/'FAILED' as TEXT.
  Using plain strings avoids an .value call on every DB write
  while keeping the CHECK constraint as the real enforcement.
  If this project grows, switch to StrEnum.

Inputs come from extract.py (amount, code) and classify.py (type_).
score.py has no imports from either — it takes primitives so it
can be tested and reasoned about independently.
"""

from typing import Optional


# ------------------------------------------------------------------ #
# Constants                                                           #
# ------------------------------------------------------------------ #

HIGH   = "HIGH"
MEDIUM = "MEDIUM"
LOW    = "LOW"
FAILED = "FAILED"

# Types that represent successful classification.
# 'unclassified' means classify.py could not determine a type —
# treated the same as a missing type for scoring purposes.
_CLASSIFIED_TYPES = frozenset({
    "send", "receive", "paybill", "buy_goods", "airtime"
})


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def score(
    amount:  Optional[int],
    code:    Optional[str],
    type_:   Optional[str],
) -> str:
    """
    Return confidence level for a transaction.

    Parameters
    ----------
    amount : Optional[int]
        Extracted KES amount. None means extraction failed.
    code : Optional[str]
        M-Pesa transaction reference. None is valid (some SMS omit it).
    type_ : Optional[str]
        Classified transaction type from classify.py.
        'unclassified' and None are treated identically.

    Returns
    -------
    str
        One of: 'HIGH', 'MEDIUM', 'LOW', 'FAILED'

    Examples
    --------
    >>> score(500, "QGL8X7Z2TR", "send")
    'HIGH'
    >>> score(500, None, "paybill")
    'MEDIUM'
    >>> score(500, None, "unclassified")
    'LOW'
    >>> score(None, "QGL8X7Z2TR", "send")
    'FAILED'
    """
    # No amount = cannot record a transaction at all.
    # Code and type are irrelevant — the core fact is missing.
    if amount is None:
        return FAILED

    is_classified = type_ in _CLASSIFIED_TYPES

    if not is_classified:
        # Amount present but type unknown.
        # We know money moved but not what kind.
        return LOW

    # Type is known. Presence of code determines HIGH vs MEDIUM.
    if code is not None:
        return HIGH

    return MEDIUM


def is_reliable(confidence: str) -> bool:
    """
    Return True if this confidence level is usable in spend queries.

    HIGH and MEDIUM are included in stats.
    LOW and FAILED are excluded — LOW has unknown type (can't
    correctly attribute to send/receive/paybill etc.), FAILED
    has no amount at all.

    This function is the single place that defines "reliable enough".
    The insights layer calls this rather than hardcoding the check.
    """
    return confidence in (HIGH, MEDIUM)
