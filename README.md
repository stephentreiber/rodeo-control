# Rodeo Control — Draws, Scoring & Broadcast Graphics Export

A local desktop app for running rodeo draws and scoring, with continuous XML
export for lower thirds and leaderboards (built for vMix and Ross XPression).

All data is stored permanently in a local SQLite database file
(`rodeo_data.db`) in this folder. Closing the app, restarting your computer,
or coming back on day 3 of a multi-day rodeo does **not** lose anything — the
data stays until you explicitly delete it.

## 1. First-time setup

1. Install Python 3.10+ from https://python.org.
   - **Windows:** check "Add Python to PATH" during install.
   - **macOS:** the installer from python.org is recommended over the
     system Python. If you use Homebrew instead, `brew install python`.
   - **Linux:** most distros already have Python 3; if not,
     `sudo apt install python3 python3-pip` (Debian/Ubuntu) or your
     distro's equivalent.
2. Get the code: clone this repo (`git clone <repo-url>`), or use GitHub's
   green **Code → Download ZIP** button, or grab the latest zip from the
   [Releases page](https://github.com/stephentreiber/rodeo-control/releases)
   (this one is named plainly `rodeo-control.zip`, with no version in the
   filename, so extracting it gives a permanently-named `rodeo-control`
   folder that later in-app updates won't make stale). Unzip it wherever
   you'd like.
3. **Windows:** double-click `install.bat` once.
   **macOS/Linux:** open a terminal in this folder and run:
   ```
   chmod +x install.sh run.sh
   ./install.sh
   ```
   (the `chmod` only needs to be done once, the first time)
4. **Windows:** double-click `run.bat` any time you want to start the app.
   **macOS/Linux:** run `./run.sh` any time you want to start it.
   Either way, your default browser opens to the control interface
   automatically.

To stop the app, close the console/terminal window it opened (or press
Ctrl+C).

**Updating to a new version:** open **Settings → Updates** in the app and
click **Check for Updates**. If a newer version is available, its release
notes are shown along with a **Download & Install Update** button — clicking
it downloads and installs the update automatically, without touching your
`rodeo_data.db` or anything in `exports/`. Fully close and restart the app
afterward (the app caches its pages in memory while running, so the update
won't actually take effect until you do). This requires an internet
connection; if the machine you're running on doesn't have one, grab the
latest release manually from the
[Releases page](https://github.com/stephentreiber/rodeo-control/releases)
and drop it in over the old folder instead.

## 2. How it works

- **Competitors** — add everyone competing once (name, hometown, sponsor,
  partner for team events, draw animal/stock, notes). These fields are
  reusable across every event/round they're entered in. Name, hometown,
  partner, and draw animal are automatically forced to ALL CAPS as you
  type them (e.g. `jake wright` → `JAKE WRIGHT`, `red deer ab` →
  `RED DEER, AB`) so typing quickly in any case is fine —
  sponsor and notes are left exactly as typed.
- **Events** — create an event, either picking a standard rodeo event from
  the suggestions or typing any custom name (for your agriculture events).
  Each event is **Judged** (0–100, highest wins), **Timed — seconds**
  (lowest wins), or **Timed — minutes:seconds** (lowest wins; enter times
  like `1:23.45`, plain seconds also work). Import auto-detects a scoring
  type, but it's just a guess — change it any time from the **Dashboard**
  with the dropdown next to each event; it re-formats existing scores
  immediately without touching the underlying data. Every event you create
  shows up as a tab; those tabs (and each event's round tabs) stay pinned
  to the top of the screen as you scroll, so switching between them never
  requires scrolling back up.
- **Rounds** — each event can have as many rounds (go-rounds) as you need —
  Round 1, Round 2, Short Go, Finals, whatever you want to call them. Rounds
  show up as sub-tabs under their event, plus an **All Rounds** tab that
  lists every competitor across every round of that event on one page (handy
  for a quick full-event overview — you can still score directly from it).
  On a longer round name, the tab truncates with an ellipsis (hover for the
  full name) while the round-number badge stays fully visible, so tabs
  don't wrap onto extra lines and eat vertical space on smaller screens.
  Each round has both a **name** (free text, shown on its tab — great for
  keeping performance-style labels like "Perf #1 6:30PM — Thu Jun 25") and a
  separate **round number** (1–10) used only for leaderboard grouping.
  Several differently-named rounds can share the same number — e.g. a slack
  performance and its matching regular performance can both be "Round 1" —
  and the Round Leaderboard will combine them automatically while each keeps
  its own tab for navigation.
- **Quick live switching** — a bar under the top nav has two dropdowns:
  **Live Event** (which event's Aggregate Leaderboard is live, and which
  event the second dropdown applies to) and **Live Round** (which round
  number of that event is live for the Round Leaderboard). Pick the event
  first, hit Go Live, then the Round dropdown updates to that event's round
  numbers — pick one and hit Go Live again. This bar, along with the event
  and round tabs, stays pinned to the top of the screen while you scroll, so
  you can switch what's live from anywhere on a long draw list without
  scrolling back up. A small arrow button in the bottom-right corner of
  every page jumps straight back to the top if you'd rather do that instead.
- **Dashboard** — a quick-navigation view of your events with just the
  shortcuts to jump into a round's scoring or an event's leaderboards. All
  the actual editing (renaming, reordering, changing scoring type, deleting)
  lives on the **Events** page instead, to keep the Dashboard uncluttered.
- **Managing events** — on the Events page, rename an event by editing its
  name box directly (saves when you click away), use the ↑/↓ arrows to
  reorder events, and change scoring type from the dropdown — each saves
  immediately, no separate Save button. Event order is used everywhere else
  events are listed, including the event tabs and the Live Event dropdown.
  Each event also has a **Team/Solo** toggle controlling whether its
  scoring table shows a Heeler column — auto-detected from the event name
  on import ("Team Roping" turns it on), and correctable here any time.
  This is deliberately a property of the event itself, not inferred from
  whether any of its competitors happen to have a team partner — a roper
  who competes in Team Roping and also runs Tie-Down Roping keeps those
  two entries, scores, and leaderboards completely separate no matter what.
- **Draw & Scoring** — open a round to add competitors to that round's draw,
  reorder them with the up/down arrows, and score them as they compete:
  click into the score box, type the number (or a code — see below), hit
  **Enter**. It saves instantly and jumps focus to the next competitor, so
  you never need the mouse once you're in the flow. Judged scores keep
  exact quarter-point precision (.25/.5/.75), not rounded — same for the
  round and aggregate leaderboards and every XML export. Recognized codes:
  `NS` No Score, `BO` Buck Off, `MO` Missed Out, `NT` No Time, `DQ`,
  `DNS` No Show. For Timed — minutes:seconds events, enter `1:23.45` or
  `2:05` (plain seconds like `45.5` also work as a shortcut). Clear a box
  and hit Enter to reset a competitor back to pending. Timed events (both
  seconds and minutes:seconds) also get **+5**/**+10** buttons next to the
  score box for common penalties — type the raw run time, click the
  penalty, and it adds to whatever's in the box and saves immediately, no
  mental math required. Click more than once to stack penalties.
- **Editing the draw** — the competitor's name, hometown, and draw
  animal/stock on the scoring page are all editable directly — click in,
  type the correction, click away (or hit Enter) and it saves. This is
  useful for last-minute stock swaps or a hometown that was wrong on the
  draw sheet. Name and hometown are shared across every round that
  competitor is entered in (editing either updates it everywhere at once),
  but the draw animal is specific to that one round — the same competitor
  can (and usually does) draw a different animal each round, and each
  round remembers its own. For team roping (or any partnered event), the
  scoring table automatically swaps the "Draw Animal" column for a
  "Heeler" column showing the partner's name and hometown — editable the
  same way — since team roping doesn't have a stock animal to track and
  header/heeler are what you actually need to see and fix.
- **Late entries** — click **+ Add Late Entry** above the scoring table
  (visible when a specific round is open, not on the All Rounds tab) to
  append a blank row to the bottom of that round's draw and jump straight
  to it with the cursor ready in the name field — type the name, tab or
  click to the hometown, and they're in the draw with no separate trip to
  the Competitors page. It picks up the next draw number automatically,
  same as everyone else. For team roping (or any partnered event), a
  linked blank heeler comes along with it too, so both header and heeler
  are immediately editable right there in the row instead of showing "no
  partner linked."
- **Lock a round** — click the 🔓/🔒 button above the scoring table (next
  to Add Late Entry) to lock name, hometown, and stock for every entry in
  that round, once the draw's finalized and you want to guard against
  accidental edits. Scores are never locked — you can always keep entering
  results, even on a locked round. Locking is per round, so you can lock
  Round 1 once it's set while still freely editing Round 2's draw. On the
  All Rounds tab, each row respects its own round's lock state.
- **Who's Up** — click "Set Who's Up" next to whoever is currently
  competing; it updates instantly in place without reloading the page, so
  you don't lose your scroll position while running down a long draw list.
- **Leaderboards** — on an event's Leaderboards page, pick which round is
  the "live" Round Leaderboard for that specific round's standings. The
  Aggregate Leaderboard needs no round picking at all — it automatically
  combines a competitor's scores across every round in that event and
  displays as `total/head` (e.g. `186/2` for 186 points on 2 head). Click
  "Set as Live Aggregate Leaderboard" once per event; it stays live and
  updates itself as more scores come in. The page also auto-refreshes every
  few seconds so you can watch it update. Ranking always favors more head
  completed first — someone on 2 head ranks ahead of someone on 1 head
  regardless of raw total, since they've simply done more; the total only
  breaks ties between competitors on the same head count.

## 3. The exported XML files

Every time you save a score, change Who's Up, or change which leaderboard is
"live," three files are rewritten in your export folder (default:
`exports/` inside this app folder, changeable in **Settings**):

- `whos_up.xml` — the current competitor's fields, for your lower third
- `round_leaderboard.xml` — ranked standings for the currently selected round
- `aggregate_leaderboard.xml` — ranked standings summed across the rounds
  you selected

All three are plain, flat XML with predictable, repeated field names —
no proprietary schema, so both vMix and Ross XPression can bind to them
directly.

### Example: `whos_up.xml`
```xml
<whos_up>
  <draw_number>4</draw_number>
  <name>Jane Doe</name>
  <hometown>Okotoks, AB</hometown>
  <sponsor>ABC Ranch</sponsor>
  <heeler_name>John Smith</heeler_name>
  <heeler_hometown>Red Deer, AB</heeler_hometown>
  <draw_animal>Firefly</draw_animal>
  <notes></notes>
  <event>Barrel Racing</event>
  <round>Round 1</round>
  <round_score>14.50</round_score>
  <time_plus_penalty>4.50+10</time_plus_penalty>
  <round_rank>2nd</round_rank>
  <aggregate_rank>4th</aggregate_rank>
  <aggregate_score>34.84/2</aggregate_score>
</whos_up>
```

`round_score` fills in the moment a score is entered for whoever's up
(blank before that), ready for a "just scored: 87.5" style lower third —
and it shows the short penalty code too (`NT`, `BO`, `MO`, `DQ`, `NS`,
`DNS`) if that's what got entered instead of a number, so the audience sees
what actually happened on a no-time or a buck-off, not just a blank field.
It's always the score for that one specific round/run only — never an
aggregate — since Who's Up always points at one specific entry.
`time_plus_penalty` breaks the same run down as "raw+penalty" (e.g.
`4.50+10` for a 4.5-second run with a 10-second penalty added via the
`+5`/`+10` buttons) — handy for a lower third that wants to show the
penalty explicitly rather than just the combined number. If there's no
penalty on record it just shows the plain time, same as `round_score`.
Either way, `round_score`, the round leaderboard, and the aggregate always
use the combined final time (including any penalty) for ranking — the
breakdown is purely a display option for Who's Up. `round_rank` and
`aggregate_rank` show where that score currently places them — "2nd" in the
live round, "4th" overall — as separate fields so you can bind them to
separate text objects (e.g. a template with static "RND:" and "AGG:" labels
next to each field, or however your graphics package wants it laid out).
`aggregate_score` is their running total across the event as `total/head`
(points on however many head they've been on so far). All four stay blank
until the competitor has an actual scored run. `heeler_name` and
`heeler_hometown` are for team roping (or any partnered event) — the
"who's up" competitor is always whoever's listed as the header/primary
draw (their info is just the regular `name`/`hometown` fields above), and
these two extra fields give you their heeler's name and hometown for the
same lower third. Both are blank for solo events.

### Example: `round_leaderboard.xml`
```xml
<leaderboard event="Team Roping" round="Round 1">
  <all_ranks>1&#13;2</all_ranks>
  <all_names>Jane Doe/John Smith&#13;Bill Jones/Sam Lee</all_names>
  <all_lastnames>Doe/Smith&#13;Jones/Lee</all_lastnames>
  <all_scores>17.42&#13;18.05</all_scores>
  <entry>
    <rank>1</rank>
    <name>Jane Doe</name>
    <hometown>Okotoks, AB</hometown>
    <sponsor>ABC Ranch</sponsor>
    <draw_animal>Firefly</draw_animal>
    <score>17.42</score>
    <team_full_names>Jane Doe/John Smith</team_full_names>
    <team_last_names>Doe/Smith</team_last_names>
  </entry>
</leaderboard>
```

### Example: `aggregate_leaderboard.xml`
```xml
<aggregate_leaderboard event="Team Roping">
  <all_ranks>1&#13;2</all_ranks>
  <all_names>Jane Doe/John Smith&#13;Bill Jones/Sam Lee</all_names>
  <all_lastnames>Doe/Smith&#13;Jones/Lee</all_lastnames>
  <all_scores>17.42/1&#13;18.05/1</all_scores>
  <entry>
    <rank>1</rank>
    <name>Jane Doe</name>
    <hometown>Okotoks, AB</hometown>
    <aggregate_score>17.42</aggregate_score>
    <total>17.42</total>
    <head>1</head>
    <team_full_names>Jane Doe/John Smith</team_full_names>
    <team_last_names>Doe/Smith</team_last_names>
  </entry>
</aggregate_leaderboard>
```

`team_full_names` and `team_last_names` are built for team roping: "Header
Full Name/Heeler Full Name" and "Header Last/Heeler Last" (e.g. `Jane
Doe/John Smith` and `Doe/Smith`). These are on every leaderboard entry
regardless of event type — for a solo event they just show that one
competitor's name/last name with no slash, so it's safe to bind these
fields on every leaderboard graphic whether or not the event is partnered.
Header and heeler are linked as an actual pair behind the scenes (not just
matching text), so their hometowns resolve correctly even if one of them
gets edited later.

`all_ranks`, `all_names`, `all_lastnames`, and `all_scores` — in that
order, at the top of the file ahead of the per-entry `<entry>` blocks —
together form a complete leaderboard as four parallel fields. Line 1 of
each always refers to the same competitor, line 2 to the next, and so on —
handy for a one-box "who's on the board" graphic built from text boxes
instead of a repeating-row binding (`all_scores` uses `total/head` for the
aggregate board, matching `aggregate_score`). `all_names` and
`all_lastnames` use the same "Header/Heeler" formatting as `team_full_names`
and `team_last_names` for team roping, so a partnered board reads e.g.
`Jane Doe/John Smith`, `Bill Jones/Sam Lee` for `all_names` and
`Doe/Smith`, `Jones/Lee` for `all_lastnames` (handy if your graphic only
has room for last names) — solo events just show the one name per line, no
stray slash. Names are joined with the literal
`&#13;` (carriage return) character reference rather than a raw line break.
This matters: XML parsers normalize actual CR/LF bytes in a file during
parsing, so a literal line break wouldn't reliably survive — but a numeric
character reference like `&#13;` is exempt from that normalization and
resolves to a real line-break character once your graphics software parses
the file, which is the standard workaround for embedding line breaks in a
single XML-sourced text field (the same trick used for vMix titles and
similar data-bound text engines).

If your graphics software can't parse a line-broken field, or can't bind to
a repeating list at all, both leaderboard files also include the same data
as individual numbered fields: `rank_1`/`name_1`/`lastname_1`/`score_1`,
`rank_2`/`name_2`/`lastname_2`/`score_2`, and so on. These always exist at
a fixed count — blank past however many competitors are actually on the
board — so the document's structure never changes shape, which matters for
software that binds to a specific named field once at setup time and
expects it to always be there.

Both the CR-joined fields and the individual numbered fields are capped
together by the same **"Number of Competitors on Leaderboard"** setting on
the **Settings** page (default 10; pick any number 1–20 or "All") — most
boards only need the top handful shown at once, not every scored
competitor. The numbered fields specifically can't be truly unlimited
(a fixed set of tags needs an actual number to exist), so picking "All"
still caps them at 20 while leaving the CR-joined fields genuinely
unlimited.

Files are written atomically (written to a temp file, then swapped in), so
your graphics software will never catch a half-written file. Every tag
described above is always present in the file, even before you've set
anything up (just empty) — the document structure never changes shape, only
the values inside it. The only thing that legitimately varies in length is
the list of `<entry>` blocks, since a leaderboard naturally has as many
rows as there are scores. This matters for broadcast graphics data
bindings, which generally expect a stable, predictable document shape — a
field that's sometimes missing outright is far more likely to show a data
error or freeze on a stale value than a field that's simply empty.

## 4. Hooking up vMix

Note: vMix and Ross XPression are both Windows-only software, regardless of
what computer Rodeo Control itself is running on. If you're running Rodeo
Control on a Mac or Linux machine, point vMix/XPression (on their own
Windows machine) at a shared network folder instead of a local path — see
the Export Folder setting mentioned in step 2 below.

1. In vMix, add a **Data Source**: Settings → Data Sources → Add.
2. Choose **XML File**, point it at e.g. `C:\RodeoApp\exports\whos_up.xml`.
3. Set the refresh interval (1–2 seconds is plenty).
4. Map the XML fields (`name`, `hometown`, `sponsor`, etc.) to your title's
   text fields in the Data Source field mapping.
5. Repeat for `round_leaderboard.xml` (bind the repeating `<entry>` list to
   a multi-row title) and `aggregate_leaderboard.xml`.

## 5. Hooking up Ross XPression

1. In XPression's Data Center / Inventory, create an XML data connection
   pointed at the same three files (or copy the export folder to wherever
   your XPression machine reads from, e.g. a shared network folder — just
   change the Export Folder in **Settings** to that shared path).
2. Bind `whos_up.xml`'s fields directly to a Data Store/Scene for your lower
   third.
3. Bind the repeating `<entry>` elements in `round_leaderboard.xml` and
   `aggregate_leaderboard.xml` to a repeating grid/list scene for your
   leaderboard graphics.

If your XPression machine is a separate computer from the one running this
app, point **Export Folder** (in Settings) at a shared network drive both
machines can see, instead of the local `exports` folder.

## 6. Importing a draw from RodeoCanada

Instead of typing in every competitor by hand, go to **Import Draw** in the
nav bar and upload a saved draw sheet — **HTML**, **Excel (.xlsx)**, or
**PDF** — or paste HTML directly. It automatically:

- Splits the page into events, rounds (including slack performances), and
  competitors
- Strips the leading draw number into its own **Draw #** field, shown as its
  own column on the scoring page and included in the XML exports — handy for
  following along live
- Fixes hometowns that are missing the comma before the province/state
  (e.g. `STETTLER AB` → `STETTLER, AB`)
- Forces name, hometown, and draw-animal fields to ALL CAPS, so manually
  typed entries and edits always match the same style as an imported draw
  regardless of how they were typed
- Detects team roping's two-line format (header roper on one row, partner on
  the row below with no draw number) and attaches the partner automatically
- Recognizes existing competitors by name so the same person entered in
  multiple events (e.g. saddle bronc and team roping) doesn't get duplicated

**PDF draw sheets**: text-based PDFs are read directly. A page that turns
out to be a scanned image (no text layer) is automatically read with OCR
instead — you'll see a warning flagging exactly which page(s) needed OCR, so
you know where to look closely on the review screen below. OCR requires
**Tesseract OCR** and **Poppler** to be installed on this machine separately
(they're not Python packages, so `pip install` alone won't get them):
- Windows: install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki)
  and [Poppler for Windows](https://github.com/oschwartz10612/poppler-windows/releases),
  and make sure both are on your `PATH`.
- macOS: `brew install tesseract poppler`
- Linux: `sudo apt install tesseract-ocr poppler-utils` (Debian/Ubuntu) or
  your distro's equivalent packages
- If neither is installed, HTML/Excel import and text-based PDF import still
  work fine — only scanned/image PDFs need them, and you'll get a clear
  message instead of a crash if OCR is attempted without them.

After uploading, you'll see a preview broken down by event and round with
everything checked by default — click anywhere on a round's row (not just
the tiny checkbox) to toggle it — and hit **Continue to Review** (the
button appears both at the top and bottom of the page, so you never have to
scroll down to it after reviewing).

Each round keeps its original name from the page — no auto-renaming. There's
a separate **Round #** dropdown per row (1–10) purely for leaderboard
grouping: giving several performances the same round number combines them
for round-leaderboard purposes without touching their names or merging their
draw lists. Leaving it on "auto" shows exactly which number it'll pick in
brackets (e.g. "auto (3)"), accounting for any rounds that event already has
and for other rows above it also left on auto. There's also an "Apply Round # to all checked rows" control at
the top of the page — check the performances that belong together (e.g. all
the slack performances), pick the round number, hit **Apply to Selected**,
and repeat for the next group. Draw order is preserved within each import.

**Review screen**: before anything is written to the database, you'll see
every competitor about to be added — grouped by event/round, one dense
table per round — with editable Name / Hometown / Draw Animal (and Partner,
for team roping) fields pre-filled from the import. This is the place to
fix anything the importer (or OCR) got wrong; hit **Import These
Competitors** once everything looks right. You can still add competitors
manually or adjust anything afterward, too.

This is tuned to the RodeoCanada draw sheet layout specifically. If another
association's site or file is laid out differently and the import misses
things, open an issue with a sample file and the parser can be extended.

Made a mistake on a round number after importing? Open that round's tab and
use the **Edit this round** section — you can change its name and/or round
number any time, and everything downstream (leaderboard grouping, exports)
picks up the change immediately.

## 7. Clearing data between rodeos

There's no single "delete everything" button on purpose — competitors,
events, and rounds each have their own Delete action so you don't
accidentally wipe an active rodeo. To start completely fresh for a new
rodeo, close the app and delete `rodeo_data.db`; a new empty one will be
created next time you run it. Consider renaming/archiving the old
`rodeo_data.db` first if you want to keep the record.

## 8. Status & roadmap

This first version covers competitors, events, rounds, draws, scoring,
Who's Up, round leaderboards, and aggregate leaderboards, all exporting to
XML. Ideas for what could come next: tie-break rules, photo/headshot
fields, exporting a full running order, or multi-rodeo history/archiving.
Issues and pull requests are welcome.
