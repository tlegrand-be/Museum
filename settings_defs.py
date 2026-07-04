DEFAULT_THEME = "classic"

# Swatches shown on the settings page (3 representative colors per theme).
THEMES = {
    "classic": {"label": "Museum Classic", "swatches": ["#1F3A2E", "#B8863B", "#F1E9D8"]},
    "clear":   {"label": "Clear & Light", "swatches": ["#1E3A8A", "#2563EB", "#F8FAFC"]},
    "dark":    {"label": "Dark Gallery", "swatches": ["#0B1220", "#38BDF8", "#131C2B"]},
    "sandy":   {"label": "Sandy Dune", "swatches": ["#8A5A3B", "#C97B4A", "#FFF6E9"]},
    "brown":   {"label": "Espresso", "swatches": ["#3B2418", "#C98A4B", "#EFE3D6"]},
}

DEFAULT_OVERVIEW_WIDGETS = [
    {"id": "days_covered", "enabled": True},
    {"id": "last_update", "enabled": True},
    {"id": "wing_chart", "enabled": True},
    {"id": "colleagues_grid", "enabled": True},
    {"id": "locations_grid", "enabled": True},
]

WIDGET_LABELS = {
    "days_covered": "Days covered stat",
    "last_update": "Last update stat",
    "wing_chart": "Time-by-museum-wing chart",
    "colleagues_grid": "Colleagues grid",
    "locations_grid": "Locations grid",
}
