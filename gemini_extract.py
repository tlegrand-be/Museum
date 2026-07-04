import os
import re
import json
import logging
from datetime import date
from pathlib import Path

from google import genai
from google.genai import types

import location_rules

MODEL_NAME = "gemini-2.5-flash"

# The one time-slot column this dashboard always tracks. Not user-configurable in the UI.
DEFAULT_TIME_SLOT = "10:00-11:45"

# Diagnostic log for every extraction attempt: what was requested, what Gemini
# actually returned, and how many entries were parsed out of it. Lets Sensei
# see *why* an upload produced a bad result instead of just that it did.
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("gemini_extract")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.FileHandler(LOG_DIR / "gemini_extraction.log", encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(_handler)


def _build_prompt(time_slot):
    return f"""You are reading a photo of a museum staff roster sheet. It is either a
WEEKDAY sheet titled "PLANNING SALLE GARDIENNAGE SEMAINE - ZAALBEWAKING WEEK" with a main
grid titled "WEEK-SEMAINE", or a WEEKEND sheet titled "PLANNING SALLE GARDIENNAGE WEEK-END
- ZAALBEWAKING WEEKEND" with a main grid titled "WEEKEND". These two versions use DIFFERENT
time-slot column boundaries. Do not assume which one this is — read the grid title and
column headers directly off the sheet.

The sheet has THREE distinct areas — pay close attention to only using the right one:

1. A header area with the date and a "responsable/verantwoordelijke" box listing
   supervisor names. IGNORE the supervisor names — they are never part of the grid.
   DO find the date printed in this header (often after "JEUDI-DONDERDAG :" or
   "DIMANCHE-ZONDAG :" or similar, in DD/MM/YYYY format, e.g. "02/07/2026" meaning
   2 July 2026).
2. A small separate mini-table near the top-left (rows starting with a time like
   "17.30 ..." or "18.30 ..."). IGNORE this mini-table — it is a separate schedule.
3. The MAIN GRID, titled either "WEEK-SEMAINE" or "WEEKEND", which is what you must
   read for shifts. It has:
   - A left column labeled "bewakingspost" listing location/post names — every row here,
     including "Coordinateur-Coördinateur" and "Mobile/Mobiel", is a valid location.
   - A "pause/pauze" column — IGNORE this column entirely.
   - Time-slot columns as headers, such as "10:00-11:45", "11:00-12:15", "11:45-12:30",
     etc. — the exact boundaries differ between weekday and weekend sheets. Each cell
     holds a person's name, sometimes with a checkmark or asterisks, shaded with a
     background color (plain/grey, yellow, blue, green, or orange).

Your job has three parts:

PART A — Find the date. Read the date from the header area and convert it to ISO
format YYYY-MM-DD. If you cannot find or are not confident about the date, set it to null.

PART B — Identify the grid. Read the MAIN GRID's title exactly as printed — it will be
"WEEK-SEMAINE" or "WEEKEND". Then read every time-slot column header in that grid, left
to right, exactly as printed (e.g. "10:00-11:45").

PART C — Extract shifts. Extract ONLY the column in the MAIN GRID whose header matches
the time slot "{time_slot}" exactly. Ignore every other time-slot column, even if it has
names in it. If no column header matches "{time_slot}" exactly, return "shifts": [] —
do not substitute a different column or guess. For each row (location) in the main grid,
in that one matching column:

1. SKIP any name whose cell background is orange/salmon-colored — orange marks a
   break-time overlap placeholder, not a real assignment.
2. Include names with any other background (plain/white, grey, yellow, blue, or green).
3. If the cell is empty, blank, hatched, or says "no need"/"-", skip that row — do not invent a name.
4. Strip out checkmark symbols (√, ✓) — they are not part of the name.
5. Strip out any asterisks (*, **, ***) next to a name — return just the bare name.
6. Read the location label from that row's left-most "bewakingspost" column, exactly as written.

Return ONLY a single JSON object, nothing else. No markdown fences, no commentary.

Format exactly like this:
{{
  "date": "YYYY-MM-DD" or null,
  "grid_title": "WEEK-SEMAINE" or "WEEKEND",
  "time_slot_columns": ["<column header 1>", "<column header 2>", ...],
  "shifts": [
    {{"name": "<person's name>", "position": "<location/row label>"}}
  ]
}}
"""


def _get_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get a free key at https://aistudio.google.com/apikey and set it before running the app."
        )
    return genai.Client(api_key=api_key)


def _strip_stars(name):
    return re.sub(r"\*+", "", name).strip()


def _dedupe_keep_first(entries):
    """Keep only the first occurrence of each name (case-insensitive), in the
    order Gemini returned them (which follows the roster's row order)."""
    seen = set()
    deduped = []
    for entry in entries:
        key = entry["name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _parse_iso_date(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        # Validate it's a real ISO date
        y, m, d = raw.split("-")
        date(int(y), int(m), int(d))
        return raw
    except (ValueError, AttributeError):
        return None


def extract_roster(image_path, time_slot=None):
    """Send the roster image to Gemini. Returns a dict:
    {"date": "YYYY-MM-DD" or None, "entries": [{"name", "position"}, ...]}
    Names have stars stripped, positions are normalized to canonical labels,
    and duplicate people (same name, multiple locations) are collapsed to the
    first entry.
    """
    time_slot = (time_slot or DEFAULT_TIME_SLOT).strip()
    image_name = os.path.basename(image_path)
    client = _get_client()

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"

    logger.info("REQUEST image=%s requested_time_slot=%s", image_name, time_slot)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            _build_prompt(time_slot),
        ],
    )

    raw_text = response.text or ""
    logger.info("RESPONSE image=%s raw_text=%r", image_name, raw_text)

    text = raw_text.strip()
    text = re.sub(r"^```(json)?", "", text.strip())
    text = re.sub(r"```$", "", text.strip()).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            logger.warning("PARSE_FAILED image=%s could not extract JSON from response", image_name)
            raise ValueError(f"Could not parse Gemini response as JSON:\n{text}")

    raw_shifts = data.get("shifts", []) if isinstance(data, dict) else []
    detected_date = _parse_iso_date(data.get("date") if isinstance(data, dict) else None)

    grid_title = data.get("grid_title") if isinstance(data, dict) else None
    if not isinstance(grid_title, str) or not grid_title.strip():
        grid_title = None
    else:
        grid_title = grid_title.strip()

    raw_columns = data.get("time_slot_columns", []) if isinstance(data, dict) else []
    if isinstance(raw_columns, list):
        time_slot_columns = [str(c).strip() for c in raw_columns if str(c).strip()]
    else:
        time_slot_columns = []

    cleaned = []
    for item in raw_shifts:
        name = _strip_stars(str(item.get("name", "")))
        position_raw = str(item.get("position", "")).strip()
        position = location_rules.normalize_location_label(position_raw)
        if name:
            cleaned.append({"name": name, "position": position or "Unknown"})

    entries = _dedupe_keep_first(cleaned)

    logger.info(
        "PARSED image=%s detected_date=%s grid_title=%s time_slot_columns=%s entry_count=%d",
        image_name, detected_date, grid_title, time_slot_columns, len(entries),
    )

    return {
        "date": detected_date,
        "entries": entries,
        "grid_title": grid_title,
        "time_slot_columns": time_slot_columns,
    }
