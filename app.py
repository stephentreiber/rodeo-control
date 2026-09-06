import webbrowser
import threading
import os
import re
import json
import uuid
import socket
import shutil
import sqlite3
import tempfile
import datetime
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, after_this_request

import database
from database import get_conn, init_db, get_setting, set_setting, DEFAULT_SETTINGS
import xml_export
import importer
import scoring

app = Flask(__name__)
app.secret_key = "rodeo-local-app-secret"
app.jinja_env.globals["is_timed"] = scoring.is_timed
app.jinja_env.globals["scoring_short_label"] = scoring.short_label
app.jinja_env.globals["SCORING_TYPES"] = scoring.SCORING_TYPES

STANDARD_EVENTS = [
    "Bareback Riding", "Saddle Bronc", "Bull Riding", "Barrel Racing",
    "Tie-Down Roping", "Team Roping", "Steer Wrestling", "Breakaway Roping",
]

STATUS_LABELS = scoring.STATUS_LABELS


def _judge_names():
    """Custom display names for judge seats 1-4, from Settings --
    defaults to "Judge N" for any seat that hasn't been given a name.
    Used everywhere a seat number would otherwise be shown alone: the
    seat picker, a judge's own scoring screen, the scorekeeper's Judges
    grid, and any disagreement messages -- so the scorekeeper can see
    exactly who's missing a score instead of just an anonymous number."""
    return {seat: (get_setting(f"judge_name_{seat}", "").strip() or f"Judge {seat}") for seat in range(1, 5)}


def _video_review_enabled():
    """Whether the VR (Video Review) quick-entry code is turned on for
    this rodeo -- off by default since not every rodeo has video review
    available (see the Settings page)."""
    return bool(get_setting("enable_video_review", DEFAULT_SETTINGS["enable_video_review"]))


def _judge_outcomes():
    """scoring.JUDGE_OUTCOMES, plus VR when this rodeo has video review
    turned on. VR is opt-in per rodeo, not a fixed part of the outcome
    vocabulary like BO/DG/MO/etc. -- computed per-request (rather than
    baked into the module-level dict in scoring.py) since the setting
    can be flipped anytime, and every place that shows or accepts a
    judge outcome needs to agree on whether VR currently counts."""
    outcomes = dict(scoring.JUDGE_OUTCOMES)
    if _video_review_enabled():
        outcomes["VR"] = "Video Review"
    return outcomes


def _judge_outcomes_no_rider_score():
    """scoring.JUDGE_OUTCOMES_NO_RIDER_SCORE, unchanged -- VR deliberately
    does NOT belong in this list. A video review often confirms the
    score as given rather than negating it, so unlike BO/DG/MO/SLAP
    (which always mean there's no score at all), a judge flagging VR
    still needs to give their normal rider score alongside it -- that's
    what ends up held pending review (see finalize_judge_score)."""
    return list(scoring.JUDGE_OUTCOMES_NO_RIDER_SCORE)


@app.context_processor
def inject_globals():
    conn = get_conn()
    nav_events = conn.execute("SELECT id, name FROM events ORDER BY position, created_at").fetchall()
    live_event_id = get_setting("live_event_id")
    live_event_name = None
    live_round_numbers = []
    if live_event_id:
        row = conn.execute("SELECT name FROM events WHERE id = ?", (live_event_id,)).fetchone()
        if row:
            live_event_name = row["name"]
            nums = conn.execute(
                "SELECT DISTINCT round_number FROM rounds WHERE event_id = ? ORDER BY round_number",
                (live_event_id,),
            ).fetchall()
            live_round_numbers = [n["round_number"] for n in nums]
    conn.close()
    rodeo_name = get_setting("rodeo_name", "My Rodeo")
    return {
        "rodeo_name": rodeo_name,
        "xml_export_status": xml_export.xml_export_status(),
        "nav_events": nav_events,
        "nav_live_event_id": live_event_id,
        "nav_live_event_name": live_event_name,
        "nav_live_round_numbers": live_round_numbers,
        "nav_active_round_number": get_setting("active_round_number"),
        "judge_names": _judge_names(),
        "enable_video_review": _video_review_enabled(),
    }


@app.route("/live/event", methods=["POST"])
def set_live_event():
    eid = request.form.get("event_id", "").strip()
    if eid:
        set_setting("live_event_id", eid)
        set_setting("active_round_number", "")  # reset round choice on event switch
        xml_export.export_all()
    return redirect(request.referrer or url_for("index"))


@app.route("/live/round", methods=["POST"])
def set_live_round():
    num = request.form.get("round_number", "").strip()
    if num:
        set_setting("active_round_number", num)
        xml_export.export_all()
    return redirect(request.referrer or url_for("index"))


# ---------- Dashboard ----------
@app.route("/")
def index():
    conn = get_conn()
    events = conn.execute("SELECT * FROM events ORDER BY position, created_at").fetchall()
    counts = conn.execute("SELECT COUNT(*) as n FROM competitors").fetchone()
    conn.close()
    return render_template("index.html", events=events, competitor_count=counts["n"])


# ---------- Competitors ----------
@app.route("/competitors", methods=["GET", "POST"])
def competitors():
    conn = get_conn()
    if request.method == "POST":
        partner_name = importer.format_name(request.form.get("partner", "").strip())
        cur = conn.execute(
            "INSERT INTO competitors (name, hometown, sponsor, partner, draw_animal, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                importer.format_name(request.form["name"].strip()),
                importer.fix_hometown(request.form.get("hometown", "").strip()),
                request.form.get("sponsor", "").strip(),
                partner_name,
                importer.format_draw_animal(request.form.get("draw_animal", "").strip()),
                request.form.get("notes", "").strip(),
            ),
        )
        new_id = cur.lastrowid
        if partner_name:
            match = conn.execute(
                "SELECT id FROM competitors WHERE UPPER(name) = UPPER(?) AND id != ?",
                (partner_name, new_id),
            ).fetchone()
            if match:
                conn.execute("UPDATE competitors SET partner_id = ? WHERE id = ?", (match["id"], new_id))
                conn.execute("UPDATE competitors SET partner_id = ? WHERE id = ?", (new_id, match["id"]))
        conn.commit()
        conn.close()
        return redirect(url_for("competitors"))
    people = conn.execute("SELECT * FROM competitors ORDER BY name").fetchall()
    conn.close()
    return render_template("competitors.html", people=people)


@app.route("/competitors/<int:cid>/delete", methods=["POST"])
def delete_competitor(cid):
    conn = get_conn()
    conn.execute("DELETE FROM entries WHERE competitor_id = ?", (cid,))
    conn.execute("DELETE FROM competitors WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("competitors"))


# ---------- Events & Rounds ----------
@app.route("/events", methods=["GET", "POST"])
def events():
    conn = get_conn()
    if request.method == "POST":
        next_pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 as n FROM events").fetchone()["n"]
        name = request.form["name"].strip()
        is_team = 1 if (request.form.get("is_team") == "on" or importer.guess_is_team_event(name)) else 0
        conn.execute(
            "INSERT INTO events (name, scoring_type, is_team, position) VALUES (?, ?, ?, ?)",
            (name, request.form["scoring_type"], is_team, next_pos),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("events"))
    evts = conn.execute("SELECT * FROM events ORDER BY position, created_at").fetchall()
    conn.close()
    return render_template("events.html", events=evts, standard_events=STANDARD_EVENTS)


@app.route("/events/<int:eid>/move/<direction>", methods=["POST"])
def move_event(eid, direction):
    conn = get_conn()
    all_ev = conn.execute("SELECT id, position FROM events ORDER BY position, created_at").fetchall()
    idx = next((i for i, e in enumerate(all_ev) if e["id"] == eid), None)
    if idx is not None:
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(all_ev):
            other = all_ev[swap_idx]
            conn.execute("UPDATE events SET position = ? WHERE id = ?", (other["position"], eid))
            conn.execute("UPDATE events SET position = ? WHERE id = ?", (all_ev[idx]["position"], other["id"]))
            conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("index"))


@app.route("/events/<int:eid>/delete", methods=["POST"])
def delete_event(eid):
    conn = get_conn()
    round_ids = [r["id"] for r in conn.execute("SELECT id FROM rounds WHERE event_id = ?", (eid,))]
    for rid in round_ids:
        conn.execute("DELETE FROM entries WHERE round_id = ?", (rid,))
    conn.execute("DELETE FROM rounds WHERE event_id = ?", (eid,))
    conn.execute("DELETE FROM events WHERE id = ?", (eid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("events"))


@app.route("/events/<int:eid>/scoring_type", methods=["POST"])
def update_scoring_type(eid):
    """Auto-detected on import, but the guess isn't always right -- editable
    from the Dashboard so it's easy to fix without re-importing."""
    new_type = request.form.get("scoring_type", "")
    if new_type in scoring.SCORING_TYPES:
        conn = get_conn()
        conn.execute("UPDATE events SET scoring_type = ? WHERE id = ?", (new_type, eid))
        conn.commit()
        conn.close()
        xml_export.export_all()
    return redirect(request.referrer or url_for("index"))


@app.route("/events/<int:eid>/update_name", methods=["POST"])
def update_event_name(eid):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_conn()
        conn.execute("UPDATE events SET name = ? WHERE id = ?", (name, eid))
        conn.commit()
        conn.close()
        xml_export.export_all()
    return redirect(request.referrer or url_for("events"))


@app.route("/events/<int:eid>/toggle_team", methods=["POST"])
def toggle_is_team(eid):
    """Whether this event shows a Heeler column and uses header/heeler
    pairing -- an explicit per-event setting rather than something
    inferred from any competitor's cross-event partner link, since a team
    roper can also be entered solo in an unrelated event."""
    conn = get_conn()
    row = conn.execute("SELECT is_team FROM events WHERE id = ?", (eid,)).fetchone()
    if row:
        conn.execute("UPDATE events SET is_team = ? WHERE id = ?", (0 if row["is_team"] else 1, eid))
        conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("events"))


def _get_live_panel_context(conn):
    """Everything the Current Contestant panel needs to render, based
    purely on whoever is genuinely live (Who's Up) right now --
    entirely independent of which round or event page happens to be
    open. This is what makes the panel decoupled from navigation: a
    scorekeeper can browse to a totally different round (or event) to
    make an edit, and the panel keeps showing the actual live
    contestant -- including its own event's scoring type, so the
    penalty buttons / Judges section reflect the LIVE entry, not
    whatever page happens to be open. Returns None if nobody's live."""
    whos_up_entry_id = conn.execute("SELECT entry_id FROM whos_up WHERE id = 1").fetchone()["entry_id"]
    if not whos_up_entry_id:
        return None

    live = conn.execute(
        """
        SELECT e.round_id, r.event_id, r.locked as round_locked
        FROM entries e JOIN rounds r ON e.round_id = r.id
        WHERE e.id = ?
        """,
        (whos_up_entry_id,),
    ).fetchone()
    if not live:
        return None

    live_event = conn.execute("SELECT * FROM events WHERE id = ?", (live["event_id"],)).fetchone()
    if not live_event:
        return None

    raw_entries = conn.execute(
        """
        SELECT e.id, e.draw_number, e.score_value, e.status, e.re_ride_taken,
               e.penalty_note, e.pending_penalty, e.pending_penalty_note, e.pending_score,
               c.name, c.hometown,
               COALESCE(pc.name, c.partner) as partner,
               pc.hometown as partner_hometown,
               COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal,
               EXISTS(SELECT 1 FROM judge_scores js WHERE js.entry_id = e.id AND js.outcome = 'RR') as has_rr_flag
        FROM entries e
        JOIN competitors c ON e.competitor_id = c.id
        LEFT JOIN competitors pc ON c.partner_id = pc.id
        WHERE e.round_id = ?
        ORDER BY e.draw_order
        """,
        (live["round_id"],),
    ).fetchall()

    panel_entries = []
    current_index = 0
    for i, e in enumerate(raw_entries):
        panel_entries.append(
            {
                "id": e["id"],
                "draw_number": e["draw_number"],
                "name": e["name"],
                "hometown": e["hometown"],
                "partner": e["partner"],
                "partner_hometown": e["partner_hometown"],
                "draw_animal": e["draw_animal"],
                "display": scoring.display_for(e["status"], e["score_value"], live_event["scoring_type"]),
                "status": e["status"],
                "round_locked": live["round_locked"],
                "re_ride_taken": bool(e["re_ride_taken"]),
                "has_rr_flag": bool(e["has_rr_flag"]),
                "penalty_note": e["penalty_note"] or "",
                # A judge-flagged Barrier/One Heel penalty called on the
                # live run before a time exists yet -- see judge_timed_penalty.
                # Only ever non-zero for whichever entry is genuinely live.
                "pending_penalty": e["pending_penalty"] or 0,
                "pending_penalty_note": e["pending_penalty_note"] or "",
                # A judged entry a judge flagged for Video Review -- the
                # computed score sits here, not yet counted, until the
                # scorekeeper resolves it (see resolve_video_review).
                "pending_score": e["pending_score"],
            }
        )
        if e["id"] == whos_up_entry_id:
            current_index = i

    return {
        "panel_entries": panel_entries,
        "current_index": current_index,
        "is_team_event": bool(live_event["is_team"]),
        "scoring_type": live_event["scoring_type"],
        "whos_up_entry_id": whos_up_entry_id,
        "event_id": live_event["id"],
        "event_name": live_event["name"],
    }


@app.route("/whos-up/panel-context")
def whos_up_panel_context():
    """JSON version of _get_live_panel_context, for the panel to refresh
    itself after Who's Up changes to an entry that isn't in whatever
    round list it currently has cached client-side (e.g. the
    scorekeeper clicked "Set Who's Up" on a row from a completely
    different round than the one the panel was showing)."""
    conn = get_conn()
    ctx = _get_live_panel_context(conn)
    conn.close()
    if not ctx:
        return jsonify(ok=True, live=False)
    return jsonify(ok=True, live=True, **ctx)


@app.route("/events/<int:eid>")
def event_detail(eid):
    conn = get_conn()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if not event:
        conn.close()
        return redirect(url_for("events"))

    all_events = conn.execute("SELECT id, name FROM events ORDER BY position, created_at").fetchall()
    rounds = conn.execute(
        "SELECT * FROM rounds WHERE event_id = ? ORDER BY round_number", (eid,)
    ).fetchall()

    requested = request.args.get("round", "")
    if requested == "all" or (not requested and not rounds):
        current_round_key = "all"
    elif requested and any(str(r["id"]) == requested for r in rounds):
        current_round_key = requested
    else:
        current_round_key = str(rounds[0]["id"]) if rounds else "all"

    if current_round_key == "all":
        entries = conn.execute(
            """
            SELECT e.id, e.round_id, e.competitor_id, e.draw_order, e.draw_number,
                   e.score_value, e.status, e.updated_at, e.re_ride_taken, e.penalty_note, e.pending_score,
                   c.name, c.hometown, c.partner_id,
                   COALESCE(pc.name, c.partner) as partner,
                   pc.hometown as partner_hometown,
                   COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal,
                   r.name as round_name, r.locked as round_locked,
                   EXISTS(SELECT 1 FROM judge_scores js WHERE js.entry_id = e.id AND js.outcome = 'RR') as has_rr_flag
            FROM entries e
            JOIN competitors c ON e.competitor_id = c.id
            LEFT JOIN competitors pc ON c.partner_id = pc.id
            JOIN rounds r ON e.round_id = r.id
            WHERE r.event_id = ?
            ORDER BY r.round_number, e.draw_order
            """,
            (eid,),
        ).fetchall()
        current_round_row = None
    else:
        entries = conn.execute(
            """
            SELECT e.id, e.round_id, e.competitor_id, e.draw_order, e.draw_number,
                   e.score_value, e.status, e.updated_at, e.re_ride_taken, e.penalty_note, e.pending_score,
                   c.name, c.hometown, c.partner_id,
                   COALESCE(pc.name, c.partner) as partner,
                   pc.hometown as partner_hometown,
                   COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal,
                   EXISTS(SELECT 1 FROM judge_scores js WHERE js.entry_id = e.id AND js.outcome = 'RR') as has_rr_flag
            FROM entries e
            JOIN competitors c ON e.competitor_id = c.id
            LEFT JOIN competitors pc ON c.partner_id = pc.id
            WHERE e.round_id = ? ORDER BY e.draw_order
            """,
            (current_round_key,),
        ).fetchall()
        current_round_row = next((r for r in rounds if str(r["id"]) == current_round_key), None)

    # Team roping (or any partnered event) doesn't track a stock animal, so
    # that column gets repurposed to show the heeler instead. This is an
    # explicit per-event flag, not inferred from whether any competitor in
    # this event happens to have a partner link -- a team roper who's also
    # entered solo in tie-down or steer wrestling still carries that
    # partner_id on their competitor record, but it has nothing to do with
    # this event.
    is_team_event = bool(event["is_team"])

    entries = [dict(e) for e in entries]
    for e in entries:
        e["display"] = scoring.display_for(e["status"], e["score_value"], event["scoring_type"])
        if current_round_key != "all":
            e["round_locked"] = current_round_row["locked"] if current_round_row else 0

    entered_ids = {e["competitor_id"] for e in entries} if current_round_key != "all" else set()
    available = []
    if current_round_key != "all":
        available = conn.execute("SELECT * FROM competitors ORDER BY name").fetchall()
        # Blank-name rows are re-ride reserve placeholders (importer.py)
        # or unfilled team-late-entry partners (add_late_entry) -- they
        # exist to be filled in via update_competitor_field, not to be
        # picked from this list, so they'd otherwise pile up here as
        # empty options round after round.
        available = [c for c in available if c["id"] not in entered_ids and c["name"].strip()]

    whos_up_entry_id = conn.execute("SELECT entry_id FROM whos_up WHERE id = 1").fetchone()["entry_id"]

    # The Current Contestant panel is deliberately independent of
    # whichever round/event this page happens to be showing -- it
    # always reflects whoever is genuinely live, so a scorekeeper can
    # browse elsewhere to make an edit and keep watching scores come in
    # for the actual live contestant the whole time.
    live_panel = _get_live_panel_context(conn)
    conn.close()

    panel_entries = live_panel["panel_entries"] if live_panel else []
    current_index = live_panel["current_index"] if live_panel else 0
    panel_is_team_event = live_panel["is_team_event"] if live_panel else False
    panel_scoring_type = live_panel["scoring_type"] if live_panel else ""
    panel_event_id = live_panel["event_id"] if live_panel else None
    panel_event_name = live_panel["event_name"] if live_panel else ""

    return render_template(
        "event_detail.html",
        event=event,
        all_events=all_events,
        rounds=rounds,
        current_round_key=current_round_key,
        current_round=current_round_row,
        entries=entries,
        available=available,
        whos_up_entry_id=whos_up_entry_id,
        is_team_event=is_team_event,
        panel_entries=panel_entries,
        current_index=current_index,
        panel_is_team_event=panel_is_team_event,
        panel_scoring_type=panel_scoring_type,
        panel_event_id=panel_event_id,
        panel_event_name=panel_event_name,
        hide_contestant_panel=(get_setting("hide_contestant_panel") == "1"),
        judge_outcomes=_judge_outcomes(),
        no_rider_score_outcomes=_judge_outcomes_no_rider_score(),
    )


@app.route("/entries/<int:eid>/judge_scores")
def entry_judge_scores(eid):
    """Every judge's individual submission for one entry -- shown to the
    scorekeeper as-is, never merged or averaged here. Different judges
    can and do call the same ride differently (one says Buck Off,
    another gives it a real score), so the scorekeeper needs to see each
    judge's own mark to make sense of what's coming in, not a single
    blended number."""
    judge_count = _judge_count()
    conn = get_conn()
    rows = conn.execute(
        "SELECT judge_seat, rider_score, stock_score, outcome FROM judge_scores WHERE entry_id = ?",
        (eid,),
    ).fetchall()
    conn.close()
    by_seat = {r["judge_seat"]: r for r in rows}
    judges = []
    for seat in range(1, judge_count + 1):
        r = by_seat.get(seat)
        judges.append(
            {
                "seat": seat,
                "submitted": r is not None,
                "rider_score": r["rider_score"] if r else None,
                "stock_score": r["stock_score"] if r else None,
                "outcome": r["outcome"] if r else None,
            }
        )
    return jsonify(ok=True, judge_count=judge_count, judges=judges)


@app.route("/entries/<int:eid>/judge_scores/<int:seat>/manual_set", methods=["POST"])
def judge_score_manual_set(eid, seat):
    """Lets the scorekeeper enter or correct one judge's score directly
    -- for when a judge's phone/tablet isn't available, or to fix a
    mistaken submission after the fact. Same validation as a judge's
    own submission (judge_submit), but deliberately skips that route's
    "is this entry still the live one" check -- the whole point here is
    being able to fix ANY entry's scores, not just whoever's currently
    up."""
    judge_count = _judge_count()
    if seat < 1 or seat > judge_count:
        return jsonify(ok=False, error="Invalid judge seat."), 400

    conn = get_conn()
    entry = conn.execute("SELECT id FROM entries WHERE id = ?", (eid,)).fetchone()
    if not entry:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404

    try:
        stock_score = float(request.form.get("stock_score", ""))
    except (TypeError, ValueError):
        conn.close()
        return jsonify(ok=False, error="Missing or invalid stock score."), 400
    if not scoring.valid_judge_score(stock_score):
        conn.close()
        return jsonify(ok=False, error="Stock score must be 1-25 in half points."), 400

    outcome = request.form.get("outcome", "").strip().upper()
    if outcome and outcome not in _judge_outcomes():
        conn.close()
        return jsonify(ok=False, error="Unrecognized outcome."), 400

    rider_score = None
    if not outcome or outcome not in _judge_outcomes_no_rider_score():
        try:
            rider_score = float(request.form.get("rider_score", ""))
        except (TypeError, ValueError):
            conn.close()
            return jsonify(ok=False, error="Missing or invalid rider score."), 400
        if not scoring.valid_judge_score(rider_score):
            conn.close()
            return jsonify(ok=False, error="Rider score must be 1-25 in half points."), 400

    conn.execute(
        """
        INSERT INTO judge_scores (entry_id, judge_seat, rider_score, stock_score, outcome, submitted_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(entry_id, judge_seat) DO UPDATE SET
            rider_score = excluded.rider_score,
            stock_score = excluded.stock_score,
            outcome = excluded.outcome,
            submitted_at = excluded.submitted_at
        """,
        (eid, seat, rider_score, stock_score, outcome or None),
    )
    conn.commit()

    combined_rider, combined_stock = _combined_preview(conn, eid, judge_count)
    conn.close()
    return jsonify(ok=True, combined_rider=combined_rider, combined_stock=combined_stock)


@app.route("/entries/<int:eid>/judge_scores/<int:seat>/clear", methods=["POST"])
def judge_score_clear(eid, seat):
    """Wipes one judge's submission for one entry back to blank/not-yet-
    submitted -- for a scratch reset (bad manual entry, or a judge's
    device sent something garbled) rather than overwriting it with a
    corrected value."""
    judge_count = _judge_count()
    conn = get_conn()
    conn.execute("DELETE FROM judge_scores WHERE entry_id = ? AND judge_seat = ?", (eid, seat))
    conn.commit()
    combined_rider, combined_stock = _combined_preview(conn, eid, judge_count)
    conn.close()
    return jsonify(ok=True, combined_rider=combined_rider, combined_stock=combined_stock)


def _combined_preview(conn, eid, judge_count):
    """The rider/stock totals this entry's judge_scores rows would
    combine to right now (or None for either side if not every judge
    has a value yet) -- used so the manual-entry table can show a live
    preview of the combined score without needing Finalize Score or a
    page reload."""
    rows = conn.execute(
        "SELECT judge_seat, rider_score, stock_score FROM judge_scores WHERE entry_id = ?", (eid,)
    ).fetchall()
    rider_by_seat = {r["judge_seat"]: r["rider_score"] for r in rows if r["rider_score"] is not None}
    stock_by_seat = {r["judge_seat"]: r["stock_score"] for r in rows if r["stock_score"] is not None}
    return (
        scoring.combine_scores_by_seat(rider_by_seat, judge_count),
        scoring.combine_scores_by_seat(stock_by_seat, judge_count),
    )


@app.route("/events/<int:eid>/judge-scores")
def judge_scores_table(eid):
    """A dense grid, one row per entry and one column-group per judge
    seat (rider score / stock score / outcome), for two things at once:
    reviewing every score a judge has given across a round at a glance,
    and typing scores in directly when a judge's phone/tablet isn't
    available -- both need the same "see and edit every seat" view, so
    one page covers both rather than building two separate UIs."""
    conn = get_conn()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if not event:
        conn.close()
        return redirect(url_for("events"))
    if scoring.is_timed(event["scoring_type"]):
        conn.close()
        flash("Judge Scores only applies to judged (roughstock) events.")
        return redirect(url_for("event_detail", eid=eid))

    rounds = conn.execute(
        "SELECT * FROM rounds WHERE event_id = ? ORDER BY round_number", (eid,)
    ).fetchall()
    requested = request.args.get("round", "")
    if requested == "all" or (not requested and not rounds):
        current_round_key = "all"
    elif requested and any(str(r["id"]) == requested for r in rounds):
        current_round_key = requested
    else:
        current_round_key = str(rounds[0]["id"]) if rounds else "all"

    if current_round_key == "all":
        entry_rows = conn.execute(
            """
            SELECT e.id, e.draw_number, e.status, e.score_value,
                   c.name, c.hometown, COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal,
                   r.name as round_name
            FROM entries e
            JOIN competitors c ON e.competitor_id = c.id
            JOIN rounds r ON e.round_id = r.id
            WHERE r.event_id = ?
            ORDER BY r.round_number, e.draw_order
            """,
            (eid,),
        ).fetchall()
    else:
        entry_rows = conn.execute(
            """
            SELECT e.id, e.draw_number, e.status, e.score_value,
                   c.name, c.hometown, COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as draw_animal
            FROM entries e
            JOIN competitors c ON e.competitor_id = c.id
            WHERE e.round_id = ? ORDER BY e.draw_order
            """,
            (current_round_key,),
        ).fetchall()

    judge_count = _judge_count()
    names = _judge_names()
    entries = []
    for e in entry_rows:
        js_rows = conn.execute(
            "SELECT judge_seat, rider_score, stock_score, outcome FROM judge_scores WHERE entry_id = ?",
            (e["id"],),
        ).fetchall()
        by_seat = {jr["judge_seat"]: jr for jr in js_rows}
        seats = []
        for seat in range(1, judge_count + 1):
            jr = by_seat.get(seat)
            seats.append({
                "seat": seat,
                "rider_score": jr["rider_score"] if jr else None,
                "stock_score": jr["stock_score"] if jr else None,
                "outcome": jr["outcome"] if jr else "",
            })
        combined_rider, combined_stock = _combined_preview(conn, e["id"], judge_count)
        entries.append({
            "id": e["id"],
            "draw_number": e["draw_number"],
            "name": e["name"],
            "hometown": e["hometown"],
            "draw_animal": e["draw_animal"],
            "round_name": e["round_name"] if current_round_key == "all" else None,
            "seats": seats,
            "combined_rider": combined_rider,
            "combined_stock": combined_stock,
            "display": scoring.display_for(e["status"], e["score_value"], event["scoring_type"]),
        })
    conn.close()

    return render_template(
        "judge_scores_table.html",
        event=event,
        rounds=rounds,
        current_round_key=current_round_key,
        entries=entries,
        judge_count=judge_count,
        judge_names=names,
        judge_outcomes=_judge_outcomes(),
        judge_outcomes_no_rider=_judge_outcomes_no_rider_score(),
    )


@app.route("/entries/<int:eid>/finalize_judge_score", methods=["POST"])
def finalize_judge_score(eid):
    """Turns judge submissions into the entry's official score.

    A penalty (BO/DG/MO/SLAP) called by even one judge overrules any
    numeric rider scores the other judges gave -- a penalty is
    typically an objective call (an infraction that judge saw), not a
    quality opinion that should get averaged away by judges who scored
    it normally. RR doesn't override anything since it still carries a
    real score; it just gets tagged onto whatever the numeric
    combination works out to. The only thing this still refuses to
    resolve on its own is two judges calling two DIFFERENT penalties
    (e.g. one says Buck Off, another says Slapped Animal) -- that's a
    genuine conflict between two judgment calls, and the scorekeeper
    sorts it out by typing the score or code in directly.

    The official score is rider+stock together, same as every other
    judged score in this app (Who's Up, round/aggregate leaderboards,
    XML exports) -- the independent stock-only tracking is purely for
    the Stock tab, it doesn't change what counts here."""
    judge_count = _judge_count()
    conn = get_conn()
    entry = conn.execute("SELECT id FROM entries WHERE id = ?", (eid,)).fetchone()
    if not entry:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404

    judge_rows = conn.execute(
        "SELECT judge_seat, rider_score, stock_score, outcome FROM judge_scores WHERE entry_id = ?", (eid,)
    ).fetchall()
    by_seat = {r["judge_seat"]: r for r in judge_rows}

    missing = [s for s in range(1, judge_count + 1) if s not in by_seat]
    if missing:
        conn.close()
        got = judge_count - len(missing)
        return jsonify(ok=False, error=f"Not all judges have submitted yet ({got} of {judge_count})."), 400

    # Only look at judges who actually called a penalty -- a judge who
    # simply gave a numeric score isn't "disagreeing," they just didn't
    # see anything worth flagging.
    distinct_outcomes = {by_seat[s]["outcome"] for s in range(1, judge_count + 1) if by_seat[s]["outcome"]}
    if len(distinct_outcomes) > 1:
        conn.close()
        names = _judge_names()
        parts = [f"{names[s]}: {by_seat[s]['outcome']}" for s in range(1, judge_count + 1) if by_seat[s]["outcome"]]
        return jsonify(
            ok=False,
            error="Judges called different penalties (" + "; ".join(parts) + ") -- enter the score manually.",
        ), 409

    outcome = next(iter(distinct_outcomes), None)
    if outcome and outcome in _judge_outcomes_no_rider_score():
        # This penalty overrules -- any numeric scores other judges gave
        # for this ride are set aside in favor of the penalty call.
        status = scoring.TOKEN_MAP[outcome][0]
        conn.execute(
            """UPDATE entries SET status = ?, score_value = NULL, penalty = 0, re_ride_taken = 0,
               pending_score = NULL, updated_at = datetime('now') WHERE id = ?""",
            (status, eid),
        )
        display = scoring.display_for(status, None, "judged")
    else:
        # No penalty call at all, or every flagged judge only flagged
        # Re-Ride or VR (both still carry a real score) -- combine
        # rider+stock normally. Each judge's rider+stock are added
        # together first, then those per-judge totals are summed (2
        # judges) or summed-then-halved (4) -- exactly how a directly-
        # typed judged score already works. re_ride_taken resets to 0
        # here too -- (re-)finalizing establishes the current official
        # score, which supersedes any earlier "took the re-ride"
        # decision (this is also how a mistaken "Take Re-Ride" click
        # gets undone: just finalize again).
        totals_by_seat = {}
        for s in range(1, judge_count + 1):
            jr = by_seat[s]
            if jr["rider_score"] is None or jr["stock_score"] is None:
                conn.close()
                return jsonify(ok=False, error="Missing a rider or stock score."), 400
            totals_by_seat[s] = jr["rider_score"] + jr["stock_score"]
        combined = scoring.combine_scores_by_seat(totals_by_seat, judge_count)
        if combined is None:
            conn.close()
            return jsonify(ok=False, error="Could not compute a combined score."), 400

        if outcome == "VR":
            # Unlike RR (fully scored immediately, with an OPTIONAL
            # later "give it up"), VR defaults to NOT counting yet --
            # the computed score sits in pending_score, held back from
            # score_value/the leaderboard, until the scorekeeper
            # confirms the review didn't change anything (see
            # resolve_video_review). A review often does confirm the
            # score as given, but sometimes doesn't, and nobody should
            # have to remember to walk back a number that's already
            # gone out on the leaderboard or broadcast.
            status = "video_review"
            conn.execute(
                """UPDATE entries SET status = 'video_review', score_value = NULL, pending_score = ?,
                   penalty = 0, re_ride_taken = 0, updated_at = datetime('now') WHERE id = ?""",
                (combined, eid),
            )
            display = scoring.display_for("video_review", None, "judged")
        else:
            status = "scored"
            conn.execute(
                """UPDATE entries SET status = 'scored', score_value = ?, penalty = 0, re_ride_taken = 0,
                   pending_score = NULL, updated_at = datetime('now') WHERE id = ?""",
                (combined, eid),
            )
            display = scoring.format_number(combined, "judged")

    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=display, status=status, outcome=outcome)


@app.route("/entries/<int:eid>/resolve_video_review", methods=["POST"])
def resolve_video_review(eid):
    """Confirms a pending Video Review score as final -- the scorekeeper's
    call once the actual review is done and the score stands as given.
    If the review instead changes something, there's no separate
    "reject" action needed: just type the corrected score or code
    directly into the field like any other correction, which already
    overwrites status/score_value (and, via the UPDATEs above, always
    clears pending_score too)."""
    conn = get_conn()
    entry = conn.execute("SELECT status, pending_score FROM entries WHERE id = ?", (eid,)).fetchone()
    if not entry:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404
    if entry["status"] != "video_review" or entry["pending_score"] is None:
        conn.close()
        return jsonify(ok=False, error="This entry isn't waiting on a video review right now."), 400

    combined = entry["pending_score"]
    conn.execute(
        "UPDATE entries SET status = 'scored', score_value = ?, pending_score = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (combined, eid),
    )
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=scoring.format_number(combined, "judged"), status="scored")


@app.route("/entries/<int:eid>/take_re_ride", methods=["POST"])
def take_re_ride(eid):
    """The rider's own call, relayed by the scorekeeper: give up the score
    that was just finalized in favor of coming back later on different
    stock. Clears the rider's score value entirely (so it no longer
    counts on the leaderboard, and the field shows "RR" same as if the
    scorekeeper had typed that code directly) -- but never touches
    anything stock-related, since stock is tracked independently via
    judge_scores and the Stock tab regardless of what happens with the
    rider. Reversible: re-clicking Finalize Score recomputes and
    reapplies the combined score, which also resets this flag."""
    conn = get_conn()
    entry = conn.execute("SELECT status FROM entries WHERE id = ?", (eid,)).fetchone()
    if not entry:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404
    if entry["status"] != "scored":
        conn.close()
        return jsonify(ok=False, error="This entry doesn't have a score to give up right now."), 400

    has_rr = conn.execute(
        "SELECT 1 FROM judge_scores WHERE entry_id = ? AND outcome = 'RR' LIMIT 1", (eid,)
    ).fetchone()
    if not has_rr:
        conn.close()
        return jsonify(ok=False, error="No judge flagged this as a re-ride."), 400

    conn.execute(
        """UPDATE entries SET status = 're_ride', score_value = NULL, penalty = 0, re_ride_taken = 1,
           updated_at = datetime('now') WHERE id = ?""",
        (eid,),
    )
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=scoring.display_for("re_ride", None, "judged"), status="re_ride")


@app.route("/settings/toggle_contestant_panel", methods=["POST"])
def toggle_contestant_panel():
    """Show/hide the Current Contestant panel on the round scoring page --
    a whole-operator preference (not per-event), for anyone who'd rather
    enter everything from the draw table instead."""
    current = get_setting("hide_contestant_panel", "")
    set_setting("hide_contestant_panel", "" if current == "1" else "1")
    return redirect(request.referrer or url_for("index"))


@app.route("/events/<int:eid>/rounds", methods=["POST"])
def add_round(eid):
    conn = get_conn()
    next_num = conn.execute(
        "SELECT COALESCE(MAX(round_number), 0) + 1 as n FROM rounds WHERE event_id = ?", (eid,)
    ).fetchone()["n"]
    round_number_raw = request.form.get("round_number", "").strip()
    round_number = int(round_number_raw) if round_number_raw.isdigit() else next_num
    name = request.form.get("name", "").strip() or f"Round {round_number}"
    cur = conn.execute(
        "INSERT INTO rounds (event_id, name, round_number) VALUES (?, ?, ?)",
        (eid, name, round_number),
    )
    new_round_id = cur.lastrowid
    conn.commit()
    conn.close()
    return redirect(url_for("event_detail", eid=eid, round=new_round_id))


@app.route("/rounds/<int:rid>/delete", methods=["POST"])
def delete_round(rid):
    conn = get_conn()
    row = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()
    eid = row["event_id"] if row else None
    conn.execute("DELETE FROM entries WHERE round_id = ?", (rid,))
    conn.execute("DELETE FROM rounds WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("event_detail", eid=eid)) if eid else redirect(url_for("events"))


@app.route("/rounds/<int:rid>/update", methods=["POST"])
def update_round(rid):
    """Edit a round's name and/or round number after the fact -- useful for
    fixing an import (e.g. it was assigned the wrong round number for
    leaderboard grouping) without having to delete and redo it."""
    conn = get_conn()
    row = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("events"))
    eid = row["event_id"]

    name = request.form.get("name", "").strip()
    round_number_raw = request.form.get("round_number", "").strip()

    updates, params = [], []
    if name:
        updates.append("name = ?")
        params.append(name)
    if round_number_raw.isdigit():
        updates.append("round_number = ?")
        params.append(int(round_number_raw))

    if updates:
        params.append(rid)
        conn.execute(f"UPDATE rounds SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("event_detail", eid=eid, round=rid))


@app.route("/rounds/<int:rid>/toggle_lock", methods=["POST"])
def toggle_round_lock(rid):
    """Locking a round disables editing name/hometown/stock for that
    round's entries -- a safeguard against accidental changes once the
    draw is finalized. Scoring is never locked."""
    conn = get_conn()
    row = conn.execute("SELECT event_id, locked FROM rounds WHERE id = ?", (rid,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("events"))
    conn.execute("UPDATE rounds SET locked = ? WHERE id = ?", (0 if row["locked"] else 1, rid))
    conn.commit()
    conn.close()
    return redirect(url_for("event_detail", eid=row["event_id"], round=rid))


# ---------- Draw / Scoring ----------
@app.route("/rounds/<int:rid>")
def round_scoring(rid):
    """Old direct round URL -- redirect into the tabbed event hub."""
    conn = get_conn()
    row = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()
    conn.close()
    if not row:
        return redirect(url_for("events"))
    return redirect(url_for("event_detail", eid=row["event_id"], round=rid))


@app.route("/rounds/<int:rid>/add_competitor", methods=["POST"])
def add_to_round(rid):
    cid = request.form["competitor_id"]
    conn = get_conn()
    eid = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()["event_id"]
    next_order = conn.execute(
        "SELECT COALESCE(MAX(draw_order), 0) + 1 as n FROM entries WHERE round_id = ?", (rid,)
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO entries (round_id, competitor_id, draw_order, status) VALUES (?, ?, ?, 'pending')",
        (rid, cid, next_order),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("event_detail", eid=eid, round=rid))


@app.route("/rounds/<int:rid>/add_late_entry", methods=["POST"])
def add_late_entry(rid):
    """Quickly append a blank row for a late competitor -- a new competitor
    record and a draw entry in one click, landing at the bottom of the
    draw ready to type a name into, without leaving the scoring page.
    For team roping (or any partnered event), a linked blank heeler
    competitor is created too, so the Heeler column is immediately
    editable instead of showing "no partner linked"."""
    conn = get_conn()
    row = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("events"))
    eid = row["event_id"]

    event_row = conn.execute("SELECT is_team FROM events WHERE id = ?", (eid,)).fetchone()
    is_team_event = bool(event_row["is_team"]) if event_row else False

    cur = conn.execute("INSERT INTO competitors (name, hometown) VALUES ('', '')")
    new_competitor_id = cur.lastrowid

    if is_team_event:
        cur_partner = conn.execute("INSERT INTO competitors (name, hometown) VALUES ('', '')")
        partner_id = cur_partner.lastrowid
        conn.execute("UPDATE competitors SET partner_id = ? WHERE id = ?", (partner_id, new_competitor_id))
        conn.execute("UPDATE competitors SET partner_id = ? WHERE id = ?", (new_competitor_id, partner_id))

    next_order = conn.execute(
        "SELECT COALESCE(MAX(draw_order), 0) + 1 as n FROM entries WHERE round_id = ?", (rid,)
    ).fetchone()["n"]
    next_draw_number = conn.execute(
        "SELECT COALESCE(MAX(draw_number), 0) + 1 as n FROM entries WHERE round_id = ?", (rid,)
    ).fetchone()["n"]
    cur2 = conn.execute(
        "INSERT INTO entries (round_id, competitor_id, draw_order, draw_number, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (rid, new_competitor_id, next_order, next_draw_number),
    )
    new_entry_id = cur2.lastrowid
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("event_detail", eid=eid, round=rid, added=new_entry_id))


@app.route("/entries/<int:eid>/remove", methods=["POST"])
def remove_entry(eid):
    conn = get_conn()
    row = conn.execute(
        "SELECT e.round_id, r.event_id, r.locked FROM entries e JOIN rounds r ON e.round_id = r.id WHERE e.id = ?",
        (eid,),
    ).fetchone()
    rid = row["round_id"] if row else None
    event_id = row["event_id"] if row else None
    if row and row["locked"]:
        conn.close()
        flash("This round is locked — unlock it to remove an entry.")
        return redirect(url_for("event_detail", eid=event_id, round=rid))
    conn.execute("UPDATE whos_up SET entry_id = NULL WHERE entry_id = ?", (eid,))
    conn.execute("DELETE FROM entries WHERE id = ?", (eid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    if event_id:
        return redirect(url_for("event_detail", eid=event_id, round=rid))
    return redirect(url_for("index"))


@app.route("/entries/<int:eid>/move/<direction>", methods=["POST"])
def move_entry(eid, direction):
    conn = get_conn()
    entry = conn.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
    rid = entry["round_id"]
    event_id = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()["event_id"]
    neighbors = conn.execute(
        "SELECT * FROM entries WHERE round_id = ? ORDER BY draw_order", (rid,)
    ).fetchall()
    idx = next(i for i, e in enumerate(neighbors) if e["id"] == eid)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap_idx < len(neighbors):
        other = neighbors[swap_idx]
        conn.execute("UPDATE entries SET draw_order = ? WHERE id = ?", (other["draw_order"], entry["id"]))
        conn.execute("UPDATE entries SET draw_order = ? WHERE id = ?", (entry["draw_order"], other["id"]))
        conn.commit()
    conn.close()
    return redirect(url_for("event_detail", eid=event_id, round=rid))


@app.route("/rounds/<int:rid>/reorder", methods=["POST"])
def reorder_entries(rid):
    """Drag-to-reorder: accepts the full ordered list of entry ids for
    this round, as the draw now reads top-to-bottom after a drag, and
    rewrites draw_order to match in one shot. Sits alongside the
    up/down arrows (move_entry) as an alternative way to do the same
    thing, rather than replacing them."""
    order_param = request.form.get("order", "")
    try:
        ordered_ids = [int(x) for x in order_param.split(",") if x.strip()]
    except ValueError:
        return jsonify(ok=False, error="Bad order list."), 400

    conn = get_conn()
    round_row = conn.execute("SELECT event_id FROM rounds WHERE id = ?", (rid,)).fetchone()
    if not round_row:
        conn.close()
        return jsonify(ok=False, error="Round not found."), 404

    # Only touch entries that actually belong to this round -- a stale
    # id list should never let one round's drag reorder reach into
    # another round's draw_order values.
    valid_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM entries WHERE round_id = ?", (rid,)
    ).fetchall()}
    ordered_ids = [eid for eid in ordered_ids if eid in valid_ids]

    for position, eid in enumerate(ordered_ids, start=1):
        conn.execute("UPDATE entries SET draw_order = ? WHERE id = ?", (position, eid))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True)


@app.route("/entries/<int:eid>/update_draw_animal", methods=["POST"])
def update_draw_animal(eid):
    """Inline edit of the per-round stock/draw-animal (a horse can change
    between when the draw was printed and when the go actually runs)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT e.id, r.locked FROM entries e JOIN rounds r ON e.round_id = r.id WHERE e.id = ?", (eid,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404
    if row["locked"]:
        conn.close()
        return jsonify(ok=False, error="This round is locked."), 403
    value = importer.format_draw_animal(request.form.get("value", "").strip())
    conn.execute("UPDATE entries SET draw_animal = ?, updated_at = datetime('now') WHERE id = ?", (value, eid))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=value)


@app.route("/competitors/<int:cid>/update_field", methods=["POST"])
def update_competitor_field(cid):
    """Inline edit of a competitor's name or hometown from the draw page.
    These are shared across every round/event the competitor is entered
    in, so the change applies everywhere at once.

    Renaming to match an EXISTING different competitor merges into that
    record instead of creating a second one with the same name -- the
    aggregate leaderboard groups by competitor id, not name text, so two
    same-named-but-different-id records would silently be treated as two
    different people. This is exactly what happens when a re-ride
    reserve row (imported with a blank name -- see importer.py) gets
    filled in with a rider who already has a competitor record from
    elsewhere in the event: without this, their re-ride score would
    never combine with their other round(s) on the aggregate board."""
    field = request.form.get("field", "")
    raw = request.form.get("value", "").strip()
    if field == "name":
        value = importer.format_name(raw)
    elif field == "hometown":
        value = importer.fix_hometown(raw)
    else:
        return jsonify(ok=False, error="Unknown field."), 400

    conn = get_conn()
    this_row = conn.execute("SELECT * FROM competitors WHERE id = ?", (cid,)).fetchone()
    if not this_row:
        conn.close()
        return jsonify(ok=False, error="Competitor not found."), 404

    if field == "name" and value:
        existing = conn.execute(
            "SELECT * FROM competitors WHERE UPPER(name) = UPPER(?) AND id != ?", (value, cid)
        ).fetchone()
        if existing:
            target_id = existing["id"]
            # Carry over anything useful this record has that the
            # target doesn't -- same "fill in blanks, never overwrite"
            # rule the importer already uses when it reuses a
            # competitor.
            updates, params = [], []
            for f in ("hometown", "sponsor", "partner", "partner_id", "draw_animal", "notes"):
                if this_row[f] and not existing[f]:
                    updates.append(f"{f} = ?")
                    params.append(this_row[f])
            if updates:
                params.append(target_id)
                conn.execute(f"UPDATE competitors SET {', '.join(updates)} WHERE id = ?", params)
            conn.execute("UPDATE entries SET competitor_id = ? WHERE competitor_id = ?", (target_id, cid))
            # Nothing else should still reference the merged-away id, but
            # a team-roping partner link could -- repoint it rather than
            # leave a dangling foreign key.
            conn.execute("UPDATE competitors SET partner_id = ? WHERE partner_id = ?", (target_id, cid))
            conn.execute("DELETE FROM competitors WHERE id = ?", (cid,))
            conn.commit()
            conn.close()
            xml_export.export_all()
            return jsonify(ok=True, display=existing["name"], merged=True, competitor_id=target_id)

    conn.execute(f"UPDATE competitors SET {field} = ? WHERE id = ?", (value, cid))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=value)


@app.route("/entries/<int:eid>/quick_score", methods=["POST"])
def quick_score(eid):
    """Fast keyboard-driven score entry. Accepts a plain number, or a typed
    code (NS, BO, MO, NT, DQ, DNS). Returns JSON so the page never reloads."""
    conn = get_conn()
    row = conn.execute(
        "SELECT e.*, ev.scoring_type FROM entries e "
        "JOIN rounds r ON e.round_id = r.id JOIN events ev ON r.event_id = ev.id "
        "WHERE e.id = ?", (eid,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404

    raw = request.form.get("raw", "")
    try:
        parsed = scoring.parse_score_input(raw, row["scoring_type"], allow_video_review=_video_review_enabled())
    except ValueError as e:
        conn.close()
        return jsonify(ok=False, error=str(e))

    display = parsed["display"]
    if parsed["status"] == "scored" and parsed["score_value"] is not None and (row["pending_penalty"] or 0) > 0:
        # A judge flagged a penalty (Barrier/One Heel) on their own device
        # before this time was typed -- fold it in now, the same way a
        # same-device staged penalty auto-applies once Enter is hit (see
        # cpStagedPenalty in event_detail.html), just sourced from a judge's
        # device instead of this browser.
        pending = row["pending_penalty"] or 0
        final_score = parsed["score_value"] + pending
        penalty_note = row["pending_penalty_note"] or ""
        conn.execute(
            "UPDATE entries SET status = ?, score_value = ?, penalty = ?, penalty_note = ?, "
            "pending_penalty = 0, pending_penalty_note = '', pending_score = NULL, updated_at = datetime('now') WHERE id = ?",
            (parsed["status"], final_score, pending, penalty_note, eid),
        )
        display = scoring.format_number(final_score, row["scoring_type"])
    else:
        penalty_note = ""
        conn.execute(
            "UPDATE entries SET status = ?, score_value = ?, penalty = 0, penalty_note = '', "
            "pending_penalty = 0, pending_penalty_note = '', pending_score = NULL, updated_at = datetime('now') WHERE id = ?",
            (parsed["status"], parsed["score_value"], eid),
        )
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(
        ok=True,
        status=parsed["status"],
        label=scoring.STATUS_LABELS.get(parsed["status"], parsed["status"]),
        display=display,
        penalty_note=penalty_note,
    )


@app.route("/entries/<int:eid>/live_penalty_state")
def entry_live_penalty_state(eid):
    """Lightweight poll target for the scorekeeper's page (Current
    Contestant panel + the matching draw-sheet row) to pick up a judge's
    own-device changes on a TIMED event -- Barrier/One Heel/Illegal Head
    Catch via judge_timed_penalty. Judged (roughstock) events already have
    their own live poll for this (entry_judge_scores/cpPollJudges in
    event_detail.html); this is the timed-event equivalent, and is what
    keeps the draw sheet in sync when a judge's tap lands on a different
    device than whichever one is showing the panel."""
    conn = get_conn()
    row = conn.execute(
        "SELECT e.status, e.score_value, e.penalty, e.penalty_note, e.pending_penalty, "
        "e.pending_penalty_note, ev.scoring_type FROM entries e "
        "JOIN rounds r ON e.round_id = r.id JOIN events ev ON r.event_id = ev.id "
        "WHERE e.id = ?",
        (eid,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="Entry not found."), 404
    return jsonify(
        ok=True,
        status=row["status"],
        display=scoring.display_for(row["status"], row["score_value"], row["scoring_type"]),
        penalty=row["penalty"] or 0,
        penalty_note=row["penalty_note"] or "",
        pending_penalty=row["pending_penalty"] or 0,
        pending_penalty_note=row["pending_penalty_note"] or "",
    )


@app.route("/entries/<int:eid>/add_penalty", methods=["POST"])
def add_penalty(eid):
    """Adds a time penalty (+5/+10 buttons) on top of whatever's already
    scored, tracking the penalty amount separately from the raw run time
    -- not just folding it into the total and losing the breakdown -- so
    who's_up.xml can show both "raw+penalty" and the combined final time."""
    conn = get_conn()
    row = conn.execute(
        "SELECT e.*, ev.scoring_type FROM entries e "
        "JOIN rounds r ON e.round_id = r.id JOIN events ev ON r.event_id = ev.id "
        "WHERE e.id = ?", (eid,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404
    if row["status"] != "scored" or row["score_value"] is None:
        conn.close()
        return jsonify(ok=False, error="Enter a time first before adding a penalty."), 400

    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        conn.close()
        return jsonify(ok=False, error="Invalid penalty amount."), 400

    new_score = row["score_value"] + amount
    new_penalty = (row["penalty"] or 0) + amount
    # No named infraction here (that's Barrier/One Heel/etc., which only a
    # judge's own device can flag) -- just "+5" or "+10" on its own, so the
    # scorekeeper's own penalty clicks show up the same way a judge's does
    # (see the row-penalty-note under the score), instead of leaving no
    # trace of where a penalty came from.
    new_note = scoring.append_penalty_note(row["penalty_note"], "", amount)
    conn.execute(
        "UPDATE entries SET score_value = ?, penalty = ?, penalty_note = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (new_score, new_penalty, new_note, eid),
    )
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=scoring.format_number(new_score, row["scoring_type"]), penalty_note=new_note)


@app.route("/entries/<int:eid>/score", methods=["POST"])
def score_entry(eid):
    """Non-JS fallback: same parsing, full page reload."""
    conn = get_conn()
    row = conn.execute(
        "SELECT e.*, ev.scoring_type, r.event_id FROM entries e "
        "JOIN rounds r ON e.round_id = r.id JOIN events ev ON r.event_id = ev.id "
        "WHERE e.id = ?", (eid,)
    ).fetchone()
    rid = row["round_id"] if row else None
    event_id = row["event_id"] if row else None
    raw = request.form.get("raw", "")
    try:
        parsed = scoring.parse_score_input(
            raw, row["scoring_type"] if row else "judged", allow_video_review=_video_review_enabled()
        )
    except ValueError as e:
        conn.close()
        flash(str(e))
        if event_id:
            return redirect(url_for("event_detail", eid=event_id, round=rid))
        return redirect(url_for("index"))

    if parsed["status"] == "scored" and parsed["score_value"] is not None and row and (row["pending_penalty"] or 0) > 0:
        # Same judge-flagged-penalty fold-in as quick_score, above.
        pending = row["pending_penalty"] or 0
        conn.execute(
            "UPDATE entries SET status = ?, score_value = ?, penalty = ?, penalty_note = ?, "
            "pending_penalty = 0, pending_penalty_note = '', pending_score = NULL, updated_at = datetime('now') WHERE id = ?",
            (parsed["status"], parsed["score_value"] + pending, pending, row["pending_penalty_note"] or "", eid),
        )
    else:
        conn.execute(
            "UPDATE entries SET status = ?, score_value = ?, penalty = 0, penalty_note = '', "
            "pending_penalty = 0, pending_penalty_note = '', pending_score = NULL, updated_at = datetime('now') WHERE id = ?",
            (parsed["status"], parsed["score_value"], eid),
        )
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("event_detail", eid=event_id, round=rid))


@app.route("/entries/<int:eid>/set_whos_up", methods=["POST"])
def set_whos_up(eid):
    conn = get_conn()
    row = conn.execute(
        "SELECT e.round_id, r.event_id FROM entries e JOIN rounds r ON e.round_id = r.id WHERE e.id = ?",
        (eid,),
    ).fetchone()
    rid = row["round_id"]
    event_id = row["event_id"]
    # Who's Up has one pointer at a time -- setting a contestant live
    # always clears any guest that was showing, and vice versa.
    conn.execute("UPDATE whos_up SET entry_id = ?, guest_id = NULL WHERE id = 1", (eid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("event_detail", eid=event_id, round=rid))


@app.route("/entries/<int:eid>/set_whos_up_ajax", methods=["POST"])
def set_whos_up_ajax(eid):
    """Same as set_whos_up but returns JSON so the page can update in place
    without a full reload -- keeps the operator's scroll position."""
    conn = get_conn()
    row = conn.execute("SELECT id FROM entries WHERE id = ?", (eid,)).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Entry not found."), 404
    conn.execute("UPDATE whos_up SET entry_id = ?, guest_id = NULL WHERE id = 1", (eid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, entry_id=eid)


@app.route("/whos-up")
def whos_up_page():
    conn = get_conn()
    row = conn.execute(
        """
        SELECT c.name as c_name, c.hometown as c_hometown, c.sponsor as c_sponsor,
               c.partner as c_partner, c.draw_animal as c_draw_animal, c.notes as c_notes,
               ev.name as event_name, r.name as round_name,
               w.guest_id,
               g.name as g_name, g.hometown as g_hometown,
               g.sponsor as g_sponsor, g.notes as g_notes
        FROM whos_up w
        LEFT JOIN entries e ON w.entry_id = e.id
        LEFT JOIN competitors c ON e.competitor_id = c.id
        LEFT JOIN rounds r ON e.round_id = r.id
        LEFT JOIN events ev ON r.event_id = ev.id
        LEFT JOIN guests g ON w.guest_id = g.id
        WHERE w.id = 1
        """
    ).fetchone()
    conn.close()

    current = None
    if row and row["guest_id"]:
        # Guests share this same "Who's Up" feed and preview page as the
        # current contestant -- see xml_export.export_whos_up() for why.
        current = {
            "name": row["g_name"], "hometown": row["g_hometown"], "sponsor": row["g_sponsor"],
            "partner": "", "draw_animal": "", "notes": row["g_notes"],
            "event_name": "", "round_name": "",
        }
    elif row and row["c_name"]:
        current = {
            "name": row["c_name"], "hometown": row["c_hometown"], "sponsor": row["c_sponsor"],
            "partner": row["c_partner"], "draw_animal": row["c_draw_animal"], "notes": row["c_notes"],
            "event_name": row["event_name"], "round_name": row["round_name"],
        }

    # Score and rank fields are already computed for the XML export, so
    # read them back from there instead of duplicating the ranking logic.
    extra = {"round_score": "", "round_rank": "", "aggregate_rank": "", "aggregate_score": ""}
    try:
        xml_path = os.path.join(get_setting("export_folder"), "whos_up.xml")
        tree = ET.parse(xml_path)
        for tag in extra:
            el = tree.find(tag)
            if el is not None and el.text:
                extra[tag] = el.text
    except (OSError, ET.ParseError):
        pass
    return render_template("whos_up.html", current=current, extra=extra)


@app.route("/whos-up/clear", methods=["POST"])
def clear_whos_up():
    conn = get_conn()
    conn.execute("UPDATE whos_up SET entry_id = NULL, guest_id = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("whos_up_page"))


# ---------- Guests ----------
# A generic roster for anyone else who needs a lower third during the
# show but isn't a competitor -- royalty, sponsors, committee members,
# the anthem singer, whoever. Shares the Who's Up feed with contestants
# (see xml_export.export_whos_up()) rather than a roster-specific export,
# so one lower-third template in the graphics software covers all of it.
@app.route("/guests", methods=["GET", "POST"])
def guests_page():
    conn = get_conn()
    if request.method == "POST":
        next_pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 as n FROM guests").fetchone()["n"]
        conn.execute(
            "INSERT INTO guests (name, hometown, sponsor, notes, position) VALUES (?, ?, ?, ?, ?)",
            (
                importer.format_name(request.form.get("name", "").strip()),
                importer.fix_hometown(request.form.get("hometown", "").strip()),
                request.form.get("sponsor", "").strip(),
                request.form.get("notes", "").strip(),
                next_pos,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("guests_page"))
    people = conn.execute("SELECT * FROM guests ORDER BY position, created_at").fetchall()
    live_id = conn.execute("SELECT guest_id FROM whos_up WHERE id = 1").fetchone()["guest_id"]
    conn.close()
    return render_template("guests.html", people=people, live_guest_id=live_id)


@app.route("/guests/<int:gid>/update_field", methods=["POST"])
def update_guest_field(gid):
    """Inline edit of a guest roster field, same click-to-edit pattern as
    the draw table's name/hometown/stock fields."""
    field = request.form.get("field", "")
    raw = request.form.get("value", "").strip()
    if field == "name":
        value = importer.format_name(raw)
    elif field == "hometown":
        value = importer.fix_hometown(raw)
    elif field in ("sponsor", "notes"):
        value = raw
    else:
        return jsonify(ok=False, error="Unknown field."), 400

    conn = get_conn()
    row = conn.execute("SELECT id FROM guests WHERE id = ?", (gid,)).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Not found."), 404
    conn.execute(f"UPDATE guests SET {field} = ? WHERE id = ?", (value, gid))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, display=value)


@app.route("/guests/<int:gid>/move/<direction>", methods=["POST"])
def move_guest(gid, direction):
    conn = get_conn()
    people = conn.execute("SELECT id, position FROM guests ORDER BY position, created_at").fetchall()
    ids = [p["id"] for p in people]
    if gid in ids:
        idx = ids.index(gid)
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(ids):
            other_id = ids[swap_idx]
            pos_a = people[idx]["position"]
            pos_b = people[swap_idx]["position"]
            conn.execute("UPDATE guests SET position = ? WHERE id = ?", (pos_b, gid))
            conn.execute("UPDATE guests SET position = ? WHERE id = ?", (pos_a, other_id))
            conn.commit()
    conn.close()
    return redirect(url_for("guests_page"))


@app.route("/guests/<int:gid>/delete", methods=["POST"])
def delete_guest(gid):
    conn = get_conn()
    conn.execute("UPDATE whos_up SET guest_id = NULL WHERE guest_id = ?", (gid,))
    conn.execute("DELETE FROM guests WHERE id = ?", (gid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return redirect(url_for("guests_page"))


@app.route("/guests/<int:gid>/set_whos_up_ajax", methods=["POST"])
def set_guest_whos_up_ajax(gid):
    conn = get_conn()
    row = conn.execute("SELECT id FROM guests WHERE id = ?", (gid,)).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, error="Not found."), 404
    conn.execute("UPDATE whos_up SET guest_id = ?, entry_id = NULL WHERE id = 1", (gid,))
    conn.commit()
    conn.close()
    xml_export.export_all()
    return jsonify(ok=True, guest_id=gid)


# ---------- 50/50 ----------
def format_dollar_amount(raw):
    """Parses whatever's typed into the 50/50 total field and formats it
    as a dollar amount ("$1,245.00"). Accepts a plain number or one
    already dressed up with a $ prefix and/or thousands commas -- those
    get stripped and reapplied consistently rather than trusting
    whatever punctuation the operator happened to type. Returns "" for
    a blank input (clearing the total is valid). Raises ValueError with
    a human-readable message on anything that isn't a number."""
    raw = (raw or "").strip()
    if raw == "":
        return ""
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"\u201c{raw}\u201d isn't a valid dollar amount.")
    if value < 0:
        raise ValueError("Total can't be negative.")
    return f"${value:,.2f}"


@app.route("/fifty-fifty", methods=["GET", "POST"])
def fifty_fifty_page():
    conn = get_conn()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "reset":
            conn.execute(
                "UPDATE fifty_fifty SET total = '', winning_number = '', updated_at = datetime('now') WHERE id = 1"
            )
        else:
            try:
                total = format_dollar_amount(request.form.get("total", ""))
            except ValueError as e:
                conn.close()
                flash(str(e))
                return redirect(url_for("fifty_fifty_page"))
            conn.execute(
                "UPDATE fifty_fifty SET total = ?, winning_number = ?, updated_at = datetime('now') WHERE id = 1",
                (total, request.form.get("winning_number", "").strip()),
            )
        conn.commit()
        conn.close()
        xml_export.export_all()
        return redirect(url_for("fifty_fifty_page"))
    row = conn.execute("SELECT * FROM fifty_fifty WHERE id = 1").fetchone()
    conn.close()
    return render_template("fifty_fifty.html", data=row)


# ---------- Judges ----------
# A separate, deliberately minimal mobile UI (its own HTML shell, not the
# admin base.html) for judges scoring roughstock events on their own
# phone/tablet. No login -- a judge just picks a seat (1..judge_count)
# each time, per the studio's call. The scorekeeper still controls
# advancement entirely through Who's Up; judges only ever see whichever
# entry is currently live and can only submit for it.
def _judge_count():
    try:
        n = int(get_setting("judge_count", "2"))
    except (TypeError, ValueError):
        n = 2
    return n if n in scoring.VALID_JUDGE_COUNTS else 2


@app.route("/judge")
def judge_login_page():
    return render_template("judge_login.html", seats=range(1, _judge_count() + 1))


@app.route("/judge/<int:seat>")
def judge_score_page(seat):
    judge_count = _judge_count()
    if seat < 1 or seat > judge_count:
        flash(f"Judge {seat} isn't active right now -- only {judge_count} judge seats are set up.")
        return redirect(url_for("judge_login_page"))
    return render_template(
        "judge_score.html",
        seat=seat,
        outcomes=_judge_outcomes(),
        no_rider_score_outcomes=_judge_outcomes_no_rider_score(),
        timed_penalty_buttons=scoring.TIMED_PENALTY_BUTTONS,
        head_catch_code=scoring.TIMED_HEAD_CATCH_CODE,
        head_catch_label=scoring.TIMED_HEAD_CATCH_LABEL,
    )


def _judge_live_entry(conn):
    """The entry currently live via Who's Up, if any, along with what a
    judge's screen needs to show for it. Returns None if nothing's live, or
    the live thing is a guest/royalty entry rather than a contestant.

    Used for BOTH judged (roughstock) and timed events -- judged events get
    the rider/stock scoring screen, timed events get the penalty-flag screen
    (see judge_score.html and scoring.TIMED_PENALTY_BUTTONS). Callers branch
    on row["scoring_type"] to decide which."""
    row = conn.execute(
        """
        SELECT e.id as entry_id, e.draw_number, e.status, e.score_value,
               e.penalty, e.penalty_note, e.pending_penalty, e.pending_penalty_note,
               c.name as rider_name, c.hometown as rider_hometown,
               COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as stock_name,
               COALESCE(pc.name, c.partner) as partner_name,
               pc.hometown as partner_hometown,
               ev.name as event_name, ev.scoring_type, ev.is_team,
               r.name as round_name
        FROM whos_up w
        LEFT JOIN entries e ON w.entry_id = e.id
        LEFT JOIN competitors c ON e.competitor_id = c.id
        LEFT JOIN competitors pc ON c.partner_id = pc.id
        LEFT JOIN rounds r ON e.round_id = r.id
        LEFT JOIN events ev ON r.event_id = ev.id
        WHERE w.id = 1
        """
    ).fetchone()
    if not row or not row["entry_id"]:
        return None
    return row


@app.route("/judge/<int:seat>/state")
def judge_state(seat):
    judge_count = _judge_count()
    if seat < 1 or seat > judge_count:
        return jsonify(ok=False, error="Invalid judge seat."), 400

    conn = get_conn()
    live = _judge_live_entry(conn)
    if not live:
        conn.close()
        return jsonify(ok=True, entry_id=None, judgeable=False)

    if scoring.is_timed(live["scoring_type"]):
        # Timed events don't have per-seat scores to combine -- any judge
        # seat can flag a penalty on the live run, and it applies once,
        # immediately (see judge_timed_penalty). "Rider" here is the header
        # on a team event, and the heeler is surfaced as its own field since
        # that's who "One Heel" concerns.
        conn.close()
        return jsonify(
            ok=True,
            entry_id=live["entry_id"],
            judgeable=True,
            mode="timed",
            rider_name=live["rider_name"] or "",
            rider_hometown=live["rider_hometown"] or "",
            is_team=bool(live["is_team"]),
            partner_name=live["partner_name"] or "",
            partner_hometown=live["partner_hometown"] or "",
            draw_number=live["draw_number"],
            event_name=live["event_name"] or "",
            round_name=live["round_name"] or "",
            scoring_type=live["scoring_type"],
            status=live["status"],
            display=scoring.display_for(live["status"], live["score_value"], live["scoring_type"]),
            penalty=live["penalty"] or 0,
            penalty_note=live["penalty_note"] or "",
            pending_penalty=live["pending_penalty"] or 0,
            pending_penalty_note=live["pending_penalty_note"] or "",
        )

    if live["scoring_type"] != "judged":
        conn.close()
        return jsonify(ok=True, entry_id=None, judgeable=False)

    mine = conn.execute(
        "SELECT rider_score, stock_score, outcome FROM judge_scores WHERE entry_id = ? AND judge_seat = ?",
        (live["entry_id"], seat),
    ).fetchone()
    conn.close()

    return jsonify(
        ok=True,
        entry_id=live["entry_id"],
        judgeable=True,
        mode="judged",
        rider_name=live["rider_name"] or "",
        rider_hometown=live["rider_hometown"] or "",
        stock_name=live["stock_name"] or "",
        draw_number=live["draw_number"],
        event_name=live["event_name"] or "",
        round_name=live["round_name"] or "",
        already_submitted=mine is not None,
        my_rider_score=mine["rider_score"] if mine else None,
        my_stock_score=mine["stock_score"] if mine else None,
        my_outcome=mine["outcome"] if mine else None,
    )


@app.route("/judge/<int:seat>/submit", methods=["POST"])
def judge_submit(seat):
    judge_count = _judge_count()
    if seat < 1 or seat > judge_count:
        return jsonify(ok=False, error="Invalid judge seat."), 400

    entry_id_raw = request.form.get("entry_id", "")
    try:
        entry_id = int(entry_id_raw)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Missing or invalid entry."), 400

    # Stock is scored independently of whatever happened with the rider --
    # a stock contractor cares how the animal performed regardless of a
    # buck off, no score, DQ, or re-ride, so it's always required here,
    # never skipped because of the rider outcome.
    try:
        stock_score = float(request.form.get("stock_score", ""))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Missing or invalid stock score."), 400
    if not scoring.valid_judge_score(stock_score):
        return jsonify(ok=False, error="Stock score must be 1-25 in half points."), 400

    # The rider side is either a plain numeric score, or one of the
    # outcome codes -- except RR, which still carries a real rider score
    # alongside the flag (the cowboy, not the judge, decides whether to
    # keep it or take the re-ride).
    outcome = request.form.get("outcome", "").strip().upper()
    if outcome and outcome not in _judge_outcomes():
        return jsonify(ok=False, error="Unrecognized outcome."), 400

    rider_score = None
    if not outcome or outcome not in _judge_outcomes_no_rider_score():
        try:
            rider_score = float(request.form.get("rider_score", ""))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Missing or invalid rider score."), 400
        if not scoring.valid_judge_score(rider_score):
            return jsonify(ok=False, error="Rider score must be 1-25 in half points."), 400

    conn = get_conn()
    # Re-check this entry is still the live, judgeable one -- a judge's
    # device can be a poll cycle behind if the scorekeeper just advanced.
    live = _judge_live_entry(conn)
    if not live or live["entry_id"] != entry_id:
        conn.close()
        return jsonify(ok=False, error="That contestant isn't up anymore -- refresh and try again."), 409

    conn.execute(
        """
        INSERT INTO judge_scores (entry_id, judge_seat, rider_score, stock_score, outcome, submitted_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(entry_id, judge_seat) DO UPDATE SET
            rider_score = excluded.rider_score,
            stock_score = excluded.stock_score,
            outcome = excluded.outcome,
            submitted_at = excluded.submitted_at
        """,
        (entry_id, seat, rider_score, stock_score, outcome or None),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/judge/<int:seat>/timed_penalty", methods=["POST"])
def judge_timed_penalty(seat):
    """A judge on a timed event flags Barrier, One Heel, or Illegal Head
    Catch from their own device. Unlike judged (roughstock) scoring, this
    applies immediately -- there's nothing to combine across seats, any
    judge watching the run can flag it, same as the scorekeeper's own
    +5/+10 buttons.

    Barrier/One Heel add a fixed amount on top of the run's time. If a time
    is already saved for this entry, that mirrors add_penalty exactly. If
    not (the infraction usually happens mid-run, before a time exists yet),
    it's held in pending_penalty/pending_penalty_note and folded in
    automatically the moment the scorekeeper saves a time (see quick_score).

    Illegal Head Catch isn't a numeric penalty -- it voids the run, so it
    sets the same no_time status the NT code already produces, overriding
    whatever else was there."""
    judge_count = _judge_count()
    if seat < 1 or seat > judge_count:
        return jsonify(ok=False, error="Invalid judge seat."), 400

    try:
        entry_id = int(request.form.get("entry_id", ""))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Missing or invalid entry."), 400
    code = (request.form.get("code") or "").strip()

    conn = get_conn()
    # Re-check this entry is still the live, judgeable one -- a judge's
    # device can be a poll cycle behind if the scorekeeper just advanced.
    live = _judge_live_entry(conn)
    if not live or live["entry_id"] != entry_id or not scoring.is_timed(live["scoring_type"]):
        conn.close()
        return jsonify(ok=False, error="That contestant isn't up anymore -- refresh and try again."), 409

    if code == scoring.TIMED_HEAD_CATCH_CODE:
        # Tag the note "(Judge)" even though there's no numeric penalty --
        # otherwise an NT appearing on its own with no explanation looks
        # like the app glitched, not like a judge made a call.
        note = f"{scoring.TIMED_HEAD_CATCH_LABEL} (Judge)"
        conn.execute(
            "UPDATE entries SET status = 'no_time', score_value = NULL, penalty = 0, penalty_note = ?, "
            "pending_penalty = 0, pending_penalty_note = '', pending_score = NULL, updated_at = datetime('now') WHERE id = ?",
            (note, entry_id),
        )
        conn.commit()
        conn.close()
        xml_export.export_all()
        return jsonify(
            ok=True, status="no_time",
            display=scoring.display_for("no_time", None, live["scoring_type"]),
            penalty=0, penalty_note=note, pending_penalty=0, pending_penalty_note="",
        )

    amount = scoring.TIMED_PENALTY_AMOUNTS.get(code)
    label = scoring.TIMED_PENALTY_LABELS.get(code)
    if amount is None:
        conn.close()
        return jsonify(ok=False, error="Unrecognized penalty."), 400

    if live["status"] == "scored" and live["score_value"] is not None:
        new_score = live["score_value"] + amount
        new_penalty = (live["penalty"] or 0) + amount
        new_note = scoring.append_penalty_note(live["penalty_note"], label, amount, suffix="(Judge)")
        conn.execute(
            "UPDATE entries SET score_value = ?, penalty = ?, penalty_note = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (new_score, new_penalty, new_note, entry_id),
        )
        conn.commit()
        conn.close()
        xml_export.export_all()
        return jsonify(
            ok=True, status="scored",
            display=scoring.format_number(new_score, live["scoring_type"]),
            penalty=new_penalty, penalty_note=new_note,
            pending_penalty=0, pending_penalty_note=live["pending_penalty_note"] or "",
        )

    new_pending = (live["pending_penalty"] or 0) + amount
    new_pending_note = scoring.append_penalty_note(live["pending_penalty_note"], label, amount, suffix="(Judge)")
    conn.execute(
        "UPDATE entries SET pending_penalty = ?, pending_penalty_note = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (new_pending, new_pending_note, entry_id),
    )
    conn.commit()
    conn.close()
    return jsonify(
        ok=True, status=live["status"],
        display=scoring.display_for(live["status"], live["score_value"], live["scoring_type"]),
        penalty=live["penalty"] or 0, penalty_note=live["penalty_note"] or "",
        pending_penalty=new_pending, pending_penalty_note=new_pending_note,
    )


# ---------- Stock leaderboard ----------
# Ranks stock animals by the STOCK half of what judges submit, entirely
# independent of the rider's own score -- a buck off, no score, DQ, or
# re-ride on the rider side has no bearing on this at all. Computed
# from judge_scores rather than a separately-stored stock total, across
# every round of the chosen event, since a stock contractor cares how
# an animal did across the whole event, not one round at a time.
#
# Only counts entries the scorekeeper has actually finalized (status
# != 'pending') -- judges can submit their marks well before that
# happens, and a score sitting in judge_scores isn't official yet. An
# entry only ever leaves 'pending' via Finalize Score (or the
# scorekeeper typing a score in directly), so this is exactly the
# right gate: showing a stock score before finalize would let the
# leaderboard jump the gun on a call the scorekeeper hasn't signed off
# on -- e.g. a still-unresolved judge disagreement, or a mark that's
# about to be corrected.
@app.route("/stock")
def stock_page():
    conn = get_conn()
    events = conn.execute(
        "SELECT id, name FROM events WHERE scoring_type = 'judged' ORDER BY position, created_at"
    ).fetchall()
    selected_event_id = request.args.get("event", "").strip()
    judge_count = _judge_count()
    rows = []

    if selected_event_id:
        entries = conn.execute(
            """
            SELECT e.id as entry_id, e.draw_number,
                   COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as stock_name,
                   c.name as rider_name,
                   r.name as round_name, r.round_number
            FROM entries e
            JOIN rounds r ON e.round_id = r.id
            JOIN competitors c ON e.competitor_id = c.id
            WHERE r.event_id = ? AND e.status != 'pending'
            ORDER BY r.round_number, e.draw_order
            """,
            (selected_event_id,),
        ).fetchall()

        for e in entries:
            judge_rows = conn.execute(
                "SELECT judge_seat, stock_score FROM judge_scores WHERE entry_id = ?", (e["entry_id"],)
            ).fetchall()
            scores_by_seat = {jr["judge_seat"]: jr["stock_score"] for jr in judge_rows if jr["stock_score"] is not None}
            if not scores_by_seat:
                continue  # no judge has scored this animal at all yet -- nothing to show
            rows.append(
                {
                    "entry_id": e["entry_id"],
                    "stock_name": e["stock_name"] or "",
                    "rider_name": e["rider_name"],
                    "round_name": e["round_name"],
                    "draw_number": e["draw_number"],
                    "stock_score": scoring.combine_scores_by_seat(scores_by_seat, judge_count),
                    "submitted_count": len(scores_by_seat),
                    "judge_count": judge_count,
                }
            )

        # Fully scored animals first (highest stock score wins, same
        # convention as judged rider scoring), pending ones after --
        # same simple sequential ranking the other leaderboards use, no
        # shared ranks for ties.
        rows.sort(key=lambda r: (r["stock_score"] is None, -(r["stock_score"] or 0)))
        rank = 0
        for r in rows:
            if r["stock_score"] is not None:
                rank += 1
                r["rank"] = rank
            else:
                r["rank"] = None

    conn.close()
    selected_event = next((e for e in events if str(e["id"]) == selected_event_id), None)
    return render_template(
        "stock.html",
        events=events,
        selected_event_id=selected_event_id,
        selected_event=selected_event,
        rows=rows,
    )


# ---------- Leaderboards ----------
@app.route("/events/<int:eid>/leaderboards", methods=["GET", "POST"])
def leaderboards(eid):
    conn = get_conn()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    all_events = conn.execute("SELECT id, name FROM events ORDER BY position, created_at").fetchall()
    rounds = conn.execute(
        "SELECT * FROM rounds WHERE event_id = ? ORDER BY round_number", (eid,)
    ).fetchall()

    # Distinct round numbers for this event, each with the list of original
    # performance names that share that number (for the hint text).
    round_number_groups = {}
    for r in rounds:
        round_number_groups.setdefault(r["round_number"], []).append(r["name"])

    if request.method == "POST":
        if request.form.get("action") == "set_round":
            set_setting("live_event_id", str(eid))
            set_setting("active_round_number", request.form["round_number"])
        elif request.form.get("action") == "set_aggregate":
            set_setting("live_event_id", str(eid))
        xml_export.export_all()
        return redirect(url_for("leaderboards", eid=eid))

    live_event_id = get_setting("live_event_id")
    is_live_event = (live_event_id == str(eid))
    active_round_number = get_setting("active_round_number") if is_live_event else None

    order = scoring.sort_order(event["scoring_type"])
    round_board = []
    if active_round_number and active_round_number in [str(n) for n in round_number_groups]:
        matching_round_ids = [r["id"] for r in rounds if str(r["round_number"]) == active_round_number]
        placeholders = ",".join("?" * len(matching_round_ids))
        round_board = conn.execute(
            f"""
            SELECT e.score_value, c.name, c.hometown
            FROM entries e JOIN competitors c ON e.competitor_id = c.id
            WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
            ORDER BY e.score_value {order}
            """,
            matching_round_ids,
        ).fetchall()
        round_board = [
            {"name": r["name"], "hometown": r["hometown"],
             "display": scoring.format_number(r["score_value"], event["scoring_type"])}
            for r in round_board
        ]

    # Aggregate always combines every round in this event -- no manual
    # round selection. Two people scoring in round 1 and round 2 of the
    # same event get their scores summed together automatically.
    all_round_ids = [r["id"] for r in rounds]
    agg_board = []
    if all_round_ids:
        placeholders = ",".join("?" * len(all_round_ids))
        rows = conn.execute(
            f"""
            SELECT c.name, c.hometown, SUM(e.score_value) as total, COUNT(e.id) as head
            FROM entries e JOIN competitors c ON e.competitor_id = c.id
            WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
            GROUP BY c.id
            """,
            all_round_ids,
        ).fetchall()
        # More head completed always ranks ahead of fewer head, regardless
        # of raw total -- only within the same head count does the total
        # decide the order (see xml_export.export_aggregate_leaderboard).
        if scoring.is_timed(event["scoring_type"]):
            rows = sorted(rows, key=lambda r: (-r["head"], r["total"]))
        else:
            rows = sorted(rows, key=lambda r: (-r["head"], -r["total"]))
        for r in rows:
            total = r["total"]
            total_str = scoring.format_number(total, event["scoring_type"])
            agg_board.append({
                "name": r["name"], "hometown": r["hometown"],
                "display": f"{total_str}/{r['head']}",
            })

    conn.close()
    return render_template(
        "leaderboards.html",
        event=event,
        all_events=all_events,
        rounds=rounds,
        round_number_groups=round_number_groups,
        round_board=round_board,
        agg_board=agg_board,
        active_round_number=active_round_number,
        agg_is_live=is_live_event,
    )


# ---------- Announcer screen ----------
# A single read-only monitor view meant to run full-screen on its own
# dedicated display throughout the show -- current contestant, the live
# round leaderboard, the live event's aggregate leaderboard, and the
# top stock scores so far for the live event. Everything here
# already exists elsewhere in the app (Who's Up, Leaderboards, Stock);
# this just pulls all four onto one screen so the announcer never has
# to flip tabs mid-broadcast. Own full-screen HTML shell (not
# base.html) for the same reason judge_score.html has one -- no admin
# nav or live-event controls cluttering a screen that's just for
# reading, and it auto-reloads every few seconds the same way
# leaderboards.html already does.
ANNOUNCER_ROUND_BOARD_LIMIT = 8
ANNOUNCER_AGG_BOARD_LIMIT = 8
ANNOUNCER_STOCK_TOP_N = 5


def _announcer_to_lead(conn, event_id, scoring_type, competitor_id, entry_status):
    """What this contestant needs to post on THIS ride to take over 1st in
    the aggregate standings -- takes over the same slot Agg Rank
    normally shows on the announcer screen, but only before a score
    exists (once scored, Agg Rank itself is the more meaningful thing
    to show). Judged (roughstock) events only: the 0.5/0.25 increment
    below is specifically the smallest possible gap between two judged
    totals under a 2-judge (summed) vs 4-judge (summed-then-halved)
    panel -- a timed event's "lead" is a faster time with no equivalent
    fixed increment, so this doesn't apply there.

    Deliberately blank (rather than a specific number) whenever taking
    the lead doesn't actually hinge on hitting a particular score: if
    this ride would leave the contestant on FEWER completed head than
    the current leader, no score changes that (more head always ranks
    above fewer, regardless of total -- see export_aggregate_leaderboard);
    if it would put them on MORE head than the leader, they move into
    1st automatically regardless of score. Only when this ride would
    leave them on the SAME head count as the leader is there an actual
    number to chase."""
    if scoring_type != "judged" or entry_status == "scored":
        return ""

    round_ids = [r["id"] for r in conn.execute("SELECT id FROM rounds WHERE event_id = ?", (event_id,))]
    if not round_ids:
        return ""
    placeholders = ",".join("?" * len(round_ids))
    rows = conn.execute(
        f"""
        SELECT competitor_id, SUM(score_value) as total, COUNT(*) as head
        FROM entries
        WHERE round_id IN ({placeholders}) AND status = 'scored'
        GROUP BY competitor_id
        """,
        round_ids,
    ).fetchall()
    if not rows:
        return ""  # nobody's scored yet this event -- nothing to beat

    leader = sorted(rows, key=lambda r: (-r["head"], -r["total"]))[0]
    own = next((r for r in rows if r["competitor_id"] == competitor_id), None)
    own_total = own["total"] if own else 0
    own_head = own["head"] if own else 0

    if own_head + 1 != leader["head"]:
        return ""

    increment = 0.5 if _judge_count() == 2 else 0.25
    needed_score = (leader["total"] + increment) - own_total
    return scoring.format_number(needed_score, "judged")


def _announcer_current_contestant(conn):
    """Same shape as whos_up_page's `current` + `extra`, reusing the
    already-exported whos_up.xml for the rank/score fields rather than
    recomputing the ranking logic a third time."""
    row = conn.execute(
        """
        SELECT c.name as c_name, c.hometown as c_hometown, c.sponsor as c_sponsor,
               c.partner as c_partner, c.draw_animal as c_draw_animal, c.notes as c_notes,
               ev.id as event_id, ev.name as event_name, ev.scoring_type,
               e.status as entry_status, e.competitor_id,
               r.name as round_name,
               w.guest_id,
               g.name as g_name, g.hometown as g_hometown,
               g.sponsor as g_sponsor, g.notes as g_notes
        FROM whos_up w
        LEFT JOIN entries e ON w.entry_id = e.id
        LEFT JOIN competitors c ON e.competitor_id = c.id
        LEFT JOIN rounds r ON e.round_id = r.id
        LEFT JOIN events ev ON r.event_id = ev.id
        LEFT JOIN guests g ON w.guest_id = g.id
        WHERE w.id = 1
        """
    ).fetchone()

    current = None
    if row and row["guest_id"]:
        current = {
            "name": row["g_name"], "hometown": row["g_hometown"], "sponsor": row["g_sponsor"],
            "partner": "", "draw_animal": "", "notes": row["g_notes"],
            "event_name": "", "round_name": "",
        }
    elif row and row["c_name"]:
        current = {
            "name": row["c_name"], "hometown": row["c_hometown"], "sponsor": row["c_sponsor"],
            "partner": row["c_partner"], "draw_animal": row["c_draw_animal"], "notes": row["c_notes"],
            "event_name": row["event_name"], "round_name": row["round_name"],
        }

    extra = {"round_score": "", "round_rank": "", "aggregate_rank": "", "aggregate_score": "", "time_plus_penalty": ""}
    try:
        xml_path = os.path.join(get_setting("export_folder"), "whos_up.xml")
        tree = ET.parse(xml_path)
        for tag in extra:
            el = tree.find(tag)
            if el is not None and el.text:
                extra[tag] = el.text
    except (OSError, ET.ParseError):
        pass

    extra["to_lead"] = ""
    if row and row["event_id"] and current is not None and not row["guest_id"]:
        extra["to_lead"] = _announcer_to_lead(
            conn, row["event_id"], row["scoring_type"], row["competitor_id"], row["entry_status"]
        )
    return current, extra


def _announcer_judge_breakdown(conn):
    """Each judge's individual rider/stock marks (and any outcome flag)
    for whoever is currently up -- live, as they're submitted, exactly
    like the scorekeeper's own Current Contestant panel already shows
    via entry_judge_scores/cpPollJudges. This is a different trust
    context than the Stock/Round/Aggregate leaderboards: those are
    rankings the announcer might read out as official results, so they
    stay gated on Finalize Score (see stock_page and _announcer_stock_top).
    This is just "what's coming in right now" for a single contestant --
    the same thing the scorekeeper is already watching to decide when to
    finalize -- so the announcer sees it the instant a judge submits,
    with unsubmitted seats shown as still-waiting rather than hidden."""
    row = conn.execute(
        """
        SELECT e.id as entry_id, ev.scoring_type
        FROM whos_up w
        JOIN entries e ON w.entry_id = e.id
        JOIN rounds r ON e.round_id = r.id
        JOIN events ev ON r.event_id = ev.id
        WHERE w.id = 1
        """
    ).fetchone()
    if not row or row["scoring_type"] != "judged":
        return []

    judge_count = _judge_count()
    names = _judge_names()
    judge_rows = conn.execute(
        "SELECT judge_seat, rider_score, stock_score, outcome FROM judge_scores WHERE entry_id = ?",
        (row["entry_id"],),
    ).fetchall()
    by_seat = {jr["judge_seat"]: jr for jr in judge_rows}

    breakdown = []
    for seat in range(1, judge_count + 1):
        jr = by_seat.get(seat)
        breakdown.append({
            "judge_name": names.get(seat, f"Judge {seat}"),
            "submitted": jr is not None,
            "rider_display": scoring.format_number(jr["rider_score"], "judged") if jr and jr["rider_score"] is not None else "",
            "stock_display": scoring.format_number(jr["stock_score"], "judged") if jr and jr["stock_score"] is not None else "",
            "outcome": _judge_outcomes().get(jr["outcome"], "") if jr and jr["outcome"] else "",
        })
    return breakdown


def _announcer_live_boards(conn):
    """The live round's leaderboard and the live event's aggregate
    leaderboard -- same queries and ranking rules as leaderboards()
    above, just capped to a top-N screen-sized slice and aware of the
    live event's name/round name for the header."""
    live_event_id = get_setting("live_event_id")
    round_board, agg_board = [], []
    live_event_name, live_round_name = "", ""

    if not live_event_id:
        return round_board, agg_board, live_event_name, live_round_name

    event = conn.execute("SELECT * FROM events WHERE id = ?", (live_event_id,)).fetchone()
    if not event:
        return round_board, agg_board, live_event_name, live_round_name
    live_event_name = event["name"]

    rounds = conn.execute(
        "SELECT * FROM rounds WHERE event_id = ? ORDER BY round_number", (live_event_id,)
    ).fetchall()
    active_round_number = get_setting("active_round_number")
    order = scoring.sort_order(event["scoring_type"])

    if active_round_number:
        matching_round_ids = [r["id"] for r in rounds if str(r["round_number"]) == str(active_round_number)]
        if matching_round_ids:
            live_round_name = next(
                (r["name"] for r in rounds if str(r["round_number"]) == str(active_round_number)), ""
            )
            placeholders = ",".join("?" * len(matching_round_ids))
            rows = conn.execute(
                f"""
                SELECT e.score_value, c.name, c.hometown
                FROM entries e JOIN competitors c ON e.competitor_id = c.id
                WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
                ORDER BY e.score_value {order}
                LIMIT ?
                """,
                matching_round_ids + [ANNOUNCER_ROUND_BOARD_LIMIT],
            ).fetchall()
            round_board = [
                {"name": r["name"], "hometown": r["hometown"],
                 "display": scoring.format_number(r["score_value"], event["scoring_type"])}
                for r in rows
            ]

    all_round_ids = [r["id"] for r in rounds]
    if all_round_ids:
        placeholders = ",".join("?" * len(all_round_ids))
        rows = conn.execute(
            f"""
            SELECT c.name, c.hometown, SUM(e.score_value) as total, COUNT(e.id) as head
            FROM entries e JOIN competitors c ON e.competitor_id = c.id
            WHERE e.round_id IN ({placeholders}) AND e.status = 'scored'
            GROUP BY c.id
            """,
            all_round_ids,
        ).fetchall()
        if scoring.is_timed(event["scoring_type"]):
            rows = sorted(rows, key=lambda r: (-r["head"], r["total"]))
        else:
            rows = sorted(rows, key=lambda r: (-r["head"], -r["total"]))
        for r in rows[:ANNOUNCER_AGG_BOARD_LIMIT]:
            total_str = scoring.format_number(r["total"], event["scoring_type"])
            agg_board.append({"name": r["name"], "hometown": r["hometown"], "display": f"{total_str}/{r['head']}"})

    return round_board, agg_board, live_event_name, live_round_name


def _announcer_stock_top(conn):
    """Top stock scores so far for the live event only (see the Live
    Event/Round controls in the topbar) -- mixing events together
    doesn't mean anything to a stock contractor or an announcer, since
    animals only ever compete against other animals within the same
    event (a bareback horse isn't ranked against a bull). Same
    finalize-gate as the Stock tab (see stock_page): only counts
    entries the scorekeeper has actually finalized."""
    live_event_id = get_setting("live_event_id")
    if not live_event_id:
        return []
    event = conn.execute("SELECT scoring_type FROM events WHERE id = ?", (live_event_id,)).fetchone()
    if not event or event["scoring_type"] != "judged":
        return []  # stock scores don't apply to a timed live event

    judge_count = _judge_count()
    entries = conn.execute(
        """
        SELECT e.id as entry_id,
               COALESCE(NULLIF(e.draw_animal, ''), c.draw_animal) as stock_name,
               c.name as rider_name, r.name as round_name
        FROM entries e
        JOIN rounds r ON e.round_id = r.id
        JOIN competitors c ON e.competitor_id = c.id
        WHERE r.event_id = ? AND e.status != 'pending'
        """,
        (live_event_id,),
    ).fetchall()

    rows = []
    for e in entries:
        judge_rows = conn.execute(
            "SELECT judge_seat, stock_score FROM judge_scores WHERE entry_id = ?", (e["entry_id"],)
        ).fetchall()
        scores_by_seat = {jr["judge_seat"]: jr["stock_score"] for jr in judge_rows if jr["stock_score"] is not None}
        if not scores_by_seat:
            continue
        combined = scoring.combine_scores_by_seat(scores_by_seat, judge_count)
        if combined is None:
            continue  # not every judge has submitted the stock score yet
        rows.append({
            "stock_name": e["stock_name"] or "",
            "rider_name": e["rider_name"],
            "round_name": e["round_name"],
            "score": combined,
        })

    rows.sort(key=lambda r: -r["score"])
    for r in rows:
        r["display"] = scoring.format_number(r["score"])
    return rows[:ANNOUNCER_STOCK_TOP_N]


def _announcer_context():
    """Everything the announcer screen shows, in one JSON-able dict --
    the single source of truth for both the initial page render and the
    fast poll endpoint below, so the two can never drift out of sync
    with each other."""
    conn = get_conn()
    current, extra = _announcer_current_contestant(conn)
    judge_breakdown = _announcer_judge_breakdown(conn)
    round_board, agg_board, live_event_name, live_round_name = _announcer_live_boards(conn)
    stock_top = _announcer_stock_top(conn)
    conn.close()
    return {
        "current": current,
        "extra": extra,
        "judge_breakdown": judge_breakdown,
        "round_board": round_board,
        "agg_board": agg_board,
        "live_event_name": live_event_name,
        "live_round_name": live_round_name,
        "stock_top": stock_top,
    }


@app.route("/announcer")
def announcer_screen():
    return render_template(
        "announcer.html",
        data=_announcer_context(),
        announcer_color_bg=get_setting("announcer_color_bg", DEFAULT_SETTINGS["announcer_color_bg"]),
        announcer_color_accent=get_setting("announcer_color_accent", DEFAULT_SETTINGS["announcer_color_accent"]),
        announcer_color_text=get_setting("announcer_color_text", DEFAULT_SETTINGS["announcer_color_text"]),
    )


@app.route("/announcer/data")
def announcer_data():
    """Polled by the announcer screen every ~1.5s (see announcer.html) so
    a just-entered score, a judge's mark landing, or Who's Up changing
    shows up almost immediately -- no full page reload, which is what
    made the old version feel laggy (up to 8s behind, and a visible
    flicker/scroll-reset on every refresh)."""
    return jsonify(_announcer_context())


# ---------- Import from RodeoCanada-style HTML ----------
IMPORT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "import_cache")
os.makedirs(IMPORT_CACHE_DIR, exist_ok=True)


def _get_or_create_event(conn, name, scoring_type):
    row = conn.execute("SELECT * FROM events WHERE UPPER(name) = UPPER(?)", (name,)).fetchone()
    if row:
        return row["id"]
    next_pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 as n FROM events").fetchone()["n"]
    is_team = 1 if importer.guess_is_team_event(name) else 0
    cur = conn.execute(
        "INSERT INTO events (name, scoring_type, is_team, position) VALUES (?, ?, ?, ?)",
        (name, scoring_type, is_team, next_pos),
    )
    return cur.lastrowid


def _get_or_create_round(conn, event_id, name, round_number=None):
    row = conn.execute(
        "SELECT * FROM rounds WHERE event_id = ? AND UPPER(name) = UPPER(?)", (event_id, name)
    ).fetchone()
    if row:
        if round_number is not None:
            conn.execute("UPDATE rounds SET round_number = ? WHERE id = ?", (round_number, row["id"]))
        return row["id"]
    if round_number is None:
        round_number = conn.execute(
            "SELECT COALESCE(MAX(round_number), 0) + 1 as n FROM rounds WHERE event_id = ?", (event_id,)
        ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO rounds (event_id, name, round_number) VALUES (?, ?, ?)",
        (event_id, name, round_number),
    )
    return cur.lastrowid


def _get_or_create_competitor(conn, name, hometown="", partner="", draw_animal=""):
    # A blank name (a re-ride reserve stock row imported with nobody
    # drawn onto it yet -- see importer.py) must never be deduplicated
    # against another blank-name competitor. Each one is a genuinely
    # distinct placeholder slot that'll get its own rider's name filled
    # in later; matching them by name would collapse every one of them
    # into a single shared record, which would silently drop every RR
    # row after the first in the same round (it'd already read as
    # "already drawn into this round" below and get skipped).
    if name.strip():
        row = conn.execute("SELECT * FROM competitors WHERE UPPER(name) = UPPER(?)", (name,)).fetchone()
        if row:
            # Fill in any blank fields we now have better data for; never overwrite
            # something the user already has on file.
            updates, params = [], []
            if hometown and not row["hometown"]:
                updates.append("hometown = ?"); params.append(hometown)
            if partner and not row["partner"]:
                updates.append("partner = ?"); params.append(partner)
            if draw_animal and not row["draw_animal"]:
                updates.append("draw_animal = ?"); params.append(draw_animal)
            if updates:
                params.append(row["id"])
                conn.execute(f"UPDATE competitors SET {', '.join(updates)} WHERE id = ?", params)
            return row["id"]
    cur = conn.execute(
        "INSERT INTO competitors (name, hometown, partner, draw_animal) VALUES (?, ?, ?, ?)",
        (name, hometown, partner, draw_animal),
    )
    return cur.lastrowid


@app.route("/import", methods=["GET"])
def import_page():
    return render_template("import.html")


@app.route("/import/preview", methods=["POST"])
def import_preview():
    html_text = None
    uploaded = request.files.get("html_file")
    if uploaded and uploaded.filename:
        html_text = uploaded.read().decode("utf-8", errors="replace")
    elif request.form.get("html_paste", "").strip():
        html_text = request.form["html_paste"]

    if not html_text:
        flash("Please upload an HTML file or paste HTML content.")
        return redirect(url_for("import_page"))

    result = importer.parse_rodeocanada_html(html_text)
    if not result["events"]:
        flash("No events could be found in that file. Double check it's a draw sheet page.")
        return redirect(url_for("import_page"))

    token = uuid.uuid4().hex
    with open(os.path.join(IMPORT_CACHE_DIR, f"{token}.json"), "w") as f:
        json.dump(result, f)

    # Preview what round number "auto" would pick for each round, assuming
    # every round is left on auto (sequential per event, continuing from
    # that event's existing highest round number if it already exists).
    conn = get_conn()
    auto_round_numbers = {}
    for ei, ev in enumerate(result["events"]):
        existing = conn.execute("SELECT id FROM events WHERE UPPER(name) = UPPER(?)", (ev["name"],)).fetchone()
        running_max = 0
        if existing:
            row = conn.execute(
                "SELECT COALESCE(MAX(round_number), 0) as n FROM rounds WHERE event_id = ?",
                (existing["id"],),
            ).fetchone()
            running_max = row["n"]
        for ri in range(len(ev["rounds"])):
            running_max += 1
            auto_round_numbers[f"{ei}:{ri}"] = running_max
    conn.close()

    # Sensible per-position defaults for the "rename across all events"
    # bulk tool below -- most rodeos use the same round names everywhere
    # (Slack, Round 1, Round 2, Short Go, etc.), so pre-filling with
    # whichever event has the most rounds gives a name to suggest at
    # every position, ready to apply with one click if it's already
    # right, or tweak first if not.
    max_rounds = max((len(ev["rounds"]) for ev in result["events"]), default=0)
    bulk_round_defaults = []
    if result["events"]:
        fullest_event = max(result["events"], key=lambda ev: len(ev["rounds"]))
        bulk_round_defaults = [r["name"] for r in fullest_event["rounds"]]

    return render_template(
        "import_preview.html",
        token=token,
        result=result,
        auto_round_numbers=auto_round_numbers,
        max_rounds=max_rounds,
        bulk_round_defaults=bulk_round_defaults,
    )


@app.route("/import/confirm", methods=["POST"])
def import_confirm():
    token = request.form.get("token", "")
    cache_path = os.path.join(IMPORT_CACHE_DIR, f"{token}.json")
    if not os.path.exists(cache_path):
        flash("That import preview has expired. Please upload the file again.")
        return redirect(url_for("import_page"))

    with open(cache_path) as f:
        result = json.load(f)

    selected_round_keys = set(request.form.getlist("round_key"))  # "eventIdx:roundIdx"

    conn = get_conn()
    competitors_created = 0
    entries_created = 0

    for ei, ev in enumerate(result["events"]):
        event_id = None
        for ri, rnd in enumerate(ev["rounds"]):
            key = f"{ei}:{ri}"
            if key not in selected_round_keys:
                continue
            if event_id is None:
                event_id = _get_or_create_event(conn, ev["name"], ev["scoring_type"])

            override_name = request.form.get(f"round_name__{key}", "").strip() or rnd["name"]
            round_number_raw = request.form.get(f"round_number__{key}", "").strip()
            round_number = int(round_number_raw) if round_number_raw.isdigit() else None
            round_id = _get_or_create_round(conn, event_id, override_name, round_number)

            existing_competitor_ids = {
                r["competitor_id"]
                for r in conn.execute("SELECT competitor_id FROM entries WHERE round_id = ?", (round_id,))
            }
            # Rounds can be merged (e.g. multiple source performances renamed
            # to the same "Round 1"), so draw_order must be a fresh running
            # sequence for this destination round, not just the original
            # per-performance draw number -- otherwise merged rounds would
            # collide on duplicate draw_order values and interleave oddly.
            next_draw_order = conn.execute(
                "SELECT COALESCE(MAX(draw_order), 0) + 1 as n FROM entries WHERE round_id = ?", (round_id,)
            ).fetchone()["n"]

            for entry in rnd["entries"]:
                before = conn.execute("SELECT COUNT(*) as n FROM competitors").fetchone()["n"]
                competitor_id = _get_or_create_competitor(
                    conn, entry["name"], entry.get("hometown", ""),
                    entry.get("partner", ""), entry.get("draw_animal", ""),
                )
                after = conn.execute("SELECT COUNT(*) as n FROM competitors").fetchone()["n"]
                competitors_created += (after - before)

                if entry.get("partner"):
                    before = conn.execute("SELECT COUNT(*) as n FROM competitors").fetchone()["n"]
                    partner_id = _get_or_create_competitor(
                        conn, entry["partner"], entry.get("partner_hometown", ""),
                        entry["name"],
                    )
                    after = conn.execute("SELECT COUNT(*) as n FROM competitors").fetchone()["n"]
                    competitors_created += (after - before)
                    # Link header <-> heeler by ID (not just by name text) so
                    # hometown etc. can be looked up reliably for exports.
                    conn.execute(
                        "UPDATE competitors SET partner_id = ? WHERE id = ? AND (partner_id IS NULL OR partner_id != ?)",
                        (partner_id, competitor_id, partner_id),
                    )
                    conn.execute(
                        "UPDATE competitors SET partner_id = ? WHERE id = ? AND (partner_id IS NULL OR partner_id != ?)",
                        (competitor_id, partner_id, competitor_id),
                    )

                if competitor_id in existing_competitor_ids:
                    continue  # already drawn into this round, don't duplicate

                conn.execute(
                    "INSERT INTO entries (round_id, competitor_id, draw_order, draw_number, draw_animal, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (round_id, competitor_id, next_draw_order, entry["draw_number"], entry.get("draw_animal", "")),
                )
                next_draw_order += 1
                existing_competitor_ids.add(competitor_id)
                entries_created += 1

    conn.commit()
    conn.close()
    os.remove(cache_path)
    xml_export.export_all()

    flash(f"Imported {entries_created} draw entries ({competitors_created} new competitors added).")
    return redirect(url_for("index"))


# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        set_setting("rodeo_name", request.form["rodeo_name"].strip())
        set_setting("export_folder", request.form["export_folder"].strip())
        limit = request.form.get("names_field_limit", "10").strip()
        if limit == "" or limit.isdigit():
            set_setting("names_field_limit", limit)
        judge_count = request.form.get("judge_count", "2").strip()
        if judge_count in ("2", "4"):
            set_setting("judge_count", judge_count)
        for seat in range(1, 5):
            set_setting(f"judge_name_{seat}", request.form.get(f"judge_name_{seat}", "").strip())

        set_setting("enable_video_review", "1" if request.form.get("enable_video_review") else "")

        if request.form.get("reset_announcer_colors"):
            set_setting("announcer_color_bg", DEFAULT_SETTINGS["announcer_color_bg"])
            set_setting("announcer_color_accent", DEFAULT_SETTINGS["announcer_color_accent"])
            set_setting("announcer_color_text", DEFAULT_SETTINGS["announcer_color_text"])
        else:
            hex_color = re.compile(r"^#[0-9a-fA-F]{6}$")
            for key in ("announcer_color_bg", "announcer_color_accent", "announcer_color_text"):
                value = request.form.get(key, "").strip()
                if hex_color.match(value):
                    set_setting(key, value)

        xml_export.export_all()
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        rodeo_name=get_setting("rodeo_name"),
        export_folder=get_setting("export_folder"),
        names_field_limit=get_setting("names_field_limit", "10"),
        judge_count=get_setting("judge_count", "2"),
        judge_names_raw={seat: get_setting(f"judge_name_{seat}", "") for seat in range(1, 5)},
        announcer_color_bg=get_setting("announcer_color_bg", DEFAULT_SETTINGS["announcer_color_bg"]),
        announcer_color_accent=get_setting("announcer_color_accent", DEFAULT_SETTINGS["announcer_color_accent"]),
        announcer_color_text=get_setting("announcer_color_text", DEFAULT_SETTINGS["announcer_color_text"]),
        judge_url=_judge_url(),
        judge_qr_svg=_judge_qr_svg(),
    )


@app.route("/export-now", methods=["POST"])
def export_now():
    xml_export.export_all()
    return redirect(request.referrer or url_for("index"))


# ---------- Full backup / restore ----------
# Everything (competitors, events, entries, scores, judge submissions,
# settings -- literally every table) lives in the one SQLite file this
# app already reads and writes, so the simplest, most complete, and
# most reliable "export" is just that file itself, rather than
# inventing a separate portable format that would need its own code
# path to build and to re-import, and could drift out of sync with the
# schema as it evolves. This is what SQLite backups are for.
BACKUP_REQUIRED_TABLES = {
    "settings", "competitors", "events", "rounds", "entries",
    "whos_up", "guests", "fifty_fifty", "judge_scores",
}


def _backup_filename():
    rodeo_name = get_setting("rodeo_name", "rodeo").strip() or "rodeo"
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", rodeo_name).strip("_") or "rodeo"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    return f"{safe_name}_backup_{stamp}.db"


@app.route("/settings/backup/export")
def backup_export():
    """Streams a full, consistent snapshot of the live database as a
    single downloadable .db file. Uses sqlite3's own backup API rather
    than just copying the file's bytes -- that stays safe even if
    something else happened to have a write in flight at the exact
    moment of backup, which a raw file copy can't guarantee."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(database.DB_PATH)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        src.close()
        dst.close()
    except Exception:
        os.remove(tmp_path)
        flash("Could not create the backup file -- check the app's console window for details.")
        return redirect(url_for("settings"))

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name=_backup_filename())


@app.route("/settings/backup/import", methods=["POST"])
def backup_import():
    """Restores from a previously exported .db file -- a full replace,
    not a merge (this is a backup/restore feature, not a way to combine
    two different rodeos' data). Always takes its own safety copy of
    whatever's currently live before overwriting it, in case the wrong
    file gets chosen by mistake -- this can't otherwise be undone."""
    uploaded = request.files.get("backup_file")
    if not uploaded or not uploaded.filename:
        flash("Choose a backup file to restore first.")
        return redirect(url_for("settings"))

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    uploaded.save(tmp_path)

    # Validate before touching anything live -- a corrupt or unrelated
    # file should never get anywhere near overwriting the real database.
    try:
        check_conn = sqlite3.connect(tmp_path)
        tables = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check_conn.close()
    except sqlite3.DatabaseError:
        os.remove(tmp_path)
        flash("That file doesn't look like a valid backup (not a SQLite database).")
        return redirect(url_for("settings"))

    if not BACKUP_REQUIRED_TABLES.issubset(tables):
        os.remove(tmp_path)
        flash("That file doesn't look like a valid Rodeo Control backup (missing expected tables).")
        return redirect(url_for("settings"))

    safety_dir = os.path.join(os.path.dirname(database.DB_PATH), "backups")
    os.makedirs(safety_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safety_path = os.path.join(safety_dir, f"pre_import_safety_{stamp}.db")

    try:
        shutil.copy2(database.DB_PATH, safety_path)
        shutil.move(tmp_path, database.DB_PATH)
    except OSError as e:
        flash(f"Could not restore the backup -- {e}")
        return redirect(url_for("settings"))

    # The restored file might be from an older build -- run every
    # pending migration against it now, same as a normal startup would.
    init_db()
    xml_export.export_all()
    flash(
        "Backup restored. Whatever was live before is saved as a safety copy in the app's "
        f"backups folder ({os.path.basename(safety_path)}) in case this wasn't the file you meant to restore."
    )
    return redirect(url_for("settings"))


def open_browser():
    # Always open the admin UI via localhost -- 0.0.0.0 below is what
    # lets *other* devices (judges' phones/tablets) reach the server,
    # but it isn't itself a valid address to browse to from this machine.
    webbrowser.open("http://127.0.0.1:5000/")


def local_lan_ip():
    """Best-effort guess at this machine's LAN IP, so the judge applet's
    address can be printed at startup instead of making the scorekeeper
    go dig it out of ipconfig. Doesn't actually send any traffic --
    opening a UDP socket to an external address just makes the OS pick
    which local interface/IP it would use, without anything being
    transmitted."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _judge_url():
    ip = local_lan_ip()
    if not ip:
        return None
    return f"http://{ip}:5000/judge"


def _judge_qr_svg():
    """SVG QR code for the judge applet's URL, embedded directly in the
    Settings page so a judge can just scan it on their own phone/tablet
    instead of typing a LAN address by hand. Returns None if the LAN IP
    couldn't be detected, or if the optional qrcode package isn't
    installed (e.g. install.bat hasn't been rerun since this was added)
    -- either way the page always shows the URL as plain, selectable
    text too, so this is convenience on top of a working fallback, not
    something the app depends on."""
    url = _judge_url()
    if not url:
        return None
    try:
        import qrcode
        import qrcode.image.svg
        import io
    except ImportError:
        return None
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # Strip the XML prolog (not valid/needed for inline SVG embedded
    # directly in an HTML page) and fix the size to clean pixels rather
    # than "mm", which renders inconsistently across devices/browsers.
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r'width="[\d.]+mm"', 'width="220"', svg, count=1)
    svg = re.sub(r'height="[\d.]+mm"', 'height="220"', svg, count=1)
    return svg


if __name__ == "__main__":
    init_db()
    xml_export.export_all()
    threading.Timer(1.0, open_browser).start()

    lan_ip = local_lan_ip()
    print("=" * 60)
    print("Rodeo Control is running.")
    print("This computer:      http://127.0.0.1:5000/")
    if lan_ip:
        print(f"Judges on this WiFi: http://{lan_ip}:5000/judge")
    else:
        print("Couldn't detect a LAN address -- judges' devices need this")
        print("computer's WiFi IP address (see `ipconfig`) followed by")
        print(":5000/judge, e.g. http://192.168.1.42:5000/judge")
    print("If a phone/tablet can't connect, check Windows Firewall hasn't")
    print("blocked this app on your network (see the prompt on first run).")
    print("=" * 60)

    # 0.0.0.0 (not 127.0.0.1) is what makes the server reachable from
    # other devices on the network at all -- judges' phones/tablets need
    # this to load the judge applet.
    app.run(host="0.0.0.0", port=5000, debug=False)
