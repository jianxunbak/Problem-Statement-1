import re

_BUILDING_TYPES = {
    "commercial_office": ["office", "commercial", "corp", "corporate", "business"],
    "residential":       ["residential", "apartment", "flat", "condo", "housing", "home", "dwelling"],
    "mixed_use":         ["mixed", "mixed-use", "mixed use"],
    "industrial":        ["industrial", "warehouse", "factory", "logistics"],
    "hotel":             ["hotel", "hospitality", "resort"],
    "retail":            ["retail", "mall", "shop", "shopping"],
}


def extract_intent(user_prompt: str) -> dict:
    """Parse building intent from the user prompt using regex — no LLM call needed.

    Any building above 4 storeys always requires staircase, fire_lift, fire_lift_lobby
    per SCDF rules, so we can determine topics purely from storey count.
    """
    text = user_prompt.lower()

    # --- Storey count ---
    # Matches: "10 storey", "10-storey", "10 story", "10 floor", "10 level", "g+9"
    storey = 1
    m = re.search(r'(\d+)\s*[-\s]?\s*(?:storey|story|floor|level|fl)', text)
    if m:
        storey = int(m.group(1))
    else:
        # "g+9" style (ground + upper floors)
        m = re.search(r'g\s*\+\s*(\d+)', text)
        if m:
            storey = int(m.group(1)) + 1

    # --- Building type ---
    building_type = "commercial_office"  # safe default for Singapore high-rise
    for btype, keywords in _BUILDING_TYPES.items():
        if any(kw in text for kw in keywords):
            building_type = btype
            break

    # --- Topics — always retrieve occupant load, exit width, corridor, travel distance for any building.
    # SCDF mandates fire safety systems above 4 storeys.
    topics = ["staircase", "occupant_load", "exit_width", "travel_distance", "corridor", "smoke_stop_lobby"]
    if storey > 4:
        topics += ["fire_lift", "fire_lift_lobby"]

    return {"topics": topics, "building_type": building_type, "storeys": storey}
