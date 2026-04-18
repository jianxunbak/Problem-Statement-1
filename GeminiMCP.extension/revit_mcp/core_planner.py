# -*- coding: utf-8 -*-
"""
Core Layout Planner

Performs constraint-based spatial planning for all building core elements
BEFORE any Revit geometry is created. Enforces minimum code-compliant
dimensions, areas, and ensures no spatial overlaps — only abutments.

Design authorities:
  - Passenger lifts: BS EN 81-20 Category 2
  - Fire fighting lift: BS EN 81-72 / BS 5588-5
  - Fire lift lobby: BS 9999 (min 6m², min 1200mm clear width)
  - Protected staircase: Building Regs Approved Document B (min 1000mm clear)

Layout strategy (Y-axis, south→north):
  [S-Staircase] ↕ [S-FireLobby] ↕ [S-FireLift] ↕ [PassengerLifts] ↕ [N-FireLift] ↕ [N-FireLobby] ↕ [N-Staircase]

All spaces share walls at abutments (no gaps, no overlaps).
"""
import math


# ─────────────────────────────────────────────────────────────────────────────
#  Code-compliant minimum space requirements (all in mm)
# ─────────────────────────────────────────────────────────────────────────────

SPACE_REQUIREMENTS = {
    # Passenger lift car (BS EN 81-20 Cat 2, min 8-person)
    "passenger_lift_car": {
        "min_width":   1100,   # mm clear internal
        "min_depth":   1400,   # mm clear internal
        "std_width":   2000,   # mm standard office car
        "std_depth":   2000,   # mm standard office car
        "wall_t":       200,   # mm shaft wall thickness
        "description": "Passenger lift car — BS EN 81-20",
    },
    # Fire fighting lift car (BS EN 81-72 / BS 5588-5)
    "fire_lift_car": {
        "min_width":   1100,   # mm clear (stretcher-accessible)
        "min_depth":   2100,   # mm clear (stretcher length)
        "std_width":   2000,   # mm
        "std_depth":   2000,   # mm
        "wall_t":       200,   # mm
        "description": "Fire fighting lift — BS EN 81-72",
    },
    # Fire lift lobby (BS 9999 / BS 5588-5)
    "fire_lobby": {
        "min_area":  6_000_000,  # mm² = 6 m²
        "min_width":    1200,    # mm clear
        "min_depth":    1400,    # mm clear
        "std_depth":    2000,    # mm clear (comfortable egress)
        "description":  "Fire lift lobby — BS 9999",
    },
    # Protected staircase (Building Regs Approved Document B)
    "staircase": {
        "min_flight_width":  1000,   # mm clear flight width
        "std_flight_width":  1200,   # mm (typical office standard)
        "min_riser":           100,  # mm
        "max_riser":           220,  # mm
        "min_tread":           250,  # mm going
        "std_riser":           150,  # mm
        "std_tread":           300,  # mm
        "description": "Protected staircase — Approved Doc B",
    },
}

WALL_T = 200  # mm — standard structural wall thickness throughout


# ─────────────────────────────────────────────────────────────────────────────
#  CoreSpacePlan  — holds precise bounding boxes for all core spaces
# ─────────────────────────────────────────────────────────────────────────────

class CoreSpacePlan:
    """
    Stores the planned rectangular bounding boxes for every core space.

    Each space is defined by an outer rectangle [x1, y1, x2, y2] in mm,
    measured from the building centroid (0, 0).

    Spaces only butt against each other — no overlaps are permitted.
    Shared walls sit exactly at the boundary between two adjacent spaces.
    """

    def __init__(self):
        self.spaces = {}          # id → dict
        self.validation_log = []  # list of warning/error strings

    # ── Public API ────────────────────────────────────────────────────────────

    def add_space(self, space_id, rect, space_type,
                  min_dims=None, min_area=None):
        """
        Register a space and validate it against minimum requirements.

        Args:
            space_id:  unique identifier string
            rect:      [x1, y1, x2, y2] in mm
            space_type: key into SPACE_REQUIREMENTS
            min_dims:  (min_width, min_depth) in mm — overrides defaults
            min_area:  minimum net area in mm²     — overrides defaults
        Returns:
            True if all requirements satisfied, False otherwise.
        """
        x1, y1, x2, y2 = rect
        w, d = x2 - x1, y2 - y1
        area = w * d
        ok = True

        req = SPACE_REQUIREMENTS.get(space_type, {})
        eff_min_w = min_dims[0] if min_dims else req.get("min_width", 0)
        eff_min_d = min_dims[1] if min_dims else req.get("min_depth", 0)
        eff_min_a = min_area if min_area is not None else req.get("min_area", 0)

        if w < eff_min_w - 1:
            msg = "[{}] Width {:.0f}mm < required {:.0f}mm ({})".format(
                space_id, w, eff_min_w, req.get("description", ""))
            self.validation_log.append("WARN: " + msg)
            ok = False
        if d < eff_min_d - 1:
            msg = "[{}] Depth {:.0f}mm < required {:.0f}mm ({})".format(
                space_id, d, eff_min_d, req.get("description", ""))
            self.validation_log.append("WARN: " + msg)
            ok = False
        if eff_min_a and area < eff_min_a - 1:
            msg = "[{}] Area {:.2f}m² < required {:.2f}m² ({})".format(
                space_id, area / 1e6, eff_min_a / 1e6, req.get("description", ""))
            self.validation_log.append("WARN: " + msg)
            ok = False

        self.spaces[space_id] = {
            "rect":  list(rect),
            "type":  space_type,
            "width": w,
            "depth": d,
            "area":  area,
            "ok":    ok,
        }
        return ok

    def check_overlaps(self, tolerance=5.0):
        """
        Verify that no two spaces overlap (touching at shared walls is OK).

        Returns:
            list of (id_a, id_b) pairs that conflict.
        """
        ids = list(self.spaces.keys())
        conflicts = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a = self.spaces[ids[i]]["rect"]
                b = self.spaces[ids[j]]["rect"]
                if (a[0] < b[2] - tolerance and a[2] > b[0] + tolerance and
                        a[1] < b[3] - tolerance and a[3] > b[1] + tolerance):
                    conflicts.append((ids[i], ids[j]))
        if conflicts:
            self.validation_log.append(
                "ERROR: {} spatial overlap(s): {}".format(len(conflicts), conflicts))
        return conflicts

    def get_rect(self, space_id):
        """Return [x1, y1, x2, y2] for a space, or None if not found."""
        s = self.spaces.get(space_id)
        return s["rect"] if s else None

    def total_bounds(self):
        """Return bounding box of the entire core assembly [x1, y1, x2, y2]."""
        all_x = [r for s in self.spaces.values() for r in [s["rect"][0], s["rect"][2]]]
        all_y = [r for s in self.spaces.values() for r in [s["rect"][1], s["rect"][3]]]
        return [min(all_x), min(all_y), max(all_x), max(all_y)] if all_x else [0, 0, 0, 0]

    def translate(self, dx, dy):
        """Shift ALL spaces by (dx, dy) mm — for core repositioning."""
        for s in self.spaces.values():
            r = s["rect"]
            s["rect"] = [r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy]

    def summary(self):
        """Return a human-readable summary of the planned layout."""
        lines = ["CoreSpacePlan summary:"]
        for sid, s in sorted(self.spaces.items(), key=lambda x: x[1]["rect"][1]):
            lines.append(
                "  {:<30s}  {:>7.0f} x {:>5.0f} mm  area {:>5.2f} m²  {}".format(
                    sid, s["width"], s["depth"], s["area"] / 1e6,
                    "" if s["ok"] else "[BELOW MIN]"))
        if self.validation_log:
            lines.append("Validation:")
            lines.extend("  " + v for v in self.validation_log)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Main planning function
# ─────────────────────────────────────────────────────────────────────────────

def plan_core_layout(num_lifts, typical_floor_height_mm, stair_spec=None,
                     lobby_width_mm=3000, center_pos=(0.0, 0.0),
                     lift_internal_size=(2500, 2500)):
    """
    Compute a complete, code-compliant central core layout.

    The layout is assembled along the Y-axis (south → north):
        South staircase
        South fire-lift lobby
        South fire-lift zone
        Passenger lift core (all blocks stacked N-S)
        North fire-lift zone
        North fire-lift lobby
        North staircase

    All rectangles are in mm measured from the building centroid (0, 0).

    Args:
        num_lifts:               Total passenger lifts required.
        typical_floor_height_mm: Typical storey height (mm) for stair sizing.
        stair_spec:              Dict with keys riser, tread, width_of_flight,
                                 landing_width.  Uses code minimums if None.
        lobby_width_mm:          Passenger lift lobby clear width (mm).
        center_pos:              (cx, cy) — core centre relative to building
                                 centroid.  Default (0, 0) for central core.
        lift_internal_size:      (w, d) mm — internal lift car size.

    Returns:
        CoreSpacePlan with all spaces registered and validated.
    """
    from . import lift_logic  # lazy import to avoid circular refs

    spec       = stair_spec or {}
    riser      = max(spec.get("riser",           150), SPACE_REQUIREMENTS["staircase"]["min_riser"])
    tread      = max(spec.get("tread",           300), SPACE_REQUIREMENTS["staircase"]["min_tread"])
    w_flight   = max(spec.get("width_of_flight", 1200), SPACE_REQUIREMENTS["staircase"]["min_flight_width"])
    w_landing  = max(spec.get("landing_width",   w_flight), w_flight)
    t          = WALL_T

    cx, cy = center_pos

    # ── 1. Passenger lift core dimensions ─────────────────────────────────────
    layout    = lift_logic.get_total_core_layout(num_lifts,
                                                 internal_size=lift_internal_size,
                                                 lobby_width=lobby_width_mm)
    lift_w    = layout["total_w"]
    lift_d    = layout["total_d"]

    lift_x1 = cx - lift_w / 2.0
    lift_y1 = cy - lift_d / 2.0
    lift_x2 = cx + lift_w / 2.0
    lift_y2 = cy + lift_d / 2.0

    # ── 2. Staircase shaft dimensions ─────────────────────────────────────────
    # Width:   2 flights + 3 walls (left, divider, right)
    stair_w = 2 * w_flight + 3 * t
    # Ensure stair zone matches (or is at least as wide as) lift core
    suite_w = max(stair_w, lift_w)   # use lift core width if wider

    # Depth:   flight run + 2 landings + 2 walls
    num_risers  = max(int(math.floor(typical_floor_height_mm / float(riser))), 2)
    flight_r1   = max(num_risers // 2, 1)
    flight_r2   = max(num_risers - flight_r1, 1)
    max_treads  = max(flight_r1 - 1, flight_r2 - 1, 1)
    flight_len  = max_treads * tread
    stair_d     = flight_len + 2 * w_landing + 2 * t

    # Hard-enforce code minimum staircase width
    stair_w = max(stair_w, 2 * SPACE_REQUIREMENTS["staircase"]["min_flight_width"] + 3 * t)

    # ── 3. Fire-lift zone dimensions ──────────────────────────────────────────
    # External shaft = internal car + 2 × wall thickness
    fl_req  = SPACE_REQUIREMENTS["fire_lift_car"]
    fl_car_d = fl_req["std_depth"]
    fl_zone_d = fl_car_d + 2 * t   # = 2400mm for std 2000mm car
    fl_zone_w = suite_w             # match suite width for clean rectangular assembly

    # ── 4. Fire-lift lobby dimensions ─────────────────────────────────────────
    lb_req       = SPACE_REQUIREMENTS["fire_lobby"]
    lb_clear_w   = suite_w - 2 * t
    lb_clear_d   = max(
        lb_req["std_depth"],
        int(math.ceil(lb_req["min_area"] / max(lb_clear_w, 1)))
    )
    # Verify minimum area
    while lb_clear_w * lb_clear_d < lb_req["min_area"]:
        lb_clear_d += 100
    lb_zone_d = lb_clear_d + t   # lobby occupies clear depth + one shared wall

    # ── 5. Assemble layout (south → north) ────────────────────────────────────
    plan = CoreSpacePlan()
    x1_suite = cx - suite_w / 2.0
    x2_suite = cx + suite_w / 2.0

    # South fire-lift zone  (immediately south of passenger lift core)
    s_fl_y2 = lift_y1
    s_fl_y1 = s_fl_y2 - fl_zone_d
    plan.add_space("FireLift_South", [x1_suite, s_fl_y1, x2_suite, s_fl_y2],
                   "fire_lift_car",
                   min_dims=(fl_req["min_width"] + 2*t, fl_req["min_depth"] + 2*t))

    # South fire-lift lobby (immediately south of south fire-lift zone)
    s_lb_y2 = s_fl_y1
    s_lb_y1 = s_lb_y2 - lb_zone_d
    plan.add_space("FireLobby_South", [x1_suite, s_lb_y1, x2_suite, s_lb_y2],
                   "fire_lobby",
                   min_dims=(lb_req["min_width"] + 2*t, lb_req["min_depth"]),
                   min_area=lb_req["min_area"])

    # South staircase (immediately south of south lobby)
    s_st_y2 = s_lb_y1
    s_st_y1 = s_st_y2 - stair_d
    s_st_x1 = cx - stair_w / 2.0
    s_st_x2 = cx + stair_w / 2.0
    plan.add_space("Stair_South", [s_st_x1, s_st_y1, s_st_x2, s_st_y2],
                   "staircase",
                   min_dims=(2 * SPACE_REQUIREMENTS["staircase"]["min_flight_width"] + 3*t, stair_d))

    # Passenger lift core (centre of assembly)
    plan.add_space("PassengerLifts", [lift_x1, lift_y1, lift_x2, lift_y2],
                   "passenger_lift_car",
                   min_dims=(SPACE_REQUIREMENTS["passenger_lift_car"]["min_width"] + 2*t,
                              SPACE_REQUIREMENTS["passenger_lift_car"]["min_depth"] + 2*t))

    # North fire-lift zone
    n_fl_y1 = lift_y2
    n_fl_y2 = n_fl_y1 + fl_zone_d
    plan.add_space("FireLift_North", [x1_suite, n_fl_y1, x2_suite, n_fl_y2],
                   "fire_lift_car",
                   min_dims=(fl_req["min_width"] + 2*t, fl_req["min_depth"] + 2*t))

    # North fire-lift lobby
    n_lb_y1 = n_fl_y2
    n_lb_y2 = n_lb_y1 + lb_zone_d
    plan.add_space("FireLobby_North", [x1_suite, n_lb_y1, x2_suite, n_lb_y2],
                   "fire_lobby",
                   min_dims=(lb_req["min_width"] + 2*t, lb_req["min_depth"]),
                   min_area=lb_req["min_area"])

    # North staircase
    n_st_y1 = n_lb_y2
    n_st_y2 = n_st_y1 + stair_d
    plan.add_space("Stair_North", [s_st_x1, n_st_y1, s_st_x2, n_st_y2],
                   "staircase",
                   min_dims=(2 * SPACE_REQUIREMENTS["staircase"]["min_flight_width"] + 3*t, stair_d))

    # ── 6. Validate ───────────────────────────────────────────────────────────
    plan.check_overlaps()

    return plan, {
        "suite_width":   suite_w,
        "stair_width":   stair_w,
        "stair_depth":   stair_d,
        "fl_zone_depth": fl_zone_d,
        "lb_zone_depth": lb_zone_d,
        "lift_width":    lift_w,
        "lift_depth":    lift_d,
    }
