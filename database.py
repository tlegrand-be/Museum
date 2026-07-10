import sqlite3
import json
import difflib
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "museum.db"


def get_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            group_name TEXT NOT NULL DEFAULT 'Other'
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            source_image TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE,
            FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE,
            UNIQUE(worker_id, location_id, shift_date)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL,
            note_date TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS location_aliases (
            raw_label TEXT PRIMARY KEY,
            canonical TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quick_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # Migration safety net: if an older database already has a `locations`
    # table without `group_name` (from before this feature existed), add it.
    existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(locations)")]
    if "group_name" not in existing_cols:
        conn.execute("ALTER TABLE locations ADD COLUMN group_name TEXT NOT NULL DEFAULT 'Other'")
    conn.commit()
    conn.close()
    run_location_maintenance()


def _merge_location_id(conn, old_id, canonical_id):
    """Move every shift off `old_id` onto `canonical_id`, then drop the
    now-empty `old_id` location row. A shift that would become an exact
    duplicate of one already on the canonical location is dropped instead of
    moved, same as rename_worker does for colleagues."""
    if old_id == canonical_id:
        return
    for shift in conn.execute("SELECT id FROM shifts WHERE location_id = ?", (old_id,)).fetchall():
        try:
            conn.execute("UPDATE shifts SET location_id = ? WHERE id = ?", (canonical_id, shift["id"]))
        except sqlite3.IntegrityError:
            conn.execute("DELETE FROM shifts WHERE id = ?", (shift["id"],))
    conn.execute("DELETE FROM locations WHERE id = ?", (old_id,))


def _merge_case_variants(conn, canonical_name):
    """Fold every location row that's a case/whitespace variant of
    canonical_name (e.g. "MUSICORUM" vs "Musicorum", saved as two separate
    rows because a manual edit bypassed OCR normalization) into one row
    spelled exactly canonical_name."""
    import location_rules

    target = canonical_name.strip().lower()
    rows = [
        r for r in conn.execute("SELECT id, name FROM locations").fetchall()
        if r["name"].strip().lower() == target
    ]
    if len(rows) <= 1 and (not rows or rows[0]["name"] == canonical_name):
        return
    canonical_row = next((r for r in rows if r["name"] == canonical_name), None)
    canonical_id = canonical_row["id"] if canonical_row else get_or_create_location(
        conn, canonical_name, location_rules.classify_group(canonical_name)
    )
    for row in rows:
        _merge_location_id(conn, row["id"], canonical_id)


def _fold_location(conn, old_name, canonical_name):
    """Permanently fold one specific legacy location name into another --
    e.g. after "Salle 52" got merged into "Balat 52-53-54" as a naming rule --
    moving its shifts across and dropping the now-empty old row. Only
    existing exact-name rows are touched; a no-op if old_name isn't present."""
    import location_rules

    old_row = conn.execute("SELECT id FROM locations WHERE name = ?", (old_name,)).fetchone()
    if not old_row:
        return
    canonical_id = get_or_create_location(conn, canonical_name, location_rules.classify_group(canonical_name))
    _merge_location_id(conn, old_row["id"], canonical_id)


def _sync_location_groups(conn):
    """Recompute every location's wing from its current name via
    classify_group(), correcting any group_name stored under an older wing
    name/definition (e.g. a wing that got renamed after the location was
    first saved)."""
    import location_rules

    for row in conn.execute("SELECT id, name, group_name FROM locations").fetchall():
        correct = location_rules.classify_group(row["name"])
        if row["group_name"] != correct:
            conn.execute("UPDATE locations SET group_name = ? WHERE id = ?", (correct, row["id"]))


def run_location_maintenance():
    """Idempotent location cleanup that runs on every startup, same as the
    schema migration above -- so a naming-rule change or wing rename in the
    code self-heals already-saved rows after a git pull + reload, with no
    manual database access needed."""
    conn = get_db()
    try:
        _merge_case_variants(conn, "Musicorum")
        _fold_location(conn, "Salle 52", "Balat 52-53-54")
        _fold_location(conn, "Balat 51-53-54", "Balat 52-53-54")
        _fold_location(conn, "Balat 7-8-9", "Balat 55-7-8-9")
        _sync_location_groups(conn)
        conn.commit()
    finally:
        conn.close()


def get_or_create_worker(conn, name):
    """Case-insensitive lookup, so "Ekofo" and "EKOFO" (e.g. from a roster
    sheet that's sometimes handwritten in caps) resolve to the same colleague
    instead of silently forking into two workers. If there's no exact match
    but the name is a close spelling of an existing colleague -- e.g. an OCR
    misread like "EKOFO" vs "EKORO" -- resolves to that colleague too, rather
    than creating a near-duplicate."""
    name = name.strip()
    row = conn.execute("SELECT id FROM workers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    similar = find_similar_worker_name(conn, name)
    if similar:
        row = conn.execute("SELECT id FROM workers WHERE name = ?", (similar,)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute("INSERT INTO workers (name) VALUES (?)", (name,))
    return cur.lastrowid


NAME_SIMILARITY_CUTOFF = 0.78


def find_similar_worker_name(conn, name):
    """If `name` doesn't already match an existing colleague (case-insensitively)
    but is a close spelling of one -- e.g. an OCR misread like "EKOFO" vs
    "EKORO" -- return that colleague's name. Used by get_or_create_worker to
    fold near-miss spellings into the existing colleague automatically.
    Returns None if there's an exact match or no close-enough one."""
    name = (name or "").strip()
    if not name:
        return None
    existing = [r["name"] for r in conn.execute("SELECT name FROM workers").fetchall()]
    lname = name.lower()
    lookup = {n.lower(): n for n in existing}
    if lname in lookup:
        return None
    match = difflib.get_close_matches(lname, lookup.keys(), n=1, cutoff=NAME_SIMILARITY_CUTOFF)
    return lookup[match[0]] if match else None


def get_or_create_location(conn, name, group_name="Other"):
    name = name.strip()
    row = conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()
    if row:
        conn.execute("UPDATE locations SET group_name = ? WHERE id = ?", (group_name, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO locations (name, group_name) VALUES (?, ?)", (name, group_name)
    )
    return cur.lastrowid


def get_location_aliases():
    """Map of lowercased raw OCR fragment -> canonical location name, learned
    from past corrections made on the Review screen."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT raw_label, canonical FROM location_aliases").fetchall()
        return {r["raw_label"]: r["canonical"] for r in rows}
    finally:
        conn.close()


def save_location_alias(raw_label, canonical):
    """Remember that a raw OCR fragment (e.g. a cropped "1A") resolves to a
    canonical location name, so future uploads don't need the same manual fix."""
    raw_label = (raw_label or "").strip().lower()
    canonical = (canonical or "").strip()
    if not raw_label or not canonical or raw_label == canonical.lower():
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO location_aliases (raw_label, canonical) VALUES (?, ?) "
            "ON CONFLICT(raw_label) DO UPDATE SET canonical = excluded.canonical",
            (raw_label, canonical),
        )
        conn.commit()
    finally:
        conn.close()


def list_uploads():
    """Every past upload batch (grouped by the source image file behind it),
    newest first, with enough detail to identify and delete a bad one."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT source_image, shift_date, COUNT(*) shift_count,
                      MIN(created_at) uploaded_at
               FROM shifts
               WHERE source_image IS NOT NULL
               GROUP BY source_image, shift_date
               ORDER BY uploaded_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_upload(source_image, shift_date):
    """Remove every shift from one upload batch, then drop any worker or
    location that's left with zero shifts anywhere as a result (they only
    ever exist as a side effect of a saved shift)."""
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM shifts WHERE source_image = ? AND shift_date = ?",
            (source_image, shift_date),
        )
        deleted = cur.rowcount
        conn.execute("DELETE FROM workers WHERE id NOT IN (SELECT DISTINCT worker_id FROM shifts)")
        conn.execute("DELETE FROM locations WHERE id NOT IN (SELECT DISTINCT location_id FROM shifts)")
        conn.commit()
        return deleted
    finally:
        conn.close()


def get_shifts_for_upload(source_image, shift_date):
    """Every individual shift row from one upload batch, for the "Modify"
    screen -- this works even after the source photo has been deleted, since
    it reads back off the shifts table rather than re-running OCR."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT s.id, w.name, l.name AS position FROM shifts s
               JOIN workers w ON w.id = s.worker_id
               JOIN locations l ON l.id = s.location_id
               WHERE s.source_image = ? AND s.shift_date = ?
               ORDER BY w.name COLLATE NOCASE""",
            (source_image, shift_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_worker_names():
    """Every known colleague name, for autocomplete on name fields -- lets
    someone correcting a typo pick the existing spelling instead of retyping
    it and risking a near-duplicate."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT name FROM workers ORDER BY name COLLATE NOCASE").fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


def update_shift_entry(shift_id, name, position, shift_date):
    """Repoint one already-saved shift at a corrected name, location, and/or
    date -- used from the "Modify" screen in Upload history, where the
    source photo may be long gone. Returns whether the shift was found and
    updated."""
    import location_rules

    name = (name or "").strip()
    position = (position or "").strip()
    shift_date = (shift_date or "").strip()
    if not name or not position or not shift_date:
        return False

    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM shifts WHERE id = ?", (shift_id,)).fetchone()
        if not row:
            return False

        group_name = location_rules.classify_group(position)
        worker_id = get_or_create_worker(conn, name)
        location_id = get_or_create_location(conn, position, group_name)

        try:
            conn.execute(
                "UPDATE shifts SET worker_id = ?, location_id = ?, shift_date = ? WHERE id = ?",
                (worker_id, location_id, shift_date, shift_id),
            )
        except sqlite3.IntegrityError:
            # This edit now matches another shift already saved for the same
            # worker/location/date -- the row being edited is a pure
            # duplicate of it, so drop it instead of leaving two.
            conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))

        conn.execute("DELETE FROM workers WHERE id NOT IN (SELECT DISTINCT worker_id FROM shifts)")
        conn.execute("DELETE FROM locations WHERE id NOT IN (SELECT DISTINCT location_id FROM shifts)")
        conn.commit()
        return True
    finally:
        conn.close()


def delete_shift_entry(shift_id):
    """Remove a single shift row outright (e.g. someone who was misread as
    present when they weren't there at all), used from the "Modify" screen.
    Cleans up any worker/location left with zero shifts as a result, same as
    delete_upload does for a whole batch."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
        conn.execute("DELETE FROM workers WHERE id NOT IN (SELECT DISTINCT worker_id FROM shifts)")
        conn.execute("DELETE FROM locations WHERE id NOT IN (SELECT DISTINCT location_id FROM shifts)")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add_shift_entry(source_image, shift_date, name, position):
    """Add a brand-new shift to an already-saved upload batch from the
    "Modify" screen -- e.g. a colleague Gemini missed entirely. Returns False
    if the name/position are blank, or if this exact worker/location/date
    combination already exists."""
    import location_rules

    name = (name or "").strip()
    position = (position or "").strip()
    if not name or not position:
        return False

    conn = get_db()
    try:
        group_name = location_rules.classify_group(position)
        worker_id = get_or_create_worker(conn, name)
        location_id = get_or_create_location(conn, position, group_name)
        try:
            conn.execute(
                "INSERT INTO shifts (worker_id, location_id, shift_date, source_image) VALUES (?, ?, ?, ?)",
                (worker_id, location_id, shift_date, source_image),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
    finally:
        conn.close()


def save_shifts(entries, shift_date, source_image=None):
    """entries: list of dicts {name, position, group (optional)}.
    Returns (count saved, count skipped as duplicates)."""
    import location_rules

    conn = get_db()
    saved, skipped = 0, 0
    try:
        for e in entries:
            name = (e.get("name") or "").strip()
            position = (e.get("position") or "").strip()
            if not name or not position:
                continue
            group_name = e.get("group") or location_rules.classify_group(position)
            worker_id = get_or_create_worker(conn, name)
            location_id = get_or_create_location(conn, position, group_name)
            try:
                conn.execute(
                    "INSERT INTO shifts (worker_id, location_id, shift_date, source_image) VALUES (?, ?, ?, ?)",
                    (worker_id, location_id, shift_date, source_image),
                )
                saved += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return saved, skipped


def overview_stats():
    import location_rules
    conn = get_db()
    total_days = conn.execute("SELECT COUNT(DISTINCT shift_date) c FROM shifts").fetchone()["c"]
    workers = conn.execute(
        """SELECT w.id, w.name, COUNT(s.id) shift_count FROM workers w
           LEFT JOIN shifts s ON s.worker_id = w.id
           GROUP BY w.id ORDER BY w.name COLLATE NOCASE"""
    ).fetchall()
    locations = conn.execute(
        """SELECT l.id, l.name, l.group_name, COUNT(s.id) shift_count FROM locations l
           LEFT JOIN shifts s ON s.location_id = l.id
           GROUP BY l.id"""
    ).fetchall()
    conn.close()
    locations_sorted = sorted(
        [dict(r) for r in locations], key=lambda l: location_rules.roster_sort_key(l["name"])
    )
    return {
        "total_days": total_days,
        "workers": [dict(r) for r in workers],
        "locations": locations_sorted,
    }


def worker_group_breakdown():
    """For the stacked 100% bar chart: for each worker (with at least one shift),
    how many shifts they've had in each museum wing (FORUM/BALAT/MAGRITTE/Other)."""
    import location_rules

    conn = get_db()
    rows = conn.execute(
        """SELECT w.name AS worker, l.group_name AS grp, COUNT(*) c
           FROM shifts s
           JOIN workers w ON w.id = s.worker_id
           JOIN locations l ON l.id = s.location_id
           GROUP BY w.id, l.group_name"""
    ).fetchall()
    conn.close()

    per_worker = {}
    for r in rows:
        per_worker.setdefault(r["worker"], {g: 0 for g in location_rules.GROUP_ORDER})
        per_worker[r["worker"]][r["grp"]] = r["c"]

    # Sort workers by total shift count, descending
    ordered_names = sorted(
        per_worker.keys(), key=lambda n: -sum(per_worker[n].values())
    )

    return {
        "labels": ordered_names,
        "groups": location_rules.GROUP_ORDER,
        "colors": location_rules.GROUP_COLORS,
        "data": {g: [per_worker[n][g] for n in ordered_names] for g in location_rules.GROUP_ORDER},
    }


def all_locations():
    """All locations, in roster-sheet order, for the worker-page sidebar."""
    import location_rules
    conn = get_db()
    rows = conn.execute("SELECT id, name, group_name FROM locations").fetchall()
    conn.close()
    locations = [dict(r) for r in rows]
    locations.sort(key=lambda l: location_rules.roster_sort_key(l["name"]))
    return locations


def last_update():
    """Most recent shift creation timestamp, formatted for display, or None if empty."""
    conn = get_db()
    row = conn.execute("SELECT MAX(created_at) c FROM shifts").fetchone()
    conn.close()
    raw = row["c"] if row else None
    if not raw:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y — %H:%M")
    except ValueError:
        return raw


def worker_location_history(worker_id, location_id):
    """All dates a specific worker worked at a specific location."""
    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    location = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if not worker or not location:
        conn.close()
        return None
    dates = conn.execute(
        """SELECT shift_date FROM shifts
           WHERE worker_id = ? AND location_id = ?
           ORDER BY shift_date DESC""",
        (worker_id, location_id),
    ).fetchall()
    conn.close()
    return {
        "worker": dict(worker),
        "location": dict(location),
        "dates": [r["shift_date"] for r in dates],
    }


def worker_detail(worker_id):
    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.close()
        return None
    by_location = conn.execute(
        """SELECT l.name, COUNT(*) c FROM shifts s
           JOIN locations l ON l.id = s.location_id
           WHERE s.worker_id = ? GROUP BY l.name ORDER BY c DESC""",
        (worker_id,),
    ).fetchall()
    history = conn.execute(
        """SELECT s.shift_date, l.name AS location FROM shifts s
           JOIN locations l ON l.id = s.location_id
           WHERE s.worker_id = ? ORDER BY s.shift_date DESC""",
        (worker_id,),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM shifts WHERE worker_id = ?", (worker_id,)).fetchone()["c"]
    conn.close()
    return {
        "worker": dict(worker),
        "total_shifts": total,
        "by_location": [dict(r) for r in by_location],
        "history": [dict(r) for r in history],
    }


def create_worker(name):
    """Add a brand-new colleague with no shifts yet -- e.g. someone starting
    next week who should already show up in name pickers. Returns
    (worker_id, None) on success, or (None, existing_name) if the name
    already belongs to someone (exact, case-insensitive, or a close-enough
    spelling), or (None, None) if the name is blank."""
    name = name.strip()
    if not name:
        return None, None
    conn = get_db()
    try:
        row = conn.execute("SELECT name FROM workers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if row:
            return None, row["name"]
        similar = find_similar_worker_name(conn, name)
        if similar:
            return None, similar
        cur = conn.execute("INSERT INTO workers (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()


def rename_worker(worker_id, new_name):
    """Rename a colleague in place, e.g. fixing a typo -- if the corrected
    name already belongs to a different existing colleague, merges this
    colleague's shifts into that one instead (repointing each shift, and
    dropping any that would become an exact duplicate) rather than leaving
    two entries for the same person. Returns the worker_id the shifts now
    live under (either this one, renamed, or the one merged into), or None
    if new_name is blank."""
    new_name = (new_name or "").strip()
    if not new_name:
        return None
    conn = get_db()
    try:
        other = conn.execute(
            "SELECT id FROM workers WHERE name = ? COLLATE NOCASE AND id != ?", (new_name, worker_id)
        ).fetchone()
        if other:
            merge_into = other["id"]
            for shift in conn.execute("SELECT id FROM shifts WHERE worker_id = ?", (worker_id,)).fetchall():
                try:
                    conn.execute("UPDATE shifts SET worker_id = ? WHERE id = ?", (merge_into, shift["id"]))
                except sqlite3.IntegrityError:
                    conn.execute("DELETE FROM shifts WHERE id = ?", (shift["id"],))
            conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
            conn.commit()
            return merge_into
        conn.execute("UPDATE workers SET name = ? WHERE id = ?", (new_name, worker_id))
        conn.commit()
        return worker_id
    finally:
        conn.close()


def delete_worker(worker_id):
    """Delete a colleague entirely, along with every shift of theirs (the
    shifts.worker_id FK is ON DELETE CASCADE) -- e.g. someone who was OCR'd
    into existence as a one-off misread and shouldn't be in the ledger at
    all. Also drops any location left with zero shifts as a result."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
        conn.execute("DELETE FROM locations WHERE id NOT IN (SELECT DISTINCT location_id FROM shifts)")
        conn.commit()
    finally:
        conn.close()


def location_detail(location_id):
    conn = get_db()
    location = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if not location:
        conn.close()
        return None
    ranking = conn.execute(
        """SELECT w.id, w.name, COUNT(*) c FROM shifts s
           JOIN workers w ON w.id = s.worker_id
           WHERE s.location_id = ? GROUP BY w.id ORDER BY c DESC""",
        (location_id,),
    ).fetchall()
    history = conn.execute(
        """SELECT s.shift_date, w.name AS worker FROM shifts s
           JOIN workers w ON w.id = s.worker_id
           WHERE s.location_id = ? ORDER BY s.shift_date DESC""",
        (location_id,),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM shifts WHERE location_id = ?", (location_id,)).fetchone()["c"]
    conn.close()
    return {
        "location": dict(location),
        "total_shifts": total,
        "ranking": [dict(r) for r in ranking],
        "history": [dict(r) for r in history],
    }


def all_shifts_for_export():
    conn = get_db()
    rows = conn.execute(
        """SELECT s.shift_date AS date, w.name AS worker, l.name AS location, l.group_name AS area
           FROM shifts s
           JOIN workers w ON w.id = s.worker_id
           JOIN locations l ON l.id = s.location_id
           ORDER BY s.shift_date DESC, w.name COLLATE NOCASE"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- Settings ----------------

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_setting_json(key, default):
    raw = get_setting(key, None)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def set_setting_json(key, value):
    set_setting(key, json.dumps(value))


def clear_all_data():
    """Wipe roster data (shifts/workers/locations) but keep settings and notes."""
    conn = get_db()
    conn.executescript("DELETE FROM shifts; DELETE FROM workers; DELETE FROM locations;")
    conn.commit()
    conn.close()


# ---------------- Notes ----------------

def add_note(tag, note_date, comment):
    conn = get_db()
    conn.execute(
        "INSERT INTO notes (tag, note_date, comment) VALUES (?, ?, ?)",
        (tag.strip(), note_date, comment.strip()),
    )
    conn.commit()
    conn.close()


def get_note(note_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_note(note_id, tag, note_date, comment):
    conn = get_db()
    conn.execute(
        "UPDATE notes SET tag = ?, note_date = ?, comment = ? WHERE id = ?",
        (tag.strip(), note_date, comment.strip(), note_id),
    )
    conn.commit()
    conn.close()


def delete_note(note_id):
    conn = get_db()
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()


def all_tags():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT tag FROM notes ORDER BY tag COLLATE NOCASE").fetchall()
    conn.close()
    # De-duplicate case-insensitively while keeping one representative spelling
    seen = {}
    for r in rows:
        key = r["tag"].strip().lower()
        seen.setdefault(key, r["tag"].strip())
    return list(seen.values())


def add_quick_note(message):
    conn = get_db()
    conn.execute(
        "INSERT INTO quick_notes (message) VALUES (?)",
        (message.strip(),),
    )
    conn.commit()
    conn.close()


def list_quick_notes():
    """Notes from the last 7 days, newest first. Also purges anything older
    while it's here — quick notes are meant to age out on their own, not
    build up as a permanent record like the tagged Notes page does."""
    from datetime import datetime

    conn = get_db()
    conn.execute("DELETE FROM quick_notes WHERE created_at < datetime('now', '-7 days')")
    conn.commit()
    rows = conn.execute("SELECT * FROM quick_notes ORDER BY created_at DESC").fetchall()
    conn.close()

    notes = []
    for r in rows:
        note = dict(r)
        try:
            dt = datetime.strptime(note["created_at"], "%Y-%m-%d %H:%M:%S")
            note["posted_at"] = dt.strftime("%b %d, %H:%M")
        except (ValueError, TypeError):
            note["posted_at"] = note["created_at"]
        notes.append(note)
    return notes


def get_quick_note(note_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM quick_notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_quick_note(note_id, message):
    conn = get_db()
    conn.execute(
        "UPDATE quick_notes SET message = ? WHERE id = ?",
        (message.strip(), note_id),
    )
    conn.commit()
    conn.close()


def delete_quick_note(note_id):
    conn = get_db()
    conn.execute("DELETE FROM quick_notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()


def notes_grouped_by_tag():
    """Group notes by category (case-insensitively, so 'Elevator' and 'elevator'
    merge into one group), each group's entries ordered by the note's own date
    (not upload/creation order), most recent first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notes ORDER BY note_date DESC, id DESC"
    ).fetchall()
    conn.close()

    grouped = {}
    display_name = {}
    for r in rows:
        key = r["tag"].strip().lower()
        display_name.setdefault(key, r["tag"].strip())
        grouped.setdefault(key, []).append(dict(r))

    result = {}
    for key in sorted(grouped.keys(), key=lambda k: display_name[k].lower()):
        result[display_name[key]] = grouped[key]
    return result
