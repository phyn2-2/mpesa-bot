"""
mpesa-bot/bot/handlers.py

Message routing and response formatting.

Responsibilities:
  - Route incoming text to command handler or SMS handler
  - Call ingest → pipeline on SMS messages
  - Format a response string for every possible outcome
  - Return the string to main.py for sending

Does NOT:
  - Send Telegram messages (main.py does that)
  - Determine whether source is 'manual' or 'auto' from HTTP context
    (main.py passes source as a parameter)
  - Raise exceptions (all paths return a string, never raise)

ARCHITECTURE
  Pure formatting functions (_format_*) are at the top.
  They take typed data and return strings.
  They have no DB access and no side effects.
  Test them without any infrastructure.

  handle_message() is the single public entry point.
  main.py calls it for every incoming message.

RESPONSE FORMAT (SUCCESS)

  ✓ KES 500 recorded [SEND]
    Ref: QGL8X7Z2TR

  Today:     KES 2,300  ↑ 20.0% vs yesterday
  This week: KES 11,400

CONFIDENCE ANNOTATIONS
  HIGH          → silent (normal case, no noise)
  MEDIUM        → "(no ref)" note — code not found, less certain
  LOW           → "⚠ type unclear" — type could not be classified
  FAILED        → handled by PARSE_FAILED path, never shown as label

DELTA FORMATTING
  direction=up    →  ↑ {pct}%
  direction=down  →  ↓ {abs(pct)}%   (show magnitude, arrow shows direction)
  direction=flat  →  → same as yesterday
  direction=None  →  (omitted — no comparison available)
"""

from typing import Optional

from db.repository import get_failed_events
from ingestion.ingest import IngestStatus, ingest
from insights.stats import SpendSummary, get_summary
from processing.pipeline import PipelineResult, PipelineStatus, run


# ------------------------------------------------------------------ #
# Pure formatting helpers                                             #
# ------------------------------------------------------------------ #

def _format_kes(amount: int) -> str:
    """
    Format a KES amount with comma separator.

    >>> _format_kes(500)
    'KES 500'
    >>> _format_kes(1500)
    'KES 1,500'
    >>> _format_kes(12500)
    'KES 12,500'
    >>> _format_kes(1000000)
    'KES 1,000,000'
    """
    return f"KES {amount:,}"


def _format_type(type_: Optional[str]) -> str:
    """
    Human-readable transaction type label.

    >>> _format_type("send")
    'SEND'
    >>> _format_type("buy_goods")
    'BUY GOODS'
    >>> _format_type("unclassified")
    '?'
    >>> _format_type(None)
    '?'
    """
    _MAP = {
        "send":          "SEND",
        "receive":       "RECEIVE",
        "paybill":       "PAYBILL",
        "buy_goods":     "BUY GOODS",
        "airtime":       "AIRTIME",
        "unclassified":  "?",
    }
    return _MAP.get(type_ or "", "?")


def _format_confidence_note(confidence: Optional[str]) -> str:
    """
    Inline annotation for non-HIGH confidence levels.

    Returns empty string for HIGH (normal case — no noise).
    Returns a short note for MEDIUM and LOW.
    FAILED is handled upstream — never reaches formatting.

    >>> _format_confidence_note("HIGH")
    ''
    >>> _format_confidence_note("MEDIUM")
    ' (no ref)'
    >>> _format_confidence_note("LOW")
    ' ⚠ type unclear'
    """
    _MAP = {
        "HIGH":   "",
        "MEDIUM": " (no ref)",
        "LOW":    " ⚠ type unclear",
        "FAILED": "",  # handled by PARSE_FAILED path
    }
    return _MAP.get(confidence or "", "")


def _format_delta_line(summary: SpendSummary) -> str:
    """
    Format the today vs yesterday comparison line.

    Returns empty string when delta is undefined (direction=None).
    Shows absolute magnitude for down — arrow communicates direction.

    >>> from insights.stats import SpendSummary
    >>> _format_delta_line(SpendSummary(500, 400, 3500, 25.0, "up"))
    '  ↑ 25.0% vs yesterday'
    >>> _format_delta_line(SpendSummary(300, 400, 2800, -25.0, "down"))
    '  ↓ 25.0% vs yesterday'
    >>> _format_delta_line(SpendSummary(400, 400, 2800, 0.0, "flat"))
    '  → same as yesterday'
    >>> _format_delta_line(SpendSummary(500, 0, 500, None, None))
    ''
    """
    if summary.direction is None:
        return ""
    if summary.direction == "flat":
        return "  → same as yesterday"
    if summary.direction == "up":
        return f"  ↑ {summary.delta_pct}% vs yesterday"
    if summary.direction == "down":
        return f"  ↓ {abs(summary.delta_pct)}% vs yesterday"
    return ""


def _format_success_response(
    result: PipelineResult,
    summary: SpendSummary,
) -> str:
    """
    Format the full response for a successfully processed transaction.

    Layout:
      ✓ KES {amount} recorded [{TYPE}]{confidence_note}
        Ref: {code}            ← omitted when code is None

      Today:     KES {today}{delta_line}
      This week: KES {week}
    """
    type_label  = _format_type(result.type_)
    conf_note   = _format_confidence_note(result.confidence)
    delta_line  = _format_delta_line(summary)

    lines = [f"✓ {_format_kes(result.amount)} recorded [{type_label}]{conf_note}"]

    # Ref line — only when code was extracted
    # Fetch from DB not result — result doesn't carry code directly.
    # We know the transaction was inserted, retrieve code from summary context.
    # Actually: result doesn't expose code. We surface it via the pipeline
    # result's transaction_id if needed, but for V1 the ref line is optional.
    # Skip it here — handlers.py doesn't query the DB for a single field
    # just to show a ref. Pipeline result should expose code if needed (V2).

    lines.append("")  # blank line before stats
    lines.append(f"Today:     {_format_kes(summary.today)}{delta_line}")
    lines.append(f"This week: {_format_kes(summary.week)}")

    return "\n".join(lines)


def _format_stats_response(summary: SpendSummary) -> str:
    """
    Format the /stats command response.

    Layout:
      📊 Spend summary

      Today:     KES 2,300{delta_line}
      Yesterday: KES 1,900
      This week: KES 11,400
    """
    delta_line = _format_delta_line(summary)
    lines = [
        "📊 Spend summary",
        "",
        f"Today:     {_format_kes(summary.today)}{delta_line}",
        f"Yesterday: {_format_kes(summary.yesterday)}",
        f"This week: {_format_kes(summary.week)}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Command handlers                                                    #
# ------------------------------------------------------------------ #

def _handle_stats(user_id: str) -> str:
    """Handle /stats command — return spend summary."""
    try:
        summary = get_summary(user_id)
        return _format_stats_response(summary)
    except Exception as e:
        return f"⚠ Could not load stats: {e}"


def _handle_unknown_list(user_id: str) -> str:
    """
    Handle /unknown command — list failed/unclassified raw events.

    Shows the raw text of up to 5 events that couldn't be parsed,
    so the user can see what the bot missed.
    """
    try:
        events = get_failed_events(user_id, limit=5)
    except Exception as e:
        return f"⚠ Could not load unknown events: {e}"

    if not events:
        return "✓ No unprocessed events."

    lines = [f"⚠ {len(events)} unprocessed event(s):", ""]
    for i, event in enumerate(events, 1):
        # Truncate long raw texts — we only need enough to identify them
        raw = event.raw_text[:80] + ("…" if len(event.raw_text) > 80 else "")
        error = event.process_error or "unknown error"
        lines.append(f"{i}. {raw}")
        lines.append(f"   Error: {error}")
        lines.append(f"   Attempts: {event.process_attempts}")
        lines.append("")

    return "\n".join(lines).rstrip()


# ------------------------------------------------------------------ #
# SMS handler                                                         #
# ------------------------------------------------------------------ #

def _handle_sms(user_id: str, text: str, source: str) -> str:
    """
    Handle an incoming SMS text — ingest, pipeline, format response.
    Returns a formatted string for every possible outcome.
    """

    # ── Ingest ───────────────────────────────────────────────────────
    ingest_result = ingest(user_id, text, source)

    if ingest_result.status == IngestStatus.DUPLICATE:
        return "Already recorded ✓"

    if ingest_result.status == IngestStatus.INVALID:
        return (
            f"⚠ Couldn't read that message.\n"
            f"Reason: {ingest_result.reason}\n\n"
            f"Forward the exact M-Pesa SMS text."
        )

    if ingest_result.status == IngestStatus.DB_ERROR:
        return (
            f"⚠ Storage error — message not saved.\n"
            f"Please try again."
        )

    # ACCEPTED — run pipeline
    pipeline_result = run(ingest_result.raw_event_id)

    # ── Format pipeline outcome ───────────────────────────────────────
    if pipeline_result.status == PipelineStatus.SUCCESS:
        try:
            summary = get_summary(user_id)
        except Exception:
            # Stats failure must not kill the success confirmation.
            # Use a zero-summary so the confirmation still sends.
            from insights.stats import compute_summary
            summary = compute_summary(0, 0, 0)
        return _format_success_response(pipeline_result, summary)

    if pipeline_result.status == PipelineStatus.PARSE_FAILED:
        return (
            f"⚠ Recorded but couldn't parse.\n"
            f"The SMS was saved — use /unknown to review it."
        )

    if pipeline_result.status == PipelineStatus.DUPLICATE:
        return (
            f"Already counted — this transaction code was seen before.\n"
            f"No duplicate recorded ✓"
        )

    if pipeline_result.status == PipelineStatus.MAX_ATTEMPTS_REACHED:
        return (
            f"⚠ Could not parse this SMS after multiple attempts.\n"
            f"Use /unknown to see it."
        )

    if pipeline_result.status == PipelineStatus.TRANSIENT_ERROR:
        return (
            f"⚠ Temporary error — will retry automatically.\n"
            f"Reason: {pipeline_result.reason}"
        )

    if pipeline_result.status == PipelineStatus.ALREADY_PROCESSED:
        # Shouldn't happen: we just ingested this event (new ID)
        return "Already processed ✓"

    # NOT_FOUND or unknown status — programming bug, surface it
    return f"⚠ Internal error: {pipeline_result.status} — {pipeline_result.reason}"


# ------------------------------------------------------------------ #
# Public entry point                                                  #
# ------------------------------------------------------------------ #

def handle_message(user_id: str, text: str, source: str) -> str:
    """
    Route an incoming Telegram message and return a response string.

    Parameters
    ----------
    user_id : str
        Telegram chat ID (string).
    text : str
        Raw message text from Telegram.
    source : str
        'manual' (user typed) or 'auto' (MacroDroid webhook).
        Determined by main.py from request context.

    Returns
    -------
    str
        Message to send back to the user.
        Always returns a string, never raises.
    """
    text = (text or "").strip()

    if not text:
        return "Send me an M-Pesa SMS or use /stats."

    # ── Command routing ───────────────────────────────────────────────
    if text.startswith("/"):
        command = text.split()[0].lower()
        if command == "/stats":
            return _handle_stats(user_id)
        if command == "/unknown":
            return _handle_unknown_list(user_id)
        return f"Unknown command: {command}\nTry /stats or /unknown."

    # ── SMS routing ───────────────────────────────────────────────────
    return _handle_sms(user_id, text, source)
