# -*- coding: utf-8 -*-
"""
Staircase generation logic for the Revit MCP building system.

Generates manifest data (walls, floors, voids) for fire-escape staircases
that integrate with the lift core as a compact rectangular assembly.

Rules implemented:
  (a) Min 2 staircases per floor.
  (b) Positioned at Y-ends of lift core, not obstructing lobby entrances.
  (c) Enclosure width matches lift core width -> single rectangular core.
  (d) Abutt lift shaft wall first; shift away only for compliance.
  (e) Every floor-plate point within 60 m of 2 staircases (recalculates).
  (f) Additional staircases added for large floor plates; minimise count.
  (g) Structural walls around staircase core -> no columns in footprint.
  (h) Floor-slab void where staircase shaft is.
  (i) Runs from 1st storey to roof with overrun; adapts to height / count.
  (j) Main landing at every floor level inside the shaft.
  (k) Tall floors use multiple flight pairs (2, 4, 6, 8) to maintain
      consistent shaft depth — sized from the typical storey height.
"""
import math

_WALL_THICKNESS = 200  # mm
_OVERRUN_HEIGHT = 5000  # mm — staircase overrun above roof (matches lift core)


# ─────────────────────────────────────────────────────────────────────
#  Flight count helper  [rule k]
# ─────────────────────────────────────────────────────────────────────

def _snap_risers(floor_height_mm, riser=150):
    """Return the number of risers for a given floor height.

    Uses round() to snap the height to the nearest mm first,
    avoiding float precision errors from feet↔mm conversion
    (e.g. 4200mm stored as 13.7795…ft * 304.8 = 4199.9999…mm).
    """
    return int(math.ceil(round(floor_height_mm) / float(riser)))


def _risers_per_flight_typical(typical_floor_height_mm, riser=150):
    """Return the number of risers per flight for a typical storey (2 flights)."""
    typical_risers = _snap_risers(typical_floor_height_mm, riser)
    return max(int(math.ceil(typical_risers / 2.0)), 1)


def _calc_num_flights(floor_height_mm, typical_floor_height_mm, riser=150):
    """Return the number of flights (always even) for a given storey height.

    Every flight has the **same number of risers** as a typical-floor
    flight.  Taller storeys simply get more flights (4, 6, 8 …).

    Example: typical floor = 3500 mm → 24 risers → 12 per flight.
    Ground floor = 7000 mm → 47 risers → needs 4 flights of 12 = 48 slots.
    """
    if typical_floor_height_mm <= 0 or floor_height_mm <= 0:
        return 2

    rpf = _risers_per_flight_typical(typical_floor_height_mm, riser)
    actual_risers = _snap_risers(floor_height_mm, riser)

    # Number of flights needed (round up), then round up to even
    num_flights = int(math.ceil(actual_risers / float(rpf)))
    if num_flights < 2:
        num_flights = 2
    if num_flights % 2 != 0:
        num_flights += 1

    return min(num_flights, 16)  # safety cap


# ─────────────────────────────────────────────────────────────────────
#  Shaft geometry
# ─────────────────────────────────────────────────────────────────────

def get_shaft_dimensions(floor_height_mm, spec=None):
    """Calculate staircase shaft dimensions for a given storey height.

    The shaft depth includes both a **main landing** (at the floor
    level, front of shaft) and a **mid-landing** (U-turn, back of
    shaft).  Layout (Y-axis, front to back)::

        wall | main_landing | flight_area | mid_landing | wall

    Returns:
        (shaft_width, shaft_depth) in mm.
    """
    spec = spec or {}
    riser = spec.get("riser", 150)
    tread = spec.get("tread", 275)
    w_flight = spec.get("width_of_flight", 1500)
    w_landing = spec.get("landing_width", 1500)
    t = _WALL_THICKNESS

    num_risers = _snap_risers(floor_height_mm, riser)
    flight_1_risers = max(num_risers // 2, 1)
    flight_2_risers = max(num_risers - flight_1_risers, 1)
    max_treads = max(flight_1_risers - 1, flight_2_risers - 1, 1)

    flight_length = max_treads * tread
    shaft_width = 2 * w_flight + 3 * t
    shaft_depth = flight_length + 2 * w_landing + 2 * t  # main + mid landing

    return shaft_width, shaft_depth


def get_max_shaft_depth(levels_data, spec=None, typical_floor_height_mm=None):
    """Return the shaft depth for the staircase enclosure.

    When *typical_floor_height_mm* is provided the shaft is sized for
    that height (2 flights).  Taller storeys use more flights instead
    of a deeper shaft — see ``_calc_num_flights``.

    Falls back to scanning all levels (legacy behaviour) when the
    typical height is not supplied.
    """
    if typical_floor_height_mm and typical_floor_height_mm > 0:
        _, d = get_shaft_dimensions(typical_floor_height_mm, spec)
        return d

    max_d = 0.0
    for i in range(len(levels_data) - 1):
        fh = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
        if fh > 0:
            _, d = get_shaft_dimensions(fh, spec)
            if d > max_d:
                max_d = d
    return max_d


# ─────────────────────────────────────────────────────────────────────
#  Position calculation (fire-safety rules)
# ─────────────────────────────────────────────────────────────────────

def calculate_staircase_positions(floor_dims_mm, core_center_mm,
                                  lift_core_bounds_mm,
                                  typical_floor_height_mm, spec=None,
                                  max_travel_mm=60000):
    """Determine staircase centre positions.

    Args:
        floor_dims_mm:  list of (width, length) per storey in mm.
        core_center_mm: (x, y) lift-core centre in mm.
        lift_core_bounds_mm: (xmin, ymin, xmax, ymax) of lift core in mm,
                             or None when no lift core exists.
        typical_floor_height_mm: representative storey height for shaft sizing.
        spec: staircase specification dict.
        max_travel_mm: max travel distance in mm (default 60 000 = 60 m).

    Returns:
        list of (x, y) staircase-centre positions in mm.
    """
    _, shaft_d = get_shaft_dimensions(typical_floor_height_mm, spec)
    cx, cy = core_center_mm

    if lift_core_bounds_mm:
        _, core_ymin, _, core_ymax = lift_core_bounds_mm
    else:
        # No lift core — leave a small gap from centre
        core_ymin = cy - 2500
        core_ymax = cy + 2500

    # --- Primary pair: abutt Y-ends of lift core [rules b, c, d] ---
    stair_south_y = core_ymin - shaft_d / 2.0
    stair_north_y = core_ymax + shaft_d / 2.0
    positions = [(cx, stair_south_y), (cx, stair_north_y)]

    # --- 60 m rule [rules e, f] ---
    max_w = max(d[0] for d in floor_dims_mm) if floor_dims_mm else 50000
    max_l = max(d[1] for d in floor_dims_mm) if floor_dims_mm else 50000

    if _check_travel_distance(positions, max_w, max_l, max_travel_mm):
        return positions

    # Need additional staircases — add at perimeter, minimising count.
    edge_x = max_w / 2.0 - 3000   # 3 m inset from slab edge
    candidates = [
        (-edge_x, cy),                # west
        ( edge_x, cy),                # east
        (-edge_x, -max_l / 4.0),     # south-west quarter
        ( edge_x, -max_l / 4.0),     # south-east quarter
        (-edge_x,  max_l / 4.0),     # north-west quarter
        ( edge_x,  max_l / 4.0),     # north-east quarter
    ]
    for cand in candidates:
        positions.append(cand)
        if _check_travel_distance(positions, max_w, max_l, max_travel_mm):
            break

    return positions


def _check_travel_distance(stair_positions, floor_w_mm, floor_l_mm,
                           max_dist_mm, num_required=2):
    """Return True if every sampled floor-plate point can reach at least
    *num_required* staircases within *max_dist_mm* (Euclidean)."""
    if len(stair_positions) < num_required:
        return False

    hw = floor_w_mm / 2.0
    hl = floor_l_mm / 2.0
    # Corners, edge midpoints, and quarter-points give good coverage.
    test_points = [
        (-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl),
        (0, -hl), (0, hl), (-hw, 0), (hw, 0),
        (-hw / 2, -hl / 2), (hw / 2, -hl / 2),
        (hw / 2, hl / 2), (-hw / 2, hl / 2),
    ]
    for px, py in test_points:
        dists = sorted(
            math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
            for sx, sy in stair_positions
        )
        if dists[num_required - 1] > max_dist_mm:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────
#  Void rectangles (for floor-slab openings)  [rule h]
# ─────────────────────────────────────────────────────────────────────

def get_void_rectangles_mm(positions, enclosure_width_mm, enclosure_depth_mm):
    """Return (x1, y1, x2, y2) rectangles for floor-slab openings.
    One rectangle per staircase enclosure."""
    voids = []
    for cx, cy in positions:
        hw = enclosure_width_mm / 2.0
        hd = enclosure_depth_mm / 2.0
        voids.append((cx - hw, cy - hd, cx + hw, cy + hd))
    return voids


# ─────────────────────────────────────────────────────────────────────
#  Manifest generation  [rules a–k]
# ─────────────────────────────────────────────────────────────────────

def generate_staircase_manifest(positions, levels_data, _enclosure_width_mm=None,
                                spec=None, typical_floor_height_mm=None):
    """Generate wall and floor manifest entries for all staircases.

    The enclosure footprint (plan size) is **fixed** for every level,
    determined by the typical storey height.  Taller storeys use more
    flights rather than a deeper shaft.

    Includes:
      - Enclosure walls (4 per level + roof overrun)
      - Divider wall (separates left/right flights)
      - Main landing floor slab at every floor level  [rule j]
      - Roof-level overrun closing slab (TOPCAP)

    Args:
        positions: list of (x, y) staircase centres in mm.
        levels_data: list of dicts with ``'id'`` and ``'elevation'`` (mm).
        enclosure_width_mm: outer enclosure width (= lift-core width for
                            alignment).  Falls back to natural shaft width.
        spec: staircase specification dict.
        typical_floor_height_mm: storey height used to size the shaft.

    Returns:
        ``{"walls": [...], "floors": [...]}``
    """
    spec = spec or {}
    riser = spec.get("riser", 150)
    tread = spec.get("tread", 275)
    w_flight = spec.get("width_of_flight", 1500)
    w_landing = spec.get("landing_width", 1500)
    t = _WALL_THICKNESS

    # --- Fixed enclosure dimensions (based on typical storey) ---
    # Enclosure width = staircase shaft width (NOT lift core width).
    # Walls butt directly against the staircase flights.
    shaft_width_nat = 2 * w_flight + 3 * t
    enc_w = shaft_width_nat
    enc_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    if enc_d <= 0:
        enc_d = get_shaft_dimensions(4000, spec)[1]  # safe fallback

    # --- Compute per-level landing extensions ---
    # The landing at level i+1 may need to extend deeper into the shaft
    # to bridge a gap between the stair endpoint and the landing edge.
    #
    # Two cases for the LAST flight pair arriving at level i+1:
    #   (a, b) with 0 < b < a: Run B (-Y RIGHT side) ends short → RIGHT extension
    #   (a, 0) solo Run A:     Run A (+Y LEFT side) overshoots  → LEFT extension
    #   (a, a) or (a, b>=a):   symmetric, no gap                → standard
    #
    # Each entry: (left_ext_mm, right_ext_mm) — extra depth beyond w_landing.
    typ_h = typical_floor_height_mm or 4000.0
    rpf = _risers_per_flight_typical(typ_h, riser)
    landing_ext_for_next = {}  # level_idx -> (right_ext, y_shift)
    for li in range(len(levels_data) - 1):
        fh = levels_data[li + 1]['elevation'] - levels_data[li]['elevation']
        if fh <= 0:
            continue
        total_r = _snap_risers(fh, riser)
        nf = _calc_num_flights(fh, typ_h, riser)
        rem = total_r
        flight_list = []
        for _ in range(nf):
            give = min(rpf, rem)
            if give <= 0:
                break
            flight_list.append(give)
            rem -= give
        # Group flights into actual dogleg pairs
        flight_pairs = []
        for fi in range(0, len(flight_list), 2):
            a = flight_list[fi]
            b = flight_list[fi + 1] if fi + 1 < len(flight_list) else 0
            if a > 0:
                flight_pairs.append((a, b))
        # Determine landing geometry adjustments at level i+1.
        # The landing must start where the LAST STEP ends:
        #
        # (a, a) symmetric: Run B arrives at flight_y_start (front)
        #   -> standard landing at front, no adjustment
        #
        # (a, b<a) short Run B: Run B ends short on RIGHT side
        #   -> RIGHT side needs extension to bridge gap
        #   -> LEFT side is fine (next Run A departs from front)
        #
        # (a, 0) solo Run A: Run A ends a_run_len past flight_y_start on LEFT
        #   -> landing must SHIFT deeper into shaft by a_run_len
        #   -> it starts where Run A ends, not at the shaft front
        right_ext = 0
        y_shift = 0  # how far the landing shifts deeper into the shaft
        if flight_pairs:
            last_a, last_b = flight_pairs[-1]
            if last_b == 0 and last_a > 0:
                # Solo Run A: landing shifts to where Run A ends
                y_shift = max(last_a - 1, 1) * tread
            elif 0 < last_b < last_a:
                # Short Run B: RIGHT side gap
                right_ext = (max(last_a - 1, 1) - max(last_b - 1, 1)) * tread
        landing_ext_for_next[li + 1] = (right_ext, y_shift)

    walls = []
    floors = []

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        s_tag = "Stair_{}".format(s_idx + 1)

        # Fixed base coordinates — same for every level
        base_x = s_cx - enc_w / 2.0
        base_y = s_cy - enc_d / 2.0

        for i, lvl in enumerate(levels_data):
            lvl_id = lvl['id']
            is_roof = (i == len(levels_data) - 1)

            # --- Determine wall height ---
            if is_roof:
                wall_height = _OVERRUN_HEIGHT
            else:
                next_lvl = levels_data[i + 1]
                wall_height = next_lvl['elevation'] - lvl['elevation']
                if wall_height <= 0:
                    continue

            common = {"level_id": lvl_id, "height": wall_height}

            # — 4 enclosure walls (fixed plan, every level including roof) —
            walls.append({
                "id": "AI_{}_L{}_W_Front".format(s_tag, i + 1),
                "start": [base_x, base_y, 0],
                "end": [base_x + enc_w, base_y, 0],
                **common
            })
            walls.append({
                "id": "AI_{}_L{}_W_Back".format(s_tag, i + 1),
                "start": [base_x, base_y + enc_d, 0],
                "end": [base_x + enc_w, base_y + enc_d, 0],
                **common
            })
            walls.append({
                "id": "AI_{}_L{}_W_Left".format(s_tag, i + 1),
                "start": [base_x, base_y, 0],
                "end": [base_x, base_y + enc_d, 0],
                **common
            })
            walls.append({
                "id": "AI_{}_L{}_W_Right".format(s_tag, i + 1),
                "start": [base_x + enc_w, base_y, 0],
                "end": [base_x + enc_w, base_y + enc_d, 0],
                **common
            })

            # --- Main landing floor slab [rule j] ---
            # Skip on 1st floor (i==0): the building floor slab already
            # serves as the ground-level landing.
            # DO generate on the roof level: stairs arrive there and
            # need a landing to step off onto.
            #
            # The landing starts where the LAST STEP ends:
            #   Standard:  at front of shaft (flight_y_start edge)
            #   Solo (a,0): shifted deeper — starts where Run A ends
            #   Short B:   front of shaft + L-shape extension on RIGHT
            if i > 0:
                stair_base_x = s_cx - shaft_width_nat / 2.0
                div_x = stair_base_x + w_flight + t
                _ext_data = landing_ext_for_next.get(i, (0, 0))
                right_ext = _ext_data[0] if isinstance(_ext_data, tuple) else _ext_data
                y_shift = _ext_data[1] if isinstance(_ext_data, tuple) and len(_ext_data) > 1 else 0

                # Landing Y range
                # y_shift is measured from flight_y_start (= base_y + t + w_landing),
                # NOT from base_y + t.  The landing starts after the last step.
                if y_shift > 0:
                    land_y_start = base_y + t + w_landing + y_shift
                else:
                    land_y_start = base_y + t
                land_y_end = land_y_start + w_landing
                # Debug: log landing position
                try:
                    from revit_mcp.runner import log as _slog
                    _slog("  Landing L{} s{}: y_shift={:.0f} right_ext={:.0f} "
                          "y=[{:.0f},{:.0f}] flight_y_start={:.0f} is_roof={}".format(
                        i + 1, s_idx + 1, y_shift, right_ext,
                        land_y_start, land_y_end,
                        base_y + t + w_landing, is_roof))
                except Exception:
                    pass

                std_y_start = base_y + t
                std_y_end = base_y + t + w_landing

                if y_shift > 0:
                    # Solo Run A (a, 0): Run A ends on the LEFT side
                    # past flight_y_start.  The landing covers the entire
                    # remaining LEFT half of the shaft — from where Run A
                    # ends all the way to the back wall.
                    #
                    #   div_x
                    #   ├────┐
                    #   │LEFT│  from land_y_start (Run A end)
                    #   │half│  to back_wall (base_y + enc_d - t)
                    #   │    │  covers flight area + mid-landing area
                    #   └────┘
                    #   base_x
                    back_wall_y = base_y + enc_d - t
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id,
                        "elevation": lvl['elevation'],
                        "points": [
                            [base_x, land_y_start],
                            [div_x, land_y_start],
                            [div_x, back_wall_y],
                            [base_x, back_wall_y]
                        ]
                    })
                elif right_ext > 0:
                    # L-shape for short Run B (a, b<a):
                    # RIGHT side (Run B side): deep — bridges the gap
                    # LEFT side: standard position at front
                    #
                    #   base_x        div_x
                    #   ┌─────┬────┐
                    #   │LEFT │RIGH│  standard [std_y_start, std_y_end]
                    #   │side │T   │
                    #   └─────┤ext │  deep [std_y_end, ext_y_end]
                    #         │    │
                    #         └────┘  base_x+enc_w
                    ext_y_end = std_y_end + right_ext
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id,
                        "elevation": lvl['elevation'],
                        "points": [
                            [base_x, std_y_start],
                            [base_x + enc_w, std_y_start],
                            [base_x + enc_w, ext_y_end],
                            [div_x, ext_y_end],
                            [div_x, std_y_end],
                            [base_x, std_y_end]
                        ]
                    })
                else:
                    # Standard rectangular landing
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id,
                        "elevation": lvl['elevation'],
                        "points": [
                            [base_x, std_y_start],
                            [base_x + enc_w, std_y_start],
                            [base_x + enc_w, std_y_end],
                            [base_x, std_y_end]
                        ]
                    })

            # --- Divider wall (separates the two flight halves) ---
            # Only spans the flight area between landings, so the
            # landing areas remain clear for people to pass through.
            # Generated on ALL levels including roof overrun so the
            # central wall extends to the staircase roof.
            stair_base_x = s_cx - shaft_width_nat / 2.0
            div_x = stair_base_x + w_flight + t
            div_y_start = base_y + t + w_landing
            div_y_end = base_y + enc_d - t - w_landing
            if div_y_end > div_y_start + 1:
                walls.append({
                    "id": "AI_{}_L{}_Div".format(s_tag, i + 1),
                    "start": [div_x, div_y_start, 0],
                    "end": [div_x, div_y_end, 0],
                    **common
                })

            if is_roof:
                # — TOPCAP: closing slab above overrun (matches lift core) —
                cap_elev = lvl['elevation'] + _OVERRUN_HEIGHT
                floors.append({
                    "id": "AI_{}_TOPCAP".format(s_tag),
                    "level_id": lvl_id,
                    "elevation": cap_elev,
                    "points": [
                        [base_x, base_y],
                        [base_x + enc_w, base_y],
                        [base_x + enc_w, base_y + enc_d],
                        [base_x, base_y + enc_d]
                    ]
                })
                continue

    return {"walls": walls, "floors": floors}


# ─────────────────────────────────────────────────────────────────────
#  Stair run geometry (for creating actual Revit stair elements)
# ─────────────────────────────────────────────────────────────────────

def get_stair_run_data(positions, levels_data, _enclosure_width_mm=None, spec=None,
                       typical_floor_height_mm=None):
    """Return geometry data for creating Revit stair runs.

    For typical-height storeys: standard 2-flight dogleg (1 pair).
    For taller storeys: multiple flight pairs (2, 3, 4 … pairs = 4, 6, 8
    flights) that fit inside the same fixed shaft footprint.

    Each entry provides the XY geometry and the number of flight pairs
    needed by ``_create_stair_runs`` in revit_workers.py.

    Returns:
        list of dicts::

            {
                "tag": "AI_Stair_1_L1_Run",
                "base_level_idx": 0,
                "top_level_idx": 1,
                "num_flight_pairs": 1,
                "flight_1": {"start": [x, y], "end": [x, y]},
                "flight_2": {"start": [x, y], "end": [x, y]},
                "main_landing": {
                    "y_start": ..., "y_end": ...,
                    "x_left": ..., "x_right": ...
                },
                "width_mm": 1500
            }
    """
    spec = spec or {}
    riser = spec.get("riser", 150)
    w_flight = spec.get("width_of_flight", 1500)
    w_landing = spec.get("landing_width", 1500)
    t = _WALL_THICKNESS

    shaft_width_nat = 2 * w_flight + 3 * t
    enc_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    if enc_d <= 0:
        enc_d = get_shaft_dimensions(4000, spec)[1]

    # Determine typical floor height for flight-count calculation
    typ_h = typical_floor_height_mm
    if not typ_h or typ_h <= 0:
        for i in range(len(levels_data) - 1):
            fh = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
            if fh > 0:
                typ_h = fh
                break
        if not typ_h or typ_h <= 0:
            typ_h = 4000.0

    runs = []

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        s_tag = "Stair_{}".format(s_idx + 1)
        base_y = s_cy - enc_d / 2.0
        stair_base_x = s_cx - shaft_width_nat / 2.0

        # Flight centre X positions (inside the two halves of the shaft)
        f1_cx = stair_base_x + t + w_flight / 2.0
        f2_cx = stair_base_x + 2 * t + w_flight + w_flight / 2.0

        # Flight Y area (between main landing and mid landing)
        flight_y_start = base_y + t + w_landing
        flight_y_end = base_y + enc_d - t - w_landing

        # Landing X bounds (inner edges of enclosure)
        land_x_left = stair_base_x + t
        land_x_right = stair_base_x + shaft_width_nat - t

        # Main landing Y bounds (for intermediate landings between pairs)
        ml_y_start = base_y + t
        ml_y_end = base_y + t + w_landing

        # Risers per flight — identical for every floor, derived from typical
        rpf = _risers_per_flight_typical(typ_h, riser)

        # Create stairs for all level pairs including up to the roof.
        for i in range(len(levels_data) - 1):
            floor_height = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
            if floor_height <= 0:
                continue

            num_flights = _calc_num_flights(floor_height, typ_h, riser)
            num_pairs = num_flights // 2

            runs.append({
                "tag": "AI_{}_L{}_Run".format(s_tag, i + 1),
                "base_level_idx": i,
                "top_level_idx": i + 1,
                "num_flight_pairs": num_pairs,
                "risers_per_flight": rpf,
                "flight_1": {
                    "start": [f1_cx, flight_y_start],
                    "end":   [f1_cx, flight_y_end],
                },
                "flight_2": {
                    "start": [f2_cx, flight_y_start],
                    "end":   [f2_cx, flight_y_end],
                },
                "main_landing": {
                    "y_start": ml_y_start,
                    "y_end": ml_y_end,
                    "x_left": land_x_left,
                    "x_right": land_x_right,
                },
                "width_mm": w_flight,
            })

    return runs
