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

# BUILD_VERSION is read by _create_stair_runs to verify fresh code is loaded
_BUILD_VERSION = "v2026-04-11-FLOORRISER-FIX"

_WALL_THICKNESS = 200  # mm
_OVERRUN_HEIGHT = 5000  # mm — staircase overrun above roof (matches lift core)


# ─────────────────────────────────────────────────────────────────────
#  Flight count helper  [rule k]
# ─────────────────────────────────────────────────────────────────────

def _snap_risers(floor_height_mm, riser=150):
    """Return the number of risers for a given floor height.

    Uses floor() so the risers ALWAYS FIT within the floor height.
    The actual riser height will be floor_height / num_risers
    (usually within ±5mm of the target riser, which is code-compliant).
    Using ceil() caused stairs to OVERSHOOT the floor level.
    """
    return max(int(math.floor(round(floor_height_mm) / float(riser))), 1)


def _risers_per_flight_typical(typical_floor_height_mm, riser=150):
    """Return the number of risers per flight for a typical storey (2 flights)."""
    typical_risers = _snap_risers(typical_floor_height_mm, riser)
    return max(int(math.ceil(typical_risers / 2.0)), 1)


def _calc_num_flights(floor_height_mm, typical_floor_height_mm, riser=150, is_top_floor=False):
    """Return the number of flights (always even) for a given storey height."""
    if typical_floor_height_mm <= 0 or floor_height_mm <= 0:
        return 2

    # Roof level uses exactly 2 flights (1 wrap) even if very long,
    # because there are no bounding headroom floors above it.
    if is_top_floor:
        return 2

    rpf = _risers_per_flight_typical(typical_floor_height_mm, riser)
    actual_risers = _snap_risers(floor_height_mm, riser)

    # Number of flights needed so each flight has <= rpf risers
    num_flights = int(math.ceil(actual_risers / float(rpf)))
    if num_flights < 2:
        num_flights = 2
    if num_flights % 2 != 0:
        num_flights += 1

    return num_flights


def adjust_storey_height(floor_height_mm, typical_floor_height_mm, riser=150, is_top_floor=False):
    """Adjust a floor height so it produces an even number of risers (even flights)
    AND respects the 2400mm vertical headroom requirement for multi-wrap stairs.
    """
    if riser <= 0 or floor_height_mm <= 0:
        return floor_height_mm

    # Helper to check if a riser count violates the 2400mm headroom rule 
    def get_violation(test_risers):
        import math
        rpf_local = _risers_per_flight_typical(typical_floor_height_mm, riser)
        nf = int(math.ceil(test_risers / float(rpf_local)))
        if nf < 2: nf = 2
        if nf % 2 != 0: nf += 1
        clearance = (test_risers * riser) / (nf / 2.0)
        return clearance < 2400

    n_risers = int(round(floor_height_mm / float(riser)))
    if n_risers % 2 != 0:
        below = n_risers - 1
        above = n_risers + 1
        h_below = abs(floor_height_mm - below * riser)
        h_above = abs(floor_height_mm - above * riser)
        n_risers = below if h_below <= h_above else above

    if not is_top_floor and get_violation(n_risers):
        offset = 2
        while True:
            # Favor shorter if both equidistant, as it guarantees fitting inside shaft better
            if not get_violation(n_risers - offset):
                n_risers = n_risers - offset
                break
            if not get_violation(n_risers + offset):
                n_risers = n_risers + offset
                break
            offset += 2

    # Minimum 2 risers
    n_risers = max(2, n_risers)
    return n_risers * riser


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
    tread = spec.get("tread", 300)
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

    The shaft depth is FIXED at the typical floor's depth.  This ensures
    a constant vertical staircase core from ground to roof — the same
    rectangular footprint on every level.

    Taller storeys fit within this fixed shaft by using more flight pairs
    (each pair has the same run length as a typical floor).
    """
    typ_h = typical_floor_height_mm if typical_floor_height_mm and typical_floor_height_mm > 0 else 4000.0
    _, d = get_shaft_dimensions(typ_h, spec)
    return d


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
    t = 200
    w_flight = spec.get("width_of_flight", 1500) if spec else 1500
    enc_w = 2 * w_flight + 3 * t
    _, enc_d = get_shaft_dimensions(typical_floor_height_mm, spec)

    # Perimeter positions at edges of the LARGEST floor plate.
    # Smaller floors may not need these cores — the per-floor skip
    # in get_stair_run_data handles that based on travel distance.
    edge_x = max_w / 2.0 - enc_w / 2.0
    edge_y = max_l / 2.0 - enc_d / 2.0

    # 1/3 diagonal spacing rule for the largest floor plate
    diagonal_mm = math.sqrt(max_w**2 + max_l**2)
    min_spacing_mm = diagonal_mm / 3.0

    # Generate perimeter candidate points
    candidates = [
        # Corners (furthest from core)
        (-edge_x, -edge_y),
        ( edge_x, -edge_y),
        (-edge_x,  edge_y),
        ( edge_x,  edge_y),
        # Mid-edges
        (-edge_x, cy),
        ( edge_x, cy),
        ( cx, -edge_y),
        ( cx,  edge_y),
        # Quarter points along long edges
        (-edge_x, -max_l / 4.0),
        ( edge_x, -max_l / 4.0),
        (-edge_x,  max_l / 4.0),
        ( edge_x,  max_l / 4.0),
    ]

    # Iteratively add the candidate that is furthest from all existing staircases
    while candidates:
        best_cand = None
        best_min_dist = -1

        for cand in candidates:
            # Distance from this candidate to the closest existing staircase
            min_dist_to_existing = min(
                math.sqrt((cand[0] - sx)**2 + (cand[1] - sy)**2)
                for sx, sy in positions
            )
            if min_dist_to_existing > best_min_dist:
                best_min_dist = min_dist_to_existing
                best_cand = cand

        # Remove the evaluated best candidate
        candidates.remove(best_cand)

        # Apply the 1/3 diagonal spacing rule
        if best_min_dist < min_spacing_mm:
            continue

        # Add the best candidate if it satisfies spacing
        positions.append(best_cand)

        # Stop early if travel distance rule is satisfied
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
    t = _WALL_THICKNESS
    for cx, cy in positions:
        hw = enclosure_width_mm / 2.0 - t / 2.0
        hd = enclosure_depth_mm / 2.0 - t / 2.0
        voids.append((cx - hw, cy - hd, cx + hw, cy + hd))
    return voids


# ─────────────────────────────────────────────────────────────────────
#  Manifest generation  [rules a–k]
# ─────────────────────────────────────────────────────────────────────

def wall_overlaps_box(wall_start, wall_end, box_bounds, tol=100):
    """Check if a wall segment lies on a bounding box edge.

    Returns:
        None  — wall does NOT overlap, emit it as-is.
        []    — wall is FULLY covered by the box, skip it entirely.
        list  — gap-filler wall segments [(start, end), ...] for
                portions NOT covered by the box.
    """
    if box_bounds is None:
        return None
    lx_min, ly_min, lx_max, ly_max = box_bounds
    sx, sy = wall_start[0], wall_start[1]
    ex, ey = wall_end[0], wall_end[1]

    # Horizontal wall (constant Y)?
    if abs(sy - ey) < 1:
        y = sy
        w_xmin, w_xmax = min(sx, ex), max(sx, ex)
        # Check if this Y matches a box core horizontal boundary
        on_core_y = (abs(y - ly_min) < tol or abs(y - ly_max) < tol)
        if not on_core_y:
            return None
        # How much of the wall is covered by the box?
        overlap_xmin = max(w_xmin, lx_min)
        overlap_xmax = min(w_xmax, lx_max)
        if overlap_xmin >= overlap_xmax - 5: # 5mm minimum overlap to trigger split
            return None 
            
        gaps = []
        if w_xmin < overlap_xmin - 5:
            gaps.append(([w_xmin, y, 0], [overlap_xmin, y, 0]))
        if w_xmax > overlap_xmax + 5:
            gaps.append(([overlap_xmax, y, 0], [w_xmax, y, 0]))
        return gaps

    # Vertical wall (constant X)?
    elif abs(sx - ex) < 1:
        x = sx
        w_ymin, w_ymax = min(sy, ey), max(sy, ey)
        on_core_x = (abs(x - lx_min) < tol or abs(x - lx_max) < tol)
        if not on_core_x:
            return None
        overlap_ymin = max(w_ymin, ly_min)
        overlap_ymax = min(w_ymax, ly_max)
        if overlap_ymin >= overlap_ymax - 5:
            return None
            
        gaps = []
        if w_ymin < overlap_ymin - 5:
            gaps.append(([x, w_ymin, 0], [x, overlap_ymin, 0]))
        if w_ymax > overlap_ymax + 5:
            gaps.append(([x, overlap_ymax, 0], [x, w_ymax, 0]))
        return gaps

    return None

def _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm=None):
    """Internal helper to calculate stable base_y based on core proximity."""
    if s_idx == 0 and lift_core_bounds_mm:
        # South staircase: Back wall (base_y + enc_d) must be at core_ymin
        _, core_ymin, _, _ = lift_core_bounds_mm
        return core_ymin - enc_d
    elif s_idx == 1 and lift_core_bounds_mm:
        # North staircase: Front wall (base_y) must be at core_ymax
        _, _, _, core_ymax = lift_core_bounds_mm
        return core_ymax
    else:
        # Perimeter staircases: use the calculated centre (s_cy)
        return s_cy - enc_d / 2.0


def generate_staircase_manifest(positions, levels_data, _enclosure_width_mm=None,
                                spec=None, typical_floor_height_mm=None,
                                lift_core_bounds_mm=None, floor_dims_mm=None):
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
        lift_core_bounds_mm: (xmin, ymin, xmax, ymax) of lift core in mm,
                             or None.  When provided, staircase enclosure
                             walls that overlap with the lift core boundary
                             are omitted (the lift core wall already serves
                             as enclosure).  Gap-filling walls are created
                             if the lift core wall is shorter than the
                             staircase wall it replaces.

    Returns:
        ``{"walls": [...], "floors": [...]}``
    """
    spec = spec or {}
    riser = spec.get("riser", 150)
    tread = spec.get("tread", 300)
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
    landing_ext_for_next = {}  # level_idx -> (right_ext, y_shift)
    for li in range(len(levels_data) - 1):
        floor_height_mm = levels_data[li + 1]['elevation'] - levels_data[li]['elevation']
        if floor_height_mm <= 0:
            continue
            
        is_top_floor = (li == len(levels_data) - 2)

        total_r = _snap_risers(floor_height_mm, riser)
        nf = _calc_num_flights(floor_height_mm, typ_h, riser, is_top_floor=is_top_floor)
        
        # Simple floor-division: distribute risers evenly across flights
        if nf > 0:
            per_flight = total_r // nf
            remainder = total_r % nf
            flight_list = [per_flight + (1 if i < remainder else 0) for i in range(nf)]
        else:
            flight_list = [total_r]
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

    # --- Lift-core overlap detection ---
    # Tolerance for "same coordinate" comparison (mm).
    _TOL = 50  # 50mm — walls within this distance are considered overlapping

    def _wall_overlaps_lift_core(wall_start, wall_end, lift_bounds):
        """Check if a wall segment lies on a lift-core boundary.

        Returns:
            None  — wall does NOT overlap, emit it as-is.
            []    — wall is FULLY covered by lift core, skip it entirely.
            list  — gap-filler wall segments [(start, end), ...] for
                    portions NOT covered by the lift core.
        """
        if lift_bounds is None:
            return None
        lx_min, ly_min, lx_max, ly_max = lift_bounds
        sx, sy = wall_start[0], wall_start[1]
        ex, ey = wall_end[0], wall_end[1]

        # Horizontal wall (constant Y)?
        if abs(sy - ey) < 1:
            y = sy
            w_xmin, w_xmax = min(sx, ex), max(sx, ex)
            # Check if this Y matches a lift-core horizontal boundary
            on_core_y = (abs(y - ly_min) < _TOL or abs(y - ly_max) < _TOL)
            if not on_core_y:
                return None
            # Lift core horizontal extent
            # How much of the staircase wall is covered by the lift core?
            overlap_min = max(w_xmin, lx_min)
            overlap_max = min(w_xmax, lx_max)
            if overlap_max - overlap_min < _TOL:
                return None  # No meaningful overlap
            # Fully covered?
            if lx_min <= w_xmin + _TOL and lx_max >= w_xmax - _TOL:
                return []  # Fully covered — skip wall
            # Partially covered — return gap segments
            gaps = []
            if w_xmin < lx_min - _TOL:
                gaps.append(([w_xmin, y, 0], [lx_min, y, 0]))
            if w_xmax > lx_max + _TOL:
                gaps.append(([lx_max, y, 0], [w_xmax, y, 0]))
            return gaps

        # Vertical wall (constant X)?
        if abs(sx - ex) < 1:
            x = sx
            w_ymin, w_ymax = min(sy, ey), max(sy, ey)
            on_core_x = (abs(x - lx_min) < _TOL or abs(x - lx_max) < _TOL)
            if not on_core_x:
                return None
            overlap_min = max(w_ymin, ly_min)
            overlap_max = min(w_ymax, ly_max)
            if overlap_max - overlap_min < _TOL:
                return None
            if ly_min <= w_ymin + _TOL and ly_max >= w_ymax - _TOL:
                return []
            gaps = []
            if w_ymin < ly_min - _TOL:
                gaps.append(([x, w_ymin, 0], [x, ly_min, 0]))
            if w_ymax > ly_max + _TOL:
                gaps.append(([x, ly_max, 0], [x, w_ymax, 0]))
            return gaps

        return None  # Diagonal wall — never overlaps

    def _emit_wall(wall_id, start, end, common_dict):
        """Create a wall dict, filtering out zero-length segments."""
        dx = abs(start[0] - end[0])
        dy = abs(start[1] - end[1])
        if dx < 2 and dy < 2:
            return None  # Too short
        return {"id": wall_id, "start": start, "end": end, **common_dict}

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        s_tag = "Stair_{}".format(s_idx + 1)

        # --- Stable base coordinates [Alignment Fix] ---
        base_x = s_cx - enc_w / 2.0
        base_y = _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm)

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
            # Each wall is checked against the lift core boundary.  If the
            # lift core already provides enclosure at that location the
            # staircase wall is omitted (or replaced with gap-fillers).
            left_y_start = base_y
            left_y_end = base_y + enc_d
            right_y_start = base_y
            right_y_end = base_y + enc_d

            # Apply T-junction geometric pullback to prevent volume overlap
            # with the lift core walls when joins are disallowed.
            if lift_core_bounds_mm:
                _, core_ymin, _, core_ymax = lift_core_bounds_mm
                core_cy = (core_ymin + core_ymax) / 2.0
                if s_cy < core_cy - 100:
                    left_y_end -= t / 2.0
                    right_y_end -= t / 2.0
                elif s_cy > core_cy + 100:
                    left_y_start += t / 2.0
                    right_y_start += t / 2.0

            candidate_walls = [
                ("AI_{}_L{}_W_Front".format(s_tag, i + 1),
                 [base_x, base_y, 0],
                 [base_x + enc_w, base_y, 0]),
                ("AI_{}_L{}_W_Back".format(s_tag, i + 1),
                 [base_x, base_y + enc_d, 0],
                 [base_x + enc_w, base_y + enc_d, 0]),
                ("AI_{}_L{}_W_Left".format(s_tag, i + 1),
                 [base_x, left_y_start, 0],
                 [base_x, left_y_end, 0]),
                ("AI_{}_L{}_W_Right".format(s_tag, i + 1),
                 [base_x + enc_w, right_y_start, 0],
                 [base_x + enc_w, right_y_end, 0]),
            ]

            for w_id, w_start, w_end in candidate_walls:
                result = wall_overlaps_box(w_start, w_end, lift_core_bounds_mm, _TOL)
                if result is None:
                    walls.append({"id": w_id, "start": w_start, "end": w_end, **common})
                elif len(result) == 0:
                    pass
                else:
                    for gi, (gs, ge) in enumerate(result):
                        w = _emit_wall("{}_Gap{}".format(w_id, gi + 1), gs, ge, common)
                        if w: walls.append(w)

            # --- Main landing floor slab [rule j] ---
            if i > 0:
                lvl = levels_data[i]
                lvl_id = lvl.get('id', 'AI_Level_{}'.format(i+1))
                stair_base_x = s_cx - shaft_width_nat / 2.0
                div_x = stair_base_x + w_flight + t
                
                def _get_flight_arr(li):
                    # Returns (f_y_start, arr_left, arr_right) for level li+1
                    f_h = levels_data[li+1]['elevation'] - levels_data[li]['elevation']
                    if f_h <= 0: return base_y + t + w_landing, base_y + t + w_landing, base_y + t + w_landing
                    tot_r = _snap_risers(f_h, riser)
                    nf = _calc_num_flights(f_h, typical_floor_height_mm or 4000.0, riser)
                    if nf > 0:
                        p_f = tot_r // nf
                        rem = tot_r % nf
                        f_list = [p_f + (1 if k < rem else 0) for k in range(nf)]
                    else: f_list = [tot_r]
                    
                    trd = spec.get("tread", 300)
                    r_len = max(f_list[0] - 1, 1) * trd
                    dyn_w = max(w_landing, (enc_d - 2*t - r_len) / 2.0)
                    fy_s = base_y + t + dyn_w
                    
                    l_a = f_list[-2] if len(f_list) >= 2 else f_list[-1]
                    l_b = f_list[-1] if len(f_list) >= 2 and len(f_list) % 2 == 0 else 0
                    la_len = max(l_a - 1, 1) * trd
                    lb_len = max(l_b - 1, 1) * trd
                    
                    if len(f_list) % 2 != 0 or l_b == 0:
                        return fy_s, fy_s + la_len, fy_s
                    else:
                        # Safety: pull landing depth back 50mm from calculated arrival to avoid angled clashes
                        return fy_s, fy_s, fy_s + (la_len - lb_len) - 50

                # Landing at level i: 
                # Left side matches departing flight (i) or standard if roof
                # Right side matches arriving flight (i-1)
                fy_s_dep, _, _ = _get_flight_arr(i) if not is_roof else (base_y + t + w_landing, 0, 0)
                _, arr_l, arr_r = _get_flight_arr(i-1)
                
                req_l = max(fy_s_dep, arr_l)
                req_r = arr_r
                front_y = base_y + t
                
                if abs(req_l - req_r) < 1.0:
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id, "elevation": lvl['elevation'],
                        "points": [[base_x, front_y], [base_x + enc_w, front_y], [base_x + enc_w, req_r], [base_x, req_l]]
                    })
                else:
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id, "elevation": lvl['elevation'],
                        "points": [[base_x, front_y], [base_x + enc_w, front_y], [base_x + enc_w, req_r], [div_x, req_r], [div_x, req_l], [base_x, req_l]]
                    })

            # Center divider wall removed per user request — not necessary
            # for the dogleg stair layout.

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
                       typical_floor_height_mm=None, lift_core_bounds_mm=None,
                       floor_dims_mm=None):
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
                    "x_left": ..., "x_right": ...
                },
                "width_mm": 1500,
                "fingerprint": "HASH_OR_STRING"
            }
    """
    import hashlib
    def calc_fingerprint(data):
        # A unique string representing the geometry of this specific stair instance
        raw = "{}:{}:{}:{}:{}".format(
            data['tag'], 
            round(data['base_elev'], 1), 
            round(data['top_elev'], 1),
            data['num_flight_pairs'],
            data['width_mm']
        )
        return hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]

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
        heights = []
        for i in range(len(levels_data) - 1):
            fh = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
            if fh > 0:
                heights.append(fh)
        if heights:
            typ_h = min(heights)  # Grab the minimum height to identify the true typical floor
        if not typ_h or typ_h <= 0:
            typ_h = 4000.0
            

    runs = []

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        s_tag = "Stair_{}".format(s_idx + 1)
        base_y = _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm)
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

        # Create stairs for all level pairs including up to the roof.
        for i in range(len(levels_data) - 1):
            floor_height = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
            if floor_height <= 0:
                continue
            is_top_floor = (i == len(levels_data) - 2)
            num_flights = _calc_num_flights(floor_height, typ_h, riser, is_top_floor=is_top_floor)
            floor_risers = _snap_risers(floor_height, riser)
            
            # Ensure even distribution across flights without rounding up
            # SPECIAL CASE: For the top floor leading to roof, force the 1st flight
            # to match typical floor riser count so mid-landings ALIGN vertically.
            if is_top_floor and num_flights == 2 and typical_floor_height_mm > 0:
                rpf_typical = _risers_per_flight_typical(typical_floor_height_mm, riser)
                if 0 < rpf_typical < floor_risers:
                    flight_list = [rpf_typical, floor_risers - rpf_typical]
                else:
                    per_flight = floor_risers // num_flights
                    remainder = floor_risers % num_flights
                    flight_list = [per_flight + (1 if j < remainder else 0) for j in range(num_flights)]
            elif num_flights > 0:
                # floor_risers is already the right count (floor-based)
                # Just ensure it's evenly split across flights
                per_flight = floor_risers // num_flights
                # If not perfectly divisible, keep current split (some floors may have one extra)
                remainder = floor_risers % num_flights
                flight_list = [per_flight + (1 if j < remainder else 0) for j in range(num_flights)]
            else:
                flight_list = [floor_risers]
            
            # Compute actual riser height for use in Revit
            actual_riser_h = floor_height / float(floor_risers) if floor_risers else float(riser)
            
            num_pairs = len(flight_list) // 2

            intermediate_heights_mm = []
            curr_h = 0
            for pi in range(num_pairs - 1):
                pair_risers = flight_list[2*pi] + (flight_list[2*pi + 1] if 2*pi + 1 < len(flight_list) else 0)
                curr_h += pair_risers * riser
                intermediate_heights_mm.append(curr_h)

            tread = spec.get("tread", 300)
            target_risers = flight_list[0] if flight_list else 1
            run_len = max(target_risers - 1, 1) * tread
            shaft_inner_d = enc_d - 2 * t
            # Remaining space is split equally between main and mid landing
            dyn_landing_w = max(w_landing, (shaft_inner_d - run_len) / 2.0)
            
            flight_y_start = base_y + t + dyn_landing_w
            flight_y_end = base_y + enc_d - t - dyn_landing_w


            # Adjust main landing depth if f2 is longer than f1 (Problem 1)
            # This ensures Flight 2 terminates exactly at the landing edge without intersections.
            ml_y_bot = base_y + t
            ml_depth = w_landing
            if len(flight_list) >= 2 and flight_list[1] > flight_list[0]:
                extra_treads = flight_list[1] - flight_list[0]
                ml_depth += extra_treads * tread
            ml_y_top = ml_y_bot + ml_depth

            run_data = {
                "tag": "AI_{}_L{}_Run".format(s_tag, i + 1),
                "base_level_idx": i,
                "top_level_idx": i + 1,
                "base_elev": levels_data[i]['elevation'],
                "top_elev": levels_data[i+1]['elevation'],
                "num_flight_pairs": num_pairs,
                "_intermediate_count": max(0, num_pairs - 1),
                "intermediate_heights_mm": intermediate_heights_mm,
                "flight_list": flight_list,
                "risers_per_flight": flight_list[0] if flight_list else 1,
                "actual_riser_height_mm": actual_riser_h if 'actual_riser_h' in dir() else float(riser),
                "flight_1": {
                    "start": [f1_cx, flight_y_start],
                    "end":   [f1_cx, flight_y_end],
                },
                "flight_2": {
                    "start": [f2_cx, flight_y_end],
                    "end":   [f2_cx, flight_y_start],
                },
                "main_landing": {
                    "y_start": ml_y_bot,
                    "y_end": ml_y_top,
                    "x_left": land_x_left,
                    "x_right": land_x_right,
                },
                "width_mm": w_flight,
                "dyn_landing_w_mm": dyn_landing_w,
            }
            run_data["fingerprint"] = calc_fingerprint(run_data)
            runs.append(run_data)

    return runs
