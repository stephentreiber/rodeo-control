import os
import time
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from database import get_conn, get_setting, set_setting
import scoring
import importer

# Placeholder swapped for the literal "&#13;" (carriage return) character
# reference right before writing to disk. A raw CR character in .text would
# get end-of-line-normalized away (by our own minidom re-parse, and again by
# any XML parser that reads the file later), so the only reliable way to
# make a real line break survive inside a single text field is the numeric
# character reference -- entity references are exempt from that
# normalization per the XML spec. This is also the documented workaround
# broadcast/graphics text engines (vMix titles, Adobe/XPression-style data
# bindings) expect for embedding line breaks in XML-sourced text.
CR_PLACEHOLDER = "@@CRBREAK@@"


def join_with_cr(items):
    """Join a list of strings with a literal-carriage-return marker that
    survives to the final XML file as the &#13; entity."""
    return CR_PLACEHOLDER.join(items)


def _limited_rows(rows):
    """Trim to the configurable Settings limit ("" or non-numeric means no
    limit) -- shared by all_names/all_ranks/all_scores so they always stay
    the same length and line up row-for-row."""
    limit_raw = get_setting("names_field_limit", "10")
    if limit_raw.isdigit() and int(limit_raw) > 0:
        rows = rows[: int(limit_raw)]
    return rows


def _team_names(name, partner_name):
    """For team roping (or any partnered entry): 'Header Name/Heeler Name'
    and 'Header Last/Heeler Last'. Falls back to just the solo competitor's
    name/last name when there's no partner, so these fields are always
    populated and safe to bind regardless of event type."""
    if not partner_name:
        return name, importer.last_name(name)
    full = f"{name}/{partner_name}"
    last = f"{importer.last_name(name)}/{importer.last_name(partner_name)}"
    return full, last


def _add_all_fields(root, rows, score_for):
    """Adds all_ranks / all_names / all_lastnames / all_scores as four
    parallel CR-joined fields (same length, same order) so line N of each
    lines up with the same competitor -- for a leaderboard graphic built
    from text boxes instead of a repeating-row binding. score_for(row) -> str.
    all_names/all_lastnames use the same "Header/Heeler" formatting as
    team_full_names/team_last_names (falls back to just the solo name when
    there's no partner)."""
    limited = _limited_rows(rows)
    team_names = [_team_names(r["name"], r["partner"]) for r in limited]
    _sub(root, "all_ranks", join_with_cr([str(i) for i in range(1, len(limited) + 1)]))
    _sub(root, "all_names", join_with_cr([n[0] for n in team_names]))
    _sub(root, "all_lastnames", join_with_cr([n[1] for n in team_names]))
    _sub(root, "all_scores", join_with_cr([score_for(r) for r in limited]))


FIXED_SLOT_FALLBACK = 20


def _fixed_slot_count():
    """How many individual numbered tags (rank_1, name_1, ...) to write.
    Reuses the Settings "Number of Competitors on Leaderboard" limit --
    but unlike the CR-joined all_* fields, a fixed set of individual tags
    can't be "unlimited" (the whole point is a document that never changes
    shape), so "All" falls back to a fixed default here specifically."""
    limit_raw = get_setting("names_field_limit", "10")
    if limit_raw.isdigit() and int(limit_raw) > 0:
        return int(limit_raw)
    return FIXED_SLOT_FALLBACK


def _add_numbered_fields(root, rows, score_for):
    """Adds rank_1/name_1/lastname_1/score_1, rank_2/name_2/..., always for
    the same fixed number of slots (blank past however many are actually
    on the board) -- for graphics software that can't parse the CR-joined
    all_* fields or bind to a repeating <entry> list at all. Slot count
    matches the Settings leaderboard-size limit."""
    count = _fixed_slot_count()
    limited = rows[:count]
    for i in range(1, count + 1):
        if i <= len(limited):
            r = limited[i - 1]
            full_name, last_name = _team_names(r["name"], r["partner"])
            _sub(root, f"rank_{i}", i)
            _sub(root, f"name_{i}", full_name)
            _sub(root, f"lastname_{i}", last_name)
            _sub(root, f"score_{i}", score_for(r))
        else:
            _sub(root, f"rank_{i}", "")
            _sub(root, f"name_{i}", "")
            _sub(root, f"lastname_{i}", "")
            _sub(root, f"score_{i}", "")


def _write_xml(root, filename):
    """Writes one export file, atomically (write to a .tmp then rename, so
    a reader never sees a half-written file). Deliberately swallows any
    write failure rather than letting it crash whatever scoring action
    triggered it -- the database write that matters has already
    succeeded by the time export_all() runs, and export_folder can be a
    network path (a UNC share, a mapped drive) rather than a local
    folder, which can drop out from under a live show for all kinds of
    ordinary reasons: a WiFi blip, the other machine asleep, the share
    disconnected. A scoring action failing outright over a transient
    graphics-export hiccup would be a much worse live-show problem than
    the export itself being briefly stale. The failure (and eventual
    recovery) is still recorded via set_setting so it can surface as a
    banner instead of silently going stale forever -- see
    xml_export_status() and the banner in base.html."""
    folder = get_setting("export_folder")
    path = os.path.join(folder, filename)
    try:
        os.makedirs(folder, exist_ok=True)
        rough = ET.tostring(root, encoding="utf-8")
        pretty = minidom.parseString(rough).toprettyxml(indent="  ", encoding="UTF-8")
        pretty = pretty.replace(CR_PLACEHOLDER.encode("utf-8"), b"&#13;")
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(pretty)
        os.replace(tmp_path, path)  # atomic write so readers never see a half-written file
    except OSError as e:
        set_setting("xml_export_last_error", f"{filename}: {e}")
        set_setting("xml_export_last_error_at", str(int(time.time())))
        print(f"[Rodeo Control] XML export to {path} failed: {e}")
        return None
    set_setting("xml_export_last_error", "")
    set_setting("xml_export_last_success_at", str(int(time.time())))
    return path


def xml_export_status():
    """Whether the last export attempt succeeded, for the site-wide
    warning banner (see base.html) -- checked once per page load, not
    polled live, since this is meant to catch "the export path has been
    broken for a while," not track it second-by-second."""
    error = get_setting("xml_export_last_error", "")
    if not error:
        return None
    success_at = get_setting("xml_export_last_success_at", "")
    return {
        "error": error,
        "error_at": _format_epoch(get_setting("xml_export_last_error_at", "")),
        "success_at": _format_epoch(success_at) if success_at else "never this session",
    }


def _format_epoch(raw):
    # %I (not %-I/%#I) deliberately -- the no-leading-zero flag isn't
    # portable between Windows (%#I) and Mac/Linux (%-I), and this
    # runs on Windows.
    try:
        return time.strftime("%I:%M %p", time.localtime(int(raw)))
    except (ValueError, TypeError):
        return ""


def _sub(parent, tag, value):
    el = ET.SubElement(parent, tag)
    el.text = "" if value is None else str(value)
    return el


def _format_number(value, scoring_type):
    return scoring.format_number(value, scoring_type)


def _format_score(value, scoring_type):
    return _format_number(value, scoring_type)


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _compute_ranks(conn, event_id, round_number, competitor_id, scoring_type):
    """Returns (round_rank, aggregate_rank) as ordinal strings ("2nd"), or
    "" if the competitor doesn't have a scored entry to rank yet."""
    order = scoring.sort_order(scoring_type)

    round_rank = ""
    if round_number is not None:
        round_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM rounds WHERE event_id = ? AND round_number = ?",
                (event_id, round_number),
            )
        ]
        if round_ids:
            placeholders = ",".join("?" * len(round_ids))
            rows = conn.execute(
                f"""
                SELECT competitor_id FROM entries
                WHERE round_id IN ({placeholders}) AND status = 'scored'
                ORDER BY score_value {order}
                """,
                round_ids,
            ).fetchall()
            for i, r in enumerate(rows, start=1):
                if r["competitor_id"] == competitor_id:
                    round_rank = _ordinal(i)
                    break

    agg_rank = ""
    all_round_ids = [r["id"] for r in conn.execute("SELECT id FROM rounds WHERE event_id = ?", (event_id,))]
    if all_round_ids:
        placeholders = ",".join("?" * len(all_round_ids))
        rows = conn.execute(
            f"""
            SELECT competitor_id, SUM(score_value) as total FROM entries
            WHERE round_id IN ({placeholders}) AND status = 'scored'
            GROUP BY competitor_id
            ORDER BY total {order}
            """,
            all_round_ids,
        ).fetchall()
        for i, r in enumerate(rows, start=1):
            if r["competitor_id"] == competitor_id:
                agg_rank = _ordinal(i)
                break

    return round_rank, agg_rank


def _compute_aggregate_display(conn, event_id, competitor_id, scoring_type):
    """Returns 'total/head' (e.g. '186/2') for this competitor across every
    scored entry in the event, or "" if they haven't scored yet."""
    all_round_ids = [r["id"] for r in conn.execute("SELECT id FROM rounds WHERE event_id = ?", (event_id,))]
    if not all_round_ids:
        return ""
    placeholders = ",".join("?" * len(all_round_ids))
    row = conn.execute(
        f"""
        SELECT SUM(score_value) as total, COUNT(*) as head
        FROM entries
        WHERE round_id IN ({placeholders}) AND status = 'scored' AND competitor_id = ?
        """,
        all_round_ids + [competitor_id],
    ).fetchone()
    if not row or not row["head"] or row["total"] is None:
        return ""
    total_str = scoring.format_number(row["total"], scoring_type)
    return f"{total_str}/{row['head']}"


def _time_plus_penalty(status, score_value, penalty, scoring_type, fallback_display):
    """'4.5+10' style breakdown of raw run time and penalty, kept separate
    from the combined final time (round_score / leaderboards use the
    combined total, unaffected by this). No penalty on record just shows
    the plain time, same as round_score. Non-numeric statuses (NT/BO/etc.)
    fall back to whatever round_score already shows for them."""
    if status != "scored" or score_value is None:
        return fallback_display
    penalty = penalty or 0
    if penalty <= 0:
        return scoring.format_number(score_value, scoring_type)
    raw = score_value - penalty
    raw_str = scoring.format_number(raw, scoring_type)
    penalty_str = scoring.format_number(penalty, "judged")
    return f"{raw_str}+{penalty_str}"


def export_whos_up():
    conn = get_conn()
    row = conn.execute(
        """
        SELECT e.id as entry_id, e.score_value, e.status, e.draw_number, e.penalty,
               e.competitor_id, e.round_id,
               c.name, c.hometown, c.sponsor, c.notes,
               COALESCE(pc.name, c.partner) as partner,
               pc.hometown as partner_hometown,
               COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal,
               ev.id as event_id, ev.name as event_name, ev.scoring_type, ev.is_team,
               r.name as round_name, r.round_number,
               w.guest_id,
               g.name as g_name, g.hometown as g_hometown,
               g.sponsor as g_sponsor, g.notes as g_notes
        FROM whos_up w
        LEFT JOIN entries e ON w.entry_id = e.id
        LEFT JOIN competitors c ON e.competitor_id = c.id
        LEFT JOIN competitors pc ON c.partner_id = pc.id
        LEFT JOIN rounds r ON e.round_id = r.id
        LEFT JOIN events ev ON r.event_id = ev.id
        LEFT JOIN guests g ON w.guest_id = g.id
        WHERE w.id = 1
        """
    ).fetchone()

    root = ET.Element("whos_up")
    if row and row["guest_id"]:
        # Guests (royalty, sponsors, committee members, the anthem singer,
        # anyone else who needs a lower third but isn't a competitor)
        # share this exact same feed and tag set as the current
        # contestant, rather than a separate export -- smaller rodeos
        # running this app without a dedicated graphics operator get one
        # lower third that already works for all of it, with nothing new
        # to bind in the graphics software. Fields with no guest
        # equivalent (draw info, scores, ranks, event/round) are simply
        # left blank, not omitted -- same present-but-empty rule as the
        # rest of this export.
        _sub(root, "draw_number", "")
        _sub(root, "name", row["g_name"])
        _sub(root, "hometown", row["g_hometown"])
        _sub(root, "sponsor", row["g_sponsor"])
        _sub(root, "heeler_name", "")
        _sub(root, "heeler_hometown", "")
        _sub(root, "draw_animal", "")
        _sub(root, "notes", row["g_notes"])
        _sub(root, "event", "")
        _sub(root, "round", "")
        _sub(root, "round_score", "")
        _sub(root, "time_plus_penalty", "")
        _sub(root, "round_rank", "")
        _sub(root, "aggregate_rank", "")
        _sub(root, "aggregate_score", "")
    elif row and row["entry_id"]:
        # Show the penalty code (NT, BO, MO, DQ, etc.) once one's been
        # entered, not just a numeric score -- important for the audience
        # to see what happened on a ride/run, not just a blank field.
        score_display = scoring.display_for(row["status"], row["score_value"], row["scoring_type"])
        round_rank, agg_rank = _compute_ranks(
            conn, row["event_id"], row["round_number"], row["competitor_id"], row["scoring_type"]
        )
        agg_display = _compute_aggregate_display(
            conn, row["event_id"], row["competitor_id"], row["scoring_type"]
        )
        _sub(root, "draw_number", row["draw_number"])
        _sub(root, "name", row["name"])
        _sub(root, "hometown", row["hometown"])
        _sub(root, "sponsor", row["sponsor"])
        _sub(root, "heeler_name", row["partner"] if row["is_team"] else "")
        _sub(root, "heeler_hometown", row["partner_hometown"] if row["is_team"] else "")
        _sub(root, "draw_animal", row["draw_animal"])
        _sub(root, "notes", row["notes"])
        _sub(root, "event", row["event_name"])
        _sub(root, "round", row["round_name"])
        _sub(root, "round_score", score_display)
        _sub(root, "time_plus_penalty", _time_plus_penalty(
            row["status"], row["score_value"], row["penalty"], row["scoring_type"], score_display
        ))
        _sub(root, "round_rank", round_rank)
        _sub(root, "aggregate_rank", agg_rank)
        _sub(root, "aggregate_score", agg_display)
    else:
        for tag in ["draw_number", "name", "hometown", "sponsor", "heeler_name", "heeler_hometown",
                    "draw_animal", "notes", "event", "round", "round_score", "time_plus_penalty",
                    "round_rank", "aggregate_rank", "aggregate_score"]:
            _sub(root, tag, "")
    conn.close()
    return _write_xml(root, "whos_up.xml")


def export_round_leaderboard():
    """Round leaderboard: groups by round NUMBER, not a single performance --
    several performances (e.g. "Perf #1" and "Slack #7") can share round
    number 1 and are combined here."""
    event_id = get_setting("live_event_id")
    round_number = get_setting("active_round_number")
    conn = get_conn()

    root = ET.Element("leaderboard")
    event_row = None
    rows = []
    scoring_type = "judged"

    if event_id and round_number != "":
        event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if event_row:
        scoring_type = event_row["scoring_type"]
        round_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM rounds WHERE event_id = ? AND round_number = ?",
                (event_id, round_number),
            )
        ]
        if round_ids:
            order = scoring.sort_order(scoring_type)
            placeholders = ",".join("?" * len(round_ids))
            rows = conn.execute(
                f"""
                SELECT e.score_value, e.status, c.name, c.hometown, c.sponsor,
                       COALESCE(pc.name, c.partner) as partner,
                       COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal
                FROM entries e
                JOIN competitors c ON e.competitor_id = c.id
                LEFT JOIN competitors pc ON c.partner_id = pc.id
                WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
                ORDER BY e.score_value {order}
                """,
                round_ids,
            ).fetchall()

    conn.close()

    # A competitor's partner_id is a property of them as a person, not of
    # this specific event -- someone who ropes team and also runs tie-down
    # still carries their team partner link on their competitor record.
    # Only apply the header/heeler pairing when this event is actually
    # marked as a team event; otherwise blank it so team_full_names /
    # all_names never show a partner that has nothing to do with this
    # leaderboard.
    if not (event_row and event_row["is_team"]):
        rows = [dict(r) for r in rows]
        for r in rows:
            r["partner"] = ""

    # Root attributes and the all_* summary fields are always present, even
    # with nothing configured yet or nobody scored -- just empty -- rather
    # than the element being missing outright. Broadcast graphics data
    # bindings generally expect a stable, predictable document shape; a
    # tag that sometimes isn't there at all is far more likely to cause a
    # data-source error or a stale/frozen field than a tag that's simply
    # empty. The <entry> list itself is inherently variable-length by
    # nature (a leaderboard has as many rows as there are scores), so that
    # part legitimately does grow and shrink.
    root.set("event", event_row["name"] if event_row else "")
    root.set("round", f"Round {round_number}" if (event_row and round_number != "") else "")

    _add_all_fields(root, rows, lambda r: _format_score(r["score_value"], scoring_type))
    _add_numbered_fields(root, rows, lambda r: _format_score(r["score_value"], scoring_type))

    for i, r in enumerate(rows, start=1):
        full_names, last_names = _team_names(r["name"], r["partner"])
        entry_el = ET.SubElement(root, "entry")
        _sub(entry_el, "rank", i)
        _sub(entry_el, "name", r["name"])
        _sub(entry_el, "hometown", r["hometown"])
        _sub(entry_el, "sponsor", r["sponsor"])
        _sub(entry_el, "draw_animal", r["draw_animal"])
        _sub(entry_el, "score", _format_score(r["score_value"], scoring_type))
        _sub(entry_el, "team_full_names", full_names)
        _sub(entry_el, "team_last_names", last_names)

    return _write_xml(root, "round_leaderboard.xml")


def export_aggregate_leaderboard():
    """Aggregate leaderboard: automatically combines a competitor's scores
    across every round in the event -- no manual round selection needed.
    Displayed as e.g. "186/2" (186 points on 2 head)."""
    event_id = get_setting("live_event_id")

    root = ET.Element("aggregate_leaderboard")
    conn = get_conn()

    event_row = None
    rows = []
    scoring_type = "judged"

    if event_id:
        event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if event_row:
        scoring_type = event_row["scoring_type"]
        round_ids = [r["id"] for r in conn.execute("SELECT id FROM rounds WHERE event_id = ?", (event_id,))]
        if round_ids:
            placeholders = ",".join("?" * len(round_ids))
            rows = conn.execute(
                f"""
                SELECT c.id, c.name, c.hometown, c.sponsor,
                       COALESCE(pc.name, c.partner) as partner,
                       SUM(e.score_value) as total, COUNT(e.id) as head
                FROM entries e
                JOIN competitors c ON e.competitor_id = c.id
                LEFT JOIN competitors pc ON c.partner_id = pc.id
                WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
                GROUP BY c.id
                """,
                round_ids,
            ).fetchall()

    conn.close()

    # Same reasoning as round_leaderboard.xml: a partner_id can carry over
    # from a completely different event, so only apply header/heeler
    # pairing when this event is actually marked as a team event.
    if not (event_row and event_row["is_team"]):
        rows = [dict(r) for r in rows]
        for r in rows:
            r["partner"] = ""

    # Same principle as round_leaderboard.xml: root attributes and the
    # all_* fields are always present (empty if there's nothing to show)
    # rather than the element being missing outright, since a stable
    # document shape is safer for a broadcast graphics data binding than
    # one that sometimes drops elements. <entry> is naturally variable
    # length, since a leaderboard has as many rows as there are scores.
    root.set("event", event_row["name"] if event_row else "")

    # More head completed always ranks ahead of fewer head, regardless of
    # raw total -- a rider who's been on 2 head has done more than one who's
    # only been on 1, even if that 1 run's time/score looks better on paper.
    # Only within the same head count does the total decide the order.
    if scoring.is_timed(scoring_type):
        rows = sorted(rows, key=lambda r: (-r["head"], r["total"]))
    else:
        rows = sorted(rows, key=lambda r: (-r["head"], -r["total"]))

    _add_all_fields(
        root, rows,
        lambda r: f"{_format_number(r['total'], scoring_type)}/{r['head']}",
    )
    _add_numbered_fields(
        root, rows,
        lambda r: f"{_format_number(r['total'], scoring_type)}/{r['head']}",
    )

    for i, r in enumerate(rows, start=1):
        full_names, last_names = _team_names(r["name"], r["partner"])
        entry_el = ET.SubElement(root, "entry")
        _sub(entry_el, "rank", i)
        _sub(entry_el, "name", r["name"])
        _sub(entry_el, "hometown", r["hometown"])
        _sub(entry_el, "sponsor", r["sponsor"])
        _sub(entry_el, "aggregate_score", f"{_format_number(r['total'], scoring_type)}/{r['head']}")
        _sub(entry_el, "total", _format_number(r["total"], scoring_type))
        _sub(entry_el, "head", r["head"])
        _sub(entry_el, "team_full_names", full_names)
        _sub(entry_el, "team_last_names", last_names)

    return _write_xml(root, "aggregate_leaderboard.xml")


def export_fifty_fifty():
    """50/50 jackpot lower third: a running total (updated whenever the
    committee wants it shown) and a winning number (filled in once drawn).
    No history/reset logic here -- just the two current values, always
    present even when blank, same rule as every other export."""
    conn = get_conn()
    row = conn.execute("SELECT total, winning_number FROM fifty_fifty WHERE id = 1").fetchone()
    conn.close()
    root = ET.Element("fifty_fifty")
    _sub(root, "total", row["total"] if row else "")
    _sub(root, "winning_number", row["winning_number"] if row else "")
    return _write_xml(root, "fifty_fifty.xml")


def export_all():
    export_whos_up()
    export_round_leaderboard()
    export_aggregate_leaderboard()
    export_fifty_fifty()
