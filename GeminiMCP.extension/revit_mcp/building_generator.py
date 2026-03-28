# -*- coding: utf-8 -*-
try:
    import clr
    clr.AddReference('RevitAPI')
    from Autodesk.Revit.DB import * # type: ignore
    import System
except:
    pass

import math
from revit_mcp.state_manager import state_manager

from revit_mcp.utils import safe_num, mm_to_ft, load_presets
from . import lift_logic

def get_model_registry(doc, zone_bbox=None):
    """
    ULTRA-HIGH-SPEED SCAN: Search for AI-managed elements using Extensible Storage filters.
    """
    import Autodesk.Revit.DB as DB # type: ignore
    from .state_manager import state_manager
    registry = {}
    
    # 1. Primary Scan: Extensible Storage Filter (Very fast)
    try:
        schema = state_manager.get_schema()
        es_filter = DB.ExtensibleStorage.ExtensibleStorageFilter(schema.GUID)
        collector = DB.FilteredElementCollector(doc).WherePasses(es_filter)
        
        if zone_bbox:
            buffer = mm_to_ft(1000)
            outline = DB.Outline(
                DB.XYZ(zone_bbox.Min.X - buffer, zone_bbox.Min.Y - buffer, zone_bbox.Min.Z - buffer),
                DB.XYZ(zone_bbox.Max.X + buffer, zone_bbox.Max.Y + buffer, zone_bbox.Max.Z + buffer)
            )
            collector.WherePasses(DB.BoundingBoxIntersectsFilter(outline))

        for el in collector:
            metadata = state_manager.get_ai_metadata(el)
            if metadata:
                registry[metadata['ai_id']] = el.Id
    except Exception:
        pass

    # 2. Fallback Scan: Legacy Comment Tags
    # Optimization: Scan all likely categories in one pass
    if len(registry) < 5:
        cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Levels, 
                DB.BuiltInCategory.OST_Grids, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
        net_cats = System.Collections.Generic.List[DB.BuiltInCategory]()
        for c in cats: net_cats.Add(c)
        filter = DB.ElementMulticategoryFilter(net_cats)
        col = DB.FilteredElementCollector(doc).WherePasses(filter).WhereElementIsNotElementType()
        for el in col:
            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if not p: p = el.LookupParameter("Comments")
            if p and p.HasValue:
                val = p.AsString()
                if val.startswith("AI_") and val not in registry:
                    registry[val] = el.Id
    
    return registry

class BuildingSystem:
    def __init__(self, doc):
        self.doc = doc

    def sync_manifest(self, manifest):
        """State-aware manifest execution with high-speed transaction batching."""
        # Immediate self-log for the worker
        def worker_log(msg):
            from .runner import log
            log("[BuildingSystem] {}".format(msg))
            
        worker_log("Starting sync_manifest execution...")
        import Autodesk.Revit.DB as DB # type: ignore
        results = []
        self.registry = get_model_registry(self.doc)
        
        tg = DB.TransactionGroup(self.doc, "AI Sync: Building Manifest")
        tg.Start()
        
        try:
            # --- PRE-PROCESSING: High-Level to Low-Level expansion ---
            if "project_setup" in manifest or "shell" in manifest:
                worker_log("Expanding high-level architectural manifest...")
                manifest = self._expand_high_level_manifest(manifest)
            
            # --- PHASE 1: LEVELS ---
            worker_log("PHASE 1: Syncing Levels...")
            t = DB.Transaction(self.doc, "AI Sync: Levels")
            t.Start()
            level_map = {} # AI_ID -> Revit Level
            levels_data = manifest.get('levels', [])
            for l_data in levels_data:
                lvl = self._sync_level(l_data)
                level_map[l_data['id']] = lvl
                results.append({"type": "level", "id": l_data['id'], "revit_id": str(lvl.Id.Value)})
            t.Commit()
            worker_log("Levels synced: {}.".format(len(level_map)))
            
            # --- PHASE 2: SHELL (Walls & Floors) ---
            worker_log("PHASE 2: Syncing Shell (Walls & Floors)...")
            t = DB.Transaction(self.doc, "AI Sync: Shell")
            t.Start()
            walls_count = 0
            for w_data in manifest.get('walls', []):
                wall = self._sync_wall(w_data, level_map)
                results.append({"type": "wall", "id": w_data['id'], "revit_id": str(wall.Id.Value)})
                walls_count += 1
                
            floors_count = 0
            for f_data in manifest.get('floors', []):
                floor = self._sync_floor(f_data, level_map)
                results.append({"type": "floor", "id": f_data['id'], "revit_id": str(floor.Id.Value)})
                floors_count += 1
            
            # --- PHASE 3: COLUMNS ---
            worker_log("PHASE 3: Syncing Columns...")
            t = DB.Transaction(self.doc, "AI Sync: Columns")
            t.Start()
            cols_count = 0
            for c_data in manifest.get('columns', []):
                col = self._sync_column(c_data, level_map)
                if col:
                    results.append({"type": "column", "id": c_data['id'], "revit_id": str(col.Id.Value)})
                    cols_count += 1
            t.Commit()
            worker_log("Columns synced: {}.".format(cols_count))
            
            tg.Assimilate()
            worker_log("sync_manifest SUCCESS.")
            return {"status": "Success", "elements": results}
            
        except Exception as e:
            import traceback
            err_trace = traceback.format_exc()
            worker_log("sync_manifest FAILED: {}\n{}".format(str(e), err_trace))
            tg.RollBack()
            return {"status": "Error", "message": str(e)}

    def _sync_level(self, data):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        elev = mm_to_ft(data['elevation'])
        name = data.get('name', ai_id)
        
        lvl = None
        if ai_id in self.registry:
            lvl = self.doc.GetElement(self.registry[ai_id])
            
        if not lvl:
            lvl = DB.Level.Create(self.doc, elev)
            state_manager.set_ai_metadata(lvl, ai_id)
        
        if lvl.Elevation != elev: lvl.Elevation = elev
        if lvl.Name != name:
            try: lvl.Name = name
            except: pass
            
        return lvl

    def _sync_wall(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        start = data['start'] # [x, y, z]
        end = data['end']     # [x, y, z]
        
        p1 = DB.XYZ(mm_to_ft(start[0]), mm_to_ft(start[1]), mm_to_ft(start[2]))
        p2 = DB.XYZ(mm_to_ft(end[0]), mm_to_ft(end[1]), mm_to_ft(end[2]))
        line = DB.Line.CreateBound(p1, p2)
        
        level = level_map.get(data.get('level_id'))
        if not level:
            level = DB.FilteredElementCollector(self.doc).OfClass(DB.Level).FirstElement()
            
        wall = None
        if ai_id in self.registry:
            wall = self.doc.GetElement(self.registry[ai_id])
            
        if wall and isinstance(wall, DB.Wall):
            # Update existing
            wall.Location.Curve = line
        else:
            # Create new
            wall = DB.Wall.Create(self.doc, line, level.Id, False)
            state_manager.set_ai_metadata(wall, ai_id)
            
        # Set Type if specified
        if data.get('type'):
            wt = self._find_type(DB.BuiltInCategory.OST_Walls, data['type'])
            if wt: wall.WallType = wt
            
        # Set Height if specified (for overruns, etc.)
        if data.get('height'):
            h_ft = mm_to_ft(data['height'])
            # Disconnect from top level if height is literal
            p_top = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
            if p_top: p_top.Set(DB.ElementId.InvalidElementId)
            p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
            if p_h: p_h.Set(h_ft)
            
        return wall

    def _sync_floor(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        ai_id = data['id']
        points = data['points'] # [[x,y], [x,y], ...]
        level = level_map.get(data.get('level_id'))
        
        curve_loop = DB.CurveLoop()
        for i in range(len(points)):
            p1_raw = points[i]
            p2_raw = points[(i+1)%len(points)]
            p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), 0)
            p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), 0)
            curve_loop.Append(DB.Line.CreateBound(p1, p2))
            
        loops = Generic.List[DB.CurveLoop]()
        loops.Add(curve_loop)
        
        floor = None
        if ai_id in self.registry:
            # Floors are tricky to update geometry (requires Sketch edit), 
            # for the AI manifest, we delete and recreate for stability if geometry changes.
            existing = self.doc.GetElement(self.registry[ai_id])
            if existing: self.doc.Delete(existing.Id)
            
        ft = DB.FilteredElementCollector(self.doc).OfClass(DB.FloorType).FirstElement()
        if data.get('type'):
            ft_match = self._find_type(DB.BuiltInCategory.OST_Floors, data['type'])
            if ft_match: ft = ft_match
            
        floor = DB.Floor.Create(self.doc, loops, ft.Id, level.Id)
        state_manager.set_ai_metadata(floor, ai_id)
        return floor

    def _sync_column(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        loc_raw = data['location'] # [x, y, z]
        p = DB.XYZ(mm_to_ft(loc_raw[0]), mm_to_ft(loc_raw[1]), mm_to_ft(loc_raw[2]))
        
        level = level_map.get(data.get('level_id'))
        top_level = level_map.get(data.get('top_level_id'))
        
        # Find structural column symbol
        symbol = self._find_type(DB.BuiltInCategory.OST_StructuralColumns, data.get('type', "Column"))
        if not symbol:
            symbol = DB.FilteredElementCollector(self.doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        
        if not symbol: return None
        if not symbol.IsActive: symbol.Activate()
        
        col = None
        if ai_id in self.registry:
            col = self.doc.GetElement(self.registry[ai_id])
            
        if col and isinstance(col, DB.FamilyInstance):
            col.Location.Point = p
        else:
            col = self.doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.Column)
            state_manager.set_ai_metadata(col, ai_id)
            
        # Set Top Level
        if top_level:
            p_top = col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
            if p_top: p_top.Set(top_level.Id)
            
        return col
            
    def _synthesize_structural_grid(self, target_dim, span_range, min_offset):
        """
        Synthesizes an optimal structural grid based on 1/3 cantilever rules.
        Returns: (final_dim, final_span)
        """
        if isinstance(span_range, (int, float)):
            min_s, max_s = float(span_range), float(span_range)
        elif isinstance(span_range, list) and len(span_range) >= 2:
            min_s, max_s = float(span_range[0]), float(span_range[1])
        else:
            min_s, max_s = 10000.0, 12000.0
            
        target_half = target_dim / 2.0
        n = int((target_half - min_offset) // min_s)
        if n < 0: n = 0
            
        def check_rules(half_w, span, n_cols):
            if n_cols == 0: return True
            cant = half_w - (n_cols * span)
            if cant < min_offset - 1.0: return False
            if cant > (span / 3.0) + 1.0: return False
            return True

        if check_rules(target_half, min_s, n):
            return target_dim, min_s
            
        # Priority 1: Add column and check range
        n_plus = n + 1
        s_min_c = target_half / (n_plus + 1.0/3.0)
        s_max_o = (target_half - min_offset) / n_plus
        low, high = max(min_s, s_min_c), min(max_s, s_max_o)
        if low <= high + 1.0: return target_dim, high
                
        # Priority 2: Extend span
        if n > 0:
            s_min_c = target_half / (n + 1.0/3.0)
            s_max_o = (target_half - min_offset) / n
            low, high = max(min_s, s_min_c), min(max_s, s_max_o)
            if low <= high + 1.0: return target_dim, high
                
        # Priority 3: Reduce Floor
        if n == 0: return (min_offset + 500) * 2.0, min_s
        new_half = n * max_s + (max_s / 3.0)
        if new_half > target_half: new_half = n * min_s + (min_s / 3.0)
        return new_half * 2.0, max_s if new_half <= target_half else min_s

    def _expand_high_level_manifest(self, manifest):
        """Converts Architectural Intent (Storeys/Shell) into concrete element lists."""
        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})
        
        # --- PRESET LOADING ---
        presets = load_presets()
        typology = setup.get("typology", "commercial_office").lower().replace(" ", "_")
        preset = presets.get(typology, presets.get("commercial_office", {}))
        
        num_storeys = int(safe_num(setup.get("levels", 1), 1))
        
        # Default Heights from Presets
        p_defaults = preset.get("building_defaults", {})
        base_height = safe_num(setup.get("level_height", p_defaults.get("typical_floor_height", 4000)), 4000.0)
        height_overrides = setup.get("height_overrides", {})
        
        # Apply Preset Height Overrides if not explicitly overridden by user
        if "1" not in height_overrides and "first_storey_floor_height" in p_defaults:
            height_overrides["1"] = p_defaults["first_storey_floor_height"]
        if str(num_storeys) not in height_overrides and "last_floor_height" in p_defaults:
            height_overrides[str(num_storeys)] = p_defaults["last_floor_height"]
            
        # --- STRUCTURAL SYNTHESIS (1/3 RULE) ---
        p_col_logic = preset.get("column_logic", {})
        dna_span = p_col_logic.get("span", [12000, 15000])
        dna_offset = safe_num(p_col_logic.get("offset_from_edge", 1000), 1000)
        
        width = safe_num(shell.get("width", 50000), 50000.0)
        length = safe_num(shell.get("length", 50000), 50000.0)
        
        # Area Goal Adjustment
        area_goal = safe_num(setup.get("typical_floor_area", preset.get("building_identity", {}).get("typical_floor_area", 0)), 0)
        if area_goal > 0 and width == 50000 and length == 50000:
            side = math.sqrt(area_goal * 1000000.0)
            width, length = side, side
            
        # Synthesis Run (enforce 1/3 rule)
        width, synth_span_w = self._synthesize_structural_grid(width, dna_span, dna_offset)
        length, synth_span_l = self._synthesize_structural_grid(length, dna_span, dna_offset)
        
        # Use simple average or W-span for the global col_span? 
        # Usually buildings have a square/regular grid. We'll prioritize W-span but keep logic robust.
        synth_col_span = (synth_span_w + synth_span_l) / 2.0
            
        floor_overrides = shell.get("floor_overrides", {})
        
        new_levels = []
        new_walls = []
        new_floors = []
        
        current_elev = 0.0
        for i in range(num_storeys + 1):
            is_roof = (i == num_storeys)
            level_idx = i + 1
            lvl_id = "AI_Level_{}".format(level_idx)
            lvl_name = "AI Level {}".format(level_idx)
            
            new_levels.append({"id": lvl_id, "name": lvl_name, "elevation": current_elev})
            
            if not is_roof:
                f_w = width
                f_l = length
                if str(level_idx) in floor_overrides:
                    ovr = floor_overrides[str(level_idx)]
                    f_w = safe_num(ovr.get("width", f_w), f_w)
                    f_l = safe_num(ovr.get("length", f_l), f_l)
                
                h_w = f_w / 2.0
                h_l = f_l / 2.0
                
                # Floor Points
                f_points = [
                    [-h_w, -h_l], [h_w, -h_l],
                    [h_w, h_l], [-h_w, h_l]
                ]
                new_floors.append({
                    "id": "AI_Floor_{}".format(level_idx),
                    "level_id": lvl_id,
                    "points": f_points
                })
                
                # 4 Walls
                pts = f_points 
                tags = ["S", "E", "N", "W"]
                for j in range(4):
                    p1 = pts[j]
                    p2 = pts[(j+1)%4]
                    new_walls.append({
                        "id": "AI_Wall_L{}_{}".format(level_idx, tags[j]),
                        "level_id": lvl_id,
                        "start": [p1[0], p1[1], 0],
                        "end": [p2[0], p2[1], 0],
                        "height": safe_num(height_overrides.get(str(level_idx), base_height), base_height)
                    })
            
            # Increment elevation
            current_elev += safe_num(height_overrides.get(str(level_idx), base_height), base_height)
            
        # --- LIFT GENERATION ---
        lifts_config = manifest.get("lifts", {})
        lift_walls = []
        core_bounds = None # (xmin, ymin, xmax, ymax)
        
        if lifts_config or num_storeys >= 3 or shell.get("include_lifts"):
            from . import lift_logic
            
            # Calculate Total Occupancy based on Efficiency & Load Factor
            total_occ = 0
            efficiency = preset.get("building_identity", {}).get("target_efficiency", 0.82)
            load_factor = preset.get("program_requirements", {}).get("occupancy_load_factor", 10.0)
            
            # Floor Centroid Detection
            min_x, min_y = float('inf'), float('inf')
            max_x, max_y = float('-inf'), float('-inf')
            
            for f in new_floors:
                pts = f.get("points", [])
                if len(pts) >= 3:
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    min_x = min(min_x, min(xs))
                    max_x = max(max_x, max(xs))
                    min_y = min(min_y, min(ys))
                    max_y = max(max_y, max(ys))
                    
                    raw_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
                    usable_area = raw_area * efficiency
                    total_occ += (usable_area / 1000000.0) / load_factor
            
            # Geometric Center of the building
            f_center_x = (min_x + max_x) / 2.0 if max_x > min_x else 0.0
            f_center_y = (min_y + max_y) / 2.0 if max_y > min_y else 0.0
            
            num_lifts = lifts_config.get("count")
            if num_lifts is None or num_lifts == "random":
                target_interval = safe_num(lifts_config.get("target_interval", preset.get("core_logic", {}).get("lift_waiting_time", 25.0)), 25.0)
                num_lifts = lift_logic.calculate_lift_requirements(
                    num_storeys, base_height, total_occ, 
                    target_interval
                )
            
            # Generate Lift Manifest (Handle Multi-Block Cores)
            num_lifts_val = int(num_lifts)
            lift_size = lifts_config.get("size", preset.get("core_logic", {}).get("lift_shaft_size", [2500, 2500]))
            lobby_w = lifts_config.get("lobby_width", 3000)
            
            layout = lift_logic.get_total_core_layout(num_lifts_val, lift_size, lobby_w)
            num_lifts_val = layout['total_lifts'] # Use adjusted count for balance
            
            # Center the entire assembly on Building Centroid
            center_pos = lifts_config.get("position")
            if not center_pos:
                center_pos = [f_center_x, f_center_y]
            
            remaining_lifts = num_lifts_val
            for b_idx in range(layout['num_blocks']):
                # Divide lifts among blocks
                b_lifts = min(remaining_lifts, layout['lifts_per_block'])
                remaining_lifts -= b_lifts
                
                # Offset each block back-to-back along Y
                # Since layout['total_d'] is based on (0,0), we offset from building center
                # Each block has its own internal lobby. 
                # For multiple blocks, we stack them relative to the building center.
                b_y_offset = (b_idx - (layout['num_blocks']-1)/2.0) * layout['block_d']
                b_center_pos = [center_pos[0], center_pos[1] + b_y_offset]
                
                b_manifest = lift_logic.generate_lift_shaft_manifest(
                    b_lifts, new_levels, 
                    center_pos=b_center_pos,
                    internal_size=lift_size,
                    lobby_width=lobby_w
                )
                # Tag elements with block index to avoid ID collision
                for w in b_walls:
                    w['id'] = f"{w['id']}_B{b_idx+1}"
                    lift_walls.append(w)
                
                for f in b_manifest.get("floors", []):
                    f['id'] = f"{f['id']}_B{b_idx+1}"
                    new_floors.append(f)
            
            new_walls.extend(lift_walls)
            
            # Calculate Core Bounds for Column Culling
            if lift_walls:
                xs = [w['start'][0] for w in lift_walls] + [w['end'][0] for w in lift_walls]
                ys = [w['start'][1] for w in lift_walls] + [w['end'][1] for w in lift_walls]
                core_bounds = (min(xs), min(ys), max(xs), max(ys))

        # --- COLUMN GENERATION ---
        new_columns = []
        col_span = safe_num(shell.get("column_span", shell.get("column_spacing")), None)
        
        # Synthesis Result fallback if no explicit override
        if col_span is None: 
            col_span = synth_col_span
        
        if col_span % 300 != 0: col_span = round(col_span / 300.0) * 300.0
        
        base_w = width
        base_l = length
        col_offset = dna_offset
        if col_offset > 0:
            base_w -= (2 * col_offset)
            base_l -= (2 * col_offset)
            
        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        def get_grid_offsets(dim_mm, span_mm, center_only=False, anchor_x=0.0):
            span_ft = mm_to_ft(span_mm)
            half_dim_ft = mm_to_ft(dim_mm) / 2.0
            anchor_ft = mm_to_ft(anchor_x)
            
            offsets = []
            # Start from anchor and go both ways
            curr = anchor_ft
            while curr <= half_dim_ft + 0.1:
                offsets.append(curr)
                curr += span_ft
            curr = anchor_ft - span_ft
            while curr >= -half_dim_ft - 0.1:
                offsets.append(curr)
                curr -= span_ft
            
            clean_offsets_ft = sorted(list(set(round(o, 4) for o in offsets)))
            if center_only and len(clean_offsets_ft) >= 3:
                clean_offsets_ft = [o for o in clean_offsets_ft if abs(o) < half_dim_ft - 0.1]
                
            from revit_mcp.utils import ft_to_mm
            return [ft_to_mm(o) for o in clean_offsets_ft]

        # Use Building Centroid as Anchor
        anchor_x = f_center_x
        anchor_y = f_center_y
            
        x_offsets = get_grid_offsets(base_w, synth_span_w, center_only, anchor_x)
        y_offsets = get_grid_offsets(base_l, synth_span_l, center_only, anchor_y)
        
        for k in range(num_storeys):
            lvl_id = "AI_Level_{}".format(k+1)
            top_lvl_id = "AI_Level_{}".format(k+2)
            
            for ox_mm in x_offsets:
                for oy_mm in y_offsets:
                    # 1. CULLING: Don't place columns inside core bounding box
                    if core_bounds:
                        m = 500 
                        if (core_bounds[0] - m <= ox_mm <= core_bounds[2] + m) and \
                           (core_bounds[1] - m <= oy_mm <= core_bounds[3] + m):
                            continue
                            
                    # Calculate IX/IY relative to anchor
                    ix = int(round((ox_mm - anchor_x) / synth_span_w))
                    iy = int(round((oy_mm - anchor_y) / synth_span_l))
                    col_id = "AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy)
                    new_columns.append({
                        "id": col_id,
                        "level_id": lvl_id,
                        "top_level_id": top_lvl_id,
                        "location": [ox_mm, oy_mm, 0],
                        "type": shell.get("column_type", "")
                    })

        return {"levels": new_levels, "walls": new_walls, "floors": new_floors, "columns": new_columns}

    def _find_type(self, category_bip, name):
        import Autodesk.Revit.DB as DB # type: ignore
        cl = DB.FilteredElementCollector(self.doc).OfCategory(category_bip).OfClass(DB.ElementType)
        for t in cl:
            if name.lower() in t.Name.lower(): return t
        return None

# Example Usage (not to be run directly as a script but used by the MCP)
# generator = BuildingSystem(doc, 5000, 8000, 3500)
# generator.generate()

