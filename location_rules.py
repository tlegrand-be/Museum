import re

# Ordered rename rules: (substring to look for in the raw OCR'd label, canonical replacement).
# Checked in order, case-insensitive. First match wins and REPLACES the whole label.
RENAME_RULES = [
    # --- FORUM wing ---
    ("vestiaire", "Vestiaire"),
    ("regentschapstraat", "Entrée principale"),
    ("onthaal groepen", "Acceuil groupes"),
    ("accueil groupes", "Acceuil groupes"),
    ("acceuil groupes", "Acceuil groupes"),
    ("ct/tc aa", "CT Ancien"),
    ("ct tc aa", "CT Ancien"),
    ("tc aa", "CT Ancien"),          # tolerate OCR dropping the leading "CT/"
    ("ct/tc moderne", "CT Moderne"),
    ("ct tc moderne", "CT Moderne"),
    ("tc moderne", "CT Moderne"),    # tolerate OCR dropping the leading "CT/"
    ("gresham", "Gresham"),
    ("argenteau", "Argenteau"),
    ("agora", "Agora -2"),
    ("forum", "Forum"),

    # --- BALAT wing --- (check specific numbered rows before generic "ingang balat")
    ("balat 1-2", "Balat 1-2"),
    ("balat 3-4", "Balat 3-4"),
    ("rubens", "Salle 52"),
    ("salle 52", "Salle 52"),
    ("zaal 52", "Salle 52"),
    ("balat 7-8-9", "Salle 55"),
    ("salle 55", "Salle 55"),
    ("zaal 55", "Salle 55"),
    ("patio 2", "Patio 2"),
    ("salle 61", "Patio 2"),
    ("zaal 61", "Patio 2"),
    ("paccar", "Paccar"),
    ("ingang balat", "Entrée Balat"),
    ("entrée balat", "Entrée Balat"),
    ("entree balat", "Entrée Balat"),

    # --- MAGRITTE wing --- (check specific MMM variants before generic "coordinateur"/"mobile")
    ("coordinateur", "Coordinateur Magritte"),
    ("coördinateur", "Coordinateur Magritte"),
    ("mobile", "Mobile"),
    ("mobiel", "Mobile"),
    ("sas", "SAS"),
    ("contrôle", "0 MMM"),
    ("controle", "0 MMM"),
    ("0 mmm", "0 MMM"),
    ("ct/tc mmm", "CT MMM"),
    ("ct tc mmm", "CT MMM"),
    ("tc mmm", "CT MMM"),            # tolerate OCR dropping the leading "CT/"
    ("lift mmm", "Lift MMM"),
    ("mmm +1", "MMM +1"),
    ("mmm+1", "MMM +1"),
    ("mmm +2", "MMM +2"),
    ("mmm+2", "MMM +2"),
    ("mmm +3", "MMM +3"),
    ("mmm+3", "MMM +3"),
]

# The order these canonical locations appear on the physical roster sheet,
# top to bottom. Used to sort location listings the way staff actually expect
# to scan them, instead of alphabetically. Anything not in this list (a new
# or unrecognized location) is sorted alphabetically after all of these.
ROSTER_ORDER = [
    "Entrée principale",
    "Acceuil groupes",
    "Forum",
    "CT Moderne",
    "CT Ancien",
    "Vestiaire",
    "Gresham",
    "Argenteau",
    "Paccar",
    "Entrée Balat",
    "Balat 1-2",
    "Balat 3-4",
    "Salle 52",
    "Salle 55",
    "Patio 2",
    "Agora -2",
    "Coordinateur Magritte",
    "Mobile",
    "SAS",
    "0 MMM",
    "CT MMM",
    "Lift MMM",
    "MMM +1",
    "MMM +2",
    "MMM +3",
    "Musicorum",
]


def roster_sort_key(name):
    """Sort key that follows the roster sheet's physical row order. Unknown
    names sort after all known ones, alphabetically among themselves."""
    try:
        return (0, ROSTER_ORDER.index(name))
    except ValueError:
        return (1, (name or "").lower())

# Generic cleanup: collapse "Salle/Zaal", "Zaal/Salle" (either order/case) down to just "Salle".
_ZAAL_PATTERN = re.compile(r"(salle|zaal)\s*/\s*(salle|zaal)", re.IGNORECASE)
_ZAAL_SUFFIX_PATTERN = re.compile(r"/\s*zaal", re.IGNORECASE)


def normalize_location_label(raw):
    """Turn a raw OCR'd location/post label into its canonical museum-dashboard name."""
    if not raw:
        return raw
    label = raw.strip()
    lower = label.lower()

    for needle, replacement in RENAME_RULES:
        if needle in lower:
            return replacement

    # No explicit rename rule matched — just clean up "Salle/Zaal" style duplication.
    cleaned = _ZAAL_PATTERN.sub("Salle", label)
    cleaned = _ZAAL_SUFFIX_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" +/-")
    return cleaned or label


def classify_group(location_name):
    """Classify a (post-normalization) location name into a main museum wing."""
    if not location_name:
        return "Other"
    n = location_name.lower()

    magritte_names = {
        "coordinateur magritte", "mobile", "sas", "0 mmm", "ct mmm",
        "lift mmm", "mmm +1", "mmm +2", "mmm +3",
    }
    if n in magritte_names or "mmm" in n or "coordinateur" in n or "mobile" in n:
        return "MAGRITTE"

    balat_names = {
        "entrée balat", "balat 1-2", "balat 3-4", "salle 52",
        "salle 55", "patio 2", "paccar",
    }
    if n in balat_names or "balat" in n or "salle" in n or "patio" in n or "paccar" in n:
        return "BALAT"

    forum_keywords = [
        "entrée principale", "acceuil groupes", "forum", "ct moderne", "ct ancien",
        "vestiaire", "gresham", "argenteau", "agora",
    ]
    if any(k in n for k in forum_keywords):
        return "FORUM"

    return "Other"


GROUP_COLORS = {
    "FORUM": "#2E8B57",
    "BALAT": "#D93B2C",
    "MAGRITTE": "#5BC8E8",
    "Other": "#8A8A78",
}

GROUP_ORDER = ["FORUM", "BALAT", "MAGRITTE", "Other"]
