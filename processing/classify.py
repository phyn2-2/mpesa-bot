"""
mpesa-bot/processing/classify.py

Transaction type classification from raw M-Pesa SMS text.

Responsibility: determine transaction type from raw text.
Does NOT extract fields — that is extract.py's job.
Does NOT score confidence — that is score.py's job.

Has zero imports from extract.py or score.py.
The pipeline (pipeline.py) wires all three together.

CLASSIFICATION STRATEGY
  Keyword-based matching on lowercased raw text.
  Simple and auditable — every rule is readable prose.
  No regex, no ML. M-Pesa keywords are stable and distinctive enough.

CHECK ORDER — order is load-bearing, do not reorder without reason:
  1. airtime    — "airtime" keyword is unambiguous, check first
  2. receive    — "you have received" / "received ksh" before send
                  (receive and send never appear together but receive
                  is checked first as the more specific pattern)
  3. send       — "sent to"
  4. paybill    — "paid to" + "account" (MORE specific than buy_goods)
  5. buy_goods  — "paid to" alone (LESS specific — must come after paybill)
  6. unclassified — explicit fallback; never raises

PAYBILL vs BUY_GOODS DISAMBIGUATION
  Both contain "paid to". The discriminating signal is "account":
    Paybill: "Ksh1,000.00 paid to KENYA POWER. Account Number 12345678"
    Buy goods: "Ksh200.00 paid to NAIVAS SUPERMARKET."
  Checking paybill before buy_goods ensures the more specific pattern
  wins. If this order is swapped, every paybill is misclassified as
  buy_goods — silent, no error, wrong data.

UNCLASSIFIED
  Returned for: agent withdrawals, M-Shwari/Fuliza loans, reversals,
  international transfers, and any future SMS format we haven't seen.
  These are stored as raw events and surfaced via /unknown.
  'unclassified' matches the DB CHECK constraint in schema.sql.
"""

from typing import Optional


# ------------------------------------------------------------------ #
# Type constants                                                      #
# ------------------------------------------------------------------ #

TYPE_SEND          = "send"
TYPE_RECEIVE       = "receive"
TYPE_PAYBILL       = "paybill"
TYPE_BUY_GOODS     = "buy_goods"
TYPE_AIRTIME       = "airtime"
TYPE_UNCLASSIFIED  = "unclassified"

# All valid classified types (excludes unclassified).
# Used by score.py to determine if a type is "known".
CLASSIFIED_TYPES = frozenset({
    TYPE_SEND, TYPE_RECEIVE, TYPE_PAYBILL, TYPE_BUY_GOODS, TYPE_AIRTIME
})


# ------------------------------------------------------------------ #
# Individual classifiers                                              #
# ------------------------------------------------------------------ #
# Each returns True if the text matches this transaction type.
# All take a pre-lowercased string — normalisation happens once
# in classify() before any of these are called.

def _is_airtime(text: str) -> bool:
    """
    Airtime: contains "airtime" keyword.
    Covers both formats:
      "Airtime purchase of Ksh50.00 for 0712345678 confirmed."
      "Ksh50.00 airtime purchased for 0712345678 on ..."
    """
    return "airtime" in text


def _is_receive(text: str) -> bool:
    """
    Receive: "you have received" OR "received ksh".
    Two patterns because Safaricom has used both over time.
    """
    return "you have received" in text or "received ksh" in text


def _is_send(text: str) -> bool:
    """
    Send money: "sent to" present in text.
    """
    return "sent to" in text


def _is_paybill(text: str) -> bool:
    """
    Paybill: "paid to" AND "account".
    Account Number marker is the discriminating signal vs buy_goods.
    Both "account number" and bare "account" are covered by "account".
    """
    return "paid to" in text and "account" in text


def _is_buy_goods(text: str) -> bool:
    """
    Buy goods (till number): "paid to" WITHOUT account marker.
    Must be checked AFTER paybill — if paybill matches, we never
    reach this function. If order is swapped, paybill is never reached.
    """
    return "paid to" in text


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def classify(raw_text: str) -> str:
    """
    Classify a raw M-Pesa SMS into a transaction type.

    Parameters
    ----------
    raw_text : str
        Raw SMS text, any case. Normalised internally — caller does
        not need to preprocess.

    Returns
    -------
    str
        One of: 'send', 'receive', 'paybill', 'buy_goods',
                'airtime', 'unclassified'

    Never raises. Unrecognised SMS returns 'unclassified'.

    Examples
    --------
    >>> classify("QGL8X7Z2TR Confirmed. Ksh500.00 sent to JOHN DOE...")
    'send'
    >>> classify("Airtime purchase of Ksh50.00 for 0712345678 confirmed.")
    'airtime'
    >>> classify("This is not an M-Pesa SMS.")
    'unclassified'
    """
    if not raw_text or not raw_text.strip():
        return TYPE_UNCLASSIFIED

    # Normalise once — all classifiers receive the same lowercased text.
    text = raw_text.lower()

    # Order is load-bearing — see module docstring before changing.
    if _is_airtime(text):
        return TYPE_AIRTIME

    if _is_receive(text):
        return TYPE_RECEIVE

    if _is_send(text):
        return TYPE_SEND

    if _is_paybill(text):
        return TYPE_PAYBILL

    if _is_buy_goods(text):
        return TYPE_BUY_GOODS

    return TYPE_UNCLASSIFIED
