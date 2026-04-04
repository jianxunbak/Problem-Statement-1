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
from . import staircase_logic

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
        from revit_mcp.building_generator import get_model_registry
        from revit_mcp.utils import mm_to_ft, safe_num, setup_failure_handling, nuclear_lockdown
        
        # 0. NUCLEAR LOCKDOWN: Forcefully disjoint all walls before any sync moves
        nuclear_lockdown(self.doc)
        
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
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
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
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
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
                
            t.Commit()
            worker_log("Shell synced: {} walls, {} floors.".format(walls_count, floors_count))
            
            # --- PHASE 3: COLUMNS ---
            worker_log("PHASE 3: Syncing Columns...")
            t = DB.Transaction(self.doc, "AI Sync: Columns")
            t.Start()
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
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
            
            # Update existing
            from revit_mcp.utils import disallow_joins
            # PRE-MOVE LOCK
            disallow_joins(wall)
            wall.Location.Curve = line
            # POST-MOVE RE-ENFORCE
            disallow_joins(wall)
        else:
            # Create new
            wall = DB.Wall.Create(self.doc, line, level.Id, False)
            from revit_mcp.utils import disallow_joins
            # POST-CREATION LOCK
            disallow_joins(wall)
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
        
        # Default Centroid
        f_center_x, f_center_y = 0.0, 0.0
        
        if lifts_config or num_storeys >= 3 or shell.get("include_lifts"):
            from . import lift_logic
            
            # Calculate Total Occupancy based on Efficiency & Load Factor
            total_occ = 0
            efficiency = preset.get("building_identity", {}).get("target_efficiency", 0.82)
            load_factor = preset.get("program_requirements", {}).get("occupancy_load_factor", 10.0)
            
            # STABLE ANCHORING: Use Level 1 Centroid for core placement.
            # This prevents core 'shivering' when upper floor plate sizes are modified.
            l1_floor = next((f for f in new_floors if f.get("level_id") == "AI_Level_1"), new_floors[0])
            l1_pts = l1_floor.get("points", [])
            if len(l1_pts) >= 3:
                l1_xs = [p[0] for p in l1_pts]
                l1_ys = [p[1] for p in l1_pts]
                f_center_x = (min(l1_xs) + max(l1_xs)) / 2.0
                f_center_y = (min(l1_ys) + max(l1_ys)) / 2.0
            else:
                f_center_x, f_center_y = 0.0, 0.0
            
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
                # STABLE OFFSET: Block 1 is always at center (0 offset).
                b_y_offset = lift_logic.get_block_y_offset(b_idx, layout['num_blocks'], layout['block_d'])
                b_center_pos = [center_pos[0], center_pos[1] + b_y_offset]
                
                b_manifest = lift_logic.generate_lift_shaft_manifest(
                    b_lifts, new_levels, 
                    center_pos=b_center_pos,
                    internal_size=lift_size,
                    lobby_width=lobby_w
                )
                # Tag elements with block index to avoid ID collision
                for w in b_manifest.get("walls", []):
                    w['id'] = "{}_B{}".format(w['id'], b_idx + 1)
                    lift_walls.append(w)
                
                for f in b_manifest.get("floors", []):
                    f['id'] = "{}_B{}".format(f['id'], b_idx + 1)
                    new_floors.append(f)
            
            if lift_walls:
                new_walls.extend(lift_walls)
                xs = [w['start'][0] for w in lift_walls] + [w['end'][0] for w in lift_walls]
                ys = [w['start'][1] for w in lift_walls] + [w['end'][1] for w in lift_walls]
                l_bounds = (min(xs), min(ys), max(xs), max(ys))
                if core_bounds:
                    core_bounds = (
                        min(core_bounds[0], l_bounds[0]),
                        min(core_bounds[1], l_bounds[1]),
                        max(core_bounds[2], l_bounds[2]),
                        max(core_bounds[3], l_bounds[3])
                    )
                else:
                    core_bounds = l_bounds

        # --- STAIRCASE GENERATION ---
        preset_fs = preset.get("core_logic", {}).get("fire_safety", {})
        p_num_stairs = preset_fs.get("fire_escape_staircases", 2)
        p_stair_spec = preset_fs.get("staircase_spec", {})
        max_travel = safe_num(preset_fs.get("max_travel_distance", 60000), 60000)

        m_stair_config = manifest.get("staircases", {})
        num_stairs = int(safe_num(m_stair_config.get("count", p_num_stairs), p_num_stairs))
        if num_storeys >= 2:
            num_stairs = max(num_stairs, 2)

        stair_spec = p_stair_spec.copy()
        m_spec = m_stair_config.get("spec", {})
        for k in ["riser", "tread", "width_of_flight", "landing_width"]:
            if k in m_spec:
                stair_spec[k] = safe_num(m_spec[k], stair_spec.get(k))

        if num_stairs > 0 and num_storeys >= 2:
            # Floor dims for 60 m rule check (use base envelope)
            floor_dims_for_stairs = [(width, length)]

            # Calculate positions: at Y-ends of lift core, with 60 m check
            positions = staircase_logic.calculate_staircase_positions(
                floor_dims_for_stairs,
                (f_center_x, f_center_y),
                core_bounds,  # already mm in this path
                base_height,
                stair_spec,
                max_travel
            )

            # Enclosure width = lift core width for alignment (rule c)
            lift_core_w = (core_bounds[2] - core_bounds[0]) if core_bounds else 0
            shaft_w_nat, shaft_d = staircase_logic.get_shaft_dimensions(base_height, stair_spec)
            enc_w = max(lift_core_w, shaft_w_nat)

            stair_manifest = staircase_logic.generate_staircase_manifest(
                positions, new_levels, enc_w, stair_spec
            )

            new_walls.extend(stair_manifest.get("walls", []))
            new_floors.extend(stair_manifest.get("floors", []))

            # Update Core Bounds for Column Culling
            if stair_manifest.get("walls"):
                s_xs = [w['start'][0] for w in stair_manifest['walls']] + [w['end'][0] for w in stair_manifest['walls']]
                s_ys = [w['start'][1] for w in stair_manifest['walls']] + [w['end'][1] for w in stair_manifest['walls']]
                if core_bounds:
                    core_bounds = (
                        min(core_bounds[0], min(s_xs)),
                        min(core_bounds[1], min(s_ys)),
                        max(core_bounds[2], max(s_xs)),
                        max(core_bounds[3], max(s_ys))
                    )
                else:
                    core_bounds = (min(s_xs), min(s_ys), max(s_xs), max(s_ys))

        # --- COLUMN GENERATION ---
        # Uniform grid from center + edge columns always at offset_from_edge.
        # Core is structural — no columns inside core footprint.
        new_columns = []
        # Use synthesis span (already optimized for building dimensions)
        col_span = synth_col_span
        if col_span % 300 != 0: col_span = round(col_span / 300.0) * 300.0

        col_offset = dna_offset  # offset_from_edge in mm

        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        def get_grid_offsets_mm(dim_mm, span_mm, anchor_mm=0.0):
            """Uniform grid from anchor + edge columns always at offset_from_edge (all mm)."""
            half_dim = dim_mm / 2.0
            limit = half_dim - col_offset
            offsets = set()
            # Always include edge columns
            offsets.add(round(limit, 1))
            offsets.add(round(-limit, 1))
            # Regular grid from anchor
            curr = anchor_mm
            while curr <= limit + 1.0:
                offsets.add(round(curr, 1))
                curr += span_mm
            curr = anchor_mm - span_mm
            while curr >= -limit - 1.0:
                offsets.add(round(curr, 1))
                curr -= span_mm
            return sorted(offsets)

        x_offsets = get_grid_offsets_mm(width, synth_span_w, f_center_x)
        y_offsets = get_grid_offsets_mm(length, synth_span_l, f_center_y)

        if core_bounds:
            anchor_x = (core_bounds[0] + core_bounds[2]) / 2.0
            anchor_y = (core_bounds[1] + core_bounds[3]) / 2.0
        else:
            anchor_x = f_center_x
            anchor_y = f_center_y

        if center_only:
            half_w = width / 2.0
            half_l = length / 2.0
            x_offsets = [o for o in x_offsets if abs(o) < half_w - 1.0]
            y_offsets = [o for o in y_offsets if abs(o) < half_l - 1.0]

        for k in range(num_storeys):
            lvl_id = "AI_Level_{}".format(k+1)
            top_lvl_id = "AI_Level_{}".format(k+2)

            for ox_mm in x_offsets:
                for oy_mm in y_offsets:
                    # Cull columns strictly inside core footprint (core is structural)
                    if core_bounds:
                        if (core_bounds[0] < ox_mm < core_bounds[2]) and \
                           (core_bounds[1] < oy_mm < core_bounds[3]):
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

