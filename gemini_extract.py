import io
import os
import re
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
   - A "pause/pauze" column — IGNORE this column entirely. It sometimes contains a
     time range too (e.g. a worker's break time) — that is NOT a time-slot column,
     it is a per-row break annotation. Do not count it among the time-slot columns.
   - After the "pause/pauze" column, a series of TIME-SLOT columns, each headed by a
     clock-time range such as "10:00-11:45" or "11:00-12:15". The exact boundaries
     differ between weekday and weekend sheets. Each cell holds a person's name,
     sometimes with a checkmark or asterisks, shaded with a background color
     (plain/grey, yellow, blue, green, orange, or red).

Your job has three parts:

PART A — Find the date. Read the date from the header area and convert it to ISO
format YYYY-MM-DD. If you cannot find or are not confident about the date, set it to null.

PART B — Identify the grid. Read the MAIN GRID's title exactly as printed — it will be
"WEEK-SEMAINE" or "WEEKEND". Then read every TIME-SLOT column header in that grid
(excluding "pause/pauze"), left to right, exactly as printed (e.g. "10:00-11:45").

PART C — Extract shifts from the FIRST time-slot column only — the leftmost column
whose header is a clock-time range, immediately after "pause/pauze". Ignore every
other time-slot column to its right, even if it has names in it. For each row
(location) in the main grid, in that one first column, in a single glance at that
one cell (do not defer color-reading to a later pass):

1. Include EVERY name in that column, in reading order top to bottom — every single
   row, without exception, no matter how many rows the grid has. Do not stop early and
   do not summarize or skip rows to save space — completeness across the WHOLE grid
   matters more than anything else in this part.
2. If the cell is empty, blank, hatched, or says "no need"/"-", skip that row — do not invent a name.
3. Strip out checkmark symbols (√, ✓) — they are not part of the name.
4. Strip out any asterisks (*, **, ***) next to a name — return just the bare name.
5. Read the location label from that row's left-most "bewakingspost" column, exactly as written.
6. Report that SAME cell's background fill as one of exactly these words:
   "plain" (white/grey/no fill), "yellow", "blue", "green", "orange", "red", or "other".
   Read the actual fill of THIS cell, not the row's pause/pauze cell and not
   neighboring rows. Orange means a warm salmon/peach/coral/tan fill — do not
   report "orange" for a pale/light blue fill, they look different and must not
   be confused. Red means a distinct bright/deep red fill — do not confuse it
   with orange; they are different categories. If you are unsure between two
   colors, prefer describing what you literally see over what you expect a
   break-placeholder to look like.

PART D — Report column_bbox: a single bounding box, as [ymin, xmin, ymax, xmax]
in the 0-1000 normalized coordinate system (0,0 is the image's top-left corner,
1000,1000 is its bottom-right corner), that tightly frames ONLY that same first
time-slot column's cells across every row of the main grid — from just below
its header down to the bottom of the last row, and from that column's left
border to its right border. Do not include the "pause/pauze" column or the
next time-slot column in this box.

Return ONLY a single JSON object, nothing else. No markdown fences, no commentary.

Format exactly like this:
{
  "date": "YYYY-MM-DD" or null,
  "grid_title": "WEEK-SEMAINE" or "WEEKEND",
  "time_slot_columns": ["<column header 1>", "<column header 2>", ...],
  "extracted_time_slot": "<the exact header of the first time-slot column you extracted from>",
  "shifts": [
    {"name": "<person's name>", "position": "<location/row label>", "background": "plain|yellow|blue|green|orange|red|other"}
  ],
  "column_bbox": [<ymin>, <xmin>, <ymax>, <xmax>]
}
"""


def _build_color_check_prompt(ordered_labels):
    numbered = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(ordered_labels))
    return f"""This image is a cropped, zoomed-in view of ONLY the first time-slot
column from a museum staff roster — one cell per row, top to bottom, nothing
else in it. It contains exactly {len(ordered_labels)} cells, top to bottom,
corresponding in order to these entries already read from the full sheet:
{numbered}

For each entry, in this exact top-to-bottom order, report that cell's
background fill as one of exactly these words: "plain" (white/grey/no fill),
"yellow", "blue", "green", "orange", "red", or "other". Orange means a warm
salmon/peach/coral/tan fill — a pale or light blue fill must be reported as
"blue", never "orange", they look different and must not be confused. Red
means a distinct bright/deep red fill, different from orange. Look at
each cell's actual fill directly; do not guess from what you'd expect a
break-placeholder to look like.

Return ONLY a JSON array of exactly {len(ordered_labels)} strings, in the same
order as the list above, nothing else. No markdown fences, no commentary.
Example: ["plain", "orange", "red"]
"""


def _crop_and_upscale_column(image_bytes, bbox_norm, pad_frac=0.01, max_dim=2400):
    """Crop the given normalized [ymin, xmin, ymax, xmax] (0-1000 scale) region
    out of the full-resolution image and upscale it, so a follow-up color-only
    Gemini call gets a much closer, higher-resolution look at just that one
    column instead of trying to judge color from a single full-page glance."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size
    ymin, xmin, ymax, xmax = bbox_norm

    left = max(0, (xmin / 1000.0) * width - width * pad_frac)
    right = min(width, (xmax / 1000.0) * width + width * pad_frac)
    top = max(0, (ymin / 1000.0) * height - height * pad_frac)
    bottom = min(height, (ymax / 1000.0) * height + height * pad_frac)

    if right - left < 2 or bottom - top < 2:
        raise ValueError(f"degenerate crop bbox {bbox_norm} on image {width}x{height}")

    crop = img.crop((int(left), int(top), int(right), int(bottom)))

    scale = min(4.0, max(1.0, max_dim / max(crop.width, crop.height)))
    if scale > 1.0:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _recheck_colors(client, image_bytes, column_bbox, rows, image_name):
    """Ask Gemini a second, dedicated question about background colors on a
    cropped-and-upscaled image of just the first time-slot column, since a
    single full-page glance is unreliable at telling orange/blue/yellow apart.
    Returns a list of background strings aligned with `rows`, or None if the
    recheck couldn't be performed — callers should then fall back to each
    row's own first-pass background guess."""
    if not rows:
        return None
    if not (isinstance(column_bbox, list) and len(column_bbox) == 4):
        logger.warning("COLOR_RECHECK_SKIPPED image=%s reason=no_bbox", image_name)
        return None

    try:
        bbox = [float(v) for v in column_bbox]
        crop_bytes = _crop_and_upscale_column(image_bytes, bbox)
    except Exception as e:
        logger.warning("COLOR_RECHECK_SKIPPED image=%s reason=crop_failed error=%s", image_name, e)
        return None

    labels = [f'{r["name"]} ({r["position_raw"] or "?"})' for r in rows]
    prompt = _build_color_check_prompt(labels)

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[types.Part.from_bytes(data=crop_bytes, mime_type="image/png"), prompt],
            config=types.GenerateContentConfig(max_output_tokens=2048),
        )
    except Exception as e:
        logger.warning("COLOR_RECHECK_FAILED image=%s error=%s", image_name, e)
        return None

    raw_text = response.text or ""
    logger.info("COLOR_RECHECK_RESPONSE image=%s raw_text=%r", image_name, raw_text)

    text = raw_text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        colors = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            logger.warning("COLOR_RECHECK_PARSE_FAILED image=%s", image_name)
            return None
        try:
            colors = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("COLOR_RECHECK_PARSE_FAILED image=%s", image_name)
            return None

    if not isinstance(colors, list) or len(colors) != len(rows):
        logger.warning(
            "COLOR_RECHECK_LENGTH_MISMATCH image=%s expected=%d got=%s",
            image_name, len(rows), len(colors) if isinstance(colors, list) else type(colors).__name__,
        )
        return None

    return [str(c).strip().lower() for c in colors]


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
            config=types.GenerateContentConfig(max_output_tokens=8192),
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
        name = _strip_stars(str(item.get("name", "")))
        if not name:
            continue
        rows.append({
            "name": name,
            "position_raw": str(item.get("position", "")).strip(),
            "background": str(item.get("background", "")).strip().lower(),
        })

    # A single full-page glance is not reliable enough to trust for silently
    # dropping people (it confuses orange/blue/yellow) — re-check colors on a
    # cropped, upscaled image of just the first time-slot column.
    column_bbox = data.get("column_bbox") if isinstance(data, dict) else None
    refined = _recheck_colors(client, image_bytes, column_bbox, rows, image_name)
    if refined is not None:
        for row, bg in zip(rows, refined):
            row["background"] = bg

    # Orange/red mean "not actually working this slot" on the physical roster,
    # but a single-glance color read is exactly the kind of judgment call that
    # gets confused (orange vs blue, etc.) -- so rather than silently dropping
    # those rows here where a misread becomes an invisible, uncorrectable data
    # loss, they're kept and marked. The Review screen shows the flagged color
    # and pre-unchecks the row, so a human confirms or overrides it instead of
    # trusting a single AI color call no one ever sees.
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

    entries = _dedupe_keep_first(cleaned)

    logger.info(
        "PARSED image=%s detected_date=%s grid_title=%s time_slot_columns=%s "
        "extracted_time_slot=%s entry_count=%d skipped_marked=%d color_recheck=%s",
        image_name, detected_date, grid_title, time_slot_columns,
        extracted_time_slot, len(entries), skipped_marked, refined is not None,
    )

    return {
        "date": detected_date,
        "entries": entries,
        "grid_title": grid_title,
        "time_slot_columns": time_slot_columns,
        "extracted_time_slot": extracted_time_slot,
    }
