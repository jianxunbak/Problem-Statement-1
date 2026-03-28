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

from revit_mcp.utils import safe_num, mm_to_ft

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

    def _expand_high_level_manifest(self, manifest):
        """Converts Architectural Intent (Storeys/Shell) into concrete element lists."""
        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})
        
        num_storeys = int(safe_num(setup.get("levels", 1), 1))
        base_height = safe_num(setup.get("level_height", 3000), 3000.0)
        height_overrides = setup.get("height_overrides", {})
        
        width = safe_num(shell.get("width", 10000), 10000.0)
        length = safe_num(shell.get("length", 15000), 15000.0)
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
            
        # --- COLUMN GENERATION ---
        new_columns = []
        # Determine Span and Rules: Priority -> shell.column_span -> detect from existing -> default 10m
        col_span = safe_num(shell.get("column_span", shell.get("column_spacing")), None)
        
        # 0. INFERENCE: If manifest doesn't specify, try to detect from existing model
        if col_span is None:
            existing_pts = []
            for tag, eid in self.registry.items():
                if tag.startswith("AI_Col_L1_"):
                    el = self.doc.GetElement(eid)
                    if el and hasattr(el.Location, "Point"):
                        existing_pts.append(el.Location.Point)
            
            if len(existing_pts) >= 2:
                pts_sorted = sorted(existing_pts, key=lambda p: (round(p.X, 2), round(p.Y, 2)))
                for i in range(len(pts_sorted)-1):
                    d = pts_sorted[i].DistanceTo(pts_sorted[i+1])
                    if d > 1.0: # at least ~300mm
                        detected_mm = round(d * 304.8 / 100.0) * 100.0
                        if detected_mm > 2000:
                            col_span = detected_mm
                            break
        
        if col_span is None:
            col_span = 10000.0 # Default to 10m if completely unknown (premium standard)
            
        if col_span % 300 != 0: 
            col_span = round(col_span / 300.0) * 300.0
        
        # 1. DYNAMIC GRID: Cover the maximum extent of ANY floor in the manifest
        # This ensures columns exist for expanded floors.
        all_w = [width]
        all_l = [length]
        for v in floor_overrides.values():
            all_w.append(safe_num(v.get("width", width), width))
            all_l.append(safe_num(v.get("length", length), length))
            
        base_w = max(all_w)
        base_l = max(all_l)
        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        def get_grid_offsets(dim_mm, span_mm, center_only=False):
            span_ft = mm_to_ft(span_mm)
            half_dim_ft = mm_to_ft(dim_mm) / 2.0
            # Phase Detection (Symmetric centering default)
            # Odd num -> column at 0 (shift 0)
            # Even num -> columns at +/- span/2 (shift span/2)
            num = int(math.ceil(dim_mm / span_mm))
            shift = 0.0
            if num % 2 == 0: shift = span_ft / 2.0
            
            offsets = []
            curr = shift
            while curr <= half_dim_ft + 0.1:
                offsets.append(curr)
                if abs(curr) > 0.001: offsets.append(-curr)
                curr += span_ft
            curr = shift - span_ft
            while curr >= -half_dim_ft - 0.1:
                offsets.append(curr)
                curr -= span_ft
            
            clean_offsets_ft = sorted(list(set(round(o, 4) for o in offsets)))
            if center_only and len(clean_offsets_ft) >= 3:
                clean_offsets_ft = [o for o in clean_offsets_ft if abs(o) < half_dim_ft - 0.1]
                
            return [ft_to_mm(o) for o in clean_offsets_ft]

        from revit_mcp.utils import ft_to_mm
        x_offsets = get_grid_offsets(base_w, col_span, center_only)
        y_offsets = get_grid_offsets(base_l, col_span, center_only)
        
        for i in range(num_storeys):
            lvl_id = "AI_Level_{}".format(i+1)
            top_lvl_id = "AI_Level_{}".format(i+2)
            
            for ox_mm in x_offsets:
                for oy_mm in y_offsets:
                    ix = int(round(ox_mm / col_span))
                    iy = int(round(oy_mm / col_span))
                    col_id = "AI_Col_L{}_GX{}_GY{}".format(i+1, ix, iy)
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

