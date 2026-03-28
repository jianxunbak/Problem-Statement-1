# -*- coding: utf-8 -*-
import math

def calculate_lift_requirements(num_floors, avg_floor_height_mm, total_building_occupancy, target_interval=25.0):
    """
    Calculate the number of lifts required based on Round Trip Time (RTT).
    """
    total_height_m = (num_floors * avg_floor_height_mm) / 1000.0
    H = total_height_m * 0.8 # Highest reversal floor
    
    # V (Rated Velocity) based on building height
    if total_height_m < 30: V = 1.6
    elif total_height_m < 60: V = 2.5
    elif total_height_m < 120: V = 5.0
    else: V = 7.0
        
    P = 10 # Average passengers per trip
    n = float(num_floors)
    if n > 1:
        S = n * (1 - math.pow(1 - 1/n, P))
    else:
        S = 1
        
    t_d = 4.0   
    t_p = 1.1   
    
    RTT = (2 * H / V) + (S + 1) * t_d + (2 * P * t_p)
    num_lifts = math.ceil(RTT / target_interval)
    
    # Minimum lifts by population coverage (Standard: ~1 lift per 350-400 people for office)
    # Raising to 400 to avoid excessive counts in mid-rise towers
    min_lifts_by_pop = math.ceil(total_building_occupancy / 400.0)
    
    return int(max(num_lifts, min_lifts_by_pop))

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

def generate_lift_shaft_manifest(num_lifts, levels_data, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000):
    """
    Generates manifest data for lift shafts centered around center_pos.
    Each row is independently centered for visual symmetry.
    """
    walls = []
    w, l = internal_size # 2500x2500
    t = 200 # wall thickness
    
    # Symmetry: Split into two rows if >= 4 lifts
    if num_lifts >= 4:
        total_in_block = min(12, num_lifts)
        lifts_in_row1 = int(math.ceil(total_in_block / 2.0))
        lifts_in_row2 = int(math.floor(total_in_block / 2.0))
    else:
        lifts_in_row1 = int(num_lifts)
        lifts_in_row2 = 0
    
    def create_row_manifest(n_lifts, row_y_offset, row_tag_prefix):
        row_walls = []
        row_floors = []
        # Calculate width of THIS specific row
        this_row_width = (n_lifts * w) + ((n_lifts + 1) * t)
        block_depth = l + (2 * t)
        
        # Center this row's X-coordinate relative to center_pos
        row_base_x = center_pos[0] - (this_row_width / 2.0)
        
        for i, lvl in enumerate(levels_data):
            lvl_id = lvl['id']
            is_last = (i == len(levels_data) - 1)
            
            base_y = center_pos[1] + row_y_offset
            
            # 1. Outer Frame
            # Front
            row_walls.append({
                "id": f"AI_{row_tag_prefix}_L{i+1}_W_Front",
                "level_id": lvl_id,
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x + this_row_width, base_y, 0]
            })
            # Back
            row_walls.append({
                "id": f"AI_{row_tag_prefix}_L{i+1}_W_Back",
                "level_id": lvl_id,
                "start": [row_base_x, base_y + block_depth, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0]
            })
            # Left
            row_walls.append({
                "id": f"AI_{row_tag_prefix}_L{i+1}_W_Left",
                "level_id": lvl_id,
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x, base_y + block_depth, 0]
            })
            # Right
            row_walls.append({
                "id": f"AI_{row_tag_prefix}_L{i+1}_W_Right",
                "level_id": lvl_id,
                "start": [row_base_x + this_row_width, base_y, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0]
            })
            
            # 2. Internal Dividers (Shared Walls)
            for j in range(1, n_lifts):
                div_x = row_base_x + (j * (w + t))
                row_walls.append({
                    "id": f"AI_{row_tag_prefix}_L{i+1}_Div{j}",
                    "level_id": lvl_id,
                    "start": [div_x, base_y, 0],
                    "end": [div_x, base_y + block_depth, 0]
                })

            # 3. Handle Overrun and TOP CAP Slab on Last Floor
            if is_last:
                for wall in row_walls:
                    if wall['level_id'] == lvl_id:
                        wall['height'] = 5000 
                
                # Add a slab to close the top of this row
                # Bounds: row_base_x -> row_base_x + this_row_width, base_y -> base_y + block_depth
                ov_elevation = lvl.get('elevation', 0) + 5000
                row_floors.append({
                    "id": f"AI_{row_tag_prefix}_TOPCAP",
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

    # Calculate Y-offsets relative to center_pos
    shaft_depth = l + (2 * t)
    all_walls = []
    all_floors = []
    
    if lifts_in_row1 > 0:
        if lifts_in_row2 > 0:
            r1_y_start = -(shaft_depth + lobby_width/2.0)
            r2_y_start = (lobby_width/2.0)
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1")
            w2, f2 = create_row_manifest(lifts_in_row2, r2_y_start, "LiftR2")
            all_walls.extend(w1); all_walls.extend(w2)
            all_floors.extend(f1); all_floors.extend(f2)
        else:
            r1_y_start = -(shaft_depth / 2.0)
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1")
            all_walls.extend(w1)
            all_floors.extend(f1)
        
    return {"walls": all_walls, "floors": all_floors}
