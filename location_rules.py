import re
import difflib

# Rules that only fire on an exact (whitespace-insensitive) match of the whole
# label. Reserved for short/ambiguous OCR fragments (e.g. "OK", "61", "1A")
# that would produce false positives if matched as a substring anywhere in a
# label the way RENAME_RULES below does.
EXACT_RULES = [
    ("ok", "CT Ancien"),          # OCR sometimes drops the "CT/TC AA/" prefix
    ("1a", "Argenteau"),
    ("4/5", "Agora -2"),
    ("8-9", "Balat 55-7-8-9"),
    ("61", "Balat Patio 2"),
    ("mmm", "Magritte SAS"),      # bare "MMM" with no further qualifier
]

# Ordered rename rules: (substring to look for in the raw OCR'd label, canonical replacement).
# Checked in order, case-insensitive. First match wins and REPLACES the whole label.
RENAME_RULES = [
    # --- FORUM wing ---
    ("vestiaire", "Vestiaire"),
    ("regentschapstraat", "Entrée principale"),
    ("straat 3", "Entrée principale"),      # tolerate OCR dropping "Regentschap"
    ("onthaal groepen", "Accueil groupes"),
    ("accueil groupes", "Accueil groupes"),
    ("acceuil groupes", "Accueil groupes"),
    ("groepen", "Accueil groupes"),          # tolerate OCR dropping "Onthaal"
    ("ct/tc aa", "CT Ancien"),
    ("ct tc aa", "CT Ancien"),
    ("tc aa", "CT Ancien"),          # tolerate OCR dropping the leading "CT/"
    ("ct aa", "CT Ancien"),          # tolerate OCR dropping the trailing "/TC"
    ("ct/tc moderne", "CT Moderne"),
    ("ct tc moderne", "CT Moderne"),
    ("tc moderne", "CT Moderne"),    # tolerate OCR dropping the leading "CT/"
    ("ct moderne", "CT Moderne"),    # tolerate OCR dropping the trailing "/TC"
    ("patio 0", "CT Moderne"),
    ("greshman", "Gresham"),         # common OCR misread of "Gresham"
    ("gresham", "Gresham"),
    ("argenteau", "Argenteau"),
    ("agora", "Agora -2"),
    ("foyer 0", "Forum"),
    ("forum", "Forum"),

    # --- BALAT wing --- (check specific numbered rows before generic "ingang balat")
    ("balat 1-2", "Balat 1-2"),
    ("balat 3-4", "Balat 3-4"),
    ("salle 51", "Balat 3-4"),
    ("zaal 51", "Balat 3-4"),
    ("rubens", "Balat 52-53-54"),
    ("salle 52", "Balat 52-53-54"),
    ("zaal 52", "Balat 52-53-54"),
    ("salle 54", "Balat 52-53-54"),
    ("zaal 54", "Balat 52-53-54"),
    ("balat 7-8-9", "Balat 55-7-8-9"),
    ("salle 55", "Balat 55-7-8-9"),
    ("zaal 55", "Balat 55-7-8-9"),
    ("patio 2", "Balat Patio 2"),
    ("salle 61", "Balat Patio 2"),
    ("zaal 61", "Balat Patio 2"),
    ("paccar", "Paccar"),
    ("ingaang balat", "Entrée Balat"),   # tolerate OCR misread of "ingang"
    ("ingang balat", "Entrée Balat"),
    ("entrée balat", "Entrée Balat"),
    ("entree balat", "Entrée Balat"),

    # --- MAGRITTE wing --- (check specific MMM variants before generic "coordinateur"/"mobile")
    ("coordinateur", "Magritte Coordinateur"),
    ("coördinateur", "Magritte Coordinateur"),
    ("mobile", "Magritte Mobile"),
    ("mobiel", "Magritte Mobile"),
    ("sas", "Magritte SAS"),
    ("contrôle", "Magritte SAS"),
    ("controle", "Magritte SAS"),
    ("0 mmm", "Magritte SAS"),
    ("ct/tc mmm", "Magritte CT"),
    ("ct tc mmm", "Magritte CT"),
    ("tc mmm", "Magritte CT"),            # tolerate OCR dropping the leading "CT/"
    ("ct mmm", "Magritte CT"),            # tolerate OCR dropping the trailing "/TC"
    ("lift mmm", "Magritte Lift"),
    ("mmm +1", "Magritte +1"),
    ("mmm+1", "Magritte +1"),
    ("mmm +2", "Magritte +2"),
    ("mmm+2", "Magritte +2"),
    ("mmm +3", "Magritte +3"),
    ("mmm+3", "Magritte +3"),

    # --- ME/WI wing ---
    ("wiertz", "Wiertz"),
    ("meunier", "Meunier"),
]

# The order these canonical locations appear on the physical roster sheet,
# top to bottom. Used to sort location listings the way staff actually expect
# to scan them, instead of alphabetically. Anything not in this list (a new
# or unrecognized location) is sorted alphabetically after all of these.
ROSTER_ORDER = [
    "Entrée principale",
    "Accueil groupes",
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
    "Balat 52-53-54",
    "Balat 55-7-8-9",
    "Balat Patio 2",
    "Agora -2",
    "Magritte Coordinateur",
    "Magritte Mobile",
    "Magritte SAS",
    "Magritte CT",
    "Magritte Lift",
    "Magritte +1",
    "Magritte +2",
    "Magritte +3",
    "Musicorum",
    "Wiertz",
    "Meunier",
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

# Several canonical names are near-siblings of each other by design (Balat
# 1-2 / 3-4, Magritte +1 / +2 / +3, ...), so a close spelling match alone
# isn't enough to auto-correct on -- the best match also has to clearly beat
# the next-best one, or two different real locations could get silently
# confused with each other (e.g. a misread "Magritte +" could as easily be
# +1, +2, or +3). Both thresholds were picked by checking real OCR-style
# misreads (e.g. "Paccar" -> "Paccan") against this exact list.
LOCATION_SIMILARITY_CUTOFF = 0.80
LOCATION_SIMILARITY_MARGIN = 0.06


def _fuzzy_canonical_location(lower_label):
    """If `lower_label` is a close-but-not-exact spelling of exactly one
    canonical location -- e.g. an OCR misread like "Paccar" -> "Paccan" --
    return that canonical name. Returns None if there's no confident,
    unambiguous match."""
    scored = sorted(
        ((difflib.SequenceMatcher(None, lower_label, c.lower()).ratio(), c) for c in ROSTER_ORDER),
        reverse=True,
    )
    best_ratio, best_name = scored[0]
    second_ratio = scored[1][0] if len(scored) > 1 else 0.0
    if best_ratio >= LOCATION_SIMILARITY_CUTOFF and (best_ratio - second_ratio) >= LOCATION_SIMILARITY_MARGIN:
        return best_name
    return None


def normalize_location_label(raw):
    """Turn a raw OCR'd location/post label into its canonical museum-dashboard name."""
    if not raw:
        return raw
    label = raw.strip()
    lower = label.lower()
    compact = re.sub(r"\s+", "", lower)

    for needle, replacement in EXACT_RULES:
        if compact == needle:
            return replacement

    for needle, replacement in RENAME_RULES:
        if needle in lower:
            return replacement

    fuzzy = _fuzzy_canonical_location(lower)
    if fuzzy:
        return fuzzy

    # No explicit rename rule or confident fuzzy match — just clean up
    # "Salle/Zaal" style duplication and keep the label as-is.
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
        "magritte coordinateur", "magritte mobile", "magritte sas", "magritte ct",
        "magritte lift", "magritte +1", "magritte +2", "magritte +3",
    }
    if n in magritte_names or "magritte" in n or "coordinateur" in n or "mobile" in n:
        return "MAGRITTE"

    balat_names = {
        "entrée balat", "balat 1-2", "balat 3-4",
        "balat 52-53-54", "balat 55-7-8-9", "balat patio 2", "paccar",
    }
    if n in balat_names or "balat" in n or "salle" in n or "patio" in n or "paccar" in n:
        return "BALAT"

    forum_keywords = [
        "entrée principale", "accueil groupes", "forum", "ct moderne", "ct ancien",
        "vestiaire", "gresham", "argenteau", "agora",
    ]
    if any(k in n for k in forum_keywords):
        return "FORUM"

    if n in {"wiertz", "meunier"} or "wiertz" in n or "meunier" in n:
        return "ME/WI"

    return "Other"


GROUP_COLORS = {
    "FORUM": "#2E8B57",
    "BALAT": "#D93B2C",
    "MAGRITTE": "#5BC8E8",
    "ME/WI": "#8E5B9E",
    "Other": "#8A8A78",
}

GROUP_ORDER = ["FORUM", "BALAT", "MAGRITTE", "ME/WI", "Other"]
