# -*- coding: utf-8 -*-
import math
from . import staircase_logic
from .utils import safe_num

# ─────────────────────────────────────────────────────────────────────────────
#  Load compliance data from JSON files — single source of truth.
#  Falls back to hardcoded values if files are missing.
# ─────────────────────────────────────────────────────────────────────────────

def _load_fsc():
    try:
        import os, json
        d = os.path.dirname(os.path.abspath(__file__))
        fs_path = os.path.join(d, "compliance_fire_safety.json")
        st_path = os.path.join(d, "compliance_structural.json")
        c_fs = json.load(open(fs_path)) if os.path.exists(fs_path) else {}
        c_st = json.load(open(st_path)) if os.path.exists(st_path) else {}
        return c_fs, c_st
    except Exception:
        return {}, {}

_FSC, _STC      = _load_fsc()
_WALL_THICKNESS = _STC.get("wall_thickness_mm", {}).get("core_structural",  350)
_OVERRUN_HEIGHT = _FSC.get("staircase", {}).get("overrun_height_mm",       5000)
_FL_CAR_SIZE    = _FSC.get("fire_lift", {}).get("car_size_mm",             2500)
_FL_SHAFT_D     = _FL_CAR_SIZE + 2 * _WALL_THICKNESS
_MAX_TRAVEL     = _FSC.get("staircase", {}).get("max_travel_distance_mm", 60000)
_PERIMETER_C    = _FSC.get("perimeter_staircase", {})
_EDGE_GAP_MM    = _PERIMETER_C.get("edge_inset_gap_mm",                     500)
_SMOKE_CLEAR_D  = _PERIMETER_C.get("smoke_stop_lobby_clear_depth_mm",      2000)

from . import lift_logic

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _passenger_lift_row_centers(center_pos_mm, layout, lobby_width):
    """Return (row1_cy, row2_cy) in mm — the Y-centre of each passenger lift row.

    For a single-block, 2-row layout (>=4 lifts per block):
        row1 faces south, row2 faces north.
    For a single-row layout (<4 lifts): both entries = block centre Y.
    Multi-block layouts return the outermost rows of the entire assembly.
    """
    l = 2500  # default internal lift car depth
    shaft_depth = l + 2 * _WALL_THICKNESS  # 2900 mm

    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]
    cy = center_pos_mm[1]

    if lifts_per_block >= 4:
        # 2-row block; row offsets relative to each block centre
        r1_offset = -(shaft_depth + lobby_width / 2.0) + shaft_depth / 2.0   # row1 centre offset
        r2_offset =  (lobby_width / 2.0)               + shaft_depth / 2.0   # row2 centre offset

        # Block 0 = southernmost block
        b0_cy = cy + lift_logic.get_block_y_offset(0, num_blocks, block_d)
        # Block last = northernmost block
        bN_cy = cy + lift_logic.get_block_y_offset(num_blocks - 1, num_blocks, block_d)

        row1_cy = b0_cy + r1_offset   # southernmost row centre
        row2_cy = bN_cy + r2_offset   # northernmost row centre
    else:
        # Single row — both fire lifts align to the single row centre
        row1_cy = cy
        row2_cy = cy

    return row1_cy, row2_cy


def _fire_lift_shaft_walls(tag, cx_mm, cy_mm, fw_mm, fd_mm, levels_data, overrun_height=_OVERRUN_HEIGHT):
    """Generate walls + topcap for a single fire-fighting lift shaft."""
    walls = []
    floors = []
    hfw, hfd = fw_mm / 2.0, fd_mm / 2.0
    x1, x2 = cx_mm - hfw, cx_mm + hfw
    y1, y2 = cy_mm - hfd, cy_mm + hfd

    for l_idx, lvl in enumerate(levels_data):
        lvl_id, elev = lvl['id'], lvl['elevation']
        is_last = (l_idx == len(levels_data) - 1)
        h = overrun_height if is_last else (levels_data[l_idx + 1]['elevation'] - elev)
        if h <= 0:
            continue
        common = {"level_id": lvl_id, "height": h, "type": "AI_Wall_Core"}
        walls.append({"id": "AI_{}_S_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x2, y1, 0], **common})
        walls.append({"id": "AI_{}_N_L{}".format(tag, l_idx + 1), "start": [x1, y2, 0], "end": [x2, y2, 0], **common})
        walls.append({"id": "AI_{}_W_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x1, y2, 0], **common})
        walls.append({"id": "AI_{}_E_L{}".format(tag, l_idx + 1), "start": [x2, y1, 0], "end": [x2, y2, 0], **common})
        if is_last:
            cap_elev = elev + overrun_height
            floors.append({"id": "AI_{}_TOPCAP".format(tag), "level_id": lvl_id, "elevation": cap_elev,
                           "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]})
    return walls, floors


def _should_use_ew_orientation(_lift_core_bounds_mm, _sw_nat, _sd_nat,
                               orientation="AUTO", floor_dims=None):
    """Return True if EW orientation should be used for the core stack.

    EW layout: lift row runs N-S, stairs at E/W ends.
    NS layout: lift row runs E-W, stairs at N/S ends (default).

    orientation: "NS" / "EW" — explicit override from manifest.
                 "AUTO" — auto-select based on floor plate aspect ratio.
    """
    if orientation == "EW":
        return True
    if orientation == "NS":
        return False
    # AUTO: choose EW when the floor plate is significantly wider than deep
    if floor_dims:
        avg_w = sum(d[0] for d in floor_dims) / len(floor_dims)
        avg_l = sum(d[1] for d in floor_dims) / len(floor_dims)
        if avg_w > avg_l * 1.5:
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_polygon_edge_angle_deg(px, py, polygon_pts):
    """Return angle (degrees, 0=east, CCW) of the polygon edge nearest to (px, py),
    snapped to the nearest 90° so stair walls remain axis-aligned."""
    best_dist = float('inf')
    best_angle = 0.0
    n = len(polygon_pts)
    for i in range(n):
        x1, y1 = polygon_pts[i][0], polygon_pts[i][1]
        x2, y2 = polygon_pts[(i + 1) % n][0], polygon_pts[(i + 1) % n][1]
        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1.0:
            continue
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
        nx, ny = x1 + t * dx, y1 + t * dy
        dist = math.sqrt((px - nx) ** 2 + (py - ny) ** 2)
        if dist < best_dist:
            best_dist = dist
            best_angle = math.degrees(math.atan2(dy, dx))
    return round(best_angle / 90.0) * 90.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_fire_safety_requirements(floor_dims_mm, core_center_mm, lift_core_bounds_mm,
                                       typical_floor_height_mm, _preset_fs, num_lifts,
                                       lobby_width=3000, compliance_overrides=None,
                                       footprint_pts=None, footprint_holes=None,
                                       orientation="AUTO"):
    """Determine positions and types for fire safety cores.

    Returns list of dicts: {"pos": (x, y), "type": "FIRE_LIFT"|"SMOKE_STOP"}

    NS layout  — pos is the entry point at the lift-core Y boundary
                 (y = lift_ymin or lift_ymax, x = lift_core_cx).
    EW layout  — pos is the entry point at the lift-core X boundary
                 (x = lift_xmin or lift_xmax, y = lift_core_cy).

    compliance_overrides: dict of RAG-derived values that override the static
        module constants (e.g. {"max_travel_distance_mm": 60000, "fire_lift_car_size_mm": 2500}).
        Falls back to module constants for any key not present.
    """
    co = compliance_overrides or {}
    max_travel_dist = (co.get("max_travel_distance_sprinklered_mm")
                       or co.get("max_travel_distance_mm", _MAX_TRAVEL))
    fl_car_size     = co.get("fire_lift_car_size_mm",  _FL_CAR_SIZE)
    fl_shaft_d      = fl_car_size + 2 * _WALL_THICKNESS
    final_sets = []

    # Compute passenger lift core bounds if not provided
    if not lift_core_bounds_mm and num_lifts:
        layout = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width)
        lift_core_bounds_mm = (
            core_center_mm[0] - layout["total_w"] / 2.0,
            core_center_mm[1] - layout["total_d"] / 2.0,
            core_center_mm[0] + layout["total_w"] / 2.0,
            core_center_mm[1] + layout["total_d"] / 2.0
        )

    if not lift_core_bounds_mm:
        # No lifts - place at center
        final_sets.append({"pos": (core_center_mm[0], core_center_mm[1]), "type": "FIRE_LIFT"})
        return final_sets

    l_xmin, l_ymin, l_xmax, l_ymax = lift_core_bounds_mm
    lift_core_cx = (l_xmin + l_xmax) / 2.0
    lift_core_cy = (l_ymin + l_ymax) / 2.0

    # Determine whether EW or NS placement gives a more compact core
    _typ_h = typical_floor_height_mm or 4000
    sw_nat = staircase_logic.get_shaft_dimensions(_typ_h, None)[0]
    sd_nat = staircase_logic.get_shaft_dimensions(_typ_h, None)[1]
    use_ew = _should_use_ew_orientation(lift_core_bounds_mm, sw_nat, sd_nat,
                                        orientation=orientation, floor_dims=floor_dims_mm)

    if use_ew:
        # EW layout: fire lifts aligned with passenger lift rows so the passenger
        # lift lobby ends remain clear (Rule 1) and fire lifts are in-row (Rule 4).
        layout_ew = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
        if layout_ew and layout_ew["lifts_per_block"] >= 4:
            # 2-row block: east set at north-row centre, west set at south-row centre.
            # Row centres are half a shaft_depth inward from the lobby boundary.
            row_cy_s = lift_core_cy - lobby_width / 2.0 - fl_shaft_d / 2.0  # south row
            row_cy_n = lift_core_cy + lobby_width / 2.0 + fl_shaft_d / 2.0  # north row
            final_sets.append({"pos": (l_xmax, row_cy_n), "type": "FIRE_LIFT"})
            final_sets.append({"pos": (l_xmin, row_cy_s), "type": "FIRE_LIFT"})
        else:
            # Single row: both fire lifts centred on the single row (= lift_core_cy)
            final_sets.append({"pos": (l_xmax, lift_core_cy), "type": "FIRE_LIFT"})
            final_sets.append({"pos": (l_xmin, lift_core_cy), "type": "FIRE_LIFT"})
    else:
        # NS (default): one safety set at each Y-end of the passenger lift core
        final_sets.append({"pos": (lift_core_cx, l_ymin), "type": "FIRE_LIFT"})
        final_sets.append({"pos": (lift_core_cx, l_ymax), "type": "FIRE_LIFT"})

    # ── Compute actual staircase CENTRES for accurate 60m check ─────────────
    # NS parallel layout: each staircase is cluster_d_p/2 beyond the lift-core
    # entry point (l_ymin or l_ymax).  Using entry points directly makes the
    # check pessimistic and causes extra perimeter sets to be added.
    _lobby_d_est  = max(2000, sd_nat - fl_shaft_d)
    _cluster_d_est = fl_shaft_d + _lobby_d_est          # ≈ 7 300 mm for 4 000 mm floors
    stair_pos = []
    for s in final_sets:
        ex, ey = s["pos"]
        if ey <= l_ymin + 10:   # south entry
            stair_pos.append((ex, ey - _cluster_d_est / 2.0))
        else:                    # north entry
            stair_pos.append((ex, ey + _cluster_d_est / 2.0))

    if staircase_logic._check_travel_distance(stair_pos, floor_dims_mm, max_travel_dist,
                                               footprint_pts=footprint_pts):
        return final_sets

    # ── Perimeter SMOKE_STOP staircases (60m rule not yet satisfied) ──────────
    # For large floor plates (e.g. 80×100m) the central core alone cannot cover
    # all corners within 60m.  Add N-S oriented smoke-stop staircase/lobby sets
    # at the building perimeter until the rule is satisfied.
    # The "pos" stores (x, y_edge) where y_edge is the building boundary line.
    # generate_fire_safety_manifest detects is_perimeter=True and uses a
    # dedicated layout rather than the core-relative NS/EW branches.
    #
    # SMALLEST-FOOTPRINT-FIRST STRATEGY (SCDF-aware):
    # Try placing perimeter stairs aligned to the smallest floor plate first.
    # If that satisfies 60m for ALL floor dimensions, use it — stairs will sit
    # inside the core of the building on most floors, minimising exposure.
    # If not compliant for any floor, escalate to the next-smallest footprint,
    # repeating until all floors comply.  Only fall back to max footprint as a
    # last resort.  This mirrors SCDF Code Part IV cl. 5.3.3 intent.
    if floor_dims_mm:
        # Build a sorted list of unique half-lengths (ascending) to try as edge positions.
        unique_half_l = sorted(set(d[1] / 2.0 for d in floor_dims_mm))
    else:
        unique_half_l = [25000.0]  # 50 m default half-length

    # Approximate shaft depth to compute staircase centre from edge position.
    _sd_approx = staircase_logic.get_shaft_dimensions(_typ_h, None)[1]

    # For irregular polygons, pre-compute an 8×8 grid of interior candidate positions
    # so perimeter stairs always land inside the actual floor plate, not at bounding-box
    # edge midpoints that may be outside a Z/L/H/etc. shaped footprint.
    _poly_candidates = None
    if footprint_pts and len(footprint_pts) >= 3:
        _fp_xs = [p[0] for p in footprint_pts]
        _fp_ys = [p[1] for p in footprint_pts]
        _fp_xmin, _fp_xmax = min(_fp_xs), max(_fp_xs)
        _fp_ymin, _fp_ymax = min(_fp_ys), max(_fp_ys)
        _gN = 8
        _gdx = (_fp_xmax - _fp_xmin) / float(_gN)
        _gdy = (_fp_ymax - _fp_ymin) / float(_gN)
        _holes = footprint_holes or []
        _interior = []
        for _ix in range(_gN):
            for _iy in range(_gN):
                _gx = _fp_xmin + (_ix + 0.5) * _gdx
                _gy = _fp_ymin + (_iy + 0.5) * _gdy
                if not staircase_logic._point_in_polygon(_gx, _gy, footprint_pts):
                    continue
                if any(staircase_logic._point_in_polygon(_gx, _gy, h) for h in _holes):
                    continue
                _interior.append((_gx, _gy))
        if _interior:
            _poly_candidates = _interior

    # Greedy: always pick the candidate furthest from ALL existing staircases.
    all_stair_pos = list(stair_pos)

    # Try each footprint from smallest to largest until 60m rule is satisfied.
    for try_hl in unique_half_l:
        if staircase_logic._check_travel_distance(all_stair_pos, floor_dims_mm, max_travel_dist,
                                                   footprint_pts=footprint_pts,
                                                   footprint_holes=footprint_holes):
            break  # already compliant from central stairs alone

        # Build candidates: polygon-interior grid points (irregular shapes) or
        # bounding-box edge midpoints (rectangular shapes).
        if _poly_candidates is not None:
            perim_candidates = list(_poly_candidates)
        else:
            perim_candidates = [
                (core_center_mm[0], -try_hl + _sd_approx / 2.0),   # south edge centre
                (core_center_mm[0],  try_hl - _sd_approx / 2.0),   # north edge centre
            ]

        # Add the best candidate from this footprint tier.
        while perim_candidates:
            if staircase_logic._check_travel_distance(all_stair_pos, floor_dims_mm, max_travel_dist,
                                                       footprint_pts=footprint_pts,
                                                       footprint_holes=footprint_holes):
                break
            best = max(perim_candidates, key=lambda c: min(
                math.sqrt((c[0] - sx) ** 2 + (c[1] - sy) ** 2)
                for sx, sy in all_stair_pos
            ))
            perim_candidates.remove(best)
            # Derive building-edge Y from staircase centre Y (used for is_south_p orientation)
            y_edge = best[1] - _sd_approx / 2.0 if best[1] < 0 else best[1] + _sd_approx / 2.0
            _perim_rot = 0.0
            if footprint_pts and len(footprint_pts) >= 3:
                _perim_rot = _nearest_polygon_edge_angle_deg(best[0], best[1], footprint_pts)
            final_sets.append({"pos": (best[0], y_edge), "type": "SMOKE_STOP",
                                "is_perimeter": True,
                                "ref_half_l": try_hl,
                                "rotation_deg": _perim_rot})
            all_stair_pos.append(best)

    # Tag each perimeter staircase with the highest 0-based floor index where it
    # is still required.  Above that index the central core stairs alone satisfy
    # the travel-distance rule, so the extra shaft can terminate early.
    if floor_dims_mm:
        for _fs in final_sets:
            if _fs.get("is_perimeter"):
                _last = 0
                for _k in range(len(floor_dims_mm) - 1, -1, -1):
                    if not staircase_logic._check_travel_distance(
                            stair_pos, [floor_dims_mm[_k]], max_travel_dist,
                            footprint_pts=footprint_pts,
                            footprint_holes=footprint_holes):
                        _last = _k
                        break
                _fs["last_floor_idx"] = _last

    return final_sets


def generate_fire_safety_manifest(safety_sets, levels_data, stair_spec,
                                  typical_floor_height_mm, _preset_fs,
                                  lift_core_bounds_mm=None, num_lifts=None,
                                  lobby_width=3000, all_floor_dims=None,
                                  compliance_overrides=None, footprint_pts=None, **kwargs):
    """Generate manifest for fire lifts, lobbies and staircases.

    Supports two layout orientations, chosen automatically in
    calculate_fire_safety_requirements() based on which gives the smaller
    aspect ratio for the combined central core:

    NS layout (default, fire lift stacked above/below passenger lift in Y):
        [S-Stair] → [S-Lobby] → [S-FireLift] → [PassengerLifts] → [N-FireLift] → [N-Lobby] → [N-Stair]

    EW layout (fire lift adjacent to passenger lift in X — more compact for
    narrower lift banks):
        [Stair_W] [Lobby_W] [FireLift_W] [PassengerLifts] [FireLift_E] [Lobby_E] [Stair_E]
        Staircase runs N-S (normal), centred on the lift-core Y centre.

    compliance_overrides: dict of RAG-derived values that override static module constants.
    """
    co = compliance_overrides or {}
    fl_car_size     = co.get("fire_lift_car_size_mm",   _FL_CAR_SIZE)
    fl_shaft_d      = fl_car_size + 2 * _WALL_THICKNESS
    overrun_height  = co.get("overrun_height_mm",       _OVERRUN_HEIGHT)
    fire_lb_area    = co.get("fire_lobby_min_area_mm2", _FSC.get("fire_lift_lobby",  {}).get("min_area_mm2",   6000000))
    smoke_clear_d   = co.get("smoke_lobby_min_depth_mm",_SMOKE_CLEAR_D)

    walls = []
    floors = []
    voids = []
    core_bounds = []
    stair_centers = []
    door_specs = []

    # Level lists for door spec generation (exclude final roof/overrun level)
    _all_lvl_ids = [lvl['id'] for lvl in levels_data[:-1]] if len(levels_data) > 1 else [levels_data[0]['id']] if levels_data else []
    _first_lvl_id = _all_lvl_ids[0] if _all_lvl_ids else None

    sw_nat = staircase_logic.get_shaft_dimensions(typical_floor_height_mm, stair_spec)[0]
    sd_nat = staircase_logic.get_max_shaft_depth(levels_data, stair_spec, typical_floor_height_mm)
    # NS layout also needs total_set_d
    _, total_set_d = staircase_logic.get_safety_set_dimensions(typical_floor_height_mm, stair_spec, True, levels_data=levels_data, compliance_overrides=co)

    l_xmin, l_ymin, l_xmax, l_ymax = lift_core_bounds_mm if lift_core_bounds_mm else (0, 0, 0, 0)
    lift_core_cy = (l_ymin + l_ymax) / 2.0

    layout = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
    row1_cy, row2_cy = _passenger_lift_row_centers((0, 0), layout, lobby_width) if layout else (0, 0)

    stair_overrides = []
    sub_boundaries = []
    stair_global_idx = 0  # incremented for every staircase across all safety sets

    t = _WALL_THICKNESS

    # Pre-compute EW dimensions (Rule 3: fire lift shaft = passenger lift shaft = fl_shaft_d)
    ew_fl_dx  = fl_shaft_d                                              # EW fire-lift X extent
    lb_net_y  = fl_shaft_d - 2 * t                                     # lobby internal Y
    ew_lb_dx  = max(2000, int(math.ceil(fire_lb_area / lb_net_y)))     # EW lobby X extent (≥ min area)

    for i, s_set in enumerate(safety_sets):
        is_fl      = (s_set["type"] == "FIRE_LIFT")
        entry_x, entry_y = s_set["pos"]
        tag        = "SafetySet_{}".format(i + 1)
        _skip_set  = False  # set True when cluster can't fit on either side

        # ── Perimeter SMOKE_STOP — dedicated layout (building edge, no fire lift) ──
        is_perimeter = s_set.get("is_perimeter", False)
        if is_perimeter:
            # Simple layout: staircase at building edge, lobby between staircase and floor plate.
            _rot_deg = s_set.get("rotation_deg", 0.0)
            _is_ew_edge = abs(abs(_rot_deg) - 90.0) < 1.0  # ±90° → EW (east/west) face

            # Truncate levels to only those where this perimeter staircase is needed.
            # "last_floor_idx" is the 0-based index (in all_floor_dims) of the highest
            # floor that requires this stair.  We include one extra level as the
            # overrun/cap so the shaft terminates cleanly at that storey.
            _last_fi = s_set.get("last_floor_idx")
            if (_last_fi is not None and all_floor_dims and
                    _last_fi + 2 < len(levels_data)):
                _set_levels = levels_data[:_last_fi + 2]
            else:
                _set_levels = levels_data
            _set_lvl_ids = [lvl['id'] for lvl in _set_levels[:-1]] if len(_set_levels) > 1 else ([_set_levels[0]['id']] if _set_levels else [])
            # Smoke-stop lobby: min 2000mm clear width (= sw_nat - 2t) already
            # satisfied by the staircase width.  Depth: 2000mm clear = 2400mm outer.
            # Target ~4-5 sqm net: 2000mm clear × (sw_nat - 2t) already large enough.
            lobby_d_p  = 2 * _WALL_THICKNESS + smoke_clear_d  # outer = clear depth + 2 walls
            fl_box = None  # no fire lift

            # Inset the staircase from the building edge so the floor-slab void does
            # not coincide with the slab boundary (which causes "can't make extrusion"
            # errors in Revit).  500 mm is enough to clear the 200 mm wall thickness
            # plus Revit's minimum-face-width tolerance.
            _EDGE_GAP = _EDGE_GAP_MM

            if _is_ew_edge:
                # EW-facing stair: width along Y, depth along X
                # entry_x = edge X coordinate, entry_y = lateral Y position
                is_east_p = (entry_x >= 0)
                if is_east_p:
                    st_x2 = entry_x - _EDGE_GAP
                    st_x1 = st_x2 - sd_nat
                    lb_x1 = st_x1 - lobby_d_p
                    lb_x2 = st_x1
                    is_rotated_suit = False
                else:
                    st_x1 = entry_x + _EDGE_GAP
                    st_x2 = st_x1 + sd_nat
                    lb_x1 = st_x2
                    lb_x2 = st_x2 + lobby_d_p
                    is_rotated_suit = True
                st_y1 = entry_y - sw_nat / 2.0
                st_y2_ew = entry_y + sw_nat / 2.0
                lb_box = [lb_x1, st_y1, lb_x2, st_y2_ew]
                st_box = [st_x1, st_y1, st_x2, st_y2_ew]
                st_cx  = (st_x1 + st_x2) / 2.0
                st_cy  = entry_y
                st_rect = [min(st_x1, lb_x1), st_y1, max(st_x2, lb_x2), st_y2_ew]
                st_base_y  = st_y1        # min Y of EW staircase — needed by shared code below
                st_y2      = st_y2_ew     # max Y alias for door-spec block below
                is_south_p = False        # EW: use "not south" door-orientation path
            else:
                # NS-facing stair: entry_y is building-edge Y (negative=south, positive=north)
                is_south_p = (entry_y <= 0)
                if is_south_p:
                    # Staircase at south edge (inset), lobby north of it (facing floor plate)
                    st_base_y  = entry_y + _EDGE_GAP
                    st_y2      = st_base_y + sd_nat
                    lb_y1, lb_y2 = st_y2, st_y2 + lobby_d_p
                    is_rotated_suit = True   # people enter staircase from north (floor-plate) face
                else:
                    # Staircase at north edge (inset), lobby south of it (facing floor plate)
                    st_y2      = entry_y - _EDGE_GAP
                    st_base_y  = st_y2 - sd_nat
                    lb_y1, lb_y2 = st_base_y - lobby_d_p, st_base_y
                    is_rotated_suit = False  # people enter staircase from south (floor-plate) face

                st_x1 = entry_x - sw_nat / 2.0
                st_x2 = entry_x + sw_nat / 2.0
                lb_box = [st_x1, lb_y1, st_x2, lb_y2]
                st_box = [st_x1, st_base_y, st_x2, st_y2]
                st_cx  = entry_x
                st_cy  = (st_base_y + st_y2) / 2.0
                st_rect = [st_x1, min(st_base_y, lb_y1), st_x2, max(st_y2, lb_y2)]

            sub_boundaries.append(None)  # no shaft
            sub_boundaries.append({"id": tag + "_Lobby", "rect": lb_box})
            sub_boundaries.append({"id": tag + "_Staircase", "rect": st_box})
            stair_overrides.append(st_base_y)
            core_bounds.append(st_rect)
            stair_centers.append((st_cx, st_cy, is_rotated_suit))

            # Lobby walls — only up to the last floor where this stair is needed
            lobby_tag  = tag + "_LB"
            lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
            lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
            for l_idx, lvl in enumerate(_set_levels):
                is_last_lvl = (l_idx == len(_set_levels) - 1)
                if is_last_lvl:
                    lvl_h = overrun_height
                else:
                    lvl_h = _set_levels[l_idx + 1]['elevation'] - lvl['elevation']
                    if lvl_h <= 0:
                        continue
                common = {"level_id": lvl['id'], "height": lvl_h, "type": "AI_Wall_Core"}
                # Skip lobby face shared with staircase W_Back/W_Front to avoid duplicate wall
                _perim_skip = "S" if is_south_p else "N"
                walls.append({"id": "AI_{}_W_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x1_w, lb_y2_w, 0], **common})
                walls.append({"id": "AI_{}_E_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x2_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
                if _perim_skip != "N":
                    walls.append({"id": "AI_{}_N_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x1_w, lb_y2_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
                if _perim_skip != "S":
                    walls.append({"id": "AI_{}_S_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y1_w, 0], **common})
            if _set_levels:
                last_lvl = _set_levels[-1]
                floors.append({
                    "id": "AI_{}_TOPCAP".format(lobby_tag),
                    "level_id": last_lvl['id'],
                    "elevation": last_lvl['elevation'] + overrun_height,
                    "points": [[lb_x1_w, lb_y1_w], [lb_x2_w, lb_y1_w], [lb_x2_w, lb_y2_w], [lb_x1_w, lb_y2_w]]
                })

            # Staircase manifest — truncated to the same level range
            st_man = staircase_logic.generate_staircase_manifest(
                [(st_cx, st_cy)], _set_levels, sw_nat, stair_spec, typical_floor_height_mm,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                compliance_overrides=co,
                base_y_override=st_base_y, rotated_indices=([0] if is_rotated_suit else []),
                stair_idx_offset=stair_global_idx
            )
            stair_global_idx += 1
            walls.extend(st_man.get("walls", []))
            floors.extend(st_man.get("floors", []))
            voids.extend(staircase_logic.get_void_rectangles_mm(
                [(st_cx, st_cy)], sw_nat, sd_nat,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                base_y_override=st_base_y,
                rotated_indices=([0] if is_rotated_suit else [])
            ))

            # ── Door specs for perimeter smoke-stop ─────────────────────────
            # stair_global_idx already incremented above → stair_num = stair_global_idx
            # Use _set_lvl_ids (truncated) so doors only appear on floors the stair serves.
            _sn_p = stair_global_idx
            _set_first_id = _set_lvl_ids[0] if _set_lvl_ids else None
            if is_south_p:
                # Staircase main landing faces NORTH (is_rotated=True), external door on SOUTH wall
                _ext_y = st_base_y          # south wall of staircase = W_Front
                _lby_conn = st_y2           # shared wall between staircase and lobby = W_Back
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_set_first_id] if _set_first_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_set_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_p)} if _set_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_Stair_{}_L{}_W_Back".format(_sn_p, k + 1) for k in range(len(_set_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y2],
                    "wall_line_mm": [[lb_box[0], lb_y2], [lb_box[2], lb_y2]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_SafetySet_{}_LB_N_L{}".format(i + 1, k + 1) for k in range(len(_set_lvl_ids))},
                })
            else:
                # Main landing faces SOUTH (is_rotated=False), external door on NORTH wall
                _ext_y = st_y2              # north wall of staircase = W_Back
                _lby_conn = st_base_y       # shared wall between staircase and lobby = W_Front
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_set_first_id] if _set_first_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_set_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_p)} if _set_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_Stair_{}_L{}_W_Front".format(_sn_p, k + 1) for k in range(len(_set_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y1],
                    "wall_line_mm": [[lb_box[0], lb_y1], [lb_box[2], lb_y1]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_SafetySet_{}_LB_S_L{}".format(i + 1, k + 1) for k in range(len(_set_lvl_ids))},
                })
            continue  # skip the normal orientation detection below

        # ── Orientation detection ────────────────────────────────────────────
        # EW entry: entry_x is at l_xmax or l_xmin; entry_y is near lift_core_cy
        _lift_cy_tol = (l_ymax - l_ymin) / 2.0 + 10
        is_east_ew = lift_core_bounds_mm and (entry_x >= l_xmax - 10) and (abs(entry_y - lift_core_cy) <= _lift_cy_tol)
        is_west_ew = lift_core_bounds_mm and (entry_x <= l_xmin + 10) and (abs(entry_y - lift_core_cy) <= _lift_cy_tol)
        is_ew      = is_east_ew or is_west_ew

        # NS orientation: entry_y at l_ymin (south) or l_ymax (north)
        is_south    = (not is_ew) and (entry_y <= l_ymin + 10)
        use_parallel = False  # set True only in NS parallel branch

        if is_ew:
            # ── EW layout (row-aligned, compact rectangular block) ───────────
            # Fire lift + lobby extend outward in X from the passenger lift row
            # end.  The staircase extends in Y (N or S) centred on the COMBINED
            # fire-lift + lobby X span so the entire set forms a compact
            # rectangle rather than an L-shape.
            fl_dx  = ew_fl_dx if is_fl else 0
            fl_Y_h = fl_shaft_d / 2.0  # fire-lift Y half-extent = pax lift shaft (Rule 3)

            if is_east_ew:
                fl_x1, fl_x2 = l_xmax,           l_xmax + fl_dx
                lb_x1, lb_x2 = l_xmax + fl_dx,   l_xmax + fl_dx + ew_lb_dx
            else:  # west
                fl_x1, fl_x2 = l_xmin - fl_dx,              l_xmin
                lb_x1, lb_x2 = l_xmin - fl_dx - ew_lb_dx,   l_xmin - fl_dx

            fl_box = [fl_x1, entry_y - fl_Y_h, fl_x2, entry_y + fl_Y_h] if is_fl else None
            lb_box = [lb_x1, entry_y - fl_Y_h, lb_x2, entry_y + fl_Y_h]

            # ── Compact rectangle fix ─────────────────────────────────────────
            # Centre the staircase on the COMBINED fire-lift + lobby X extent.
            # If the staircase is wider than FL+LB, extend the lobby so it still
            # fits — preventing any protrusion that would create an L-shape.
            combined_x1 = fl_x1 if is_east_ew else lb_x1
            combined_x2 = lb_x2 if is_east_ew else fl_x2
            combined_span = combined_x2 - combined_x1  # fl_dx + ew_lb_dx

            if combined_span < sw_nat:
                # Widen the lobby just enough so staircase sits entirely inside
                extra = sw_nat - combined_span
                if is_east_ew:
                    lb_x2 += extra
                    combined_x2 = lb_x2
                else:
                    lb_x1 -= extra
                    combined_x1 = lb_x1
                lb_box = [lb_x1, entry_y - fl_Y_h, lb_x2, entry_y + fl_Y_h]

            combined_cx = (combined_x1 + combined_x2) / 2.0
            st_x1_c = combined_cx - sw_nat / 2.0
            st_x2_c = combined_cx + sw_nat / 2.0

            if entry_y >= lift_core_cy:
                # North-side fire lift → staircase extends north from FL+LB north face.
                # Standard (non-rotated): main landing at staircase south = lobby north ✓
                st_y1 = entry_y + fl_Y_h
                st_y2 = entry_y + fl_Y_h + sd_nat
                is_rotated_suit = False
            else:
                # South-side fire lift → staircase extends south from FL+LB south face.
                # Rotated: main landing at staircase north = lobby south ✓
                st_y1 = entry_y - fl_Y_h - sd_nat
                st_y2 = entry_y - fl_Y_h
                is_rotated_suit = True

            st_box    = [st_x1_c, st_y1, st_x2_c, st_y2]
            st_cx     = (st_x1_c + st_x2_c) / 2.0
            st_cy     = (st_y1 + st_y2) / 2.0
            st_base_y = st_y1

            # Bounding rect: proper rectangle (staircase X contained within FL+LB X)
            set_x1 = combined_x1
            set_x2 = combined_x2
            set_y1 = min(entry_y - fl_Y_h, st_y1)
            set_y2 = max(entry_y + fl_Y_h, st_y2)
            st_rect = [set_x1, set_y1, set_x2, set_y2]

        else:
            # ── NS PARALLEL layout ───────────────────────────────────────────
            # Staircase and fire zone (shaft + lobby) placed SIDE-BY-SIDE,
            # both extending the same cluster depth away from the lift bank.
            #
            # Plan view (south cluster example):
            #
            #   lift bank Y face (l_ymin)
            #   ┌──────────────────────┬───────────────┐
            #   │   Fire Lift Shaft    │               │
            #   │   (inner, 2900mm D)  │  Staircase    │
            #   ├──────────────────────┤  (sw_nat wide)│
            #   │   Fire Lift Lobby    │               │
            #   │   (outer, 2000mm D)  │               │
            #   └──────────────────────┴───────────────┘
            #   (open floor-plate side — lobby accessed from here)
            #
            # Pinwheel arrangement:
            #   South → fire zone WEST, staircase EAST
            #   North → fire zone EAST, staircase WEST
            #
            # Fire zone width = fl_shaft_d; lobby same width.
            # Cluster depth   = fl_shaft_d + lobby_d (= fl_shaft_d + max(2000, remaining))
            # Total X         = fl_shaft_d + sw_nat  (must fit inside lift bank)
            # Fallback to sequential stack if lift bank too narrow.

            lift_bank_w  = l_xmax - l_xmin
            fz_w         = fl_shaft_d                             # fire-zone X width = shaft width
            total_pair_w = fz_w + sw_nat                          # total X needed
            use_parallel = (total_pair_w <= lift_bank_w + 1)      # fits with 1mm tolerance

            # Cluster depth: shaft (inner) + lobby (outer).
            # Lobby must satisfy minimum depth from RAG AND minimum area from RAG.
            fl_shaft_d_p = fl_shaft_d
            _fire_lb_min_d = co.get("fire_lobby_min_depth_mm", 2000)
            _lb_net_w_p    = fl_shaft_d - 2 * t         # lobby net width (shaft width - 2 walls)
            _lb_d_from_area = int(math.ceil(fire_lb_area / max(_lb_net_w_p, 1))) + t
            lobby_d_p    = max(_fire_lb_min_d, _lb_d_from_area, sd_nat - fl_shaft_d_p)
            cluster_d_p  = fl_shaft_d_p + lobby_d_p

            if not use_parallel:
                # ── Fallback: original sequential NS stack ───────────────────
                fire_lift_d = fl_shaft_d if is_fl else 0
                lobby_d     = total_set_d - sd_nat - fire_lift_d
                if not is_fl:
                    lobby_d = total_set_d - sd_nat
                sw_h  = sw_nat / 2.0
                fl_hw = fl_shaft_d / 2.0
                st_cx = entry_x
                if is_south:
                    fl_box    = [entry_x - fl_hw, entry_y - fire_lift_d, entry_x + fl_hw, entry_y] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d, entry_x + sw_h, entry_y - fire_lift_d]
                    st_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d - sd_nat, entry_x + sw_h, entry_y - fire_lift_d - lobby_d]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, st_box[1], entry_x + sw_h, entry_y]
                    # Polygon check: if south cluster falls outside floor plate (e.g. U-shape notch) flip north
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_sq
                        _sq_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _sq_corners = [pt for b in _sq_all
                                       for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_corners):
                            is_south = False
                            _ny = l_ymax
                            fl_box    = [entry_x - fl_hw, _ny, entry_x + fl_hw, _ny + fire_lift_d] if is_fl else None
                            lb_box    = [entry_x - sw_h, _ny + fire_lift_d, entry_x + sw_h, _ny + fire_lift_d + lobby_d]
                            st_box    = [entry_x - sw_h, _ny + fire_lift_d + lobby_d, entry_x + sw_h, _ny + fire_lift_d + lobby_d + sd_nat]
                            st_cy     = (st_box[1] + st_box[3]) / 2.0
                            st_base_y = st_box[1]
                            st_rect   = [entry_x - sw_h, _ny, entry_x + sw_h, st_box[3]]
                            # Both sides failed — suppress this safety set
                            _sq_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                            _sq_n_corners = [pt for b in _sq_n_all
                                             for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                            if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_n_corners):
                                _skip_set = True
                else:
                    fl_box    = [entry_x - fl_hw, entry_y, entry_x + fl_hw, entry_y + fire_lift_d] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y + fire_lift_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d]
                    st_box    = [entry_x - sw_h, entry_y + fire_lift_d + lobby_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d + sd_nat]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, entry_y, entry_x + sw_h, st_box[3]]
                    # Polygon check: if north cluster also outside floor plate
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_sq
                        _sq_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _sq_n_corners = [pt for b in _sq_n_all
                                         for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_n_corners):
                            _skip_set = True
                is_rotated_suit = is_south
                # ── Door specs: NS sequential ────────────────────────────────
                # stair_global_idx not yet incremented → stair_num = stair_global_idx + 1
                _sn_seq = stair_global_idx + 1
                if is_south:
                    # Staircase main landing faces NORTH (is_rotated=True)
                    # ExtDoor on south face (W_Front); LobbyDoor on north face (W_Back)
                    # Doors tucked 900mm from west end (= t + half-door + 50mm clearance)
                    # FireLiftDoor centered on fire shaft (issue 1)
                    _fl_cx_s = (fl_box[0] + fl_box[2]) / 2.0 if fl_box else (lb_box[0] + lb_box[2]) / 2.0
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_box[0] + 900, st_box[1]],
                        "wall_line_mm": [[st_box[0], st_box[1]], [st_box[2], st_box[1]]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_seq)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_box[0] + 900, st_box[3]],
                        "wall_line_mm": [[st_box[0], st_box[3]], [st_box[2], st_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Back".format(_sn_seq, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    # Lobby entry from office floor: on EAST wall (side accessible from open floor plate)
                    # tucked 900mm from south end so hinge is near the staircase perpendicular wall
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [lb_box[2], lb_box[1] + 900],
                        "wall_line_mm": [[lb_box[2], lb_box[1]], [lb_box[2], lb_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_E_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx_s, fl_box[1]],
                            "wall_line_mm": [[fl_box[0], fl_box[1]], [fl_box[2], fl_box[1]]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
                else:
                    # Main landing faces SOUTH (is_rotated=False)
                    # ExtDoor on north face (W_Back); LobbyDoor on south face (W_Front)
                    _fl_cx_n = (fl_box[0] + fl_box[2]) / 2.0 if fl_box else (lb_box[0] + lb_box[2]) / 2.0
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_box[0] + 900, st_box[3]],
                        "wall_line_mm": [[st_box[0], st_box[3]], [st_box[2], st_box[3]]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_seq)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_box[0] + 900, st_box[1]],
                        "wall_line_mm": [[st_box[0], st_box[1]], [st_box[2], st_box[1]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Front".format(_sn_seq, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    # Lobby entry from office floor: on EAST wall (side accessible from open floor plate)
                    # tucked 900mm from north end so hinge is near the staircase perpendicular wall
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [lb_box[2], lb_box[3] - 900],
                        "wall_line_mm": [[lb_box[2], lb_box[1]], [lb_box[2], lb_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_E_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx_n, fl_box[3]],
                            "wall_line_mm": [[fl_box[0], fl_box[3]], [fl_box[2], fl_box[3]]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
            else:
                # ── Parallel layout ──────────────────────────────────────────
                use_parallel = True
                if is_south:
                    # South cluster: extends southward from l_ymin
                    # Fire zone WEST, staircase EAST (pinwheel)
                    fz_x1 = l_xmin
                    fz_x2 = l_xmin + fz_w
                    st_x1 = fz_x2
                    st_x2 = fz_x2 + sw_nat

                    # Fire shaft at inner Y (adjacent to lift bank), lobby at outer Y
                    fl_y1 = l_ymin - fl_shaft_d_p;  fl_y2 = l_ymin
                    lb_y1 = l_ymin - cluster_d_p;   lb_y2 = fl_y1
                    fl_box = [fz_x1, fl_y1, fz_x2, fl_y2] if is_fl else None
                    lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]

                    st_box    = [st_x1, l_ymin - cluster_d_p, st_x2, l_ymin]
                    st_cx     = (st_x1 + st_x2) / 2.0
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]     # south face = outer (floor plate side)
                    is_rotated_suit = False   # people enter from south face, flights go north
                    # Use full lift bank width so the column grid excludes the entire
                    # cluster zone, including any gap between the staircase east edge
                    # and the lift core east wall (l_xmax).
                    st_rect   = [l_xmin, l_ymin - cluster_d_p, l_xmax, l_ymin]
                    # Polygon check: if south cluster falls outside floor plate (e.g. U/H notch) flip north
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_p
                        _p_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _p_corners = [pt for b in _p_all
                                      for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_corners):
                            is_south = False
                            st_x1 = l_xmin;             st_x2 = l_xmin + sw_nat
                            fz_x1 = st_x2;              fz_x2 = fz_x1 + fz_w
                            fl_y1 = l_ymax;              fl_y2 = l_ymax + fl_shaft_d_p
                            lb_y1 = fl_y2;               lb_y2 = l_ymax + cluster_d_p
                            fl_box = [fz_x1, fl_y1, fz_x2, fl_y2] if is_fl else None
                            lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]
                            st_box    = [st_x1, l_ymax, st_x2, l_ymax + cluster_d_p]
                            st_cx     = (st_x1 + st_x2) / 2.0
                            st_cy     = (st_box[1] + st_box[3]) / 2.0
                            st_base_y = st_box[1]
                            is_rotated_suit = True
                            st_rect   = [l_xmin, l_ymax, l_xmax, l_ymax + cluster_d_p]
                            # Both sides failed — suppress this safety set
                            _p_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                            _p_n_corners = [pt for b in _p_n_all
                                            for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                            if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_n_corners):
                                _skip_set = True
                else:
                    # North cluster: extends northward from l_ymax
                    # Fire zone EAST, staircase WEST (pinwheel)
                    st_x1 = l_xmin
                    st_x2 = l_xmin + sw_nat
                    fz_x1 = st_x2
                    fz_x2 = fz_x1 + fz_w

                    # Fire shaft at inner Y (adjacent to lift bank), lobby at outer Y
                    fl_y1 = l_ymax;              fl_y2 = l_ymax + fl_shaft_d_p
                    lb_y1 = fl_y2;               lb_y2 = l_ymax + cluster_d_p
                    fl_box = [fz_x1, fl_y1, fz_x2, fl_y2] if is_fl else None
                    lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]

                    st_box    = [st_x1, l_ymax, st_x2, l_ymax + cluster_d_p]
                    st_cx     = (st_x1 + st_x2) / 2.0
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]     # south face = inner (adjacent to lift bank)
                    is_rotated_suit = True    # people enter from north face, so rotate 180°
                    # Use full lift bank width so the column grid excludes the entire
                    # cluster zone, including any gap between the fire zone east edge
                    # and the lift core east wall (l_xmax).
                    st_rect   = [l_xmin, l_ymax, l_xmax, l_ymax + cluster_d_p]
                    # Polygon check: if north cluster falls outside floor plate
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_p
                        _p_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _p_n_corners = [pt for b in _p_n_all
                                        for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_n_corners):
                            _skip_set = True

                # ── Door specs: NS parallel ──────────────────────────────────
                # stair_global_idx not yet incremented → stair_num = stair_global_idx + 1
                _sn_par = stair_global_idx + 1
                if is_south:
                    # South cluster: ExtDoor on south outer wall, tucked 900mm from west end.
                    # LobbyDoor on west wall (W_Left), at midpoint of the lobby wall section
                    # (away from both the south corner and the fire shaft boundary) to prevent
                    # door swing clash with the ExtDoor at the south-west corner.
                    # EntryDoor and FireLiftDoor centered on respective shaft south walls.
                    _fl_cx = (fz_x1 + fz_x2) / 2.0  # fire lift shaft center X
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_x1 + 900, lb_y1],
                        "wall_line_mm": [[st_x1, lb_y1], [st_x2, lb_y1]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_par)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_x1, (lb_y1 + lb_y2) / 2.0],
                        "wall_line_mm": [[st_x1, lb_y1], [st_x1, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Left".format(_sn_par, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [_fl_cx, lb_y1],
                        "wall_line_mm": [[fz_x1, lb_y1], [fz_x2, lb_y1]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx, fl_y1],
                            "wall_line_mm": [[fz_x1, fl_y1], [fz_x2, fl_y1]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
                else:
                    # North cluster: ExtDoor on north outer wall, tucked 900mm from east end.
                    # LobbyDoor on east wall (W_Right), at midpoint of the lobby wall section
                    # (away from both the north corner and the fire shaft boundary) to prevent
                    # door swing clash with the ExtDoor at the north-east corner.
                    # EntryDoor and FireLiftDoor centered on respective shaft north walls.
                    _fl_cx = (fz_x1 + fz_x2) / 2.0  # fire lift shaft center X
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_x2 - 900, l_ymax + cluster_d_p],
                        "wall_line_mm": [[st_x1, l_ymax + cluster_d_p], [st_x2, l_ymax + cluster_d_p]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_par)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_x2, (lb_y1 + lb_y2) / 2.0],
                        "wall_line_mm": [[st_x2, lb_y1], [st_x2, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Right".format(_sn_par, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [_fl_cx, lb_y2],
                        "wall_line_mm": [[fz_x1, lb_y2], [fz_x2, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx, fl_y2],
                            "wall_line_mm": [[fz_x1, fl_y2], [fz_x2, fl_y2]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })

        # ── Determine which lobby face is shared with the fire shaft (to avoid duplicate wall) ──
        if is_fl:
            if is_ew:
                _skip_lobby_face = "W" if is_east_ew else "E"
            else:
                _skip_lobby_face = "N" if is_south else "S"
        else:
            _skip_lobby_face = None

        # ── Sub-boundary reservations ────────────────────────────────────────
        if _skip_set:
            continue
        sub_boundaries.append({"id": tag + "_Shaft", "rect": fl_box} if is_fl else None)
        sub_boundaries.append({"id": tag + "_Lobby", "rect": lb_box})
        sub_boundaries.append({"id": tag + "_Staircase", "rect": st_box})

        stair_overrides.append(st_base_y)
        core_bounds.append(st_rect)
        stair_centers.append((st_cx, st_cy, is_rotated_suit))

        # ── Fire-lift shaft walls ─────────────────────────────────────────────
        if is_fl and fl_box is not None:
            fl_cx = (fl_box[0] + fl_box[2]) / 2.0
            fl_cy = (fl_box[1] + fl_box[3]) / 2.0
            fl_fw = fl_box[2] - fl_box[0]   # X extent → fw_mm
            fl_fd = fl_box[3] - fl_box[1]   # Y extent → fd_mm
            ls_walls, ls_floors = _fire_lift_shaft_walls(tag + "_FL", fl_cx, fl_cy, fl_fw, fl_fd, levels_data, overrun_height=overrun_height)
            walls.extend(ls_walls)
            floors.extend(ls_floors)

        # ── Lobby walls (all 4 sides — doors added separately at later stage) ──
        lobby_tag   = tag + "_LB"
        lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
        lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
        for l_idx, lvl in enumerate(levels_data):
            is_last_lvl = (l_idx == len(levels_data) - 1)
            if is_last_lvl:
                lvl_h = overrun_height
            else:
                lvl_h = levels_data[l_idx + 1]['elevation'] - lvl['elevation']
                if lvl_h <= 0:
                    continue
            common = {"level_id": lvl['id'], "height": lvl_h, "type": "AI_Wall_Core"}
            # Skip the face shared with the fire shaft — the shaft wall already covers it
            if _skip_lobby_face != "W":
                walls.append({"id": "AI_{}_W_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x1_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "E":
                walls.append({"id": "AI_{}_E_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x2_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "N":
                walls.append({"id": "AI_{}_N_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y2_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "S":
                walls.append({"id": "AI_{}_S_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y1_w, 0], **common})

        # ── Lobby TOPCAP — closing slab above overrun (matches lift shaft / staircase) ──
        if levels_data:
            last_lvl = levels_data[-1]
            cap_elev = last_lvl['elevation'] + overrun_height
            floors.append({
                "id": "AI_{}_TOPCAP".format(lobby_tag),
                "level_id": last_lvl['id'],
                "elevation": cap_elev,
                "points": [
                    [lb_x1_w, lb_y1_w], [lb_x2_w, lb_y1_w],
                    [lb_x2_w, lb_y2_w], [lb_x1_w, lb_y2_w]
                ]
            })

        # ── Staircase manifest ────────────────────────────────────────────────
        # EW and parallel-NS staircases are offset in X from centre — pass None
        # so the staircase doesn't try to suppress walls at lift core Y boundaries.
        st_lc_bounds = None if (is_ew or use_parallel) else lift_core_bounds_mm
        st_num_lifts = None if (is_ew or use_parallel) else num_lifts
        st_man = staircase_logic.generate_staircase_manifest(
            [(st_cx, st_cy)], levels_data, sw_nat, stair_spec, typical_floor_height_mm,
            lift_core_bounds_mm=st_lc_bounds, num_lifts=st_num_lifts, lobby_width=lobby_width,
            base_y_override=st_base_y, rotated_indices=([0] if is_rotated_suit else []),
            stair_idx_offset=stair_global_idx, compliance_overrides=co
        )
        stair_global_idx += 1
        walls.extend(st_man.get("walls", []))
        floors.extend(st_man.get("floors", []))
        voids.extend(staircase_logic.get_void_rectangles_mm(
            [(st_cx, st_cy)], sw_nat, sd_nat,
            lift_core_bounds_mm=st_lc_bounds,
            num_lifts=st_num_lifts, lobby_width=lobby_width,
            base_y_override=st_base_y,
            rotated_indices=([0] if is_rotated_suit else [])
        ))

    # ── Exposed staircase detection ─────────────────────────────────────────
    # For each perimeter staircase, determine which floor levels have a floor
    # plate smaller than the staircase footprint.  These levels have the
    # staircase partially or fully outside the slab edge.
    # Returns a list of dicts so building_generator.py can optionally widen
    # those floor plates to enclose the stairs (Step 2 of the design intent).
    exposed_stair_info = []
    if all_floor_dims and stair_centers:
        for s_set_idx, s_set in enumerate(safety_sets):
            if not s_set.get("is_perimeter"):
                continue
            # Retrieve the staircase X-extents and Y-extents for this set.
            # stair_centers list is 1:1 with is_perimeter sets only — find index.
            # core_bounds tracks [xmin, ymin, xmax, ymax] for each safety set.
            if s_set_idx >= len(core_bounds):
                continue
            sb = core_bounds[s_set_idx]  # [x1, y1, x2, y2]
            st_x1, st_y1, st_x2, st_y2 = sb
            st_hl = max(abs(st_y1), abs(st_y2))  # furthest Y extent from centre

            exposed_levels = []
            for lvl_idx, (fw, fl) in enumerate(all_floor_dims):
                floor_hl = fl / 2.0
                floor_hw = fw / 2.0
                # Exposed if staircase Y-extent exceeds floor half-length
                # OR staircase X-extent exceeds floor half-width
                y_exposed = st_hl > floor_hl
                x_exposed = (abs(st_x1) > floor_hw) or (abs(st_x2) > floor_hw)
                if y_exposed or x_exposed:
                    # Minimum widths needed to fully enclose the staircase
                    min_l_needed = st_hl * 2.0
                    min_w_needed = (abs(st_x1) + abs(st_x2))
                    exposed_levels.append({
                        "level_index": lvl_idx + 1,   # 1-based
                        "floor_width": fw,
                        "floor_length": fl,
                        "min_width_to_enclose": round(min_w_needed),
                        "min_length_to_enclose": round(min_l_needed),
                    })
            if exposed_levels:
                exposed_stair_info.append({
                    "stair_set_index": s_set_idx,
                    "stair_bounds": [st_x1, st_y1, st_x2, st_y2],
                    "exposed_levels": exposed_levels,
                })

    return {
        "walls":             walls,
        "floors":            floors,
        "voids":             voids,
        "core_bounds":       core_bounds,
        "stair_centers":     stair_centers,
        "sub_boundaries":    [s for s in sub_boundaries if s],
        "stair_overrides":   stair_overrides,
        "door_specs":        door_specs,
        "exposed_stair_info": exposed_stair_info,
    }


def _check_radius_coverage(positions, floor_dims_mm, radius_mm):
    """Check if all corners of the floor plate are within radius_mm of at least one position."""
    if not positions: return False
    for (fw, fd) in floor_dims_mm:
        corners = [
            (-fw/2, -fd/2), (fw/2, -fd/2),
            (fw/2, fd/2),  (-fw/2, fd/2)
        ]
        for cx, cy in corners:
            covered = False
            for px, py in positions:
                dist = math.sqrt((cx - px)**2 + (cy - py)**2)
                if dist <= radius_mm:
                    covered = True
                    break
            if not covered: return False
    return True
