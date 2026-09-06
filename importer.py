"""
Parses RodeoCanada-style draw sheet HTML (a single <table> of rows shaped like:

    EVENT       :SADDLE BRONC
    PERFORMANCE:  #1 6:30PM | THU JUN 25 2026
    1 KYLE WANCHUK | STETTLER AB | C5S 3371 DEVILS SON
    ...
    RR | | C5S 557 ROBINS MESS          <- reserve/re-ride stock, no competitor
                                          <- blank separator row
    PERFORMANCE:  #2 1PM FRI | JUN 26 2026
    ...

Team roping (and any other two-person event) lists the header/roper on one
row with a leading draw number, and the partner on the very next row with
no leading number:

    1 CYLE DENISON | LOGANSPORT LA |
    BRAYLON TRYAN  | LIPAN, TX     |

into a structured, de-duplicated list of events -> rounds -> entries.
"""
import html
import re
from bs4 import BeautifulSoup

PROV_STATE_CODES = {
    # Canadian provinces/territories
    "AB", "BC", "SK", "MB", "ON", "QC", "NS", "NB", "PE", "NL", "YT", "NT", "NU",
    # US states
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    "MX",
}

# Known scoring type for common CPRA-style events; anything not listed here
# defaults to "judged" and can be changed by the user after import.
TIMED_EVENT_KEYWORDS = [
    "barrel racing", "tie-down roping", "tie down roping", "team roping",
    "steer wrestling", "breakaway roping",
]

NUMBERED_ROW = re.compile(r"^(\d+)\s+(.+)$")


def _clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fix_hometown(raw):
    """Forces ALL CAPS, while still fixing a missing comma before the
    province/state code (e.g. 'stettler ab' -> 'STETTLER, AB')."""
    raw = _clean(raw)
    if not raw:
        return ""
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return ", ".join(p.upper() for p in parts)
    tokens = raw.split(" ")
    if len(tokens) >= 2 and tokens[-1].upper() in PROV_STATE_CODES:
        city = " ".join(tokens[:-1])
        code = tokens[-1].upper()
        return f"{city.upper()}, {code}"
    return raw.upper()


def format_name(raw):
    """Forces ALL CAPS for competitor/partner names."""
    return _clean(raw).upper()


def last_name(full_name):
    """Best-effort last name: the final whitespace-separated token. Good
    enough for typical Western names; multi-word surnames (e.g. "Van Der
    Berg") will only capture the final word."""
    full_name = (full_name or "").strip()
    if not full_name:
        return ""
    return full_name.split()[-1]


def format_draw_animal(raw):
    """Forces ALL CAPS for the draw-animal/stock field."""
    return _clean(raw).upper()


def _guess_scoring_type(event_name):
    lower = event_name.lower()
    for kw in TIMED_EVENT_KEYWORDS:
        if kw in lower:
            return "timed"
    return "judged"


def guess_is_team_event(event_name):
    """Whether this event should show a Heeler column and use the
    header/heeler pairing -- based on the event's own name, not on any
    individual competitor's partner link (which can carry over from a
    different event entirely)."""
    return "team roping" in event_name.lower()


def _clean_event_name(raw):
    # "EVENT       :SADDLE BRONC" / "EVENT: BARREL RACING" -> "Saddle Bronc"
    raw = _clean(raw)
    raw = re.sub(r"^EVENT\s*:?\s*", "", raw, flags=re.IGNORECASE)
    return raw.strip().title()


def _clean_round_name(perf_cell, date_cell):
    # "PERFORMANCE:  #1 6:30PM" -> "#1 6:30PM"
    # "SLACK PERF:  #7 SL 9:30AM THU" -> "Slack #7 SL 9:30AM THU"
    perf_cell = _clean(perf_cell)
    is_slack = perf_cell.upper().startswith("SLACK")
    label = re.sub(r"^(SLACK\s+PERF|PERFORMANCE)\s*:?\s*", "", perf_cell, flags=re.IGNORECASE)
    date_cell = _clean(date_cell).title()
    prefix = "Slack" if is_slack else "Perf"
    parts = [p for p in [f"{prefix} {label}".strip(), date_cell] if p]
    return " \u2014 ".join(parts) if parts else (perf_cell or "Round")


def parse_rodeocanada_html(html_text):
    """
    Returns a dict:
    {
      "events": [
        {
          "name": "Saddle Bronc",
          "scoring_type": "judged",
          "rounds": [
            {
              "name": "#1 6:30PM \u2014 Thu Jun 25 2026",
              "entries": [
                {"draw_number": 1, "name": "Kyle Wanchuk", "hometown": "Stettler, AB",
                 "draw_animal": "C5S 3371 Devils Son", "partner": ""},
                ...
              ]
            },
            ...
          ]
        },
        ...
      ],
      "warnings": [ ... ]  # rows we couldn't confidently classify
    }
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    warnings = []
    if not tables:
        return {"events": [], "warnings": ["No <table> found in the uploaded file."]}

    # Use the largest table -- draw sheets are a single big table, but the
    # page may have small layout tables elsewhere.
    table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = table.find_all("tr")

    events_by_name = {}   # upper-name -> event dict (preserves merge across repeated EVENT blocks)
    event_order = []
    current_event = None
    current_round = None
    last_entry = None     # for attaching a team-roping-style partner row

    for row in rows:
        tds = row.find_all("td")
        vals = [_clean(td.get_text()) for td in tds]
        first = vals[0] if vals else ""

        if not first:
            # blank separator row
            last_entry = None
            continue

        if first.upper().startswith("EVENT"):
            name = _clean_event_name(first)
            key = name.upper()
            if key not in events_by_name:
                events_by_name[key] = {
                    "name": name,
                    "scoring_type": _guess_scoring_type(name),
                    "rounds": [],
                }
                event_order.append(key)
            current_event = events_by_name[key]
            current_round = None
            last_entry = None
            continue

        if first.upper().startswith("PERFORMANCE") or first.upper().startswith("SLACK PERF"):
            if current_event is None:
                warnings.append(f"Found a round header before any EVENT header: {first!r}")
                continue
            date_cell = vals[1] if len(vals) > 1 else ""
            round_name = _clean_round_name(first, date_cell)
            current_round = {"name": round_name, "entries": []}
            current_event["rounds"].append(current_round)
            last_entry = None
            continue

        if first.upper() == "RR":
            # Reserve / re-ride stock -- no competitor has drawn it yet.
            # Imported as its own entry with a blank name at the bottom
            # of the round (it's already at the bottom of the source
            # sheet, so appending here preserves that), so the
            # scorekeeper can just type the rider's name in once a
            # re-ride actually happens, instead of the stock silently
            # vanishing from the import or "RR" showing as if it were a
            # contestant's name.
            if current_round is not None:
                draw_animal = format_draw_animal(vals[2]) if len(vals) > 2 else ""
                current_round["entries"].append(
                    {
                        "draw_number": None,
                        "name": "",
                        "hometown": "",
                        "draw_animal": draw_animal,
                        "partner": "",
                    }
                )
            last_entry = None
            continue

        if current_round is None:
            warnings.append(f"Skipped a row with no active round: {vals!r}")
            continue

        hometown = fix_hometown(vals[1]) if len(vals) > 1 else ""
        draw_animal = format_draw_animal(vals[2]) if len(vals) > 2 else ""

        m = NUMBERED_ROW.match(first)
        if m:
            draw_number = int(m.group(1))
            name = format_name(m.group(2))
            entry = {
                "draw_number": draw_number,
                "name": name,
                "hometown": hometown,
                "draw_animal": draw_animal,
                "partner": "",
            }
            current_round["entries"].append(entry)
            last_entry = entry
        else:
            # No leading number -- this is a partner line (team roping etc.)
            # attached to the immediately preceding numbered entry.
            if last_entry is not None and not last_entry["partner"]:
                last_entry["partner"] = format_name(first)
                # A partner's hometown is informational only; competitor
                # roster entry for the partner is created separately at
                # import time so they also get their own competitor record.
                last_entry["partner_hometown"] = hometown
            else:
                warnings.append(f"Unrecognized row (no draw number, no prior entry to attach to): {vals!r}")
            last_entry = None

    events = [events_by_name[k] for k in event_order]
    return {"events": events, "warnings": warnings}
