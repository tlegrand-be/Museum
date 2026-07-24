import os
import uuid
import json
import calendar
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort, session
)
from werkzeug.utils import secure_filename

load_dotenv()

import database
import gemini_extract
import excel_export
import location_rules
import magritte_shifts
import settings_defs

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
PENDING_DIR = BASE_DIR / "instance" / "pending"
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years

# Two shared passcodes. SITE_PASSCODE unlocks full admin access; VIEWER_PASSCODE
# unlocks read-only access plus the (cookie-only, personal) settings page.
# Override either with the matching environment variable.
SITE_PASSCODE = os.environ.get("SITE_PASSCODE", "4444")
VIEWER_PASSCODE = os.environ.get("VIEWER_PASSCODE", "1234")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(days=30)

UPLOAD_DIR.mkdir(exist_ok=True)
PENDING_DIR.mkdir(parents=True, exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def get_cookie_json(name, default):
    raw = request.cookies.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def effective_group_colors():
    """Museum-wing chart colors, personalized per-browser via cookie, falling
    back to the site defaults for any wing the visitor hasn't customized."""
    overrides = get_cookie_json("wing_colors", {})
    colors = dict(location_rules.GROUP_COLORS)
    for k, v in overrides.items():
        if k in colors and isinstance(v, str):
            colors[k] = v
    return colors


@app.before_request
def ensure_db():
    # Always runs (not just on first-ever request) since init_db() is all
    # CREATE TABLE IF NOT EXISTS / guarded ALTER — this is what makes a new
    # table added in a later update actually appear on an already-deployed
    # database after a git pull + reload, with no manual migration step.
    database.init_db()


@app.before_request
def require_passcode():
    if request.endpoint in ("passcode_page", "static"):
        return
    if not session.get("role"):
        return redirect(url_for("passcode_page"))


@app.context_processor
def inject_globals():
    return {
        "current_theme": request.cookies.get("theme", settings_defs.DEFAULT_THEME),
        "group_colors": effective_group_colors(),
        "is_admin": session.get("role") == "admin",
    }


def admin_required(view):
    """Gate a whole view to the admin passcode. Viewers get bounced to the
    overview with an explanation instead of a raw 403, since this app has no
    separate admin area -- viewers can otherwise reach every URL."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("You need admin access to do that.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


# ---------------- Passcode gate ----------------

@app.route("/passcode", methods=["GET", "POST"])
def passcode_page():
    if request.method == "POST":
        code = request.form.get("passcode", "")
        if code == SITE_PASSCODE:
            session.permanent = True
            session["role"] = "admin"
            return redirect(url_for("index"))
        if code == VIEWER_PASSCODE:
            session.permanent = True
            session["role"] = "viewer"
            return redirect(url_for("index"))
        flash("Incorrect passcode.", "error")
    return render_template("passcode.html")


@app.route("/lock")
def lock():
    session.pop("role", None)
    return redirect(url_for("passcode_page"))


# ---------------- Overview ----------------

@app.route("/")
def index():
    stats = database.overview_stats()
    group_chart = database.worker_group_breakdown()
    last_update = database.last_update()
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    quick_notes = database.list_quick_notes(limit=3)
    return render_template(
        "index.html", stats=stats, group_chart=group_chart,
        last_update=last_update, widgets=widgets, quick_notes=quick_notes,
    )


@app.route("/notes/quick", methods=["POST"])
def add_quick_note():
    message = (request.form.get("message") or "").strip()
    if message:
        database.add_quick_note(message)
    return redirect(url_for("index"))


@app.route("/notes/quick/<int:note_id>/edit", methods=["POST"])
@admin_required
def edit_quick_note(note_id):
    message = (request.form.get("message") or "").strip()
    if message:
        database.update_quick_note(note_id, message)
    return redirect(url_for("index"))


@app.route("/notes/quick/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_quick_note(note_id):
    database.delete_quick_note(note_id)
    return redirect(url_for("index"))


# ---------------- Upload / Review ----------------

@app.route("/upload", methods=["GET", "POST"])
@admin_required
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    file = request.files.get("photo")

    if not file or file.filename == "":
        flash("Please choose an image to upload.", "error")
        return redirect(url_for("upload"))

    if not allowed_file(file.filename):
        flash("Please upload a PNG, JPG, or WEBP image.", "error")
        return redirect(url_for("upload"))

    tmp_name = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
    filepath = UPLOAD_DIR / tmp_name
    file.save(filepath)

    try:
        result = gemini_extract.extract_roster(str(filepath))
    except Exception as e:
        flash(f"Could not read the image: {e}", "error")
        return redirect(url_for("upload"))

    entries = result.get("entries", [])
    detected_date = result.get("date") or date.today().isoformat()
    detected_columns = result.get("time_slot_columns") or []

    # Apply previously-learned corrections for cropped/partial location labels
    # (e.g. a bare "1A" that a human previously corrected to "Argenteau" on
    # the Review screen) before showing entries for review.
    aliases = database.get_location_aliases()
    for e in entries:
        key = (e.get("raw_position") or "").strip().lower()
        if key in aliases:
            e["position"] = aliases[key]

    if not detected_columns:
        flash("Could not identify the time-slot columns on that roster. Try a clearer photo.", "error")
        return redirect(url_for("upload"))

    if not entries:
        extracted_slot = result.get("extracted_time_slot") or detected_columns[0]
        flash(f'No names were detected in the "{extracted_slot}" column for that date. Try a clearer photo.', "error")
        return redirect(url_for("upload"))

    token = uuid.uuid4().hex
    pending_path = PENDING_DIR / f"{token}.json"
    pending_path.write_text(json.dumps({
        "entries": entries,
        "shift_date": detected_date,
        "source_image": tmp_name,
    }))

    return redirect(url_for("review", token=token))


@app.route("/review/<token>", methods=["GET", "POST"])
@admin_required
def review(token):
    pending_path = PENDING_DIR / f"{token}.json"
    if not pending_path.exists():
        flash("That review session expired. Please upload again.", "error")
        return redirect(url_for("upload"))

    data = json.loads(pending_path.read_text())

    if request.method == "POST":
        names = request.form.getlist("name")
        positions = request.form.getlist("position")
        raw_positions = request.form.getlist("raw_position")
        orig_positions = request.form.getlist("orig_position")
        keep = request.form.getlist("keep")

        entries = []
        for i in range(len(names)):
            if str(i) not in keep:
                continue
            submitted = positions[i].strip()
            entries.append({"name": names[i], "position": submitted})

            # If this label wasn't already resolved by a known rule (raw ==
            # the shown, pre-edit value) and the user corrected it here,
            # remember that fragment for next time.
            raw = raw_positions[i] if i < len(raw_positions) else ""
            orig = orig_positions[i] if i < len(orig_positions) else ""
            if raw.strip().lower() == orig.strip().lower() and submitted.lower() != orig.strip().lower():
                database.save_location_alias(raw, submitted)

        shift_date = request.form.get("shift_date") or data["shift_date"]
        saved, skipped = database.save_shifts(entries, shift_date, data.get("source_image"))
        pending_path.unlink(missing_ok=True)

        # The photo's only job was getting data into the ledger — once that's
        # done, delete it so PythonAnywhere's limited disk quota doesn't fill
        # up with roster photos that are never looked at again.
        source_image = data.get("source_image")
        if source_image:
            (UPLOAD_DIR / source_image).unlink(missing_ok=True)

        msg = f"Saved {saved} shift{'s' if saved != 1 else ''}."
        if skipped:
            msg += f" ({skipped} already existed for that date and were skipped.)"
        flash(msg, "success")
        return redirect(url_for("index"))

    locations = list(location_rules.ROSTER_ORDER)
    worker_names = database.all_worker_names()
    return render_template(
        "review.html", token=token, data=data, locations=locations, worker_names=worker_names,
    )


UPLOADS_PER_PAGE = 10


@app.route("/uploads")
def uploads_page():
    all_uploads = database.list_uploads()
    total_pages = max(1, -(-len(all_uploads) // UPLOADS_PER_PAGE))
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(page, 1), total_pages)

    start = (page - 1) * UPLOADS_PER_PAGE
    uploads = all_uploads[start:start + UPLOADS_PER_PAGE]
    for u in uploads:
        u["photo_available"] = bool(u.get("source_image")) and (UPLOAD_DIR / u["source_image"]).is_file()
    return render_template(
        "uploads.html", uploads=uploads, page=page, total_pages=total_pages,
        total_uploads=len(all_uploads),
    )


@app.route("/uploads/delete", methods=["POST"])
@admin_required
def delete_upload():
    source_image = request.form.get("source_image")
    shift_date = request.form.get("shift_date")
    if not source_image or not shift_date:
        abort(400)
    deleted = database.delete_upload(source_image, shift_date)
    flash(f"Removed {deleted} shift{'s' if deleted != 1 else ''} from that upload.", "success")
    return redirect(url_for("uploads_page"))


@app.route("/uploads/edit", methods=["GET", "POST"])
@admin_required
def edit_upload():
    source_image = request.values.get("source_image")
    shift_date = request.values.get("shift_date")
    if not source_image or not shift_date:
        abort(400)

    if request.method == "POST":
        shift_ids = request.form.getlist("shift_id")
        names = request.form.getlist("name")
        positions = request.form.getlist("position")
        original_ids = request.form.getlist("original_shift_id")
        new_shift_date = request.form.get("new_shift_date") or shift_date

        # Rows the admin removed with the "x" button never get submitted as
        # shift_id/name/position, but their id is still in original_shift_id
        # (rendered once per row up front) -- the difference is what to delete.
        # Rows added with "+ Add colleague" submit an empty shift_id.
        kept_ids = {int(i) for i in shift_ids if i}
        removed_ids = {int(i) for i in original_ids} - kept_ids

        updated = 0
        added = 0
        for shift_id, name, position in zip(shift_ids, names, positions):
            if shift_id:
                if database.update_shift_entry(int(shift_id), name, position, new_shift_date):
                    updated += 1
            else:
                if database.add_shift_entry(source_image, new_shift_date, name, position):
                    added += 1

        removed = sum(database.delete_shift_entry(shift_id) for shift_id in removed_ids)

        parts = []
        if updated:
            parts.append(f"Updated {updated} entr{'y' if updated == 1 else 'ies'}.")
        if added:
            parts.append(f"Added {added}.")
        if removed:
            parts.append(f"Removed {removed}.")
        if new_shift_date != shift_date:
            parts.append(f"Date changed to {new_shift_date}.")
        flash(" ".join(parts) if parts else "No changes.", "success")
        return redirect(url_for("uploads_page"))

    entries = database.get_shifts_for_upload(source_image, shift_date)
    if not entries:
        abort(404)
    locations = list(location_rules.ROSTER_ORDER)
    worker_names = database.all_worker_names()
    return render_template(
        "edit_upload.html", entries=entries, locations=locations,
        source_image=source_image, shift_date=shift_date, worker_names=worker_names,
    )


@app.route("/uploads/image/<path:filename>")
def upload_image(filename):
    filepath = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in filepath.parents or not filepath.is_file():
        abort(404)
    return send_file(filepath)


# ---------------- Colleagues / Locations ----------------

@app.route("/colleagues")
def colleagues_page():
    stats = database.overview_stats()
    return render_template("colleagues.html", workers=stats["workers"])


@app.route("/colleagues/add", methods=["POST"])
@admin_required
def add_worker():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Please enter a name.", "error")
        return redirect(url_for("colleagues_page"))
    worker_id, conflict = database.create_worker(name)
    if worker_id is None:
        flash(
            f'A colleague named "{conflict}" already exists (or is very close to it) -- '
            'use that entry, or rename it first if this is genuinely a different person.',
            "error",
        )
        return redirect(url_for("colleagues_page"))
    flash(f'Added "{name}".', "success")
    return redirect(url_for("worker_page", worker_id=worker_id))


@app.route("/colleagues/compare")
def compare_colleagues():
    worker_ids = sorted(set(request.args.getlist("worker_id", type=int)))
    workers = database.workers_by_ids(worker_ids)
    if len(workers) < 2:
        flash("Pick at least 2 colleagues to compare.", "error")
        return redirect(url_for("colleagues_page"))
    shared = database.shared_wing_shifts([w["id"] for w in workers])
    return render_template("compare_colleagues.html", workers=workers, shared=shared)


@app.route("/locations")
def locations_page():
    stats = database.overview_stats()
    wing_ranking = database.wing_worker_ranking()
    return render_template(
        "locations.html", locations=stats["locations"], wing_ranking=wing_ranking,
    )


@app.route("/worker/<int:worker_id>")
def worker_page(worker_id):
    detail = database.worker_detail(worker_id)
    if not detail:
        abort(404)
    locations = database.all_locations()
    return render_template(
        "worker.html", detail=detail, locations=locations,
        worker_id_for_sidebar=worker_id, active_location_id=None,
    )


@app.route("/worker/<int:worker_id>/rename", methods=["POST"])
@admin_required
def rename_worker(worker_id):
    detail = database.worker_detail(worker_id)
    if not detail:
        abort(404)
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("Please enter a name.", "error")
        return redirect(url_for("worker_page", worker_id=worker_id))
    if new_name.lower() == detail["worker"]["name"].lower():
        return redirect(url_for("worker_page", worker_id=worker_id))
    result_id = database.rename_worker(worker_id, new_name)
    if result_id != worker_id:
        flash(f'Renamed to "{new_name}", merging with the existing colleague of that name.', "success")
    else:
        flash(f'Renamed to "{new_name}".', "success")
    return redirect(url_for("worker_page", worker_id=result_id))


@app.route("/worker/<int:worker_id>/delete", methods=["POST"])
@admin_required
def delete_worker(worker_id):
    detail = database.worker_detail(worker_id)
    if not detail:
        abort(404)
    database.delete_worker(worker_id)
    flash(f'Removed "{detail["worker"]["name"]}" and all {detail["total_shifts"]} of their shift(s).', "success")
    return redirect(url_for("colleagues_page"))


@app.route("/worker/<int:worker_id>/location/<int:location_id>")
def worker_location_page(worker_id, location_id):
    detail = database.worker_location_history(worker_id, location_id)
    if not detail:
        abort(404)
    locations = database.all_locations()
    return render_template(
        "worker_location.html", detail=detail, locations=locations,
        worker_id=worker_id, worker_id_for_sidebar=worker_id,
        active_location_id=location_id,
    )


@app.route("/location/<int:location_id>")
def location_page(location_id):
    detail = database.location_detail(location_id)
    if not detail:
        abort(404)
    return render_template("location.html", detail=detail)


# ---------------- Plannings ----------------

@app.route("/plannings")
def plannings_page():
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month

    # Navigating past Jan/Dec rolls into the next/previous year.
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1

    selected_day = request.args.get("day")
    if selected_day:
        try:
            date.fromisoformat(selected_day)
        except ValueError:
            selected_day = None

    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    dates_with_shifts = database.dates_with_shifts_in_month(year, month)
    day_entries = database.shifts_for_date(selected_day) if selected_day else None

    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)

    return render_template(
        "plannings.html",
        year=year, month=month, month_name=calendar.month_name[month],
        weeks=weeks, dates_with_shifts=dates_with_shifts,
        selected_day=selected_day, day_entries=day_entries,
        today_iso=today.isoformat(),
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
    )


# ---------------- Magritte Shifts ----------------

@app.route("/magritte-shifts")
@admin_required
def magritte_shifts_page():
    shift_date = request.args.get("date") or date.today().isoformat()
    try:
        date.fromisoformat(shift_date)
    except ValueError:
        shift_date = date.today().isoformat()

    coordinateur_slot = request.args.get("coordinateur_slot", 0, type=int)
    if coordinateur_slot not in (0, 1):
        coordinateur_slot = 0

    entries = database.magritte_shifts_for_date(shift_date)
    schedule = magritte_shifts.compute_break_schedule(entries, shift_date, coordinateur_slot)

    prev_day = (date.fromisoformat(shift_date) - timedelta(days=1)).isoformat()
    next_day = (date.fromisoformat(shift_date) + timedelta(days=1)).isoformat()

    return render_template(
        "magritte_shifts.html",
        shift_date=shift_date, prev_day=prev_day, next_day=next_day,
        coordinateur_slot=coordinateur_slot, schedule=schedule,
        security_types=database.SECURITY_TYPES,
    )


@app.route("/magritte-shifts/security-type/<int:worker_id>", methods=["POST"])
@admin_required
def set_magritte_security_type(worker_id):
    security_type = request.form.get("security_type") or None
    shift_date = request.form.get("shift_date") or date.today().isoformat()
    coordinateur_slot = request.form.get("coordinateur_slot", "0")
    if security_type and security_type not in database.SECURITY_TYPES:
        abort(400)
    database.set_worker_security_type(worker_id, security_type)
    return redirect(url_for("magritte_shifts_page", date=shift_date, coordinateur_slot=coordinateur_slot))


# ---------------- Notes ----------------

@app.route("/notes", methods=["GET", "POST"])
def notes_page():
    if request.method == "POST":
        if session.get("role") != "admin":
            flash("You need admin access to do that.", "error")
            return redirect(url_for("notes_page"))
        tag = request.form.get("tag", "").strip()
        note_date = request.form.get("note_date") or date.today().isoformat()
        comment = request.form.get("comment", "").strip()
        if not tag or not comment:
            flash("Please fill in both a category and a comment.", "error")
        else:
            database.add_note(tag, note_date, comment)
            flash("Note added.", "success")
        return redirect(url_for("notes_page"))

    grouped = database.notes_grouped_by_tag()
    tags = database.all_tags()
    return render_template(
        "notes.html", grouped=grouped, tags=tags, today=date.today().isoformat()
    )


@app.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_note(note_id):
    note = database.get_note(note_id)
    if not note:
        abort(404)

    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        note_date = request.form.get("note_date") or note["note_date"]
        comment = request.form.get("comment", "").strip()
        if not tag or not comment:
            flash("Please fill in both a category and a comment.", "error")
            return redirect(url_for("edit_note", note_id=note_id))
        database.update_note(note_id, tag, note_date, comment)
        flash("Note updated.", "success")
        return redirect(url_for("notes_page"))

    tags = database.all_tags()
    return render_template("edit_note.html", note=note, tags=tags)


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_note(note_id):
    database.delete_note(note_id)
    flash("Note deleted.", "success")
    return redirect(url_for("notes_page"))


# ---------------- Settings ----------------
# Theme, overview widget layout, and wing colors are personal preferences,
# stored in a cookie on your browser only -- they never affect what other
# visitors to this dashboard see. Data reset is shared, since it affects
# the underlying data everyone sees.

@app.route("/settings")
def settings_page():
    theme = request.cookies.get("theme", settings_defs.DEFAULT_THEME)
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    colors = effective_group_colors()
    return render_template(
        "settings.html", theme=theme, themes=settings_defs.THEMES,
        widgets=widgets, widget_labels=settings_defs.WIDGET_LABELS,
        colors=colors, default_colors=location_rules.GROUP_COLORS,
    )


@app.route("/settings/theme", methods=["POST"])
def set_theme():
    theme = request.form.get("theme")
    resp = redirect(url_for("settings_page"))
    if theme in settings_defs.THEMES:
        resp.set_cookie("theme", theme, max_age=COOKIE_MAX_AGE)
        flash("Theme updated (this browser only).", "success")
    return resp


@app.route("/settings/colors", methods=["POST"])
def set_colors():
    colors = {}
    for group in location_rules.GROUP_ORDER:
        val = request.form.get(f"color_{group}", "").strip()
        if val:
            colors[group] = val
    resp = redirect(url_for("settings_page"))
    resp.set_cookie("wing_colors", json.dumps(colors), max_age=COOKIE_MAX_AGE)
    flash("Chart colors updated (this browser only).", "success")
    return resp


@app.route("/settings/colors/reset", methods=["POST"])
def reset_colors():
    resp = redirect(url_for("settings_page"))
    resp.delete_cookie("wing_colors")
    flash("Chart colors reset to defaults (this browser only).", "success")
    return resp


@app.route("/settings/widget/<widget_id>/toggle", methods=["POST"])
def toggle_widget(widget_id):
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    for w in widgets:
        if w["id"] == widget_id:
            w["enabled"] = not w["enabled"]
    resp = redirect(url_for("settings_page"))
    resp.set_cookie("overview_widgets", json.dumps(widgets), max_age=COOKIE_MAX_AGE)
    return resp


@app.route("/settings/widget/<widget_id>/move/<direction>", methods=["POST"])
def move_widget(widget_id, direction):
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    idx = next((i for i, w in enumerate(widgets) if w["id"] == widget_id), None)
    if idx is not None:
        if direction == "up" and idx > 0:
            widgets[idx - 1], widgets[idx] = widgets[idx], widgets[idx - 1]
        elif direction == "down" and idx < len(widgets) - 1:
            widgets[idx + 1], widgets[idx] = widgets[idx], widgets[idx + 1]
    resp = redirect(url_for("settings_page"))
    resp.set_cookie("overview_widgets", json.dumps(widgets), max_age=COOKIE_MAX_AGE)
    return resp


@app.route("/settings/clear-data", methods=["POST"])
@admin_required
def clear_data():
    confirm = request.form.get("confirm", "")
    if confirm == "DELETE":
        database.clear_all_data()
        flash("All roster data cleared (this affects everyone).", "success")
    else:
        flash('Type DELETE exactly (in capitals) to confirm clearing all data.', "error")
    return redirect(url_for("settings_page"))


# ---------------- Export ----------------

@app.route("/export")
def export():
    buffer = excel_export.build_export()
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"museum_shifts_{date.today().isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    database.init_db()
    app.run(debug=True, port=5000)
