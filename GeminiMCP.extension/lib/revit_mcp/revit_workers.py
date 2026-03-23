# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# Each method does its own local import on the correct thread.
from .bridge import mcp_event_handler

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

    def execute_fast_manifest(self, manifest):
        """High-speed execution using intent-based logic and state-aware updates"""
        import Autodesk.Revit.DB as DB # type: ignore
        from .building_generator import get_model_registry # type: ignore
        doc = self.doc
        results = {"levels": [], "elements": []}
        registry = get_model_registry(doc)
        print("RevitWorkers: State Scan completed. {} AI elements found in registry.".format(len(registry)))
        
        reused_count = 0
        created_count = 0
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

        setup = manifest.get("project_setup", {})
        levels_val = setup.get("levels", setup.get("storeys", setup.get("floors", default_levels)))
        height_val = setup.get("level_height", setup.get("floor_height", default_height))
        
        # Handle cases where Gemini returns a list or a single integer
        if isinstance(levels_val, list):
            count = len(levels_val)
            elevations = [mm_to_ft(e) for e in levels_val]
        else:
            count = int(safe_num(levels_val, 1))
            elevations = [mm_to_ft(i * safe_num(height_val, 4000)) for i in range(count + 1)]
            count = len(elevations)
        
        current_levels = []
        for i in range(count):
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

        # 2. Grid System (Aligned to Shell)
        shell = manifest.get("shell", {})
        w = mm_to_ft(shell.get("width", 10000))
        l = mm_to_ft(shell.get("length", 15000))
        
        # Only create boundary grids if they don't exist
        grid_tags = ["AI_Grid_X1", "AI_Grid_X2", "AI_Grid_Y1", "AI_Grid_Y2"]
        grid_lines = [
            DB.Line.CreateBound(DB.XYZ(0,-mm_to_ft(2000),0), DB.XYZ(0,l+mm_to_ft(2000),0)),
            DB.Line.CreateBound(DB.XYZ(w,-mm_to_ft(2000),0), DB.XYZ(w,l+mm_to_ft(2000),0)),
            DB.Line.CreateBound(DB.XYZ(-mm_to_ft(2000),0,0), DB.XYZ(w+mm_to_ft(2000),0,0)),
            DB.Line.CreateBound(DB.XYZ(-mm_to_ft(2000),l,0), DB.XYZ(w+mm_to_ft(2000),l,0))
        ]
        
        for i in range(4):
            tag = grid_tags[i]
            line = grid_lines[i]
            if tag in registry:
                g = doc.GetElement(registry[tag])
                if isinstance(g, DB.Grid):
                    g.Curve = line
                    results["elements"].append(str(g.Id.Value))
                    continue
            
            g = DB.Grid.Create(doc, line)
            safe_set_comment(g, tag)
            results["elements"].append(str(g.Id.Value))

        # 3. Mass Shelling (State-Aware Walls)
        points = [DB.XYZ(0,0,0), DB.XYZ(w,0,0), DB.XYZ(w,l,0), DB.XYZ(0,l,0)]
        
        for k, lvl in enumerate(current_levels):
            wall_tags = ["AI_Wall_L{}_N", "AI_Wall_L{}_E", "AI_Wall_L{}_S", "AI_Wall_L{}_W"]
            for j in range(4):
                tag = wall_tags[j].format(k+1)
                elev = elevations[k]
                p_start = DB.XYZ(points[j].X, points[j].Y, elev)
                p_end = DB.XYZ(points[(j+1)%4].X, points[(j+1)%4].Y, elev)
                line = DB.Line.CreateBound(p_start, p_end)
                
                if tag in registry:
                    wall = doc.GetElement(registry[tag])
                    if isinstance(wall, DB.Wall):
                        wall.Location.Curve = line
                        reused_count += 1
                        # Rule 2: 1000mm Parapet for the last storey, otherwise match level height
                        h_ft = elevations[k+1] - elevations[k] if k < len(current_levels)-1 else mm_to_ft(1000)
                        
                        # Must clear Top Constraint first, otherwise WALL_USER_HEIGHT_PARAM is read-only
                        try:
                            top_param = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
                            if top_param:
                                top_param.Set(DB.ElementId.InvalidElementId)
                        except: pass
                        
                        h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if h_param and not h_param.IsReadOnly: h_param.Set(h_ft)
                        
                        # Now re-set Top Constraint to next level if applicable
                        if k < len(current_levels) - 1:
                            try:
                                wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k+1].Id)
                            except: pass
                            
                        # CRITICAL: Always reset base and top offsets so reused geometry snaps firmly without manual offsets
                        try:
                            wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(0.0)
                            wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(0.0)
                        except: pass
                            
                        # Rule 3: Correct Orientation & Exterior Face Alignment
                        lp = get_location_line_param(wall)
                        if lp: lp.Set(2) # Finish Face: Exterior
                        
                        # Fix Flip Orientation (Regenerate to ensure geometry is valid)
                        try:
                            doc.Regenerate()
                            center = (points[0] + points[2]) * 0.5
                            mid = line.Evaluate(0.5, True)
                            if wall.Orientation.DotProduct(mid - center) < 0:
                                wall.Flip()
                        except: pass
                        
                        results["elements"].append(str(wall.Id.Value))
                        continue
                
                wall = DB.Wall.Create(doc, line, lvl.Id, False)
                created_count += 1
                safe_set_comment(wall, tag)
                
                # Rule 3: Correct Orientation & Exterior Face Alignment
                # Finish Face: Exterior = 2
                lp = get_location_line_param(wall)
                if lp: lp.Set(2)
                
                # Ensure orientation is Outward relative to building center
                try:
                    doc.Regenerate()
                    center = (points[0] + points[2]) * 0.5
                    mid = line.Evaluate(0.5, True)
                    if wall.Orientation.DotProduct(mid - center) < 0:
                        wall.Flip()
                except: pass
                
                # Rule 2: 1000mm Parapet / Height
                h_ft = elevations[k+1] - elevations[k] if k < len(current_levels)-1 else mm_to_ft(1000)
                h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                if h_param: h_param.Set(h_ft)
                
                # Rule 1: Top Constraint
                if k < len(current_levels) - 1:
                    wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k+1].Id)
                    
                # Reset offsets for newly created walls just to be absolutely safe
                try:
                    wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_OFFSET).Set(0.0)
                    wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_OFFSET).Set(0.0)
                except: pass
                
                results["elements"].append(str(wall.Id.Value))
                
                # Math Delegation: Auto-array windows
                skin = manifest.get("skin", {})
                if skin.get("window_intent") == "array":
                    self._auto_array_windows(wall, skin.get("spacing", 1500))

        # 4. State-Aware Floors
        import System.Collections.Generic as Generic # type: ignore
        ft = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        if ft:
            for k, lvl in enumerate(current_levels):
                tag = "AI_Floor_L{}".format(k+1)
                loop = DB.CurveLoop()
                for j in range(4):
                    loop.Append(DB.Line.CreateBound(points[j], points[(j+1)%4]))
                
                if tag in registry:
                    existing = doc.GetElement(registry[tag])
                    if existing: 
                        doc.Delete(existing.Id)
                        reused_count += 1 # Technical reuse via state-aware replacement
                else:
                    created_count += 1
                
                loops = Generic.List[DB.CurveLoop]()
                loops.Add(loop)
                floor = DB.Floor.Create(doc, loops, ft.Id, lvl.Id)
                safe_set_comment(floor, tag)
                results["elements"].append(str(floor.Id.Value))

        # 5. State-Aware Structural Columns
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        if symbol:
            if not symbol.IsActive: symbol.Activate()
            for k, lvl in enumerate(current_levels):
                for j in range(4):
                    tag = "AI_Col_L{}_{}".format(k+1, j+1)
                    elev = elevations[k]
                    p = DB.XYZ(points[j].X, points[j].Y, elev)
                    
                    col = None
                    if tag in registry:
                        col = doc.GetElement(registry[tag])
                    
                    if col and isinstance(col, DB.FamilyInstance):
                        # Update existing column's location and level
                        # Note: Moving columns is complex, this is a simplified update
                        col.Location.Point = p
                        col.LevelId = lvl.Id
                    else:
                        # Create new column
                        col = doc.Create.NewFamilyInstance(p, symbol, lvl, DB.Structure.StructuralType.StructuralColumn)
                        safe_set_comment(col, tag)
                    results["elements"].append(str(col.Id.Value))

        # 6. Orphan Cleanup (BIM Health)
        deleted_count = 0
        all_touched = set(results["levels"]) | set(results["elements"])
        for tag, eid in registry.items():
            if str(eid.Value) not in all_touched:
                try: 
                    doc.Delete(eid)
                    deleted_count += 1
                except: pass

        print("Fast-Track Summary: Reused: {}, Created: {}, Deleted: {}".format(reused_count, created_count, deleted_count))
        results["summary"] = {"reused": reused_count, "created": created_count, "deleted": deleted_count}
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
            
            # 1. Join Walls with Floors
            for w in walls:
                for f in floors:
                    try: 
                        if not DB.JoinGeometryUtils.AreElementsJoined(doc, w, f):
                            DB.JoinGeometryUtils.JoinGeometry(doc, w, f)
                    except: pass
            
            # 2. Force Wall Corners Join (Safety fallback)
            for i in range(len(walls)):
                for j in range(i + 1, len(walls)):
                    try:
                        # Only try if they are within proximity (simplified check)
                        if walls[i].LevelId == walls[j].LevelId:
                             if not DB.JoinGeometryUtils.AreElementsJoined(doc, walls[i], walls[j]):
                                 DB.JoinGeometryUtils.JoinGeometry(doc, walls[i], walls[j])
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
        grids = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
        if len(grids) < 2: return
        
        x_grids = []
        y_grids = []
        for g in grids:
            curve = g.Curve
            if not isinstance(curve, DB.Line): continue
            direction = curve.Direction
            if abs(direction.Y) > 0.9: x_grids.append(g)
            elif abs(direction.X) > 0.9: y_grids.append(g)

        def create_dim(view, grid_list, offset):
            if len(grid_list) < 2: return
            refs = DB.ReferenceArray()
            # Sort by coordinate
            is_x = abs(offset.X) > 0
            grid_list.sort(key=lambda g: g.Curve.GetEndPoint(0).X if not is_x else g.Curve.GetEndPoint(0).Y)
            
            for g in grid_list: refs.Append(DB.Reference(g))
            
            # Baseline
            p1 = grid_list[0].Curve.GetEndPoint(0) + offset
            p2 = grid_list[-1].Curve.GetEndPoint(0) + offset
            line = DB.Line.CreateBound(p1, p2)
            doc.Create.NewDimension(view, line, refs)

        create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(3000), 0))
        create_dim(view, y_grids, DB.XYZ(-mm_to_ft(3000), 0, 0))

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
