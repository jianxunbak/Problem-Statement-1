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


def _fire_lift_shaft_walls(tag, cx_mm, cy_mm, fw_mm, fd_mm, levels_data):
    """Generate walls + topcap for a single fire-fighting lift shaft."""
    walls = []
    floors = []
    hfw, hfd = fw_mm / 2.0, fd_mm / 2.0
    x1, x2 = cx_mm - hfw, cx_mm + hfw
    y1, y2 = cy_mm - hfd, cy_mm + hfd

    for l_idx, lvl in enumerate(levels_data):
        lvl_id, elev = lvl['id'], lvl['elevation']
        is_last = (l_idx == len(levels_data) - 1)
        h = _OVERRUN_HEIGHT if is_last else (levels_data[l_idx + 1]['elevation'] - elev)
        if h <= 0:
            continue
        common = {"level_id": lvl_id, "height": h, "type": "AI_Wall_Core"}
        walls.append({"id": "AI_{}_S_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x2, y1, 0], **common})
        walls.append({"id": "AI_{}_N_L{}".format(tag, l_idx + 1), "start": [x1, y2, 0], "end": [x2, y2, 0], **common})
        walls.append({"id": "AI_{}_W_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x1, y2, 0], **common})
        walls.append({"id": "AI_{}_E_L{}".format(tag, l_idx + 1), "start": [x2, y1, 0], "end": [x2, y2, 0], **common})
        if is_last:
            cap_elev = elev + _OVERRUN_HEIGHT
            floors.append({"id": "AI_{}_TOPCAP".format(tag), "level_id": lvl_id, "elevation": cap_elev,
                           "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]})
    return walls, floors


def _should_use_ew_orientation(_lift_core_bounds_mm, _sw_nat, _sd_nat):
    """Return True if EW orientation should be used.

    EW orientation would require a 90-degree staircase rotation (flights running
    east-west instead of north-south) to avoid L-shaped fire safety sets.
    That rotation is not yet implemented in staircase_logic / revit_workers, so
    we always return False and use the NS layout, which naturally produces a
    compact rectangle (fire lift → lobby → staircase stacked in Y).
    """
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_fire_safety_requirements(floor_dims_mm, core_center_mm, lift_core_bounds_mm,
                                       typical_floor_height_mm, preset_fs, num_lifts,
                                       lobby_width=3000):
    """Determine positions and types for fire safety cores.

    Returns list of dicts: {"pos": (x, y), "type": "FIRE_LIFT"|"SMOKE_STOP"}

    NS layout  — pos is the entry point at the lift-core Y boundary
                 (y = lift_ymin or lift_ymax, x = lift_core_cx).
    EW layout  — pos is the entry point at the lift-core X boundary
                 (x = lift_xmin or lift_xmax, y = lift_core_cy).
    """
    max_travel_dist = _MAX_TRAVEL
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
    use_ew = _should_use_ew_orientation(lift_core_bounds_mm, sw_nat, sd_nat)

    if use_ew:
        # EW layout: fire lifts aligned with passenger lift rows so the passenger
        # lift lobby ends remain clear (Rule 1) and fire lifts are in-row (Rule 4).
        layout_ew = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
        if layout_ew and layout_ew["lifts_per_block"] >= 4:
            # 2-row block: east set at north-row centre, west set at south-row centre.
            # Row centres are half a shaft_depth inward from the lobby boundary.
            row_cy_s = lift_core_cy - lobby_width / 2.0 - _FL_SHAFT_D / 2.0  # south row
            row_cy_n = lift_core_cy + lobby_width / 2.0 + _FL_SHAFT_D / 2.0  # north row
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
    _lobby_d_est  = max(2000, sd_nat - _FL_SHAFT_D)
    _cluster_d_est = _FL_SHAFT_D + _lobby_d_est          # ≈ 7 300 mm for 4 000 mm floors
    stair_pos = []
    for s in final_sets:
        ex, ey = s["pos"]
        if ey <= l_ymin + 10:   # south entry
            stair_pos.append((ex, ey - _cluster_d_est / 2.0))
        else:                    # north entry
            stair_pos.append((ex, ey + _cluster_d_est / 2.0))

    if staircase_logic._check_travel_distance(stair_pos, floor_dims_mm, max_travel_dist):
        return final_sets

    # ── Perimeter SMOKE_STOP staircases (60m rule not yet satisfied) ──────────
    # For large floor plates (e.g. 80×100m) the central core alone cannot cover
    # all corners within 60m.  Add N-S oriented smoke-stop staircase/lobby sets
    # at the building perimeter until the rule is satisfied.
    # The "pos" stores (x, y_edge) where y_edge is the building boundary line.
    # generate_fire_safety_manifest detects is_perimeter=True and uses a
    # dedicated layout rather than the core-relative NS/EW branches.
    if floor_dims_mm:
        max_w = max(d[0] for d in floor_dims_mm)
        max_l = max(d[1] for d in floor_dims_mm)
    else:
        max_w, max_l = 50000, 50000
    hw_p = max_w / 2.0
    hl_p = max_l / 2.0

    # Approximate shaft depth to compute staircase centre from edge position.
    _sd_approx = staircase_logic.get_shaft_dimensions(_typ_h, None)[1]

    # Candidate perimeter positions (NS-oriented: south/north building edges).
    # A single staircase at the midpoint of each edge (x=0) covers all corners
    # for buildings up to ~120 m wide.  Placing staircases at x=0 instead of
    # the x-extremes also minimises the total number added (typically 2 for an
    # 80×100 m plate instead of 4 from a spread-first greedy).
    perim_candidates = []
    for x_frac in [0.0]:   # midpoint of each long edge — optimal for ≤120 m width
        x_pos = x_frac * hw_p
        # Staircase centres (used for travel-distance check)
        perim_candidates.append((x_pos, -hl_p + _sd_approx / 2.0))   # south edge
        perim_candidates.append((x_pos,  hl_p - _sd_approx / 2.0))   # north edge

    # Greedy: always pick the candidate furthest from ALL existing staircases.
    all_stair_pos = list(stair_pos)
    while perim_candidates:
        if staircase_logic._check_travel_distance(all_stair_pos, floor_dims_mm, max_travel_dist):
            break
        best = max(perim_candidates, key=lambda c: min(
            math.sqrt((c[0] - sx) ** 2 + (c[1] - sy) ** 2)
            for sx, sy in all_stair_pos
        ))
        perim_candidates.remove(best)
        # Reconstruct building-edge Y from centre Y
        y_edge = best[1] - _sd_approx / 2.0 if best[1] < 0 else best[1] + _sd_approx / 2.0
        final_sets.append({"pos": (best[0], y_edge), "type": "SMOKE_STOP", "is_perimeter": True})
        all_stair_pos.append(best)

    return final_sets


def generate_fire_safety_manifest(safety_sets, levels_data, stair_spec,
                                  typical_floor_height_mm, preset_fs,
                                  lift_core_bounds_mm=None, num_lifts=None,
                                  lobby_width=3000):
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
    """
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
    _, total_set_d = staircase_logic.get_safety_set_dimensions(typical_floor_height_mm, stair_spec, True, levels_data=levels_data)

    l_xmin, l_ymin, l_xmax, l_ymax = lift_core_bounds_mm if lift_core_bounds_mm else (0, 0, 0, 0)
    lift_core_cy = (l_ymin + l_ymax) / 2.0

    layout = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
    row1_cy, row2_cy = _passenger_lift_row_centers((0, 0), layout, lobby_width) if layout else (0, 0)

    stair_overrides = []
    sub_boundaries = []
    stair_global_idx = 0  # incremented for every staircase across all safety sets

    t = _WALL_THICKNESS

    # Pre-compute EW dimensions (Rule 3: fire lift shaft = passenger lift shaft = _FL_SHAFT_D)
    ew_fl_dx  = _FL_SHAFT_D                                       # EW fire-lift X extent (2900 mm)
    lb_net_y  = _FL_SHAFT_D - 2 * t                               # lobby internal Y = 2500 mm
    ew_lb_dx  = max(2000, int(math.ceil(6000000.0 / lb_net_y)))  # EW lobby X extent (≥2400 mm for 6 m²)

    for i, s_set in enumerate(safety_sets):
        is_fl      = (s_set["type"] == "FIRE_LIFT")
        entry_x, entry_y = s_set["pos"]
        tag        = "SafetySet_{}".format(i + 1)

        # ── Perimeter SMOKE_STOP — dedicated layout (building edge, no fire lift) ──
        is_perimeter = s_set.get("is_perimeter", False)
        if is_perimeter:
            # Simple layout: staircase at building edge, lobby between staircase and floor plate.
            # entry_y is the building-edge Y (negative = south, positive = north).
            is_south_p = (entry_y <= 0)
            # Smoke-stop lobby: min 2000mm clear width (= sw_nat - 2t) already
            # satisfied by the staircase width.  Depth: 2000mm clear = 2400mm outer.
            # Target ~4-5 sqm net: 2000mm clear × (sw_nat - 2t) already large enough.
            lobby_d_p  = 2 * _WALL_THICKNESS + _SMOKE_CLEAR_D  # outer = clear depth + 2 walls
            fl_box = None  # no fire lift

            # Inset the staircase from the building edge so the floor-slab void does
            # not coincide with the slab boundary (which causes "can't make extrusion"
            # errors in Revit).  500 mm is enough to clear the 200 mm wall thickness
            # plus Revit's minimum-face-width tolerance.
            _EDGE_GAP = _EDGE_GAP_MM

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

            # Lobby walls
            lobby_tag  = tag + "_LB"
            lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
            lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
            for l_idx, lvl in enumerate(levels_data):
                is_last_lvl = (l_idx == len(levels_data) - 1)
                if is_last_lvl:
                    lvl_h = _OVERRUN_HEIGHT
                else:
                    lvl_h = levels_data[l_idx + 1]['elevation'] - lvl['elevation']
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
            if levels_data:
                last_lvl = levels_data[-1]
                floors.append({
                    "id": "AI_{}_TOPCAP".format(lobby_tag),
                    "level_id": last_lvl['id'],
                    "elevation": last_lvl['elevation'] + _OVERRUN_HEIGHT,
                    "points": [[lb_x1_w, lb_y1_w], [lb_x2_w, lb_y1_w], [lb_x2_w, lb_y2_w], [lb_x1_w, lb_y2_w]]
                })

            # Staircase manifest (no lift-core bounds for perimeter sets)
            st_man = staircase_logic.generate_staircase_manifest(
                [(st_cx, st_cy)], levels_data, sw_nat, stair_spec, typical_floor_height_mm,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
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
            _sn_p = stair_global_idx
            if is_south_p:
                # Staircase main landing faces NORTH (is_rotated=True), external door on SOUTH wall
                _ext_y = st_base_y          # south wall of staircase = W_Front
                _lby_conn = st_y2           # shared wall between staircase and lobby = W_Back
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_first_lvl_id] if _first_lvl_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_p)} if _all_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Back".format(_sn_p, k + 1) for k in range(len(_all_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y2],
                    "wall_line_mm": [[lb_box[0], lb_y2], [lb_box[2], lb_y2]],
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                })
            else:
                # Main landing faces SOUTH (is_rotated=False), external door on NORTH wall
                _ext_y = st_y2              # north wall of staircase = W_Back
                _lby_conn = st_base_y       # shared wall between staircase and lobby = W_Front
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_first_lvl_id] if _first_lvl_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_p)} if _all_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Front".format(_sn_p, k + 1) for k in range(len(_all_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y1],
                    "wall_line_mm": [[lb_box[0], lb_y1], [lb_box[2], lb_y1]],
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
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
            fl_Y_h = _FL_SHAFT_D / 2.0  # fire-lift Y half-extent = pax lift shaft (Rule 3)

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
            # Fire zone width = _FL_SHAFT_D (2900mm); lobby same width.
            # Cluster depth   = fl_shaft_d + lobby_d (= _FL_SHAFT_D + max(2000, remaining))
            # Total X         = _FL_SHAFT_D + sw_nat  (must fit inside lift bank)
            # Fallback to sequential stack if lift bank too narrow.

            lift_bank_w  = l_xmax - l_xmin
            fz_w         = _FL_SHAFT_D                            # fire-zone X width = shaft width
            total_pair_w = fz_w + sw_nat                          # total X needed
            use_parallel = (total_pair_w <= lift_bank_w + 1)      # fits with 1mm tolerance

            # Cluster depth: shaft (inner) + lobby (outer), minimum lobby 2000mm
            fl_shaft_d_p = _FL_SHAFT_D       # 2900mm — shaft occupies inner depth
            lobby_d_p    = max(2000, sd_nat - fl_shaft_d_p)
            cluster_d_p  = fl_shaft_d_p + lobby_d_p

            if not use_parallel:
                # ── Fallback: original sequential NS stack ───────────────────
                fire_lift_d = _FL_SHAFT_D if is_fl else 0
                lobby_d     = total_set_d - sd_nat - fire_lift_d
                if not is_fl:
                    lobby_d = total_set_d - sd_nat
                sw_h  = sw_nat / 2.0
                fl_hw = _FL_SHAFT_D / 2.0
                st_cx = entry_x
                if is_south:
                    fl_box    = [entry_x - fl_hw, entry_y - fire_lift_d, entry_x + fl_hw, entry_y] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d, entry_x + sw_h, entry_y - fire_lift_d]
                    st_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d - sd_nat, entry_x + sw_h, entry_y - fire_lift_d - lobby_d]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, st_box[1], entry_x + sw_h, entry_y]
                else:
                    fl_box    = [entry_x - fl_hw, entry_y, entry_x + fl_hw, entry_y + fire_lift_d] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y + fire_lift_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d]
                    st_box    = [entry_x - sw_h, entry_y + fire_lift_d + lobby_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d + sd_nat]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, entry_y, entry_x + sw_h, st_box[3]]
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
            ls_walls, ls_floors = _fire_lift_shaft_walls(tag + "_FL", fl_cx, fl_cy, fl_fw, fl_fd, levels_data)
            walls.extend(ls_walls)
            floors.extend(ls_floors)

        # ── Lobby walls (all 4 sides — doors added separately at later stage) ──
        lobby_tag   = tag + "_LB"
        lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
        lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
        for l_idx, lvl in enumerate(levels_data):
            is_last_lvl = (l_idx == len(levels_data) - 1)
            if is_last_lvl:
                lvl_h = _OVERRUN_HEIGHT
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
            cap_elev = last_lvl['elevation'] + _OVERRUN_HEIGHT
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
            stair_idx_offset=stair_global_idx
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

    return {
        "walls":          walls,
        "floors":         floors,
        "voids":          voids,
        "core_bounds":    core_bounds,
        "stair_centers":  stair_centers,
        "sub_boundaries": [s for s in sub_boundaries if s],
        "stair_overrides": stair_overrides,
        "door_specs":     door_specs,
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
