import io
import os
import re
import colorsys
import json
import logging
from datetime import date
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

import location_rules

MODEL_NAME = "gemini-2.5-flash"

# Diagnostic log for every extraction attempt: what Gemini actually returned
# and how many entries were parsed out of it. Lets Sensei see *why* an upload
# produced a bad result instead of just that it did.
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("gemini_extract")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.FileHandler(LOG_DIR / "gemini_extraction.log", encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(_handler)


def _build_prompt():
    return """You are reading a photo of a museum staff roster sheet. It is either a
WEEKDAY sheet titled "PLANNING SALLE GARDIENNAGE SEMAINE - ZAALBEWAKING WEEK" with a main
grid titled "WEEK-SEMAINE", or a WEEKEND sheet titled "PLANNING SALLE GARDIENNAGE WEEK-END
- ZAALBEWAKING WEEKEND" with a main grid titled "WEEKEND". These two versions use DIFFERENT
time-slot column boundaries. Do not assume which one this is — read the grid title and
column headers directly off the sheet.

The sheet has FOUR distinct areas — pay close attention to only using the right one:

1. A header area with the date and a "responsable/verantwoordelijke" box listing
   supervisor names. IGNORE the supervisor names — they are never part of the grid.
   DO find the date printed in this header (often after "JEUDI-DONDERDAG :" or
   "DIMANCHE-ZONDAG :" or similar, in DD/MM/YYYY format, e.g. "02/07/2026" meaning
   2 July 2026).
2. A small separate mini-table near the top-left (rows starting with a time like
   "17.30 ..." or "18.30 ..."). IGNORE this mini-table — it is a separate schedule.
3. A small separate box near the top-right, next to (or below) the "responsable"
   box, NOT part of the main grid, with exactly two rows labeled "WIERTZ" and
   "MEUNIER" (each label printed outside/left of a bordered box), each paired with
   one person's name inside that box on the same line. Either row may be blank if
   uncovered that day, and it may not exist at all on some sheets. Read it into
   "outsiders" (see PART E) — never merge it into the main grid's shifts or into
   the responsable box.
4. The MAIN GRID, titled either "WEEK-SEMAINE" or "WEEKEND", which is what you must
   read for shifts. It has:
   - A left column labeled "bewakingspost" listing location/post names — every row here,
     including "Coordinateur-Coördinateur" and "Mobile/Mobiel", is a valid location.
   - A "pause/pauze" column — IGNORE this column entirely. It sometimes contains a
     time range too (e.g. a worker's break time) — that is NOT a time-slot column,
     it is a per-row break annotation. Do not count it among the time-slot columns.
   - After the "pause/pauze" column, a series of TIME-SLOT columns, each headed by a
     clock-time range such as "10:00-11:45" or "11:00-12:15". The exact boundaries
     differ between weekday and weekend sheets. Each cell holds a person's name,
     sometimes with a checkmark or asterisks, shaded with a background color
     (plain/grey, yellow, blue, green, orange, or red).

Your job has five parts:

PART A — Find the date. Read the date from the header area and convert it to ISO
format YYYY-MM-DD. If you cannot find or are not confident about the date, set it to null.

PART B — Identify the grid. Read the MAIN GRID's title exactly as printed — it will be
"WEEK-SEMAINE" or "WEEKEND". Then read every TIME-SLOT column header in that grid
(excluding "pause/pauze"), left to right, exactly as printed (e.g. "10:00-11:45").

PART C — Extract shifts from the FIRST time-slot column only — the leftmost column
whose header is a clock-time range, immediately after "pause/pauze". Ignore every
other time-slot column to its right, even if it has names in it. For each row
(location) in the main grid, in that one first column:

1. Include EVERY name in that column, in reading order top to bottom — every single
   row, without exception, no matter how many rows the grid has. Do not stop early and
   do not summarize or skip rows to save space — completeness across the WHOLE grid
   matters more than anything else in this part.
2. If the cell is empty, blank, hatched, or says "no need"/"-", skip that row — do not invent a name.
3. Strip out checkmark symbols (√, ✓) — they are not part of the name.
4. Strip out any asterisks (*, **, ***) next to a name — return just the bare name.
4a. Read every letter of the name individually, especially the last one --
    this roster has two DIFFERENT people named "GODART" and "GODARD" (last
    letter T vs D), which look almost identical in handwriting. Report
    exactly the last letter you see stroke-by-stroke; do not default to
    whichever spelling you've seen more often.
5. Read the location label from that row's left-most "bewakingspost" column, exactly as written.
   Follow the row's horizontal line from the name cell straight left to its label —
   rows are thin and close together, so do not let your eyes drift to the row above
   or below (e.g. "CT Moderne" and "CT AA/OK" are adjacent and easy to swap).
6. Report that SAME cell's background fill as one of exactly these words:
   "plain" (white/grey/no fill), "yellow", "blue", "green", "orange", "red", or "other".
   Read the actual fill of THIS cell, not the row's pause/pauze cell and not
   neighboring rows. Orange means a warm salmon/peach/coral/tan fill — do not
   report "orange" for a pale/light blue fill, they look different and must not
   be confused. Red means a distinct bright/deep red fill — do not confuse it
   with orange; they are different categories. If you are unsure between two
   colors, prefer describing what you literally see over what you expect a
   break-placeholder to look like.

7. Report "box": that name cell's bounding box, as [ymin, xmin, ymax, xmax] in
   the 0-1000 normalized coordinate system (0,0 is the image's top-left corner,
   1000,1000 its bottom-right). Frame just that one cell, border to border.

PART D — Report "column_bbox": the bounding box, in the same 0-1000
[ymin, xmin, ymax, xmax] format, that tightly frames ONLY that same first
time-slot column's cells across every row of the main grid — from just below
its header down to the bottom of the last row, and from that column's left
border to its right border. Do not include the "pause/pauze" column or the
next time-slot column in this box.

PART E — The small WIERTZ/MEUNIER box described in area 3 above. For each of
its two rows, top to bottom, report the location label exactly as printed and
the name next to it, with checkmarks and asterisks stripped off. Read each name
from the SAME line as its own label — do not swap the two. Leave out a row
whose name is blank. If the box doesn't exist on this sheet, use an empty array.

Return ONLY a single JSON object, nothing else. No markdown fences, no commentary.

Format exactly like this:
{
  "date": "YYYY-MM-DD" or null,
  "grid_title": "WEEK-SEMAINE" or "WEEKEND",
  "time_slot_columns": ["<column header 1>", "<column header 2>", ...],
  "extracted_time_slot": "<the exact header of the first time-slot column you extracted from>",
  "shifts": [
    {"name": "<person's name>", "position": "<location/row label>", "background": "plain|yellow|blue|green|orange|red|other", "box": [<ymin>, <xmin>, <ymax>, <xmax>]}
  ],
  "column_bbox": [<ymin>, <xmin>, <ymax>, <xmax>],
  "outsiders": [
    {"location": "WIERTZ", "name": "<person's name>"}
  ]
}
"""


# Background colors are read from the photo's pixels locally rather than by a
# second Gemini call: the fills are flat printed colors that separate cleanly
# by hue once the photo's color cast is removed. Measured on a real roster
# photo (after white-balancing against the plain cells): plain ~0 saturation,
# orange hue ~25, yellow ~54, green ~100, light blue ~204 (saturation ~0.17).
PLAIN_MAX_SATURATION = 0.10
HUE_BANDS = [            # (upper hue bound in degrees, color), checked in order
    (12, "red"),
    (40, "orange"),
    (75, "yellow"),
    (170, "green"),
    (260, "blue"),
    (330, "other"),
    (360, "red"),
]
INK_MAX_BRIGHTNESS = 110  # darker pixels are handwriting/print, not the fill


def _box_to_pixels(box, width, height):
    """[ymin, xmin, ymax, xmax] on the 0-1000 scale -> (left, top, right,
    bottom) pixel coordinates, or None if the box is malformed/degenerate."""
    if not (isinstance(box, (list, tuple)) and len(box) == 4):
        return None
    try:
        ymin, xmin, ymax, xmax = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    left, right = sorted((xmin / 1000.0 * width, xmax / 1000.0 * width))
    top, bottom = sorted((ymin / 1000.0 * height, ymax / 1000.0 * height))
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right - left < 2 or bottom - top < 2:
        return None
    return left, top, right, bottom


def _median_fill(img, rect):
    """Per-channel median of the non-ink pixels in `rect`, or None if the
    region is (nearly) all ink."""
    region = img.crop(tuple(int(v) for v in rect))
    pixels = [p for p in region.getdata() if max(p) > INK_MAX_BRIGHTNESS]
    if len(pixels) < 8:
        return None
    return [sorted(channel)[len(channel) // 2] for channel in zip(*pixels)]


def _classify_fill(rgb, paper):
    """Name the fill color of `rgb`, after white-balancing against `paper`
    (the color a plain cell has under this particular photo's lighting)."""
    balanced = [min(1.0, c / max(p, 1) * 0.92) for c, p in zip(rgb, paper)]
    hue, saturation, _ = colorsys.rgb_to_hsv(*balanced)
    if saturation < PLAIN_MAX_SATURATION:
        return "plain"
    hue *= 360
    for upper, color in HUE_BANDS:
        if hue < upper:
            return color
    return "other"


def _read_cell_colors(image_bytes, column_bbox, cell_boxes, image_name):
    """Classify each name cell's background straight from the photo's pixels.
    Each cell is sampled through its middle band only (inner 40% of its
    height), so a slightly-off box still lands inside the right row. The
    column's x-range comes from column_bbox when available, since it's one
    larger box and so more reliably placed than each tiny cell box.
    Returns a list aligned with `cell_boxes` holding a color name, or None for
    any cell that couldn't be read (callers fall back to Gemini's own guess)."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        logger.warning("COLOR_READ_SKIPPED image=%s reason=open_failed error=%s", image_name, e)
        return [None] * len(cell_boxes)
    width, height = img.size

    column = _box_to_pixels(column_bbox, width, height)
    cells = [_box_to_pixels(b, width, height) for b in cell_boxes]

    # Most first-column cells are plain, so the column's overall median is a
    # good estimate of "plain paper" under this photo's lighting.
    paper = _median_fill(img, column) if column else None
    if paper is None:
        known = [c for c in cells if c]
        if known:
            paper = _median_fill(img, (
                min(c[0] for c in known), min(c[1] for c in known),
                max(c[2] for c in known), max(c[3] for c in known),
            ))
    if paper is None:
        logger.warning("COLOR_READ_SKIPPED image=%s reason=no_reference", image_name)
        return [None] * len(cell_boxes)

    colors = []
    for cell in cells:
        if not cell:
            colors.append(None)
            continue
        left, top, right, bottom = cell
        if column:
            left, right = column[0], column[2]
        dx, dy = (right - left) * 0.15, (bottom - top) * 0.30
        fill = _median_fill(img, (left + dx, top + dy, right - dx, bottom - dy))
        colors.append(_classify_fill(fill, paper) if fill else None)
    return colors


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


def _clean_optional_str(value):
    """Gemini sometimes omits a field or returns an empty string -- normalize
    both to None so callers get a consistent "not provided" signal."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def extract_roster(image_path):
    """Send the roster image to Gemini. Always extracts from the FIRST
    time-slot column present on the sheet (10:00-11:45 on weekday sheets,
    11:00-12:15 on weekend sheets, etc.) rather than a fixed target, since
    the two sheet layouts use different column boundaries. Returns a dict:
    {"date", "entries", "grid_title", "time_slot_columns", "extracted_time_slot"}.
    Names have stars stripped, positions are normalized to canonical labels,
    and duplicate people (same name, multiple locations) are collapsed to the
    first entry.
    """
    image_name = os.path.basename(image_path)
    client = _get_client()

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"

    data = None
    attempts = 2
    for attempt in range(1, attempts + 1):
        logger.info("REQUEST image=%s attempt=%d", image_name, attempt)

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                _build_prompt(),
            ],
            # JSON mode makes an unparseable reply (and the retry it costs)
            # much rarer. The limit is higher than before since each shift now
            # carries a bounding box, and on gemini-2.5 thinking tokens count
            # against it too.
            config=types.GenerateContentConfig(
                max_output_tokens=16384,
                response_mime_type="application/json",
            ),
        )

        raw_text = response.text or ""
        finish_reason = None
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason
        logger.info(
            "RESPONSE image=%s attempt=%d finish_reason=%s raw_text=%r",
            image_name, attempt, finish_reason, raw_text,
        )

        text = raw_text.strip()
        text = re.sub(r"^```(json)?", "", text.strip())
        text = re.sub(r"```$", "", text.strip()).strip()

        try:
            data = json.loads(text)
            break
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                if not match:
                    raise json.JSONDecodeError("no JSON object found", text, 0)
                data = json.loads(match.group(0))
                break
            except json.JSONDecodeError:
                logger.warning(
                    "PARSE_FAILED image=%s attempt=%d finish_reason=%s could not parse JSON from response",
                    image_name, attempt, finish_reason,
                )
                if attempt == attempts:
                    raise ValueError(
                        "Gemini's response wasn't valid data. Please try uploading again."
                    ) from None
                continue

    raw_shifts = data.get("shifts", []) if isinstance(data, dict) else []
    detected_date = _parse_iso_date(data.get("date") if isinstance(data, dict) else None)
    grid_title = _clean_optional_str(data.get("grid_title") if isinstance(data, dict) else None)
    extracted_time_slot = _clean_optional_str(
        data.get("extracted_time_slot") if isinstance(data, dict) else None
    )

    raw_columns = data.get("time_slot_columns", []) if isinstance(data, dict) else []
    if isinstance(raw_columns, list):
        time_slot_columns = [str(c).strip() for c in raw_columns if str(c).strip()]
    else:
        time_slot_columns = []

    rows = []
    for item in raw_shifts:
        if not isinstance(item, dict):
            continue
        name = _strip_stars(str(item.get("name", "")))
        if not name:
            continue
        rows.append({
            "name": name,
            "position_raw": str(item.get("position", "")).strip(),
            "background": str(item.get("background", "")).strip().lower(),
            "box": item.get("box"),
        })

    # Gemini's one-glance color call confuses orange/blue/yellow, so each
    # cell's background is re-read from the photo's own pixels, locally -- no
    # extra Gemini request. Gemini's guess is only kept for a cell whose
    # pixels couldn't be read (e.g. a missing or garbled box).
    column_bbox = data.get("column_bbox") if isinstance(data, dict) else None
    local_colors = _read_cell_colors(image_bytes, column_bbox, [r["box"] for r in rows], image_name)
    colors_read = 0
    colors_changed = 0
    for row, color in zip(rows, local_colors):
        if color is None:
            continue
        colors_read += 1
        if color != row["background"]:
            colors_changed += 1
            logger.info(
                "COLOR_OVERRIDE image=%s name=%s gemini=%s pixels=%s",
                image_name, row["name"], row["background"], color,
            )
        row["background"] = color

    # Orange/red mean "not actually working this slot" on the physical roster,
    # but a color read can still be wrong -- so rather than silently dropping
    # those rows here where a misread becomes an invisible, uncorrectable data
    # loss, they're kept and marked. The Review screen shows the flagged color
    # and pre-unchecks the row, so a human confirms or overrides it.
    cleaned = []
    skipped_marked = 0
    for row in rows:
        flagged = row["background"] in ("orange", "red")
        if flagged:
            skipped_marked += 1
        position = location_rules.normalize_location_label(row["position_raw"])
        cleaned.append({
            "name": row["name"],
            "position": position or "Unknown",
            "raw_position": row["position_raw"],
            "background": row["background"],
            "flagged_skip": flagged,
        })

    # The WIERTZ/MEUNIER box sits outside the main grid; it's read in the same
    # request, into its own "outsiders" list.
    outsider_shifts = data.get("outsiders") if isinstance(data, dict) else None

    outsider_count = 0
    if isinstance(outsider_shifts, list):
        for item in outsider_shifts:
            if not isinstance(item, dict):
                continue
            name = _strip_stars(str(item.get("name", "")))
            raw_position = str(item.get("location", "")).strip()
            if not name or not raw_position:
                continue
            position = location_rules.normalize_location_label(raw_position)
            cleaned.append({
                "name": name,
                "position": position or "Unknown",
                "raw_position": raw_position,
                "background": "plain",
                "flagged_skip": False,
            })
            outsider_count += 1

    entries = _dedupe_keep_first(cleaned)

    logger.info(
        "PARSED image=%s detected_date=%s grid_title=%s time_slot_columns=%s "
        "extracted_time_slot=%s entry_count=%d skipped_marked=%d colors_read=%d/%d "
        "colors_changed=%d outsider_count=%d",
        image_name, detected_date, grid_title, time_slot_columns,
        extracted_time_slot, len(entries), skipped_marked, colors_read, len(rows),
        colors_changed, outsider_count,
    )

    return {
        "date": detected_date,
        "entries": entries,
        "grid_title": grid_title,
        "time_slot_columns": time_slot_columns,
        "extracted_time_slot": extracted_time_slot,
    }
