"""Computes the day's staggered lunch-break schedule for the Magritte wing.

Rules (confirmed against real roster photos in uploads/ + direct admin input):

- Every colleague has a fixed intern/extern security type: intern = 45 min
  lunch, extern = 30 min.
- The 5 "floor" posts (Magritte CT, Magritte Lift, Magritte +1/+2/+3) take
  their break one at a time, in a fixed order -- always starting from the
  top floor and working down to CT -- so only one of the five is ever
  uncovered. Each person's break starts the moment the previous one's ends,
  but never before their own type's base time.
- Magritte SAS (which already absorbs "0 MMM Controle" as the same
  canonical location) gets its own independent one-at-a-time slot, seeded at
  the same base time -- a Mobile/SAS colleague covers for whoever's out, so
  it doesn't have to wait behind the floor chain.
- Magritte Mobile eats at a fixed clock time regardless of type (only the
  duration varies) -- Mobile is the floater covering everyone else, so their
  own break time isn't staggered against anyone.
- Magritte Coordinateur is flexible between two admin-chosen preferred
  slots; the admin picks which one applies for the day.
- Weekend base times are 30 minutes later across the board than weekday
  (matches the observed weekend roster + the stated Mobile weekend time).
"""

from datetime import date, datetime, time, timedelta

DURATIONS = {"intern": 45, "extern": 30}

WEEKDAY_BASE = {
    "floor_extern": time(11, 30),
    "floor_intern": time(11, 45),
    "mobile": time(14, 0),
    "coordinateur_options": [time(12, 30), time(13, 15)],
}

WEEKEND_BASE = {
    "floor_extern": time(12, 0),
    "floor_intern": time(12, 15),
    "mobile": time(14, 30),
    "coordinateur_options": [time(13, 0), time(13, 45)],
}

FLOOR_CHAIN = ["Magritte +3", "Magritte +2", "Magritte +1", "Magritte Lift", "Magritte CT"]
SAS_LOCATION = "Magritte SAS"
MOBILE_LOCATION = "Magritte Mobile"
COORDINATEUR_LOCATION = "Magritte Coordinateur"

KNOWN_LOCATIONS = set(FLOOR_CHAIN) | {SAS_LOCATION, MOBILE_LOCATION, COORDINATEUR_LOCATION}


def _add_minutes(t, minutes):
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def _group_by_location(entries):
    by_location = {}
    for e in entries:
        by_location.setdefault(e["location"], []).append(e)
    for loc in by_location:
        by_location[loc].sort(key=lambda e: e["worker"].lower())
    return by_location


def compute_break_schedule(entries, shift_date_str, coordinateur_slot=0):
    """entries: list of dicts {worker, security_type, location} for one date's
    Magritte-wing roster (as returned by database.magritte_shifts_for_date).
    coordinateur_slot: 0 or 1, picks which of the two preferred Coordinateur
    times to use. Returns a dict with the computed schedule rows plus any
    entries that couldn't be scheduled (missing security type, or an
    unrecognized Magritte location)."""
    is_weekend = date.fromisoformat(shift_date_str).weekday() >= 5
    base = WEEKEND_BASE if is_weekend else WEEKDAY_BASE

    with_type = [e for e in entries if e.get("security_type") in DURATIONS]
    missing_type = [e for e in entries if e.get("security_type") not in DURATIONS]
    unrecognized = [e for e in with_type if e["location"] not in KNOWN_LOCATIONS]
    with_type = [e for e in with_type if e["location"] in KNOWN_LOCATIONS]

    by_location = _group_by_location(with_type)
    rows = []

    # Floor chain: one at a time, top floor down to CT.
    chain_end = None
    for location in FLOOR_CHAIN:
        for e in by_location.get(location, []):
            floor_time = base["floor_intern"] if e["security_type"] == "intern" else base["floor_extern"]
            start = floor_time if chain_end is None else max(floor_time, chain_end)
            end = _add_minutes(start, DURATIONS[e["security_type"]])
            rows.append({
                "location": location, "worker": e["worker"], "security_type": e["security_type"],
                "break_start": start, "break_end": end,
                "note": "One at a time with the other floor/lift posts",
            })
            chain_end = end

    # SAS: independent one-at-a-time slot, not chained to the floor group.
    sas_end = None
    for e in by_location.get(SAS_LOCATION, []):
        floor_time = base["floor_intern"] if e["security_type"] == "intern" else base["floor_extern"]
        start = floor_time if sas_end is None else max(floor_time, sas_end)
        end = _add_minutes(start, DURATIONS[e["security_type"]])
        rows.append({
            "location": SAS_LOCATION, "worker": e["worker"], "security_type": e["security_type"],
            "break_start": start, "break_end": end,
            "note": "Independent of the floor/lift chain",
        })
        sas_end = end

    # Mobile: fixed clock time regardless of type; only duration varies.
    mobile_end = None
    for e in by_location.get(MOBILE_LOCATION, []):
        start = base["mobile"] if mobile_end is None else mobile_end
        end = _add_minutes(start, DURATIONS[e["security_type"]])
        rows.append({
            "location": MOBILE_LOCATION, "worker": e["worker"], "security_type": e["security_type"],
            "break_start": start, "break_end": end,
            "note": "Fixed Mobile lunch time",
        })
        mobile_end = end

    # Coordinateur: flexible, defaults to the admin-picked preferred slot.
    coord_start = base["coordinateur_options"][coordinateur_slot]
    coord_end = None
    for e in by_location.get(COORDINATEUR_LOCATION, []):
        start = coord_start if coord_end is None else coord_end
        end = _add_minutes(start, DURATIONS[e["security_type"]])
        rows.append({
            "location": COORDINATEUR_LOCATION, "worker": e["worker"], "security_type": e["security_type"],
            "break_start": start, "break_end": end,
            "note": "Flexible slot (admin-selected)",
        })
        coord_end = end

    rows.sort(key=lambda r: r["break_start"])

    return {
        "is_weekend": is_weekend,
        "rows": rows,
        "missing_type": missing_type,
        "unrecognized": unrecognized,
        "coordinateur_options": base["coordinateur_options"],
    }
