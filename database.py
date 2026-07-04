import sqlite3
import json
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
        """
    )
    # Migration safety net: if an older database already has a `locations`
    # table without `group_name` (from before this feature existed), add it.
    existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(locations)")]
    if "group_name" not in existing_cols:
        conn.execute("ALTER TABLE locations ADD COLUMN group_name TEXT NOT NULL DEFAULT 'Other'")
    conn.commit()
    conn.close()


def get_or_create_worker(conn, name):
    name = name.strip()
    row = conn.execute("SELECT id FROM workers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO workers (name) VALUES (?)", (name,))
    return cur.lastrowid


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


def save_shifts(entries, shift_date, source_image=None):
    """entries: list of dicts {name, position, group (optional)}.
    Returns count saved, count skipped (duplicates)."""
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


# ---------------- Extended statistics ----------------

def shifts_by_group():
    import location_rules
    conn = get_db()
    rows = conn.execute(
        """SELECT l.group_name AS grp, COUNT(*) c FROM shifts s
           JOIN locations l ON l.id = s.location_id
           GROUP BY l.group_name"""
    ).fetchall()
    conn.close()
    counts = {g: 0 for g in location_rules.GROUP_ORDER}
    for r in rows:
        counts[r["grp"]] = r["c"]
    return counts


def top_locations(limit=12):
    conn = get_db()
    rows = conn.execute(
        """SELECT l.name, l.group_name, COUNT(*) c FROM shifts s
           JOIN locations l ON l.id = s.location_id
           GROUP BY l.id ORDER BY c DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def worker_leaderboard(limit=20):
    conn = get_db()
    rows = conn.execute(
        """SELECT w.id, w.name, COUNT(*) c FROM shifts s
           JOIN workers w ON w.id = s.worker_id
           GROUP BY w.id ORDER BY c DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def shifts_over_time():
    conn = get_db()
    rows = conn.execute(
        """SELECT shift_date, COUNT(*) c FROM shifts
           GROUP BY shift_date ORDER BY shift_date"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def shifts_by_weekday():
    conn = get_db()
    rows = conn.execute("SELECT shift_date FROM shifts").fetchall()
    conn.close()
    from datetime import datetime
    counts = [0] * 7  # Monday=0 ... Sunday=6
    for r in rows:
        try:
            d = datetime.strptime(r["shift_date"], "%Y-%m-%d")
            counts[d.weekday()] += 1
        except (ValueError, TypeError):
            continue
    return {"labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "data": counts}
