"""
Parses whatever gets typed into the quick-entry score field: either a plain
number (the score/time), or one of a small set of typed codes for common
non-numeric outcomes. Built for speed during live action -- no mouse, no
dropdown, just type and hit Enter.
"""

# code -> (status key, short display shown back in the field)
TOKEN_MAP = {
    "NS": ("no_score", "NS"),          # No Score
    "BO": ("buck_off", "BO"),          # Buck Off (roughstock)
    "MO": ("missed_out", "MO"),        # Missed Out (roping - didn't catch; also used by roughstock judges)
    "NT": ("no_time", "NT"),           # No Time
    "DQ": ("dq", "DQ"),                # Disqualified
    "DNS": ("no_show", "DNS"),         # Did Not Show
    "DG": ("double_grab", "DG"),       # Double Grab (breakaway roping; also used by roughstock judges)
    "RR": ("re_ride", "RR"),           # Re-Ride (roughstock - comes back later on different stock)
    "SLAP": ("slapped", "SLAP"),       # Slapped the animal with the free hand (roughstock)
    "VR": ("video_review", "VR"),      # Video Review -- optional, see enable_video_review setting
}

STATUS_LABELS = {
    "pending": "Pending",
    "scored": "Scored",
    "no_score": "No Score",
    "buck_off": "Buck Off",
    "missed_out": "Missed Out",
    "no_time": "No Time",
    "dq": "DQ",
    "no_show": "No Show",
    "double_grab": "Double Grab",
    "re_ride": "Re-Ride",
    "slapped": "Slapped Animal",
    "video_review": "Video Review",
}

SCORING_TYPES = {
    "judged": "Judged (0-100, higher wins)",
    "timed": "Timed - seconds (lower wins)",
    "timed_mmss": "Timed - minutes:seconds (lower wins)",
}

SHORT_LABELS = {
    "judged": "Judged",
    "timed": "Timed",
    "timed_mmss": "Timed (M:SS)",
}


def is_timed(scoring_type):
    return scoring_type in ("timed", "timed_mmss")


def short_label(scoring_type):
    return SHORT_LABELS.get(scoring_type, scoring_type)


def sort_order(scoring_type):
    """SQL ORDER BY direction for this scoring type."""
    return "ASC" if is_timed(scoring_type) else "DESC"

VALID_CODES_HINT = "NS, BO, MO, NT, DQ, DNS, DG, RR, SLAP"


def valid_codes_hint(allow_video_review=False):
    """Same as VALID_CODES_HINT, with VR appended only when Video Review
    is turned on for this rodeo (see the enable_video_review setting) --
    an error message shouldn't advertise a code that won't actually work."""
    return VALID_CODES_HINT + (", VR" if allow_video_review else "")


def format_number(value, scoring_type="judged"):
    """Format a score for display/export. Timed events use hundredths
    (seconds). Timed (M:SS) events format as minutes:seconds.hundredths.
    Judged events commonly use quarter-point increments (.25/.5/.75), so
    this always shows two decimal places -- including whole-number scores
    like 87.00 -- so every score in a column lines up the same width
    regardless of whether other entries have quarter- or half-point scores."""
    if value is None:
        return ""
    if scoring_type == "timed_mmss":
        minutes = int(value // 60)
        seconds = value - minutes * 60
        return f"{minutes}:{seconds:05.2f}"
    return f"{value:.2f}"


def parse_score_input(raw, scoring_type="judged", allow_video_review=False):
    """
    Returns {"status": ..., "score_value": float|None, "display": str}
    Raises ValueError with a human-readable message on bad input.

    allow_video_review gates the VR code specifically (see the
    enable_video_review setting) -- it's off by default since not every
    rodeo uses video review, so typing VR before it's turned on in
    Settings is treated the same as any other unrecognized code rather
    than silently working.
    """
    raw = (raw or "").strip()

    if raw == "":
        return {"status": "pending", "score_value": None, "display": ""}

    upper = raw.upper()
    if upper == "VR" and not allow_video_review:
        raise ValueError(
            "\u201cVR\u201d (Video Review) isn't turned on for this rodeo -- enable it in Settings first."
        )
    if upper in TOKEN_MAP:
        status, token = TOKEN_MAP[upper]
        return {"status": status, "score_value": None, "display": token}

    if scoring_type == "timed_mmss" and ":" in raw:
        minutes_str, _, seconds_str = raw.partition(":")
        try:
            minutes = int(minutes_str)
            seconds = float(seconds_str)
        except ValueError:
            raise ValueError(f"\u201c{raw}\u201d isn't a valid time. Use M:SS or M:SS.ss (e.g. 1:23.45).")
        if minutes < 0 or seconds < 0:
            raise ValueError("Time can't be negative.")
        value = minutes * 60 + seconds
    else:
        try:
            value = float(raw)
        except ValueError:
            hint = valid_codes_hint(allow_video_review)
            if scoring_type == "timed_mmss":
                raise ValueError(
                    f"\u201c{raw}\u201d isn't a valid time or a recognized code ({hint}). "
                    f"Use M:SS (e.g. 1:23.45) or plain seconds."
                )
            raise ValueError(f"\u201c{raw}\u201d isn't a number or a recognized code ({hint}).")
        if value < 0:
            raise ValueError("Score can't be negative.")

    display = format_number(value, scoring_type)
    return {"status": "scored", "score_value": value, "display": display}


def display_for(status, score_value, scoring_type="judged"):
    """Given a stored status/score_value, return what should show in the field."""
    if status == "scored" and score_value is not None:
        return format_number(score_value, scoring_type)
    for s, token in TOKEN_MAP.values():
        if s == status:
            return token
    return ""


# ---------- Judge scoring (roughstock events) ----------
# Each judge scores the rider and the stock separately, 1-25 in half
# points each. Rider and stock are combined independently of one
# another (a buck off, no score, DQ, or re-ride on the rider side never
# affects the stock side, and vice versa) -- see JUDGE_OUTCOMES below.
# A 2-judge panel sums both scores together (max 50). A 4-judge panel
# also just sums all four together, then divides by 2 -- NOT by 4 --
# which keeps the combined score on that same 0-50 scale rather than
# averaging it down to a single judge's 0-25 scale. Either panel size
# is combined with the same function, once for the rider's scores and
# separately for the stock's.
VALID_JUDGE_COUNTS = (2, 4)
JUDGE_SCORE_MIN = 1
JUDGE_SCORE_MAX = 25
JUDGE_SCORE_STEP = 0.5


def combine_scores_by_seat(scores_by_seat, judge_count):
    """scores_by_seat: {seat_number: score_value}, for ONE side of the
    scoring (rider or stock -- call this separately for each). Returns
    the combined total once every expected seat (1..judge_count) has a
    value, else None so callers can tell "not ready yet" apart from a
    real computed number.
    """
    values = []
    for seat in range(1, judge_count + 1):
        v = scores_by_seat.get(seat)
        if v is None:
            return None
        values.append(v)

    if judge_count == 2:
        return sum(values)
    # 4 judges: sum all four, then divide by 2 (not by 4/len(values)) --
    # see the module comment above for why.
    return sum(values) / 2


def valid_judge_score(value):
    """Whether a single rider or stock score is a legal 1-25, half-point
    value. Used to validate what a judge's device submits."""
    if value is None:
        return False
    if value < JUDGE_SCORE_MIN or value > JUDGE_SCORE_MAX:
        return False
    doubled = value * 2
    return abs(doubled - round(doubled)) < 1e-9


# A judge can flag the RIDER side of a ride instead of (or, for a
# re-ride, alongside) a numeric rider score -- the same outcome
# vocabulary the scorekeeper's own quick-entry already uses (see
# TOKEN_MAP above), so a "BO" here means the same thing it does there.
#
# BO/DG/MO/SLAP mean there's no rider score at all -- the rider stepper
# gets disabled when one of these is picked. RR is different: the ride
# still gets a real rider score (the cowboy has the option to take it
# or take the re-ride -- that's the scorekeeper's call, not the
# judge's), so picking RR just tags the submission without touching
# the rider stepper. Either way, the STOCK score is never affected by
# any of these -- stock contractors care about how the animal
# performed regardless of what happened with the rider, so stock
# scoring stays fully independent and always enterable.
JUDGE_OUTCOMES = {
    "BO": "Buck Off",
    "DG": "Double Grab",
    "MO": "Missed Out",
    "RR": "Re-Ride",
    "SLAP": "Slapped Animal",
}

# Outcomes that mean there's no rider score to give at all. RR is
# deliberately excluded -- it still carries a real rider score.
JUDGE_OUTCOMES_NO_RIDER_SCORE = ("BO", "DG", "MO", "SLAP")


# ---------- Judge-flagged timed-event penalties ----------
# A judge on a timed event (barrel racing, roping, etc.) can flag one of a
# small set of standard infractions from their own device, alongside the
# scorekeeper's own generic +5/+10 penalty buttons. Barrier and One Heel are
# numeric penalties added on top of the run's time -- same mechanism as the
# scorekeeper's buttons, just sourced from the judge instead. Illegal Head
# Catch isn't a numeric penalty at all -- it means there's no time for the
# run, so it's wired to the same "no_time" status the NT code already
# produces (see TOKEN_MAP above), not a new one.
TIMED_PENALTY_BUTTONS = [
    {"code": "barrier", "label": "Barrier", "amount": 10},
    {"code": "one_heel", "label": "One Heel", "amount": 5},
]
TIMED_PENALTY_AMOUNTS = {b["code"]: b["amount"] for b in TIMED_PENALTY_BUTTONS}
TIMED_PENALTY_LABELS = {b["code"]: b["label"] for b in TIMED_PENALTY_BUTTONS}

TIMED_HEAD_CATCH_CODE = "head_catch"
TIMED_HEAD_CATCH_LABEL = "Illegal Head Catch"


def append_penalty_note(existing, label, amount, suffix=""):
    """Appends one more \"Label +N\" piece to a penalty breakdown string,
    e.g. \"Barrier +10\" then \"Barrier +10, One Heel +5\" once a second
    penalty gets called on the same run. %g trims the trailing zero off
    whole-number amounts (10, not 10.0). An optional suffix (e.g. "(Judge)")
    is appended after the amount, not before the label, so it reads
    "Barrier +10 (Judge)" rather than "Barrier (Judge) +10". label can be
    blank -- the scorekeeper's own +5/+10 buttons aren't tied to a named
    infraction like Barrier/One Heel, so that just produces "+5" on its
    own rather than a dangling space before the amount."""
    piece = f"+{amount:g}" if not label else f"{label} +{amount:g}"
    if suffix:
        piece = f"{piece} {suffix}"
    existing = (existing or "").strip()
    return piece if not existing else f"{existing}, {piece}"
