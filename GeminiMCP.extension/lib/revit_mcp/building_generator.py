# -*- coding: utf-8 -*-
try:
    import clr
    clr.AddReference('RevitAPI')
    clr.AddReference('RevitAPIUI')
    from Autodesk.Revit.DB import *  # type: ignore
except:
    pass  # Will be available via Autodesk.Revit.DB direct import on main thread
import math

def safe_num(val, default=0):
    try:
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            import re
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            return float(m[0]) if m else float(default)
        return float(default)
    except: return float(default)

# Internal Unit Conversion
def mm_to_ft(mm):
    import Autodesk.Revit.DB as _DB  # type: ignore
    return _DB.UnitUtils.ConvertToInternalUnits(safe_num(mm), _DB.UnitTypeId.Millimeters)

def get_model_registry(doc):
    """
    Search the model for elements tagged with 'AI_' in their Comments parameter.
    Returns a dictionary mapping tags to ElementIds.
    """
    import Autodesk.Revit.DB as DB # type: ignore
    registry = {}
    # Use FilteredElementCollector on all elements to ensure we find hosts even if hidden
    collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
    
    for element in collector:
        try:
            comments_param = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if not comments_param:
                comments_param = element.LookupParameter("Comments")
            if comments_param and comments_param.HasValue:
                tag = comments_param.AsString()
                if tag and tag.startswith("AI_"):
                    registry[tag] = element.Id
                    continue
        except: pass
        
        try:
            if isinstance(element, DB.Level):
                name = element.Name
                if name.startswith("AI Level") or name.startswith("AI_Level"):
                    num = "".join(filter(str.isdigit, name))
                    if num:
                        registry["AI_Level_" + num] = element.Id
        except: pass
    return registry

def safe_set_comment(element, tag):
    import Autodesk.Revit.DB as DB # type: ignore
    # Try global instance comments first
    p = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if not p:
        p = element.LookupParameter("Comments")
    if p:
        p.Set(tag)
    return p

class BuildingSystem:
    def __init__(self, doc, width_mm, depth_mm, height_mm):
        import Autodesk.Revit.DB as DB  # type: ignore
        self.doc = doc
        self.DB = DB
        self.w = mm_to_ft(width_mm)
        self.d = mm_to_ft(depth_mm)
        self.h = mm_to_ft(height_mm)
        
        # Rule 1: Immediate Bounding Box Calculation
        self.bbox = self._calculate_bounding_box()
        self.registry = get_model_registry(doc)
        
    def _calculate_bounding_box(self):
        """Rule 1: Establish X, Y, Z limits first"""
        DB = self.DB
        return {
            "min": DB.XYZ(0, 0, 0),
            "max": DB.XYZ(self.w, self.d, self.h),
            "center": DB.XYZ(self.w/2, self.d/2, self.h/2)
        }

    def generate(self):
        """Main execution logic for state-aware generation"""
        DB = self.DB
        tg = DB.TransactionGroup(self.doc, "AI: Generate Building System")
        tg.Start()
        
        try:
            t = DB.Transaction(self.doc, "AI: Build Foundation & Levels")
            t.Start()
            level1 = self._get_or_create_level("AI_Level_1", 0)
            level2 = self._get_or_create_level("AI_Level_2", self.h)
            t.Commit()
                
            t = DB.Transaction(self.doc, "AI: Build Enclosure")
            t.Start()
            p1 = DB.XYZ(self.bbox["min"].X, self.bbox["min"].Y, 0)
            p2 = DB.XYZ(self.bbox["max"].X, self.bbox["min"].Y, 0)
            p3 = DB.XYZ(self.bbox["max"].X, self.bbox["max"].Y, 0)
            p4 = DB.XYZ(self.bbox["min"].X, self.bbox["max"].Y, 0)
            corners = [p1, p2, p3, p4]
            
            self._get_or_create_walls(corners, level1, level2)
            self._update_or_create_floor(corners, level1)
            t.Commit()
                
            tg.Assimilate()
            return {"status": "Success", "bounds": {"w": self.w, "d": self.d, "h": self.h}}
        except Exception as e:
            try: tg.RollBack()
            except: pass
            return {"status": "Error", "message": str(e)}

    def _get_or_create_level(self, tag, elevation):
        DB = self.DB
        lvl_name = tag.replace("AI_", "Level ")
        lvl = None
        
        if tag in self.registry:
            lvl = self.doc.GetElement(self.registry[tag])
        
        if not lvl:
            for l in DB.FilteredElementCollector(self.doc).OfClass(DB.Level):
                if l.Name == lvl_name:
                    lvl = l; break
        
        if lvl and isinstance(lvl, DB.Level):
            lvl.Elevation = elevation
            safe_set_comment(lvl, tag)
        else:
            lvl = DB.Level.Create(self.doc, elevation)
            try: lvl.Name = lvl_name
            except: pass
            safe_set_comment(lvl, tag)
            
        return lvl

    def _get_or_create_walls(self, corners, level_base, level_top):
        DB = self.DB
        wall_tags = ["AI_Wall_North", "AI_Wall_East", "AI_Wall_South", "AI_Wall_West"]
        
        for i in range(4):
            tag = wall_tags[i]
            p_start = corners[i]
            p_end = corners[(i+1)%4]
            line = DB.Line.CreateBound(p_start, p_end)
            
            if tag in self.registry:
                wall = self.doc.GetElement(self.registry[tag])
                if wall and isinstance(wall, DB.Wall):
                    wall.Location.Curve = line
                    lp = wall.LookupParameter("Location Line")
                    if lp: lp.Set(2)
                    # Clear top constraint first so height is writable
                    try:
                        top_param = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
                        if top_param: top_param.Set(DB.ElementId.InvalidElementId)
                    except: pass
                    h_ft = level_top.Elevation - level_base.Elevation if level_top.Id != level_base.Id else mm_to_ft(1000)
                    h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                    if h_param and not h_param.IsReadOnly: h_param.Set(h_ft)
                    continue
            
            wall = DB.Wall.Create(self.doc, line, level_base.Id, False)
            safe_set_comment(wall, tag)
            
            loc_param = wall.LookupParameter("Location Line")
            if loc_param: loc_param.Set(2)
            
            if level_top.Id != level_base.Id:
                wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(level_top.Id)
            else:
                h_param = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                if h_param and not h_param.IsReadOnly: h_param.Set(mm_to_ft(1000))

    def _update_or_create_floor(self, corners, level):
        DB = self.DB
        import System.Collections.Generic as Generic  # type: ignore
        tag = "AI_Floor_Ground"
        curve_loop = DB.CurveLoop()
        for i in range(4):
            curve_loop.Append(DB.Line.CreateBound(corners[i], corners[(i+1)%4]))
            
        if tag in self.registry:
            existing = self.doc.GetElement(self.registry[tag])
            if existing:
                try: self.doc.Delete(existing.Id)
                except: pass
        
        ft = DB.FilteredElementCollector(self.doc).OfClass(DB.FloorType).FirstElement()
        loops = Generic.List[DB.CurveLoop]()
        loops.Add(curve_loop)
        
        floor = DB.Floor.Create(self.doc, loops, ft.Id, level.Id)
        safe_set_comment(floor, tag)
        return floor

# Example Usage (not to be run directly as a script but used by the MCP)
# generator = BuildingSystem(doc, 5000, 8000, 3500)
# generator.generate()

