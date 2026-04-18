# -*- coding: utf-8 -*-
import math

def calculate_lift_requirements(num_floors, avg_floor_height_mm, total_building_occupancy, target_interval=25.0):
    """
    Calculate the number of lifts using Round Trip Time (RTT) analysis.
    Uses the RTT method from BS EN 81-20 / CIBSE Guide D.
    Result is capped at realistic architectural norms to prevent absurd counts.
    """
    total_height_m = (num_floors * avg_floor_height_mm) / 1000.0
    H = total_height_m * 0.8  # Average highest reversal floor

    # Lift speed by building height
    if total_height_m < 30:   V = 1.6
    elif total_height_m < 60: V = 2.5
    elif total_height_m < 120: V = 5.0
    else:                      V = 7.0

    P = 10    # Average passengers per trip
    n = float(num_floors)
    S = n * (1 - math.pow(1 - 1.0 / n, P)) if n > 1 else 1.0

    t_d = 4.0   # Door open+close time per stop (s)
    t_p = 1.1   # Passenger transfer time per person (s)

    RTT = (2 * H / V) + (S + 1) * t_d + (2 * P * t_p)

    # 5-minute peak demand: typically 12% of occupancy tries to travel in 5 min
    # Each lift carries P passengers per RTT seconds.
    # Lifts needed = peak_demand / (P * 300 / RTT)
    peak_demand = total_building_occupancy * 0.12   # 12% in 5 min
    handling_capacity_per_lift = P * (300.0 / RTT)   # persons per 5 min per lift
    lifts_by_demand = math.ceil(peak_demand / handling_capacity_per_lift) if handling_capacity_per_lift > 0 else 2

    # RTT-based count, minimum 2, hard cap 24 (2 full banks of 12).
    # Population-based sanity cap: ~1 lift per 300 occupants is the
    # industry norm for office (CIBSE Guide D prestige interval ~25 s).
    max_from_pop = max(2, int(math.ceil(total_building_occupancy / 300.0)))
    num_lifts = max(lifts_by_demand, 2)
    num_lifts = min(num_lifts, max_from_pop, 24)  # cap at pop-based and absolute max

    return int(num_lifts)

def get_core_dimensions(num_lifts, internal_size=(2500, 2500), lobby_width=3000):
    """Calculates the total width and depth of a SINGLE lift core block (max 12)."""
    w, l = internal_size
    t = 200 # wall thickness
    
    # Each core: max 12 lifts, max 6 per side
    if num_lifts >= 4:
        # Split into two rows
        n1 = int(math.ceil(num_lifts / 2.0))
        n2 = int(math.floor(num_lifts / 2.0))
        # Clamp to max 6 per side (though num_lifts should be capped at 12 anyway)
        n1 = min(6, n1)
        bw1 = (n1 * w) + ((n1 + 1) * t)
        bw2 = (n2 * w) + ((n2 + 1) * t)
        block_width = max(bw1, bw2)
        block_depth = (2 * (l + 2 * t)) + lobby_width
    else:
        block_width = (num_lifts * w) + ((num_lifts + 1) * t)
        block_depth = l + (2 * t)
        
    return block_width, block_depth

def get_total_core_layout(num_lifts, internal_size=(2500, 2500), lobby_width=3000):
    """Calculates multi-core layout (back-to-back) if num_lifts > 12.
    Ensures equal distribution among cores."""
    # Strict 12-lift max per block
    num_blocks = int(math.ceil(num_lifts / 12.0))
    
    # Ensure equal number of lifts per core as requested
    # We round up the total count to a multiple of num_blocks
    total_lifts = int(math.ceil(num_lifts / float(num_blocks)) * num_blocks)
    lifts_per_block = total_lifts // num_blocks
    
    block_w, block_d = get_core_dimensions(lifts_per_block, internal_size, lobby_width)
    total_w = block_w
    total_d = block_d * num_blocks
    
    return {
        "num_blocks": num_blocks,
        "lifts_per_block": lifts_per_block,
        "total_lifts": total_lifts, # The adjusted total
        "block_w": block_w,
        "block_d": block_d,
        "total_w": total_w,
        "total_d": total_d
    }

def get_block_y_offset(b_idx, num_blocks, block_d):
    """
    Return the Y offset for lift-core block b_idx so the entire multi-block
    assembly is centred on the building centroid (Y = 0).

    For N blocks each of depth block_d the assembly total depth is N * block_d.
    We start block 0 at  -(N-1)/2 * block_d  so the geometric centre lands at 0.

    Examples
    --------
    N=1 : block 0 at  0              (single core, unchanged)
    N=2 : block 0 at -block_d/2,     block 1 at +block_d/2
    N=3 : block 0 at -block_d,       block 1 at  0,  block 2 at +block_d
    N=4 : block 0 at -1.5*block_d,   ...,           block 3 at +1.5*block_d

    Previously block 0 was always at 0 and block 1 at +block_d, which shifted
    the two-block assembly centre to +block_d/2 (~4 400 mm for a typical core),
    making both the lift core and the staircases appear off-centre on typical
    floor plates.
    """
    start_offset = -((num_blocks - 1) / 2.0) * block_d
    return start_offset + b_idx * block_d


def get_shaft_void_rectangles_mm(num_lifts, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000):
    """
    Returns a list of (x1, y1, x2, y2) mm rectangles for the INNER CLEAR SPACE of each
    individual lift shaft.  These are used to cut floor-slab openings.
    The rectangles are aligned to the inner faces of the shaft walls (offset by wall
    thickness t = 200 mm inward from the outer wall face).
    One rectangle per lift car — e.g. 4 lifts → 4 rectangles.
    Multi-block layouts (>12 lifts) are handled internally using get_total_core_layout.
    """
    layout = get_total_core_layout(num_lifts, internal_size, lobby_width)
    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]

    all_voids = []
    for b_idx in range(num_blocks):
        block_center_y = center_pos[1] + get_block_y_offset(b_idx, num_blocks, block_d)
        block_center = (center_pos[0], block_center_y)
        all_voids.extend(_get_block_void_rectangles_mm(lifts_per_block, block_center, internal_size, lobby_width))
    return all_voids


def _get_block_void_rectangles_mm(num_lifts, center_pos, internal_size=(2500, 2500), lobby_width=3000):
    """Void rectangles for a single lift core block (max 12 lifts)."""
    w, l = internal_size
    t = 200

    total_in_block = min(12, num_lifts)
    if total_in_block >= 4:
        n1 = int(math.ceil(total_in_block / 2.0))
        n2 = int(math.floor(total_in_block / 2.0))
    else:
        n1 = total_in_block
        n2 = 0

    row1_w = (n1 * w) + ((n1 + 1) * t)
    row2_w = (n2 * w) + ((n2 + 1) * t) if n2 > 0 else 0
    max_block_w = max(row1_w, row2_w)
    shaft_depth = l + (2 * t)

    voids = []

    def _row_voids(n_lifts, row_y_offset):
        this_row_w = (n_lifts * w) + ((n_lifts + 1) * t)
        row_base_x = center_pos[0] - (max_block_w / 2.0) + (max_block_w - this_row_w) / 2.0
        base_y = center_pos[1] + row_y_offset
        for j in range(n_lifts):
            x1 = row_base_x + j * (w + t) + (t / 2.0) + 1.0
            x2 = x1 + w - 2.0
            y1 = base_y + (t / 2.0) + 1.0
            y2 = y1 + l + t - 2.0
            voids.append((x1, y1, x2, y2))

    if n1 > 0:
        if n2 > 0:
            _row_voids(n1, -(shaft_depth + lobby_width / 2.0))
            _row_voids(n2, lobby_width / 2.0)
        else:
            _row_voids(n1, -(shaft_depth / 2.0))

    return voids


def generate_lift_shaft_manifest(num_lifts, levels_data, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000):
    """
    Generates manifest data for lift shafts centered around center_pos.
    Both rows are aligned to the same max_block_width for correct visual symmetry.
    Wall IDs use the level name (e.g. AI Level 3) for stable re-identification.
    Supports multi-block layouts (>12 lifts) by generating walls for each block.
    """
    layout = get_total_core_layout(num_lifts, internal_size, lobby_width)
    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]

    all_walls_total = []
    all_floors_total = []

    for b_idx in range(num_blocks):
        block_center_y = center_pos[1] + get_block_y_offset(b_idx, num_blocks, block_d)
        block_center = (center_pos[0], block_center_y)
        block_tag = "B{}".format(b_idx) if num_blocks > 1 else ""
        w_block, f_block = _generate_single_block_manifest(
            lifts_per_block, levels_data, block_center, internal_size, lobby_width, block_tag
        )
        all_walls_total.extend(w_block)
        all_floors_total.extend(f_block)

    return {"walls": all_walls_total, "floors": all_floors_total}


def _generate_single_block_manifest(num_lifts, levels_data, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000, block_tag=""):
    """Generate wall/floor manifest for a single lift core block (max 12 lifts)."""
    w, l = internal_size
    t = 200  # wall thickness

    # Symmetry: Split into two rows if >= 4 lifts
    if num_lifts >= 4:
        lifts_in_row1 = int(math.ceil(num_lifts / 2.0))
        lifts_in_row2 = int(math.floor(num_lifts / 2.0))
    else:
        lifts_in_row1 = int(num_lifts)
        lifts_in_row2 = 0

    # PRE-COMPUTE max block width so BOTH rows are centered relative to it.
    # This prevents the visual off-centre look when rows have unequal lift counts.
    row1_width = (lifts_in_row1 * w) + ((lifts_in_row1 + 1) * t)
    row2_width = (lifts_in_row2 * w) + ((lifts_in_row2 + 1) * t) if lifts_in_row2 > 0 else 0
    max_block_width = max(row1_width, row2_width)

    def create_row_manifest(n_lifts, row_y_offset, row_tag_prefix):
        row_walls = []
        row_floors = []
        this_row_width = (n_lifts * w) + ((n_lifts + 1) * t)
        block_depth = l + (2 * t)

        # FIX: Center relative to max_block_width, not this_row_width.
        # This keeps both rows visually aligned to the same X boundary.
        row_base_x = center_pos[0] - (max_block_width / 2.0) + (max_block_width - this_row_width) / 2.0

        for i, lvl in enumerate(levels_data):
            lvl_id = lvl['id']
            is_last = (i == len(levels_data) - 1)

            base_y = center_pos[1] + row_y_offset

            # Wall height from elevation delta
            wall_h = 4000.0
            if not is_last and i + 1 < len(levels_data):
                wall_h = levels_data[i + 1].get('elevation', 0) - lvl.get('elevation', 0)

            # FIX: Use level NAME as ID tag component (not loop index) for stable re-identification.
            # The level name is always deterministic: "AI Level 1", "AI Level 2" etc.
            safe_lvl_tag = lvl_id.replace(" ", "_")  # e.g. "AI_Level_3"
            common_props = {"level_id": lvl_id, "height": wall_h}

            row_walls.append({
                "id": "AI_{}__{}_W_Front".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x + this_row_width, base_y, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Back".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y + block_depth, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Left".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x, base_y + block_depth, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Right".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x + this_row_width, base_y, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0],
                **common_props
            })

            # Internal Dividers
            for j in range(1, n_lifts):
                div_x = row_base_x + (j * (w + t))
                row_walls.append({
                    "id": "AI_{}__{}__Div{}".format(row_tag_prefix, safe_lvl_tag, j),
                    "start": [div_x, base_y, 0],
                    "end": [div_x, base_y + block_depth, 0],
                    **common_props
                })

            # TOP CAP on last level (overrun walls + closing slab)
            if is_last:
                for wall in row_walls:
                    if wall['level_id'] == lvl_id:
                        wall['height'] = 5000

                ov_elevation = lvl.get('elevation', 0) + 5000
                row_floors.append({
                    "id": "AI_{}__TOPCAP".format(row_tag_prefix),
                    "level_id": lvl_id,
                    "elevation": ov_elevation,
                    "points": [
                        [row_base_x, base_y],
                        [row_base_x + this_row_width, base_y],
                        [row_base_x + this_row_width, base_y + block_depth],
                        [row_base_x, base_y + block_depth]
                    ]
                })

        return row_walls, row_floors

    # Y-offsets
    shaft_depth = l + (2 * t)
    all_walls = []
    all_floors = []

    if lifts_in_row1 > 0:
        if lifts_in_row2 > 0:
            r1_y_start = -(shaft_depth + lobby_width / 2.0)
            r2_y_start = (lobby_width / 2.0)
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1" + block_tag)
            w2, f2 = create_row_manifest(lifts_in_row2, r2_y_start, "LiftR2" + block_tag)
            all_walls.extend(w1)
            all_walls.extend(w2)
            all_floors.extend(f1)
            all_floors.extend(f2)
        else:
            r1_y_start = -(shaft_depth / 2.0)
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1" + block_tag)
            all_walls.extend(w1)
            all_floors.extend(f1)

    return all_walls, all_floors

