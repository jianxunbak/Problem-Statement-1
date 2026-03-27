# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# Each method does its own local import on the correct thread.
from .gemini_client import client
from .bridge import mcp_event_handler
import math

def safe_num(val, default=0):
    try:
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            # Strip non-numeric like 'mm'
            import re
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            return float(m[0]) if m else float(default)
        return float(default)
    except: return float(default)

def mm_to_ft(mm): 
    from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
    return UnitUtils.ConvertToInternalUnits(safe_num(mm), UnitTypeId.Millimeters)

def sqmm_to_sqft(sqmm):
    from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
    return UnitUtils.ConvertToInternalUnits(float(sqmm), UnitTypeId.SquareMillimeters)

def safe_set_comment(element, tag):
    import Autodesk.Revit.DB as DB # type: ignore
    # Try global instance comments first
    p = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if not p:
        p = element.LookupParameter("Comments")
    if p:
        p.Set(tag)
    return p

def get_location_line_param(wall):
    import Autodesk.Revit.DB as DB # type: ignore
    bip = getattr(DB.BuiltInParameter, "WALL_KEY_REF_PARAM", None)
    if bip:
        p = wall.get_Parameter(bip)
        if p: return p
    return wall.LookupParameter("Location Line")

class RevitWorkers:
    def __init__(self, doc):
        self.doc = doc

    def log(self, message):
        client.log(message)

    def execute_fast_manifest(self, manifest):
        """High-speed execution using intent-based logic and state-aware updates"""
        self.log("--- execute_fast_manifest START ---")
        import Autodesk.Revit.DB as DB # type: ignore
        from .building_generator import get_model_registry # type: ignore
        doc = self.doc
        results = {"levels": [], "elements": []}
        
        # 1. Faster Registry Scan
        self.log("Step 1: Scanning model for AI-tagged elements...")
        registry = get_model_registry(doc)
        self.log("Scan complete. Registry size: {}.".format(len(registry)))
            
        reused_count = 0
        created_count = 0
        deleted_count = 0
        
        # 1-A. Calculate defaults from existing state
        existing_levels_count = sum(1 for k in registry.keys() if k.startswith("AI_Level_"))
        default_levels = max(1, existing_levels_count - 1)
        
        default_height = 4000
        if "AI_Level_1" in registry and "AI_Level_2" in registry:
            try:
                l1 = doc.GetElement(registry["AI_Level_1"])
                l2 = doc.GetElement(registry["AI_Level_2"])
                if l1 and l2:
                    from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
                    diff_ft = l2.Elevation - l1.Elevation
                    default_height = UnitUtils.ConvertFromInternalUnits(diff_ft, UnitTypeId.Millimeters)
            except: pass

        tg = DB.TransactionGroup(doc, "AI Build: Fast Manifest")
        tg.Start()

        self.log("Step 2: Processing levels from manifest...")
        t = DB.Transaction(doc, "AI Build: Levels")
        t.Start()
        setup = manifest.get("project_setup", {})
        levels_val = setup.get("levels", setup.get("storeys", setup.get("floors", default_levels)))
        height_val = setup.get("level_height", setup.get("floor_height", default_height))
        self.log("Config: {} storeys, {} default height.".format(levels_val, height_val))
        
        # Parse custom height overrides (e.g., {"1": 5000, "2": 4500})
        height_overrides = setup.get("height_overrides", {})
        
        # Handle cases where Gemini returns a list or a single integer
        import random
        if isinstance(levels_val, list):
            count = len(levels_val)
            elevations = [mm_to_ft(e) for e in levels_val]
        else:
            count = int(safe_num(levels_val, 1))
            elevations = [0.0]
            current_elev = 0.0
            # N storeys means N floor heights, resulting in N+1 levels
            for i in range(1, count + 1):
                h_over = height_overrides.get(str(i))
                # Robust Randomization for Heights
                if h_over == "random" or height_val == "random":
                    h_mm = random.uniform(3200, 4200)
                else:
                    h_mm = safe_num(h_over) if h_over is not None else safe_num(height_val, 3500)
                
                current_elev += mm_to_ft(h_mm)
                elevations.append(current_elev)
            # count is now the number of storeys, number of levels is count + 1
            levels_total = len(elevations)
        
        # 1-A. Extract Column Spacing and Footprint Extents
        shell = manifest.get("shell", {})
        column_spacing_mm = shell.get("column_spacing", shell.get("column_span", 6000))
        spacing = mm_to_ft(column_spacing_mm)
        
        w_default = 10000
        l_default = 15000
        try:
            if "AI_Wall_L1_S" in registry:
                w_wall = doc.GetElement(registry["AI_Wall_L1_S"])
                if w_wall and hasattr(w_wall.Location, "Curve"):
                    from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
                    w_default = UnitUtils.ConvertFromInternalUnits(w_wall.Location.Curve.Length, UnitTypeId.Millimeters)
            if "AI_Wall_L1_W" in registry:
                e_wall = doc.GetElement(registry["AI_Wall_L1_W"])
                if e_wall and hasattr(e_wall.Location, "Curve"):
                    from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
                    l_default = UnitUtils.ConvertFromInternalUnits(e_wall.Location.Curve.Length, UnitTypeId.Millimeters)
        except: pass
        
        current_levels = []
        for i in range(levels_total):
            tag = "AI_Level_" + str(i+1)
            lvl_name = "AI Level " + str(i+1)
            lvl_name_alt = "Level " + str(i+1)
            elev = elevations[i]
            
            # 1. Try Registry Tag (State-Aware)
            lvl = None
            if tag in registry:
                lvl = doc.GetElement(registry[tag])
            
            # 2. Try Fallback Name (BIM Integrity)
            if not lvl:
                for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
                    if l.Name == lvl_name or l.Name == lvl_name_alt:
                        lvl = l; break
            
            # 3. Update or Create
            if lvl and isinstance(lvl, DB.Level):
                lvl.Elevation = elev
                safe_set_comment(lvl, tag)
                # Keep the name consistent
                try: lvl.Name = lvl_name
                except: pass
                reused_count += 1
            else:
                lvl = DB.Level.Create(doc, elev)
                try: lvl.Name = lvl_name
                except: pass 
                safe_set_comment(lvl, tag)
                created_count += 1
                
            current_levels.append(lvl)
            results["levels"].append(str(lvl.Id.Value))
            
            # 4. Ensure Floor Plan view exists for every level
            try:
                view_exists = False
                all_views = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
                for v in all_views:
                    if v.GenLevel and v.GenLevel.Id == lvl.Id and v.ViewType == DB.ViewType.FloorPlan:
                        view_exists = True
                        break
                
                if not view_exists:
                    # Find a FloorPlan ViewFamilyType
                    vt = None
                    for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                        if vft.ViewFamily == DB.ViewFamily.FloorPlan:
                            vt = vft
                            break
                    if vt:
                        DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
            except: pass
            
        # 1-A. Finalize levels so they are hostable
        doc.Regenerate()
        t.Commit()

        # 2. Grid System (Aligned to Shell)
        t = DB.Transaction(doc, "AI Build: Shell")
        t.Start()
        w = mm_to_ft(shell.get("width", w_default))
        l = mm_to_ft(shell.get("length", l_default))
        
        # Grids moved to Phase 5.5 to allow alignment with dynamic columns
        pass

        # 1-B. Pre-compute dimensions for all floors
        floor_overrides = shell.get("floor_overrides", {})
        shelter_floors = shell.get("shelter_floors", True)
        
        # Robust Fallback for Base Dimensions
        raw_w = shell.get("width", w_default)
        raw_l = shell.get("length", l_default)
        base_w = safe_num(raw_w, 30000) if raw_w != "random" else 30000
        base_l = safe_num(raw_l, 50000) if raw_l != "random" else 50000
        
        floor_dims = []
        for k in range(len(current_levels)):
            floor_num = str(k + 1)
            overrides = floor_overrides.get(floor_num, {})
            
            w_val = overrides.get("width", shell.get("width", w_default))
            l_val = overrides.get("length", shell.get("length", l_default))
            
            # Algorithmic Randomization (High Speed)
            if w_val == "random":
                w_mm = base_w * random.uniform(0.8, 1.2)
            else:
                w_mm = safe_num(w_val, base_w)
                
            if l_val == "random":
                l_mm = base_l * random.uniform(0.8, 1.2)
            else:
                l_mm = safe_num(l_val, base_l)
                
            # Ensure minimum viable size (1m)
            w_mm = max(1000.0, w_mm)
            l_mm = max(1000.0, l_mm)
            floor_dims.append((mm_to_ft(w_mm), mm_to_ft(l_mm)))

        # 3. Mass Shelling (State-Aware Walls)
        updated_walls = []
        for k, lvl in enumerate(current_levels):
            w_k, l_k = floor_dims[k]
            points_k = [DB.XYZ(0,0,0), DB.XYZ(w_k,0,0), DB.XYZ(w_k,l_k,0), DB.XYZ(0,l_k,0)]
            wall_tags = ["AI_Wall_L{}_N", "AI_Wall_L{}_E", "AI_Wall_L{}_S", "AI_Wall_L{}_W"]
            
            for j in range(4):
                tag = wall_tags[j].format(k+1)
                p_start = DB.XYZ(points_k[j].X, points_k[j].Y, elevations[k])
                p_end = DB.XYZ(points_k[(j+1)%4].X, points_k[(j+1)%4].Y, elevations[k])
                
                # ROBUSTNESS: Check distance BEFORE CreateBound to prevent "Short Curve" crash
                if p_start.DistanceTo(p_end) < mm_to_ft(2.0): continue 
                
                line = DB.Line.CreateBound(p_start, p_end)
                if line.Length < mm_to_ft(100): continue 
                
                wall_id = registry.get(tag)
                wall = doc.GetElement(wall_id) if wall_id else None
                
                if wall and isinstance(wall, DB.Wall):
                    # SPEED BOOST: Disable Auto-Join while moving
                    if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                    
                    half_w = wall.WallType.Width / 2.0
                    center = (points_k[0] + points_k[2]) * 0.5
                    inward_vec = (center - line.Evaluate(0.5, True)).Normalize()
                    wall.Location.Curve = line.CreateTransformed(DB.Transform.CreateTranslation(inward_vec * half_w))
                    
                    reused_count += 1
                    updated_walls.append(wall)
                else:
                    wall = DB.Wall.Create(doc, line, lvl.Id, False)
                    if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                    half_w = wall.WallType.Width / 2.0
                    center = (points_k[0] + points_k[2]) * 0.5
                    inward_vec = (center - line.Evaluate(0.5, True)).Normalize()
                    wall.Location.Curve = line.CreateTransformed(DB.Transform.CreateTranslation(inward_vec * half_w))
                    safe_set_comment(wall, tag)
                    created_count += 1
                    updated_walls.append(wall)
                
                # Rule: Top Constraint and Height
                try:
                    h_ft = elevations[k+1] - elevations[k] if k < len(current_levels)-1 else mm_to_ft(1000)
                    if k < len(current_levels) - 1:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k+1].Id)
                    else:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                        h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if h_param: h_param.Set(h_ft)
                    wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(0.0)
                    wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(0.0)
                except: pass
                
                results["elements"].append(str(wall.Id.Value))

        # 3.5 Intermediate Parapets (Exposed Roofs)
        if shelter_floors:
            for k, lvl in enumerate(current_levels):
                if k == 0: continue
                w_k, l_k = floor_dims[k]
                w_below, l_below = floor_dims[k-1]
                
                if w_below <= w_k and l_below <= l_k: continue
                
                w_slab = max(w_k, w_below)
                l_slab = max(l_k, l_below)
                elev = elevations[k]
                parapet_lines = []
                
                if w_slab > w_k:
                    parapet_lines.append( (DB.XYZ(w_k, 0, elev), DB.XYZ(w_slab, 0, elev), "S") )
                    parapet_lines.append( (DB.XYZ(w_slab, 0, elev), DB.XYZ(w_slab, l_slab, elev), "E") )
                elif l_slab > l_k:
                    parapet_lines.append( (DB.XYZ(w_slab, l_k, elev), DB.XYZ(w_slab, l_slab, elev), "E") )
                    
                if l_slab > l_k:
                    parapet_lines.append( (DB.XYZ(w_slab, l_slab, elev), DB.XYZ(0, l_slab, elev), "N") )
                    parapet_lines.append( (DB.XYZ(0, l_slab, elev), DB.XYZ(0, l_k, elev), "W") )
                elif w_slab > w_k:
                    parapet_lines.append( (DB.XYZ(w_slab, l_slab, elev), DB.XYZ(w_k, l_slab, elev), "N") )
                    
                for p_start, p_end, face in parapet_lines:
                    tag = "AI_Parapet_L{}_{}".format(k+1, face)
                    # ROBUSTNESS: Check distance before creating parapet line
                    if p_start.DistanceTo(p_end) < mm_to_ft(2.0): continue 
                    line = DB.Line.CreateBound(p_start, p_end)
                    
                    if tag in registry:
                        wall = doc.GetElement(registry[tag])
                        if isinstance(wall, DB.Wall):
                            half_w = wall.WallType.Width / 2.0
                            slab_center = DB.XYZ(w_slab/2, l_slab/2, elev)
                            mid = line.Evaluate(0.5, True)
                            inward_vec = (slab_center - mid).Normalize()
                            
                            trans = DB.Transform.CreateTranslation(inward_vec * half_w)
                            offset_line = line.CreateTransformed(trans)
                            wall.Location.Curve = offset_line
                            
                            # doc.Regenerate()
                            
                            reused_count += 1
                            h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                            if h_param and not h_param.IsReadOnly: h_param.Set(mm_to_ft(1000))
                            try:
                                wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(0.0)
                                wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(0.0)
                            except: pass
                            
                            # Re-verify orientation
                            # ... Flip logic if needed (usually handled by the fact it was already flipped)
                            
                            results["elements"].append(str(wall.Id.Value))
                            continue
                            
                    wall = DB.Wall.Create(doc, line, lvl.Id, False)
                    created_count += 1
                    safe_set_comment(wall, tag)
                    
                    # Manual Alignment: Shift Inward
                    half_w = wall.WallType.Width / 2.0
                    slab_center = DB.XYZ(w_slab/2, l_slab/2, elev)
                    mid = line.Evaluate(0.5, True)
                    inward_vec = (slab_center - mid).Normalize()
                    
                    trans = DB.Transform.CreateTranslation(inward_vec * half_w)
                    offset_line = line.CreateTransformed(trans)
                    wall.Location.Curve = offset_line
                    
                    # doc.Regenerate()
                    
                    try:
                        h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if h_param: h_param.Set(mm_to_ft(1000))
                        wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(0.0)
                        wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(0.0)
                    except: pass
                    
                    # Ensure orientation is Outward
                    try:
                        # doc.Regenerate()
                        # Use a simple center for intermediate parapets based on the slab they are on
                        slab_center = DB.XYZ(w_slab/2, l_slab/2, elev)
                        mid = line.Evaluate(0.5, True)
                        if wall.Orientation.DotProduct(mid - slab_center) < 0:
                            wall.Flip()
                    except: pass
                    
                    results["elements"].append(str(wall.Id.Value))

        
        # 4. State-Aware Floors (Optimized)
        import System.Collections.Generic as Generic # type: ignore
        ft_floor = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        if ft_floor:
            for k, lvl in enumerate(current_levels):
                w_k, l_k = floor_dims[k]
                if shelter_floors and k > 0:
                    w_below, l_below = floor_dims[k-1]
                    w_slab, l_slab = max(w_k, w_below), max(l_k, l_below)
                else:
                    w_slab, l_slab = w_k, l_k
                    
                points_k = [DB.XYZ(0,0,0), DB.XYZ(w_slab,0,0), DB.XYZ(w_slab,l_slab,0), DB.XYZ(0,l_slab,0)]
                tag = "AI_Floor_L{}".format(k+1)
                loop = DB.CurveLoop()
                for j in range(4):
                    p1, p2 = points_k[j], points_k[(j+1)%4]
                    if p1.DistanceTo(p2) < mm_to_ft(2.0): continue 
                    loop.Append(DB.Line.CreateBound(p1, p2))
                
                if loop.IsOpen(): continue
                if not any(loop): continue # Building segment too small for a floor
                
                if tag in registry:
                    existing = doc.GetElement(registry[tag])
                    if existing: 
                        doc.Delete(existing.Id)
                
                loops = Generic.List[DB.CurveLoop]()
                loops.Add(loop)
                floor = DB.Floor.Create(doc, loops, ft_floor.Id, lvl.Id)
                safe_set_comment(floor, tag)
                results["elements"].append(str(floor.Id.Value))
                created_count += 1

        # doc.Regenerate() # Final sync
        t.Commit()
        
        # 5. State-Aware Columns (Structural with Architectural Fallback)
        t = DB.Transaction(doc, "AI Build: Structural")
        t.Start()
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        stype = DB.Structure.StructuralType.Column
        
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
            stype = DB.Structure.StructuralType.NonStructural
            
        if symbol:
            if not symbol.IsActive: symbol.Activate()
            
            # Get column size for edge protection
            col_hw, col_hd = 0.0, 0.0
            try:
                pw = symbol.LookupParameter("Width") or symbol.get_Parameter(DB.BuiltInParameter.COLUMN_WIDTH_PARAM)
                pd = symbol.LookupParameter("Depth") or symbol.get_Parameter(DB.BuiltInParameter.COLUMN_DEPTH_PARAM)
                if pw: col_hw = pw.AsDouble() / 2.0
                if pd: col_hd = pd.AsDouble() / 2.0
            except: pass

            # 5.1 PRE-CALCULATE GLOBAL GRID (Structural Alignment)
            max_w = max(d[0] for d in floor_dims)
            max_l = max(d[1] for d in floor_dims)
            nx = int(math.ceil(max_w / spacing)) if max_w > spacing else 1
            ny = int(math.ceil(max_l / spacing)) if max_l > spacing else 1
            actual_sx = max_w / nx
            actual_sy = max_l / ny
            
            # 5.2 PRE-CALCULATE VERTICAL CONTINUITY 
            highest_level_needed = {} 
            for ix in range(nx + 1):
                for iy in range(ny + 1):
                    px, py = ix * actual_sx, iy * actual_sy
                    if ix == 0: px += col_hw
                    elif ix == nx: px -= col_hw
                    if iy == 0: py += col_hd
                    elif iy == ny: py -= col_hd
                    
                    max_k = -1
                    for k, dims in enumerate(floor_dims):
                        w_k, l_k = dims
                        if (px <= w_k + 0.01) and (py <= l_k + 0.01):
                            max_k = k
                    highest_level_needed[(ix, iy)] = max_k

            # 5.3 PLACE COLUMNS
            for k, lvl in enumerate(current_levels[:-1]):
                elev = elevations[k]
                for ix in range(nx + 1):
                    for iy in range(ny + 1):
                        if highest_level_needed[(ix, iy)] < k: continue
                        
                        tag = "AI_Col_L{}_G{}_{}".format(k+1, ix, iy)
                        px, py = ix * actual_sx, iy * actual_sy
                        if ix == 0: px += col_hw
                        elif ix == nx: px -= col_hw
                        if iy == 0: py += col_hd
                        elif iy == ny: py -= col_hd
                        
                        p = DB.XYZ(px, py, elev)
                        col = doc.GetElement(registry[tag]) if tag in registry else None
                        if not (col and isinstance(col, DB.FamilyInstance)):
                            col = doc.Create.NewFamilyInstance(p, symbol, lvl, stype)
                            safe_set_comment(col, tag)
                            created_count += 1
                        else:
                            col.Location.Point = p
                            reused_count += 1
                        
                        try:
                            top_lvl = current_levels[k+1]
                            col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(top_lvl.Id)
                            col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(0.0)
                            col.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM).Set(0.0)
                        except: pass
                        results["elements"].append(str(col.Id.Value))

            # 5.5. GLOBAL GRIDS (Covers the Union)
            buff = mm_to_ft(3000)
            
            # Helper to manage unique grid names robustly
            def apply_unique_grid_name(grid, desired_name):
                if grid.Name == desired_name: return
                # Check for conflicts
                other_grid = next((g for g in DB.FilteredElementCollector(doc).OfClass(DB.Grid) if g.Name == desired_name and g.Id != grid.Id), None)
                if other_grid:
                    # Rename the obstacle to something temporary
                    import time
                    try: other_grid.Name = "TEMP_GRID_" + str(int(time.time() * 1000))[5:]
                    except: pass 
                try: grid.Name = desired_name
                except: pass

            for ix in range(nx + 1):
                tag = "AI_Grid_X_{}".format(ix)
                px = ix * actual_sx
                if ix == 0: px += col_hw
                elif ix == nx: px -= col_hw
                
                line = DB.Line.CreateBound(DB.XYZ(px, -buff, 0), DB.XYZ(px, max_l + buff, 0))
                grid_id = registry.get(tag)
                grid = doc.GetElement(grid_id) if grid_id else None
                
                if not (grid and isinstance(grid, DB.Grid)):
                    grid = DB.Grid.Create(doc, line)
                    safe_set_comment(grid, tag)
                    apply_unique_grid_name(grid, "AI X" + str(ix+1))
                    created_count += 1
                else:
                    grid.Curve = line
                    reused_count += 1
                
                try: grid.SetVerticalExtents(current_levels[0].Elevation - buff, elevations[-1] + buff)
                except: pass
                results["elements"].append(str(grid.Id.Value))

            for iy in range(ny + 1):
                tag = "AI_Grid_Y_{}".format(iy)
                py = iy * actual_sy
                if iy == 0: py += col_hd
                elif iy == ny: py -= col_hd
                
                line = DB.Line.CreateBound(DB.XYZ(-buff, py, 0), DB.XYZ(max_w + buff, py, 0))
                grid_id = registry.get(tag)
                grid = doc.GetElement(grid_id) if grid_id else None
                
                if not (grid and isinstance(grid, DB.Grid)):
                    grid = DB.Grid.Create(doc, line)
                    safe_set_comment(grid, tag)
                    apply_unique_grid_name(grid, "AI Y" + str(iy+1))
                    created_count += 1
                else:
                    grid.Curve = line
                    reused_count += 1
                    
                try: grid.SetVerticalExtents(current_levels[0].Elevation - buff, elevations[-1] + buff)
                except: pass
                results["elements"].append(str(grid.Id.Value))
        # 6. RE-ENABLE AUTO-JOIN FOR PERFORMANCE
        for w in updated_walls:
            try:
                if hasattr(w, "SetAllowAutoJoin"): w.SetAllowAutoJoin(True)
            except: pass

        # 7. CLEANUP UNUSED REGISTRY ENTRIES
        all_touched_ids = set(results["levels"]) | set(results["elements"])
        # Non-levels first
        for tag, eid in registry.items():
            if str(eid.Value) not in all_touched_ids:
                el = doc.GetElement(eid)
                if el and not isinstance(el, DB.Level): 
                    doc.Delete(eid)
                    deleted_count += 1
        # Levels last
        for tag, eid in registry.items():
            if str(eid.Value) not in all_touched_ids:
                el = doc.GetElement(eid)
                if el and isinstance(el, DB.Level): 
                    doc.Delete(eid)
                    deleted_count += 1

        # 7.5 FINAL SYNC FOR ANNOTATIONS
        doc.Regenerate()

        # 7. Annotate Grids (2D Dimensions in ALL Floor Plans)
        try:
            all_plans = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
            for v in all_plans:
                if not v.IsTemplate and v.ViewType == DB.ViewType.FloorPlan:
                    if any(l.Id == v.GenLevel.Id for l in current_levels):
                        self._dimension_grids_in_view(v)
        except Exception as e:
            self.log("RevitWorkers: Global Dimensioning failed: {}".format(str(e)))

        # 8. Automated Cross-Sections (X-X and Y-Y)
        try:
            max_w = max(d[0] for d in floor_dims)
            max_l = max(d[1] for d in floor_dims)
            max_h = elevations[-1] + mm_to_ft(5000)
            
            # Section A (X-X, Looking North)
            sec_a = self._create_or_update_section("Section X-X", 
                                          DB.XYZ(max_w/2.0, max_l/2.0, max_h/2.0),
                                          DB.XYZ(1,0,0), DB.XYZ(0,1,0), DB.XYZ(0,0,1),
                                          max_w + mm_to_ft(4000), max_h + mm_to_ft(4000), mm_to_ft(10000))
            if sec_a:
                self._dimension_grids_in_view(sec_a)
                self._dimension_levels_in_view(sec_a)

            # Section B (Y-Y, Looking West)
            sec_b = self._create_or_update_section("Section Y-Y", 
                                          DB.XYZ(max_w/2.0, max_l/2.0, max_h/2.0),
                                          DB.XYZ(0,1,0), DB.XYZ(0,0,1), DB.XYZ(-1,0,0),
                                          max_l + mm_to_ft(4000), max_h + mm_to_ft(4000), mm_to_ft(10000))
            if sec_b:
                self._dimension_grids_in_view(sec_b)
                self._dimension_levels_in_view(sec_b)
        except Exception as e:
            self.log("RevitWorkers: Sectioning failed: {}".format(str(e)))
        
        # doc.Regenerate() # Final sync
        t.Commit()
        tg.Assimilate()
        
        self.log("Fast-Track Summary: Reused: {}, Created: {}, Deleted: {}".format(reused_count, created_count, deleted_count))
        results["summary"] = {"reused": int(reused_count), "created": int(created_count), "deleted": int(deleted_count)}
        return results

    def _auto_array_windows(self, wall, spacing_mm):
        """Math Delegation: LLM doesn't need to calculate window positions"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        spacing = mm_to_ft(spacing_mm)
        lc = wall.Location
        line = lc.Curve
        length = line.Length
        
        if length < spacing: return
        
        count = int(length / spacing)
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()
        
        direction = (line.GetEndPoint(1) - line.GetEndPoint(0)).Normalize()
        for i in range(1, count):
            p = line.GetEndPoint(0) + direction * (i * spacing)
            doc.Create.NewFamilyInstance(p, symbol, wall, doc.GetElement(wall.LevelId), DB.Structure.StructuralType.NonStructural)

    def build_standard_stair(self, data, state):
        """Worker for Vertical Circulation"""
        import Autodesk.Revit.DB as DB # type: ignore
        import Autodesk.Revit.DB.Architecture as Arch # type: ignore
        
        base_lvl_id = DB.ElementId(int(state.get(data['base_level_id'])))
        top_lvl_id = DB.ElementId(int(state.get(data['top_level_id'])))
        loc = DB.XYZ(mm_to_ft(data['x']), mm_to_ft(data['y']), 0)
        
        scope = Arch.StairsEditScope(self.doc, "BIM: Stair")
        stair_id = scope.Start(base_lvl_id, top_lvl_id)
        t = DB.Transaction(self.doc, "Stair Run")
        t.Start()
        try:
            # Simple straight run for MVP, U-shape logic is complex but we can approximate
            p1 = loc
            p2 = loc + DB.XYZ(mm_to_ft(3000), 0, 0)
            line = DB.Line.CreateBound(p1, p2)
            Arch.StairsRun.CreateStraightRun(self.doc, stair_id, line, Arch.StairsRunJustification.Center)
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        scope.Commit(Arch.StairsFailureHandlingOptions())
        return [{"stair_id": str(stair_id.Value)}]

    def generate_service_core(self, data, state):
        """Worker for Core Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        pts = data['boundary_points']
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Service Core")
        t.Start()
        try:
            # 1. Create reinforced concrete walls
            wt = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
            lvl = doc.ActiveView.GenLevel
            curve_loop = DB.CurveLoop()
            
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                line = DB.Line.CreateBound(p1, p2)
                DB.Wall.Create(doc, line, wt.Id, lvl.Id, mm_to_ft(20000), 0, False, False)
                curve_loop.Append(line)
            
            # 2. Shaft Opening
            loops = [curve_loop]
            DB.Opening.CreateShaft(doc, lvl.Id, lvl.Id, curve_loop) # Simplified
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def generate_curtain_facade(self, data, state):
        """Worker for Curtain Systems"""
        import Autodesk.Revit.DB as DB # type: ignore
        wall = self.doc.GetElement(DB.ElementId(int(data['wall_id'])))
        
        t = DB.Transaction(self.doc, "BIM: Curtain Facade")
        t.Start()
        try:
            # Change wall type to Curtain Wall
            cw_type = None
            for wt in DB.FilteredElementCollector(self.doc).OfClass(DB.WallType):
                if wt.Kind == DB.WallKind.Curtain:
                    cw_type = wt; break
            if cw_type: wall.WallType = cw_type
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def create_parametric_roof(self, data, state):
        """Worker for Roof Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        
        pts = data['boundary_points']
        lvl = self.doc.ActiveView.GenLevel
        
        t = DB.Transaction(self.doc, "BIM: Roof")
        t.Start()
        try:
            footprint = DB.CurveArray()
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                footprint.Append(DB.Line.CreateBound(p1, p2))
            
            mapping = DB.ModelCurveArray()
            roof = self.doc.Create.NewFootprintRoof(footprint, lvl, DB.FilteredElementCollector(self.doc).OfClass(DB.RoofType).FirstElement(), mapping)
            for curve in mapping:
                roof.set_DefinesSlope(curve, True)
                roof.set_Slope(curve, data.get('slope', 30.0) * (3.14159 / 180.0))
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"roof_id": str(roof.Id.Value)}

    def perform_global_cleanup(self):
        """Worker for BIM Health: Join Walls/Floors and Wall/Wall corners"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        t = DB.Transaction(doc, "BIM: Global Cleanup")
        t.Start()
        try:
            elements = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
            walls = [e for e in elements if isinstance(e, DB.Wall)]
            floors = [e for e in elements if isinstance(e, DB.Floor)]
            
            # 1. Targeted Join: Walls with Floors on same levels
            from collections import defaultdict
            walls_by_level = defaultdict(list)
            for w in walls: walls_by_level[w.LevelId].append(w)
            
            for f in floors:
                level_id = f.LevelId
                # Join with walls starting on this level
                potential_walls = walls_by_level[level_id]
                for w in potential_walls:
                    try:
                        if not DB.JoinGeometryUtils.AreElementsJoined(doc, w, f):
                            DB.JoinGeometryUtils.JoinGeometry(doc, w, f)
                    except: pass
            
            # 2. Targeted Wall-Wall Joins: Proximity check on same level
            for lvl_id, lvl_walls in walls_by_level.items():
                for i in range(len(lvl_walls)):
                    for j in range(i + 1, len(lvl_walls)):
                        try:
                            if not DB.JoinGeometryUtils.AreElementsJoined(doc, lvl_walls[i], lvl_walls[j]):
                                DB.JoinGeometryUtils.JoinGeometry(doc, lvl_walls[i], lvl_walls[j])
                        except: pass
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"status": "Global Cleanup completed successfully"}

    def generate_submission_set(self):
        """Worker for Documentation"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Documentation")
        t.Start()
        try:
            # 1. Create Views
            lvls = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
            vt = None
            for f in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if f.ViewFamily == DB.ViewFamily.FloorPlan:
                    vt = f; break
            
            plans = []
            existing_plans = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
            
            for lvl in lvls:
                # Check if a floor plan already exists for this level
                existing = None
                for ep in existing_plans:
                    if ep.ViewType == DB.ViewType.FloorPlan and ep.GenLevel and ep.GenLevel.Id == lvl.Id:
                        existing = ep
                        break
                        
                if existing:
                    plans.append(existing)
                    continue
                    
                v = DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
                plans.append(v)
                # 1.5 Auto-Dimension Grids
                try: self._dimension_grids_in_view(v)
                except: pass
            
            # 2. Create Sheet
            tb = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).FirstElementId()
            sheet = DB.ViewSheet.Create(doc, tb)
            sheet.Name = "SUBMISSION SET"
            
            # 3. Simple Placement
            pt = DB.XYZ(0, 0, 0)
            if plans:
                try:
                    # Viewport.Create throws ArgumentException if the view is already placed
                    DB.Viewport.Create(doc, sheet.Id, plans[0].Id, pt)
                except:
                    pass
            
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"sheet_id": str(sheet.Id.Value)}

    def _dimension_grids_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Cleanup old AI dimensions in this view
        dim_count = 0
        try:
            old_dims = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Dimension).ToElements()
            for od in old_dims:
                p = od.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and "AI_Dim" in p.AsString():
                    doc.Delete(od.Id)
                    dim_count += 1
        except: pass
        if dim_count > 0: self.log("RevitWorkers: Cleaned up {} old AI dimensions.".format(dim_count))

        grids = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
        self.log("RevitWorkers: Found {} grids in view for dimensioning.".format(len(grids)))
        if len(grids) < 2: return
        
        x_grids = []
        y_grids = []
        for g in grids:
            curve = g.Curve
            if not isinstance(curve, DB.Line): continue
            direction = curve.Direction
            if abs(direction.Y) > 0.9: x_grids.append(g)
            elif abs(direction.X) > 0.9: y_grids.append(g)

        def create_dim(view, grid_list, offset, is_overall=False):
            if len(grid_list) < 2: return
            refs = DB.ReferenceArray()
            
            # Sort by coordinate
            is_x_dim = abs(offset.X) > 0.1 # This is a Y-Grid dimension (horizontal chain)
            grid_list.sort(key=lambda g: g.Curve.GetEndPoint(0).X if not is_x_dim else g.Curve.GetEndPoint(0).Y)
            
            if is_overall:
                refs.Append(DB.Reference(grid_list[0]))
                refs.Append(DB.Reference(grid_list[-1]))
            else:
                for g in grid_list: refs.Append(DB.Reference(g))
            
            # Placement line
            p1 = grid_list[0].Curve.GetEndPoint(0) + offset
            p2 = grid_list[-1].Curve.GetEndPoint(0) + offset
            if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
            line = DB.Line.CreateBound(p1, p2)
            
            try:
                dim = doc.Create.NewDimension(view, line, refs)
                safe_set_comment(dim, "AI_Dim")
            except: pass

        # For Floor Plans: X-Grids (Vertical) vs Y-Grids (Horizontal)
        # For Section Views: Only vertical lines (grids seen in projection) are visible.
        
        is_plan = view.ViewType == DB.ViewType.FloorPlan
        
        if is_plan:
            create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(3000), 0), is_overall=False)
            create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(5000), 0), is_overall=True)
            create_dim(view, y_grids, DB.XYZ(-mm_to_ft(3000), 0, 0), is_overall=False)
            create_dim(view, y_grids, DB.XYZ(-mm_to_ft(5000), 0, 0), is_overall=True)
        else:
            # Section view: All visible grids are vertical lines in the view plane.
            # Combine all for a single horizontal dimension chain.
            v_grids = x_grids + y_grids
            if len(v_grids) >= 2:
                # Offset in the view's current Up direction
                up = view.UpDirection.Normalize() * mm_to_ft(3000)
                create_dim(view, v_grids, up, is_overall=False)
                create_dim(view, v_grids, up + view.UpDirection.Normalize() * mm_to_ft(2000), is_overall=True)

    def _dimension_levels_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
        if len(levels) < 2: return
        
        # Sort by elevation
        levels.sort(key=lambda l: l.Elevation)
        
        refs = DB.ReferenceArray()
        for l in levels: refs.Append(DB.Reference(l))
        
        # Vertical placement line (offset left of building)
        offset_x = -mm_to_ft(5000)
        p1 = DB.XYZ(offset_x, 0, levels[0].Elevation)
        p2 = DB.XYZ(offset_x, 0, levels[-1].Elevation)
        if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
        line = DB.Line.CreateBound(p1, p2)
        
        try:
            dim = doc.Create.NewDimension(view, line, refs)
            safe_set_comment(dim, "AI_Dim")
            
            # Overall height
            refs_o = DB.ReferenceArray()
            refs_o.Append(DB.Reference(levels[0]))
            refs_o.Append(DB.Reference(levels[-1]))
            line_o = DB.Line.CreateBound(p1 + DB.XYZ(-mm_to_ft(2000),0,0), p2 + DB.XYZ(-mm_to_ft(2000),0,0))
            dim_o = doc.Create.NewDimension(view, line_o, refs_o)
            safe_set_comment(dim_o, "AI_Dim")
        except: pass

    def _create_or_update_section(self, name, center, basis_x, basis_y, basis_z, width, height, far_clip):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Find or Create Section View
        view = None
        for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSection):
            if v.Name == name:
                view = v; break
        
        # 2. Bounding Box for Section
        # This defines the view's coordinate system and crop region
        bbox = DB.BoundingBoxXYZ()
        bbox.Enabled = True
        bbox.Transform = DB.Transform.Identity
        bbox.Transform.Origin = center
        bbox.Transform.BasisX = basis_x
        bbox.Transform.BasisY = basis_y
        bbox.Transform.BasisZ = basis_z
        
        # Extents (in internal coordinates of the BBox)
        bbox.Min = DB.XYZ(-width/2.0, -height/2.0, -far_clip)
        bbox.Max = DB.XYZ(width/2.0, height/2.0, 0)
        
        if not view:
            # Find Section ViewFamilyType
            vft = None
            for vfam in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if vfam.ViewFamily == DB.ViewFamily.Section:
                    vft = vfam; break
            if not vft: return None
            view = DB.ViewSection.CreateSection(doc, vft.Id, bbox)
            view.Name = name
        else:
            # Update Crop Region (This is complex in Revit, but we can update the Section Box if we had the original tag)
            # For simplicity, we just reuse the existing view with its previous box unless user asks for re-centering.
            pass
            
        return view

def execute_in_transaction_group(doc, name, action_func):
    import Autodesk.Revit.DB as DB # type: ignore
    tg = DB.TransactionGroup(doc, name)
    tg.Start()
    try:
        # We might have nested individual transactions inside workers
        result = action_func()
        tg.Assimilate()
        return result
    except Exception as e:
        tg.RollBack()
        raise e
