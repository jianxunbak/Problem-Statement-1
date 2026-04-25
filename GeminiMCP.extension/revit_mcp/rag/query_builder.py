# Multiple focused sub-queries per topic.
# Each query targets a specific dimension/requirement so Vertex returns the right page.
# {building_type} and {storeys} are substituted at query time.
TOPIC_QUERIES = {
    "staircase": [
        "SCDF Clause 2.2.15 exit staircase minimum flight width riser tread headroom mm",
        "SCDF Clause 2.2.15d riser tread headroom exit staircase dimensions mm",
        "SCDF minimum headroom clearance exit staircase overrun mm Clause 2.2.15",
        "SCDF minimum number of exit staircases required Clause 2.2.11 {building_type}",
        "SCDF Clause 2.2.11 number of exits required {building_type}",
    ],
    "fire_lift": [
        "SCDF Clause 6.6 fire lift minimum car platform size width depth mm office building",
        "SCDF Clause 6.6.2 fire lift car platform minimum dimensions mm",
        "SCDF fire lift minimum door clear width load capacity speed Clause 6.6",
    ],
    "fire_lift_lobby": [
        "SCDF Clause 2.2.13b fire lift lobby minimum floor area m2 minimum clear width mm",
        "SCDF smoke-free lobby also serves fire lift lobby floor area 6m2 minimum width 2m Clause 2.2.13b",
        "SCDF fire lift lobby minimum size area width Clause 2.2.13",
    ],
    "smoke_stop_lobby": [
        "SCDF Clause 2.2.13b smoke-free lobby minimum floor area 3m2 minimum clear width 1.2m",
        "SCDF smoke-free lobby minimum size area width Clause 2.2.13",
        "SCDF smoke stop lobby minimum floor area minimum clear width mm Clause 2.2.13b",
    ],
    "occupant_load": [
        "SCDF Table 2.2A occupant load factor m2 per person office {building_type}",
        "SCDF Table occupancy load factor floor area per person office admin general",
        "SCDF Clause 2.2.4 occupant load calculation floor area per person {building_type}",
    ],
    "exit_width": [
        "SCDF Table 2.2A persons per unit width staircase exit passageway {building_type} non-sprinklered",
        "SCDF Clause 2.2.5 capacity exits unit width 500mm persons per unit staircase {building_type}",
        "SCDF Table 2.2A column 7 staircase exit passageway persons per unit non-sprinklered offices",
    ],
    "travel_distance": [
        "SCDF Table 2.2A maximum travel distance {building_type} two-way non-sprinklered sprinklered metres",
        "SCDF Table 2.2A offices two-way travel distance non-sprinklered 45m sprinklered 75m",
        "SCDF Clause 2.2.6 maximum travel distance {building_type} Table 2.2A metres",
    ],
    "corridor": [
        "SCDF minimum corridor width mm exit access {building_type} Clause 2.2",
        "SCDF minimum internal corridor width exit access means of escape mm",
        "SCDF access corridor minimum clear width mm Clause 2.3",
    ],
}

# Human-readable building type labels for query substitution
_TYPE_LABELS = {
    "commercial_office": "commercial office",
    "residential":       "residential",
    "mixed_use":         "mixed-use",
    "industrial":        "industrial",
    "hotel":             "hotel",
    "retail":            "retail",
}


def build_queries(topic: str, intent: dict) -> list:
    """Return a list of focused query strings for the given topic."""
    templates = TOPIC_QUERIES.get(topic, [f"SCDF {topic} requirements"])
    building_type = _TYPE_LABELS.get(intent.get("building_type", ""), intent.get("building_type", "commercial office"))
    storeys = intent.get("storeys", "")
    storey_suffix = f" {storeys} storeys" if storeys else ""
    return [t.format(building_type=building_type) + storey_suffix for t in templates]


def build_query(topic: str, intent: dict) -> str:
    """Legacy single-query interface — returns the first sub-query."""
    return build_queries(topic, intent)[0]
