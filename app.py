import os
import uuid
import json
from datetime import date, timedelta
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort, session
)
from werkzeug.utils import secure_filename

import database
import gemini_extract
import excel_export
import location_rules
import settings_defs

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
PENDING_DIR = BASE_DIR / "instance" / "pending"
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years

# Shared site passcode. Override with the SITE_PASSCODE environment variable
# if you want to change it without editing code.
SITE_PASSCODE = os.environ.get("SITE_PASSCODE", "4444")

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
    if not (BASE_DIR / "instance" / "museum.db").exists():
        database.init_db()


@app.before_request
def require_passcode():
    if request.endpoint in ("passcode_page", "static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("passcode_page"))


@app.context_processor
def inject_globals():
    return {
        "current_theme": request.cookies.get("theme", settings_defs.DEFAULT_THEME),
        "group_colors": effective_group_colors(),
    }


# ---------------- Passcode gate ----------------

@app.route("/passcode", methods=["GET", "POST"])
def passcode_page():
    if request.method == "POST":
        code = request.form.get("passcode", "")
        if code == SITE_PASSCODE:
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("index"))
        flash("Incorrect passcode.", "error")
    return render_template("passcode.html")


@app.route("/lock")
def lock():
    session.pop("authenticated", None)
    return redirect(url_for("passcode_page"))


# ---------------- Overview ----------------

@app.route("/")
def index():
    stats = database.overview_stats()
    group_chart = database.worker_group_breakdown()
    last_update = database.last_update()
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    return render_template(
        "index.html", stats=stats, group_chart=group_chart,
        last_update=last_update, widgets=widgets,
    )


# ---------------- Upload / Review ----------------

@app.route("/upload", methods=["GET", "POST"])
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

    time_slot = database.get_setting("time_slot", gemini_extract.DEFAULT_TIME_SLOT)

    try:
        result = gemini_extract.extract_roster(str(filepath), time_slot=time_slot)
    except Exception as e:
        flash(f"Could not read the image: {e}", "error")
        return redirect(url_for("upload"))

    entries = result.get("entries", [])
    detected_date = result.get("date") or date.today().isoformat()

    if not entries:
        flash("No names/positions were detected in that image. Try a clearer photo.", "error")
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
def review(token):
    pending_path = PENDING_DIR / f"{token}.json"
    if not pending_path.exists():
        flash("That review session expired. Please upload again.", "error")
        return redirect(url_for("upload"))

    data = json.loads(pending_path.read_text())

    if request.method == "POST":
        names = request.form.getlist("name")
        positions = request.form.getlist("position")
        keep = request.form.getlist("keep")

        entries = []
        for i in range(len(names)):
            if str(i) in keep:
                entries.append({"name": names[i], "position": positions[i]})

        shift_date = request.form.get("shift_date") or data["shift_date"]
        saved, skipped = database.save_shifts(entries, shift_date, data.get("source_image"))
        pending_path.unlink(missing_ok=True)

        msg = f"Saved {saved} shift{'s' if saved != 1 else ''}."
        if skipped:
            msg += f" ({skipped} already existed for that date and were skipped.)"
        flash(msg, "success")
        return redirect(url_for("index"))

    return render_template("review.html", token=token, data=data)


# ---------------- Colleagues / Locations ----------------

@app.route("/colleagues")
def colleagues_page():
    stats = database.overview_stats()
    return render_template("colleagues.html", workers=stats["workers"])


@app.route("/locations")
def locations_page():
    stats = database.overview_stats()
    return render_template("locations.html", locations=stats["locations"])


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


# ---------------- Notes ----------------

@app.route("/notes", methods=["GET", "POST"])
def notes_page():
    if request.method == "POST":
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
def delete_note(note_id):
    database.delete_note(note_id)
    flash("Note deleted.", "success")
    return redirect(url_for("notes_page"))


# ---------------- More statistics ----------------

@app.route("/statistics")
def statistics_page():
    return render_template(
        "statistics.html",
        group_stats=database.shifts_by_group(),
        top_locations=database.top_locations(),
        leaderboard=database.worker_leaderboard(),
        over_time=database.shifts_over_time(),
        weekday=database.shifts_by_weekday(),
    )


# ---------------- Settings ----------------
# Theme, overview widget layout, and wing colors are personal preferences,
# stored in a cookie on your browser only -- they never affect what other
# visitors to this dashboard see. Time slot and data resets are shared,
# since they affect the underlying data everyone sees.

@app.route("/settings")
def settings_page():
    theme = request.cookies.get("theme", settings_defs.DEFAULT_THEME)
    widgets = get_cookie_json("overview_widgets", settings_defs.DEFAULT_OVERVIEW_WIDGETS)
    time_slot = database.get_setting("time_slot", gemini_extract.DEFAULT_TIME_SLOT)
    colors = effective_group_colors()
    return render_template(
        "settings.html", theme=theme, themes=settings_defs.THEMES,
        widgets=widgets, widget_labels=settings_defs.WIDGET_LABELS,
        time_slot=time_slot, colors=colors, default_colors=location_rules.GROUP_COLORS,
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


@app.route("/settings/time-slot", methods=["POST"])
def set_time_slot():
    ts = request.form.get("time_slot", "").strip()
    if ts:
        database.set_setting("time_slot", ts)
        flash("Default time slot updated (applies to everyone).", "success")
    return redirect(url_for("settings_page"))


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
