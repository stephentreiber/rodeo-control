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
import io
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


def _parse_draw_rows(rows):
    """Core row-classification engine shared by every draw-sheet import
    format (HTML table, Excel sheet, PDF text/OCR lines). Each source
    format only needs to reduce itself to the same shape -- an iterable of
    rows, each row a list of raw cell values left-to-right in source
    column order -- and this applies the same EVENT / PERFORMANCE /
    numbered-row / RR / partner-row rules to all of them, so the parsing
    logic (and any future fix to it) lives in exactly one place.

    Returns the same {"events": [...], "warnings": [...]} shape documented
    on parse_rodeocanada_html below.
    """
    warnings = []
    events_by_name = {}   # upper-name -> event dict (preserves merge across repeated EVENT blocks)
    event_order = []
    current_event = None
    current_round = None
    last_entry = None     # for attaching a team-roping-style partner row

    for row in rows:
        vals = [_clean("" if c is None else str(c)) for c in row]
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
    if not tables:
        return {"events": [], "warnings": ["No <table> found in the uploaded file."]}

    # Use the largest table -- draw sheets are a single big table, but the
    # page may have small layout tables elsewhere.
    table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = [[td.get_text() for td in tr.find_all("td")] for tr in table.find_all("tr")]
    return _parse_draw_rows(rows)


def parse_xlsx_draw_sheet(file_bytes):
    """Same draw-sheet shape as the RodeoCanada HTML export, but as an
    Excel workbook -- one worksheet row per source row, one cell per
    column, laid out exactly like the HTML table's <td> cells (col 0:
    EVENT/PERFORMANCE marker or "draw# name", col 1: hometown, col 2:
    draw animal/stock). Uses whichever sheet has the most rows, in case
    the workbook has extra summary/instruction tabs alongside the draw.
    """
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        return {"events": [], "warnings": [f"Could not read that Excel file: {exc}"]}

    sheet = max(wb.worksheets, key=lambda ws: ws.max_row)
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    return _parse_draw_rows(rows)


_MULTISPACE = re.compile(r"\s{2,}")


def _split_text_line(line):
    """Turns one line of extracted/OCR'd PDF text into cell values. These
    source draw sheets use literal "|" column separators even in their
    plain-text/PDF renderings (matching the HTML version), so that's the
    primary split. OCR output is less reliable about preserving "|"
    characters, so if none survived, fall back to splitting on runs of
    2+ spaces, which is usually how columns are visually separated."""
    line = line.strip()
    if not line:
        return []
    if "|" in line:
        return [cell.strip() for cell in line.split("|")]
    return [cell.strip() for cell in _MULTISPACE.split(line)]


def parse_pdf_draw_sheet(file_bytes):
    """Same draw-sheet shape again, from a PDF. Tries normal text
    extraction first (fast, exact, no external binaries needed); any page
    that comes back with little or no text is assumed to be a scanned
    image and is rasterized + OCR'd instead. Returns the same
    {"events", "warnings"} shape, with an added note in "warnings" for any
    page that needed OCR (OCR is never as reliable as real text, so it's
    worth flagging which pages to double-check on the review screen).
    """
    import pdfplumber

    ocr_pages = []
    rows = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if len(text.strip()) < 20:
                    # Little to no extractable text -- likely a scanned
                    # image page. Fall back to OCR for this page only.
                    ocr_text = _ocr_pdf_page(file_bytes, page_num)
                    if ocr_text is None:
                        return {
                            "events": [],
                            "warnings": [
                                f"Page {page_num} appears to be a scanned image and OCR is not "
                                "available on this machine (Tesseract OCR + Poppler must be "
                                "installed separately -- see README)."
                            ],
                        }
                    ocr_pages.append(page_num)
                    text = ocr_text
                for line in text.splitlines():
                    rows.append(_split_text_line(line))
                rows.append([])  # blank separator between pages, mirrors blank rows in the source
    except Exception as exc:
        return {"events": [], "warnings": [f"Could not read that PDF: {exc}"]}

    result = _parse_draw_rows(rows)
    if ocr_pages:
        pages_str = ", ".join(str(p) for p in ocr_pages)
        result["warnings"].insert(
            0,
            f"Page(s) {pages_str} had no extractable text and were read using OCR instead -- "
            "double-check names, hometowns, and stock on the review screen before importing, "
            "since OCR is never as reliable as the original text.",
        )
    return result


def _ocr_pdf_page(file_bytes, page_num):
    """Rasterizes a single PDF page and runs Tesseract OCR on it. Returns
    None (rather than raising) if either Poppler (needed to rasterize the
    page) or the Tesseract binary itself isn't installed on this machine,
    so the caller can give a clear one-time-setup message instead of a
    crash."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PDFInfoNotInstalledError
    except ImportError:
        return None
    try:
        images = convert_from_bytes(file_bytes, first_page=page_num, last_page=page_num, dpi=300)
    except PDFInfoNotInstalledError:
        return None
    if not images:
        return None
    try:
        return pytesseract.image_to_string(images[0])
    except pytesseract.TesseractNotFoundError:
        return None
