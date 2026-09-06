import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rodeo_data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hometown TEXT DEFAULT '',
    sponsor TEXT DEFAULT '',
    partner TEXT DEFAULT '',
    partner_id INTEGER,
    draw_animal TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(partner_id) REFERENCES competitors(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scoring_type TEXT NOT NULL DEFAULT 'judged',
    is_team INTEGER NOT NULL DEFAULT 0,
    position INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    competitor_id INTEGER NOT NULL,
    draw_order INTEGER NOT NULL DEFAULT 0,
    draw_number INTEGER,
    draw_animal TEXT,
    score_value REAL,
    penalty REAL NOT NULL DEFAULT 0,
    penalty_note TEXT NOT NULL DEFAULT '',
    pending_penalty REAL NOT NULL DEFAULT 0,
    pending_penalty_note TEXT NOT NULL DEFAULT '',
    pending_score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    re_ride_taken INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(round_id) REFERENCES rounds(id),
    FOREIGN KEY(competitor_id) REFERENCES competitors(id)
);

CREATE TABLE IF NOT EXISTS whos_up (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    entry_id INTEGER,
    guest_id INTEGER,
    FOREIGN KEY(entry_id) REFERENCES entries(id),
    FOREIGN KEY(guest_id) REFERENCES guests(id)
);

CREATE TABLE IF NOT EXISTS guests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hometown TEXT DEFAULT '',
    sponsor TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    position INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fifty_fifty (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total TEXT DEFAULT '',
    winning_number TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judge_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    judge_seat INTEGER NOT NULL,
    rider_score REAL,
    stock_score REAL,
    outcome TEXT,
    submitted_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(entry_id) REFERENCES entries(id),
    UNIQUE(entry_id, judge_seat)
);
"""

DEFAULT_SETTINGS = {
    "rodeo_name": "My Rodeo",
    "export_folder": os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports"),
    "live_event_id": "",
    "active_round_number": "",
    "names_field_limit": "10",
    "hide_contestant_panel": "",
    "judge_count": "2",
    # Blank by default for each -- falls back to "Judge N" wherever
    # shown (see _judge_names in app.py). Lets the scorekeeper see who
    # actually hasn't submitted at a glance instead of an anonymous
    # seat number, and lets judges confirm they picked the right seat.
    "judge_name_1": "",
    "judge_name_2": "",
    "judge_name_3": "",
    "judge_name_4": "",
    # The Announcer Screen's own three-color palette -- it's a separate
    # full-screen dark monitor view (see announcer.html/announcer.css),
    # not part of the main app's theme, so it gets its own small palette
    # customizable per-booth rather than inheriting the main app's
    # colors, which wouldn't suit a screen meant to be read from across
    # a room. Defaults match the original hardcoded dark brown/gold look
    # exactly.
    "announcer_color_bg": "#14100d",
    "announcer_color_accent": "#d9a441",
    "announcer_color_text": "#f2e9dd",
    # Off by default -- video review isn't available at every rodeo, so
    # the VR code in the quick-entry score field only works once this is
    # explicitly turned on (see scoring.parse_score_input).
    "enable_video_review": "",
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()

    # Rename the pre-generalization "royalty" table to "guests" if an
    # earlier build already created and populated it -- must happen
    # before the schema script below, since CREATE TABLE IF NOT EXISTS
    # "guests" would otherwise create an empty guests table first and
    # make this rename look already-done, silently losing that data.
    existing_tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "guests" not in existing_tables and "royalty" in existing_tables:
        conn.execute("ALTER TABLE royalty RENAME TO guests")

    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO whos_up (id, entry_id) VALUES (1, NULL)")
    conn.execute("INSERT OR IGNORE INTO fifty_fifty (id, total, winning_number) VALUES (1, '', '')")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    # Lightweight migration for databases created before draw_number existed.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(entries)")]
    if "draw_number" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN draw_number INTEGER")
    if "draw_animal" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN draw_animal TEXT")
    if "penalty" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN penalty REAL NOT NULL DEFAULT 0")
    # Lightweight migration for databases created before judge-flagged timed-
    # event penalties existed (barrier, one heel, etc. -- see judge_score.html
    # and scoring.TIMED_PENALTY_BUTTONS). penalty_note is a human-readable
    # breakdown of what's already folded into `penalty` (e.g. "Barrier +10"),
    # kept separate from the numeric total so the scorekeeper can see WHY a
    # penalty exists, not just how big it is. pending_penalty/_note hold a
    # penalty a judge flagged on their own device before the scorekeeper has
    # typed a time for that run yet -- add_penalty (and the scorekeeper's own
    # same-device staging) both require an existing scored entry to add on
    # top of, but a judge's device has no way to know if a time is typed and
    # not yet saved, so this has to persist server-side instead. It gets
    # folded into score_value/penalty automatically the moment a real time is
    # saved for that entry (see quick_score/score_entry).
    if "penalty_note" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN penalty_note TEXT NOT NULL DEFAULT ''")
    if "pending_penalty" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN pending_penalty REAL NOT NULL DEFAULT 0")
    if "pending_penalty_note" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN pending_penalty_note TEXT NOT NULL DEFAULT ''")
    # A judged (roughstock) entry a judge flagged for Video Review (see
    # scoring.TOKEN_MAP's "VR" entry) holds its computed rider+stock
    # total here instead of in score_value while status='video_review'
    # -- unlike Re-Ride, VR defaults to NOT counting on the leaderboard
    # yet, since a review often confirms the score but sometimes
    # changes it. resolve_video_review moves this into score_value once
    # the scorekeeper confirms the review didn't change anything.
    if "pending_score" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN pending_score REAL")

    # Cleanup for installs that ran the brief window where the main
    # app's colors were customizable (theme_color_primary/accent/tint).
    # That control was removed -- colors are just fixed in style.css's
    # :root now -- so these are dead settings; delete them if present
    # rather than leaving orphaned rows around.
    conn.execute("DELETE FROM settings WHERE key IN ('theme_color_primary', 'theme_color_accent', 'theme_color_tint')")

    comp_cols = [r["name"] for r in conn.execute("PRAGMA table_info(competitors)")]
    if "partner_id" not in comp_cols:
        conn.execute("ALTER TABLE competitors ADD COLUMN partner_id INTEGER")

    round_cols = [r["name"] for r in conn.execute("PRAGMA table_info(rounds)")]
    if "locked" not in round_cols:
        conn.execute("ALTER TABLE rounds ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")

    # Lightweight migration for databases created before Guests existed --
    # whos_up needs a second pointer alongside entry_id so it can point at
    # either a competitor's draw entry OR a guest roster record, never
    # both (whichever is set last wins; the other side is always cleared).
    whos_up_cols = [r["name"] for r in conn.execute("PRAGMA table_info(whos_up)")]
    if "guest_id" not in whos_up_cols:
        conn.execute("ALTER TABLE whos_up ADD COLUMN guest_id INTEGER")
        # Carry over the live pointer from the old column name, if any --
        # the column itself is left in place rather than dropped (simpler
        # and safer than DROP COLUMN across SQLite versions) but is dead
        # from here on; nothing else reads it.
        if "royalty_id" in whos_up_cols:
            conn.execute("UPDATE whos_up SET guest_id = royalty_id WHERE royalty_id IS NOT NULL")

    event_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(events)")]
    if "is_team" not in event_cols2:
        conn.execute("ALTER TABLE events ADD COLUMN is_team INTEGER NOT NULL DEFAULT 0")

    # Lightweight migration for databases created before judges could mark
    # a non-numeric outcome (Buck Off, No Score, DQ) instead of a rider/
    # stock score.
    judge_score_cols = [r["name"] for r in conn.execute("PRAGMA table_info(judge_scores)")]
    if "outcome" not in judge_score_cols:
        conn.execute("ALTER TABLE judge_scores ADD COLUMN outcome TEXT")

    # Groundwork for the scorekeeper's upcoming "take the re-ride" control:
    # a re-ride still carries a real rider score (judges submit one, and
    # the cowboy can choose to keep it), but if the re-ride is taken that
    # score needs to come off the leaderboard -- while the stock score
    # stays, since stock contractors are judged independently of the
    # rider. This flag is unused by anything yet; it's here so that
    # piece doesn't need its own migration later.
    entry_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(entries)")]
    if "re_ride_taken" not in entry_cols2:
        conn.execute("ALTER TABLE entries ADD COLUMN re_ride_taken INTEGER NOT NULL DEFAULT 0")
        # Backfill from the event NAME, not from any competitor's partner
        # link -- a competitor's partner_id is a property of that person
        # and can carry over from a totally different event (e.g. someone
        # who ropes team and also runs tie-down), so it's not a safe signal
        # for "is this event itself a team event."
        conn.execute("UPDATE events SET is_team = 1 WHERE UPPER(name) LIKE '%TEAM ROPING%'")

    # Lightweight migration for databases created before event ordering existed.
    event_cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)")]
    if "position" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN position INTEGER DEFAULT 0")
        rows = conn.execute("SELECT id FROM events ORDER BY created_at").fetchall()
        for i, r in enumerate(rows):
            conn.execute("UPDATE events SET position = ? WHERE id = ?", (i, r["id"]))

    # Best-effort link any partner references that are still just free text
    # (manually-typed partner names, or data from before partner_id existed)
    # to an actual competitor record by exact name match, so hometown etc.
    # can be looked up for team roping exports. Cheap and idempotent, so it
    # just runs every startup rather than needing its own migration flag.
    unlinked = conn.execute(
        "SELECT id, partner FROM competitors WHERE partner_id IS NULL AND partner != ''"
    ).fetchall()
    for row in unlinked:
        match = conn.execute(
            "SELECT id FROM competitors WHERE UPPER(name) = UPPER(?) AND id != ?",
            (row["partner"], row["id"]),
        ).fetchone()
        if match:
            conn.execute("UPDATE competitors SET partner_id = ? WHERE id = ?", (match["id"], row["id"]))

    conn.commit()
    conn.close()
    os.makedirs(DEFAULT_SETTINGS["export_folder"], exist_ok=True)


def get_setting(key, default=""):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()
