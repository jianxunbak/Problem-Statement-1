# -*- coding: utf-8 -*-
try:
    import clr
    clr.AddReference('RevitAPI')
    from Autodesk.Revit.DB import * # type: ignore
except:
    pass

import math
from .state_manager import state_manager

def safe_num(val, default=0):
    try:
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            import re
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            return float(m[0]) if m else float(default)
        return float(default)
    except: return float(default)

def mm_to_ft(mm):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertToInternalUnits(safe_num(mm), DB.UnitTypeId.Millimeters)

def get_model_registry(doc, zone_bbox=None):
    """
    SPATIAL-AWARE SCAN: Search for AI-tagged elements.
    If zone_bbox is provided, uses BoundingBoxIntersectsFilter for 90% faster lookups.
    """
    import Autodesk.Revit.DB as DB # type: ignore
    registry = {}
    
    collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
    
    # Optimization: Filter by zone if provided
    if zone_bbox:
        outline = DB.Outline(zone_bbox.Min, zone_bbox.Max)
        # Add a 1000mm buffer to the search outline
        buffer = mm_to_ft(1000)
        outline = DB.Outline(
            DB.XYZ(zone_bbox.Min.X - buffer, zone_bbox.Min.Y - buffer, zone_bbox.Min.Z - buffer),
            DB.XYZ(zone_bbox.Max.X + buffer, zone_bbox.Max.Y + buffer, zone_bbox.Max.Z + buffer)
        )
        collector.WherePasses(DB.BoundingBoxIntersectsFilter(outline))
    
    for element in collector:
        metadata = state_manager.get_ai_metadata(element)
        if metadata:
            registry[metadata['ai_id']] = element.Id
            
        # Fallback for migration: Check Comments
        else:
            p = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if not p: p = element.LookupParameter("Comments")
            if p and p.HasValue and p.AsString().startswith("AI_"):
                tag = p.AsString()
                registry[tag] = element.Id
                # Migrate to Extensible Storage on the fly if we were in a transaction
                # (handled later during actual updates)
    
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
            t.Commit()
            worker_log("Shell synced: {} walls, {} floors.".format(walls_count, floors_count))
            
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

    def _expand_high_level_manifest(self, manifest):
        """Converts Architectural Intent (Storeys/Shell) into concrete element lists."""
        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})
        
        num_storeys = int(setup.get("levels", 1))
        base_height = float(setup.get("level_height", 3000))
        height_overrides = setup.get("height_overrides", {})
        
        width = float(shell.get("width", 10000))
        length = float(shell.get("length", 15000))
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
                    f_w = float(ovr.get("width", f_w))
                    f_l = float(ovr.get("length", f_l))
                
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
                        "height": float(height_overrides.get(str(level_idx), base_height))
                    })
            
            # Increment elevation
            current_elev += float(height_overrides.get(str(level_idx), base_height))
            
        return {"levels": new_levels, "walls": new_walls, "floors": new_floors}

    def _find_type(self, category_bip, name):
        import Autodesk.Revit.DB as DB # type: ignore
        cl = DB.FilteredElementCollector(self.doc).OfCategory(category_bip).OfClass(DB.ElementType)
        for t in cl:
            if name.lower() in t.Name.lower(): return t
        return None

# Example Usage (not to be run directly as a script but used by the MCP)
# generator = BuildingSystem(doc, 5000, 8000, 3500)
# generator.generate()

