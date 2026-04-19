TOPIC_QUERIES = {
    "staircase": "staircase minimum width count requirements fire escape pressurisation",
    "fire_lift": "fire lift dimensions car size door width load capacity requirements",
    "fire_lift_lobby": "fire lift lobby minimum area dimensions pressurisation smoke barrier",
}


def build_query(topic: str, intent: dict) -> str:
    base = TOPIC_QUERIES.get(topic, topic)
    building_type = intent.get("building_type", "")
    storeys = intent.get("storeys", "")
    return f"{base} {building_type} {storeys} storey Singapore"
